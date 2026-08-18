"""Backup and restore.

Two formats, deliberately:

**SQLite copy** — a byte-exact snapshot of the database taken through SQLite's
own backup API, so it is consistent even if something is mid-write. This is the
one to restore from.

**JSON dump** — human-readable, diffable, and portable to any future version of
the app. Slower to restore but it survives a schema change that a binary copy
would not.

Restoring always moves the current database aside first, so a restore can itself
be undone.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, inspect, select
from sqlalchemy.orm import Session

import config
from database.database import get_engine, init_db, reset_engine_cache, session_scope
from database.models import ALL_MODELS, Base
from services.common import ServiceError

BACKUP_SUFFIX = ".db"
JSON_SUFFIX = ".json"
ZIP_SUFFIX = ".zip"
PRE_RESTORE_PREFIX = "pre-restore-"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _unique(path: Path) -> Path:
    """Avoid clobbering a backup taken in the same second."""
    if not path.exists():
        return path
    for suffix in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{suffix}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}-{datetime.now().microsecond}{path.suffix}")


def backup_dir(session: Optional[Session] = None) -> Path:
    if session is not None:
        from services.settings_service import resolve_backup_dir

        return resolve_backup_dir(session)
    config.ensure_dirs()
    return config.BACKUP_DIR


# ==========================================================================
# SQLite snapshot
# ==========================================================================
def create_sqlite_backup(*, target_dir: Optional[Path] = None,
                         label: str = "", db_path: Optional[Path] = None) -> Path:
    """Consistent copy of the database file using the SQLite backup API."""
    source = Path(db_path) if db_path else config.db_path()
    if not source.exists():
        raise ServiceError("There is no database to back up yet.")
    destination_dir = Path(target_dir) if target_dir else backup_dir()
    destination_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{label}" if label else ""
    destination = _unique(
        destination_dir / f"finance-{_timestamp()}{suffix}{BACKUP_SUFFIX}")

    source_conn = sqlite3.connect(str(source))
    try:
        target_conn = sqlite3.connect(str(destination))
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()
    return destination


# ==========================================================================
# JSON dump
# ==========================================================================
def _serialise(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def dump_json(session: Session) -> dict[str, Any]:
    """Every table as plain JSON, plus a manifest."""
    from database.migrations import SCHEMA_VERSION

    payload: dict[str, Any] = {
        "meta": {
            "app": config.APP_NAME,
            "app_version": config.APP_VERSION,
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
        "tables": {},
    }
    for model in ALL_MODELS:
        rows = session.execute(select(model)).unique().scalars().all()
        table_name = model.__tablename__
        mapper = inspect(model)
        payload["tables"][table_name] = [
            {column.key: _serialise(getattr(row, column.key, None))
             for column in mapper.columns}
            for row in rows
        ]
    payload["meta"]["row_counts"] = {
        name: len(rows) for name, rows in payload["tables"].items()
    }
    return payload


def create_json_backup(session: Session, *, target_dir: Optional[Path] = None,
                       label: str = "") -> Path:
    destination_dir = Path(target_dir) if target_dir else backup_dir(session)
    destination_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{label}" if label else ""
    destination = _unique(
        destination_dir / f"finance-{_timestamp()}{suffix}{JSON_SUFFIX}")
    destination.write_text(
        json.dumps(dump_json(session), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return destination


def json_bytes(session: Session) -> bytes:
    return json.dumps(dump_json(session), ensure_ascii=False, indent=2).encode("utf-8")


def create_zip_backup(session: Session, *, target_dir: Optional[Path] = None) -> Path:
    """Both formats in one archive — the belt-and-braces option."""
    destination_dir = Path(target_dir) if target_dir else backup_dir(session)
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    destination = _unique(destination_dir / f"finance-backup-{stamp}{ZIP_SUFFIX}")

    db_copy = create_sqlite_backup(target_dir=destination_dir, label="zip")
    json_payload = json_bytes(session)
    try:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(db_copy, arcname="finance.db")
            archive.writestr("finance.json", json_payload)
            archive.writestr("README.txt", _archive_readme(stamp))
    finally:
        db_copy.unlink(missing_ok=True)
    return destination


def _archive_readme(stamp: str) -> str:
    return (
        f"{config.APP_NAME} backup — {stamp}\n\n"
        "finance.db    SQLite snapshot. Restore this for an exact copy.\n"
        "finance.json  Readable dump of every table. Use this if the schema has\n"
        "              moved on since the backup was taken.\n\n"
        "Restore from inside the app: Settings -> Backup & restore.\n"
        "Your data has never left this computer.\n"
    )


# ==========================================================================
# Listing
# ==========================================================================
@dataclass
class BackupFile:
    path: Path
    size_bytes: int
    modified: datetime
    kind: str

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def size_label(self) -> str:
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"


def list_backups(session: Optional[Session] = None,
                 *, target_dir: Optional[Path] = None) -> list[BackupFile]:
    directory = Path(target_dir) if target_dir else backup_dir(session)
    if not directory.exists():
        return []
    entries: list[BackupFile] = []
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".db", ".json", ".zip"}:
            continue
        stat = path.stat()
        entries.append(BackupFile(
            path=path,
            size_bytes=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime),
            kind=path.suffix.lower().lstrip("."),
        ))
    entries.sort(key=lambda item: item.modified, reverse=True)
    return entries


def prune_backups(keep: int = 20, *, target_dir: Optional[Path] = None) -> int:
    entries = list_backups(target_dir=target_dir)
    removed = 0
    for entry in entries[max(0, keep):]:
        try:
            entry.path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


# ==========================================================================
# Restore
# ==========================================================================
@dataclass
class RestoreReport:
    source: str = ""
    kind: str = ""
    previous_saved_to: Optional[str] = None
    tables_restored: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        total = sum(self.tables_restored.values())
        if self.kind == "sqlite":
            return f"Database replaced from {self.source}."
        return f"{total} row(s) restored across {len(self.tables_restored)} table(s)."


def restore_sqlite(path: Path, *, db_path: Optional[Path] = None) -> RestoreReport:
    """Replace the live database with a snapshot, keeping the old one aside."""
    source = Path(path)
    if not source.exists():
        raise ServiceError(f"Backup file not found: {source}")
    _validate_sqlite(source)

    target = Path(db_path) if db_path else config.db_path()
    report = RestoreReport(source=source.name, kind="sqlite")

    reset_engine_cache()
    if target.exists():
        safety = target.parent / f"{PRE_RESTORE_PREFIX}{_timestamp()}-{target.name}"
        shutil.copy2(target, safety)
        report.previous_saved_to = str(safety)
    for extra in (target.with_name(target.name + "-wal"), target.with_name(target.name + "-shm")):
        extra.unlink(missing_ok=True)

    shutil.copy2(source, target)
    reset_engine_cache()
    init_db(target)
    return report


def _validate_sqlite(path: Path) -> None:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ServiceError(f"That file is not a readable SQLite database ({exc}).")
    try:
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except sqlite3.DatabaseError as exc:
        raise ServiceError(f"That file is not a valid SQLite database ({exc}).")
    finally:
        conn.close()
    required = {"transactions", "accounts", "categories"}
    missing = required - names
    if missing:
        raise ServiceError(
            "That database does not look like a Personal Finance backup "
            f"(missing tables: {', '.join(sorted(missing))})."
        )


def restore_json(payload: dict[str, Any] | bytes | str,
                 *, db_path: Optional[Path] = None,
                 backup_first: bool = True) -> RestoreReport:
    """Rebuild every table from a JSON dump.

    Existing rows are removed first, so this is a replace, not a merge. A
    snapshot of the current database is taken beforehand unless disabled.
    """
    if isinstance(payload, (bytes, str)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise ServiceError(f"That file is not valid JSON ({exc}).")
    if not isinstance(payload, dict) or "tables" not in payload:
        raise ServiceError("That JSON file is not a Personal Finance backup.")

    tables: dict[str, list[dict]] = payload["tables"]
    report = RestoreReport(source=str(payload.get("meta", {}).get("created_at", "JSON")),
                          kind="json")
    target = Path(db_path) if db_path else config.db_path()

    if backup_first and target.exists():
        safety = create_sqlite_backup(label="pre-restore", db_path=target)
        report.previous_saved_to = str(safety)

    init_db(target, seed_defaults=False)
    by_name = {model.__tablename__: model for model in ALL_MODELS}
    ordered = [table.name for table in Base.metadata.sorted_tables]

    with session_scope(target) as session:
        for name in reversed(ordered):
            model = by_name.get(name)
            if model is not None:
                session.execute(delete(model))
        session.flush()

        for name in ordered:
            model = by_name.get(name)
            if model is None or name not in tables:
                continue
            mapper = inspect(model)
            columns = {column.key: column for column in mapper.columns}
            count = 0
            for record in tables[name]:
                values = {}
                for key, raw in record.items():
                    column = columns.get(key)
                    if column is None:
                        continue
                    values[key] = _deserialise(raw, column)
                session.add(model(**values))
                count += 1
            session.flush()
            report.tables_restored[name] = count

        from database.seed import seed_defaults

        seed_defaults(session)
    return report


def _deserialise(value: Any, column) -> Any:
    if value is None:
        return None
    python_type = None
    try:
        python_type = column.type.python_type
    except (NotImplementedError, AttributeError):
        python_type = None

    type_name = type(column.type).__name__
    if type_name == "Money" or type_name == "Rate":
        return Decimal(str(value))
    if type_name == "JSONText":
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    if python_type is date and isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    if python_type is datetime and isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    if python_type is bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    return value


def restore_zip(path: Path, *, prefer: str = "sqlite",
                db_path: Optional[Path] = None) -> RestoreReport:
    """Restore from a combined archive, preferring the exact SQLite copy."""
    source = Path(path)
    if not source.exists():
        raise ServiceError(f"Archive not found: {source}")
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        if prefer == "sqlite" and "finance.db" in names:
            extracted = Path(config.DATA_DIR) / f"_restore-{_timestamp()}.db"
            extracted.parent.mkdir(parents=True, exist_ok=True)
            extracted.write_bytes(archive.read("finance.db"))
            try:
                return restore_sqlite(extracted, db_path=db_path)
            finally:
                extracted.unlink(missing_ok=True)
        if "finance.json" in names:
            return restore_json(archive.read("finance.json"), db_path=db_path)
    raise ServiceError("That archive contains neither finance.db nor finance.json.")


def restore_from_upload(filename: str, data: bytes,
                        *, db_path: Optional[Path] = None) -> RestoreReport:
    """Restore from an uploaded file, dispatching on its extension."""
    suffix = Path(filename).suffix.lower()
    staging = Path(config.DATA_DIR) / f"_upload-{_timestamp()}{suffix or '.bin'}"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(data)
    try:
        if suffix == ".json":
            return restore_json(data, db_path=db_path)
        if suffix == ".zip":
            return restore_zip(staging, db_path=db_path)
        return restore_sqlite(staging, db_path=db_path)
    finally:
        staging.unlink(missing_ok=True)
