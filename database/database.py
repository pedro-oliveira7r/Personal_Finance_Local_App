"""Engine and session management for the local SQLite database.

The engine is created lazily and memoised per database path, so switching
between the real and the demo database inside one process is safe.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import config
from database.models import Base

_lock = threading.RLock()
_engines: dict[str, Engine] = {}
_factories: dict[str, sessionmaker] = {}
_initialised: set[str] = set()


def _configure_sqlite(dbapi_connection, connection_record):  # pragma: no cover - driver hook
    """Turn on the pragmas SQLite leaves off by default."""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL keeps reads fast while a write is in flight; harmless locally.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def get_engine(path: Optional[Path] = None) -> Engine:
    """Return (creating if needed) the engine for ``path``."""
    resolved = Path(path) if path else config.db_path()
    key = str(resolved)
    with _lock:
        engine = _engines.get(key)
        if engine is None:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(
                f"sqlite:///{resolved}",
                future=True,
                echo=False,
                connect_args={"check_same_thread": False, "timeout": 15},
            )
            event.listen(engine, "connect", _configure_sqlite)
            _engines[key] = engine
            _factories[key] = sessionmaker(
                bind=engine, expire_on_commit=False, autoflush=False, future=True
            )
        return engine


def get_session_factory(path: Optional[Path] = None) -> sessionmaker:
    get_engine(path)
    resolved = Path(path) if path else config.db_path()
    return _factories[str(resolved)]


def init_db(path: Optional[Path] = None, *, seed_defaults: bool = True) -> Path:
    """Create the schema (and default rows) if they are not there yet.

    Idempotent: safe to call on every application start.
    """
    resolved = Path(path) if path else config.db_path()
    key = str(resolved)
    with _lock:
        engine = get_engine(resolved)
        if key in _initialised:
            return resolved
        config.ensure_dirs()
        Base.metadata.create_all(engine)
        from database.migrations import run_migrations

        run_migrations(engine)
        if seed_defaults:
            from database.seed import seed_defaults as _seed

            factory = get_session_factory(resolved)
            with factory() as session:
                _seed(session)
                session.commit()
        _initialised.add(key)
        return resolved


@contextmanager
def session_scope(path: Optional[Path] = None) -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on error."""
    factory = get_session_factory(path)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def read_session(path: Optional[Path] = None) -> Iterator[Session]:
    """Read-only session — never commits."""
    factory = get_session_factory(path)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def reset_engine_cache() -> None:
    """Dispose every engine. Used by tests and after a database restore."""
    with _lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()
        _factories.clear()
        _initialised.clear()


def database_stats(path: Optional[Path] = None) -> dict[str, object]:
    """Row counts and file size, for the Settings screen."""
    resolved = Path(path) if path else config.db_path()
    engine = get_engine(resolved)
    stats: dict[str, object] = {
        "path": str(resolved),
        "exists": resolved.exists(),
        "size_bytes": resolved.stat().st_size if resolved.exists() else 0,
        "tables": {},
    }
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            try:
                count = conn.execute(text(f"SELECT COUNT(*) FROM {table.name}")).scalar()
            except Exception:
                count = None
            stats["tables"][table.name] = count
    return stats


def vacuum(path: Optional[Path] = None) -> None:
    engine = get_engine(path)
    with engine.connect() as conn:
        conn.exec_driver_sql("VACUUM")
