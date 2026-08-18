"""Test fixtures: an isolated database per test, no shared state."""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """Point the app at a throwaway SQLite file."""
    import config
    from database.database import reset_engine_cache

    path = tmp_path / "test.db"
    monkeypatch.setenv("PFA_DB_PATH", str(path))
    monkeypatch.setenv("PFA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups", raising=False)
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports", raising=False)
    reset_engine_cache()
    yield path
    reset_engine_cache()


@pytest.fixture()
def session(db_path):
    """A committed-per-test session against a freshly seeded database."""
    from database.database import get_session_factory, init_db

    init_db(db_path)
    factory = get_session_factory(db_path)
    db = factory()
    try:
        yield db
        db.commit()
    finally:
        db.rollback()
        db.close()


@pytest.fixture()
def accounts(session):
    """Checking, savings, wallet, credit card and an investment account."""
    from constants import AccountType
    from services import account_service

    created = {}
    specs = [
        ("Checking", AccountType.CHECKING.value, "1000", True),
        ("Savings", AccountType.SAVINGS.value, "500", True),
        ("Wallet", AccountType.CASH.value, "100", True),
        ("Card", AccountType.CREDIT_CARD.value, "0", False),
        ("Broker", AccountType.INVESTMENT.value, "2000", False),
    ]
    for name, kind, opening, in_cash in specs:
        # The default seed already made accounts with other names; these are ours.
        created[name] = account_service.create_account(session, {
            "name": name, "type": kind, "opening_balance": opening,
            "opening_date": date(2026, 1, 1), "include_in_cash": in_cash,
        })
    session.commit()
    return created


@pytest.fixture()
def categories(session):
    """Handles for a few well-known seeded categories."""
    from services import category_service

    def find(path: str, kind: str | None = None):
        found = category_service.resolve_path(session, path, kind=kind)
        assert found is not None, f"seed category missing: {path}"
        return found

    return {
        "salary": find("Salary › Net salary", "income"),
        "groceries": find("Food › Groceries", "expense"),
        "rent": find("Housing › Rent", "expense"),
        "emergency": find("Emergency fund", "savings"),
        "investments": find("Investments › Stocks & ETFs", "investment"),
        "debt": find("Debt repayment", "debt"),
    }


@pytest.fixture()
def D():
    return Decimal
