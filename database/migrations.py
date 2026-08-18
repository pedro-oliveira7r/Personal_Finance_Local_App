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

from typing import Callable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from database.models import Base

#: Bump when an explicit step is added below.
SCHEMA_VERSION = 1

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


STEPS: list[Callable[[Engine], None]] = [_step_0_to_1]


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
