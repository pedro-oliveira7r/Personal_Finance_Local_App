"""Lightweight forward-only migration runner.

There is no Alembic dependency on purpose: this is a single-user local app and
a 100-line runner that (a) applies ordered SQL steps and (b) adds any column
present in the models but missing from the file is both easier to audit and
impossible to mis-configure.

Adding a column to ``models.py`` is picked up automatically on next launch.
Anything destructive (renames, type changes) must be written as an explicit
step so it is reviewable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from database.models import Base

#: Bump when an explicit step is added below.
SCHEMA_VERSION = 3

VERSION_KEY = "schema_version"


def _read_version(engine: Engine) -> int:
    with engine.connect() as conn:
        try:
            row = conn.execute(
                text("SELECT value FROM schema_meta WHERE key = :k"), {"k": VERSION_KEY}
            ).first()
        except Exception:
            return 0
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _write_version(engine: Engine, version: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schema_meta (key, value) VALUES (:k, :v) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ),
            {"k": VERSION_KEY, "v": str(version)},
        )


# --------------------------------------------------------------------------
# Explicit steps. Each entry migrates FROM its index TO index + 1.
# --------------------------------------------------------------------------
def _step_0_to_1(engine: Engine) -> None:
    """Initial version — ``create_all`` has already built everything."""
    return None


def _step_1_to_2(engine: Engine) -> None:
    """Multi-currency: stamp every denominated row with the book's currency.

    ``ensure_columns`` runs *after* the step list in :func:`run_migrations`, so
    the new columns do not exist yet on a real upgrade — a fresh database gets
    them from ``create_all`` and would mask the failure. Adding them here first
    is what makes this step work on an existing book.

    The columns declare ``default="BRL"``, which is what the generated
    ``ALTER TABLE`` backfills. That is wrong for a book whose primary currency
    was never BRL, so every denominated table is restamped from
    ``app_settings.base_currency`` instead.
    """
    ensure_columns(engine)

    tables = ("accounts", "budget_lines", "goals", "debts", "recurring_rules")
    with engine.begin() as conn:
        row = conn.exec_driver_sql(
            "SELECT base_currency FROM app_settings WHERE id = 1"
        ).fetchone()
        primary = (row[0] if row and row[0] else "BRL").upper()
        for table in tables:
            conn.exec_driver_sql(
                f"UPDATE {table} SET currency = ? WHERE currency IS NULL OR currency = 'BRL'",
                (primary,),
            )
        conn.exec_driver_sql(
            "UPDATE app_settings SET active_currencies = ? WHERE active_currencies IS NULL",
            (f'["{primary}"]',),
        )


def _step_2_to_3(engine: Engine) -> None:
    """Give net-worth snapshots a currency — one row per currency per date.

    ``as_of_date`` was declared ``unique=True`` on the column, and SQLite cannot
    drop or alter a constraint in place. That leaves the twelve-step table
    rebuild: build the new shape, copy every row, drop the old, rename.

    This is the only destructive step in the multi-currency work, so it takes a
    file backup first and refuses to drop the original unless the copy came
    through with exactly the same number of rows.
    """
    ensure_columns(engine)

    inspector = inspect(engine)
    if "net_worth_snapshots" not in set(inspector.get_table_names()):
        return
    existing = {col["name"] for col in inspector.get_columns("net_worth_snapshots")}
    if "currency" in existing and _has_unique(engine, "net_worth_snapshots",
                                              "uq_nw_date_currency"):
        return  # already rebuilt

    _backup_before_rebuild(engine)

    with engine.begin() as conn:
        primary = conn.exec_driver_sql(
            "SELECT base_currency FROM app_settings WHERE id = 1"
        ).fetchone()
        code = (primary[0] if primary and primary[0] else "BRL").upper()
        before = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM net_worth_snapshots").fetchone()[0]

        conn.exec_driver_sql("DROP TABLE IF EXISTS net_worth_snapshots__new")
        conn.exec_driver_sql("""
            CREATE TABLE net_worth_snapshots__new (
                id INTEGER NOT NULL PRIMARY KEY,
                as_of_date DATE NOT NULL,
                currency VARCHAR(3) NOT NULL DEFAULT 'BRL',
                total_assets INTEGER NOT NULL,
                total_liabilities INTEGER NOT NULL,
                net_worth INTEGER NOT NULL,
                detail TEXT,
                is_manual BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME,
                updated_at DATETIME,
                CONSTRAINT uq_nw_date_currency UNIQUE (as_of_date, currency)
            )
        """)
        conn.exec_driver_sql(
            """
            INSERT INTO net_worth_snapshots__new
                (id, as_of_date, currency, total_assets, total_liabilities,
                 net_worth, detail, is_manual, created_at, updated_at)
            SELECT id, as_of_date, ?, total_assets, total_liabilities,
                   net_worth, detail, is_manual, created_at, updated_at
            FROM net_worth_snapshots
            """,
            (code,),
        )
        after = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM net_worth_snapshots__new").fetchone()[0]
        if after != before:
            conn.exec_driver_sql("DROP TABLE net_worth_snapshots__new")
            raise RuntimeError(
                f"net-worth snapshot rebuild copied {after} of {before} rows; "
                f"the original table has been left untouched."
            )
        conn.exec_driver_sql("DROP TABLE net_worth_snapshots")
        conn.exec_driver_sql(
            "ALTER TABLE net_worth_snapshots__new RENAME TO net_worth_snapshots")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_nw_date "
            "ON net_worth_snapshots (as_of_date)")


def _has_unique(engine: Engine, table: str, name: str) -> bool:
    try:
        return any(idx.get("name") == name
                   for idx in inspect(engine).get_indexes(table))
    except Exception:
        return False


def _backup_before_rebuild(engine: Engine) -> None:
    """Copy the database file before a step rewrites a table.

    Best effort: a missing backup directory must not stop the upgrade, but a
    successful one means a bad rebuild is recoverable rather than final.
    """
    import shutil
    from datetime import datetime

    try:
        import config

        source = Path(engine.url.database or "")
        if not source.exists():
            return
        config.ensure_dirs()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = config.BACKUP_DIR / f"pre-migration-{stamp}-{source.name}"
        shutil.copy2(source, target)
    except Exception:
        return


STEPS: list[Callable[[Engine], None]] = [_step_0_to_1, _step_1_to_2, _step_2_to_3]


def _sqlite_type(column) -> str:
    """Render a column type for ``ALTER TABLE ... ADD COLUMN``."""
    try:
        compiled = column.type.compile(dialect=None)
    except Exception:
        compiled = "TEXT"
    mapping = {
        "INTEGER": "INTEGER", "TEXT": "TEXT", "DATE": "DATE",
        "DATETIME": "DATETIME", "BOOLEAN": "BOOLEAN",
    }
    upper = str(compiled).upper()
    for key, value in mapping.items():
        if upper.startswith(key):
            return value
    if "CHAR" in upper or "STRING" in upper:
        return "TEXT"
    if "NUMERIC" in upper or "DECIMAL" in upper or "FLOAT" in upper:
        return "NUMERIC"
    return "TEXT"


def _default_literal(column) -> str | None:
    """A safe literal default so ``NOT NULL`` additions do not fail."""
    default = column.default
    if default is None or getattr(default, "is_callable", False):
        if not column.nullable:
            kind = _sqlite_type(column)
            return "0" if kind in {"INTEGER", "NUMERIC", "BOOLEAN"} else "''"
        return None
    value = getattr(default, "arg", None)
    if callable(value):
        return None
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def ensure_columns(engine: Engine) -> list[str]:
    """Add model columns that are missing from the database file.

    Returns the list of ``table.column`` names that were added.
    """
    added: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {_sqlite_type(column)}"
            literal = _default_literal(column)
            if not column.nullable and literal is not None:
                ddl += f" NOT NULL DEFAULT {literal}"
            elif literal is not None:
                ddl += f" DEFAULT {literal}"
            with engine.begin() as conn:
                conn.exec_driver_sql(ddl)
            added.append(f"{table.name}.{column.name}")
    return added


def run_migrations(engine: Engine) -> dict[str, object]:
    """Bring the database file up to :data:`SCHEMA_VERSION`."""
    Base.metadata.create_all(engine)  # no-op when everything exists
    current = _read_version(engine)
    applied: list[int] = []
    while current < SCHEMA_VERSION and current < len(STEPS):
        STEPS[current](engine)
        current += 1
        applied.append(current)
    added = ensure_columns(engine)
    _write_version(engine, max(current, SCHEMA_VERSION))
    return {"from_version": _read_version(engine), "applied": applied, "added_columns": added}
