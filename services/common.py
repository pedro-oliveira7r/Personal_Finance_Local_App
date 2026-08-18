"""Shared plumbing for the service layer.

Loaders here convert ORM rows into the plain dataclasses the calculation
modules expect, which keeps every formula testable without a database and
means the UI only ever deals with one shape of data.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.orm import Session

from calculations.cashflow import AccountInfo, CashTxn, account_info_from_orm
from calculations.money import ZERO, money
from calculations.periods import Period, make_period, period_for_date
from constants import AvailabilityRule, BudgetMethod, TxnStatus
from database.models import (
    Account,
    AccountValuation,
    AppSettings,
    Category,
    RecycleBin,
    Transaction,
)


class ServiceError(Exception):
    """A problem worth showing to the user verbatim."""


class NotFoundError(ServiceError):
    pass


class ConflictError(ServiceError):
    """The operation would damage or duplicate existing data."""


# ==========================================================================
# Settings snapshot
# ==========================================================================
@dataclass(frozen=True)
class SettingsSnapshot:
    """Immutable copy of user preferences, safe to pass around and cache."""

    base_currency: str = "BRL"
    date_format: str = "DD/MM/YYYY"
    date_pattern: str = "%d/%m/%Y"
    show_cents: bool = True
    first_day_of_month: int = 1
    fiscal_year_start_month: int = 1
    budget_method: str = BudgetMethod.ZERO_BASED.value
    carry_over_surplus: bool = True
    income_availability_rule: str = AvailabilityRule.EARNED_PERIOD.value
    income_cutoff_day: int = 25
    warning_threshold_pct: Decimal = Decimal("80")
    critical_threshold_pct: Decimal = Decimal("100")
    variance_tolerance_pct: Decimal = Decimal("5")
    forecast_months: int = 12
    theme: str = "auto"
    backup_dir: Optional[str] = None
    onboarded: bool = False

    def period_for(self, value: date) -> Period:
        return period_for_date(value, self.first_day_of_month)

    def period(self, year: int, month: int) -> Period:
        return make_period(year, month, self.first_day_of_month)

    def current_period(self, today: Optional[date] = None) -> Period:
        return self.period_for(today or date.today())


def settings_snapshot(session: Session) -> SettingsSnapshot:
    from constants import DATE_FORMATS

    row = session.get(AppSettings, 1)
    if row is None:
        from database.seed import get_or_create_settings

        row = get_or_create_settings(session)
        session.commit()
    return SettingsSnapshot(
        base_currency=row.base_currency,
        date_format=row.date_format,
        date_pattern=DATE_FORMATS.get(row.date_format, "%d/%m/%Y"),
        show_cents=row.show_cents,
        first_day_of_month=row.first_day_of_month,
        fiscal_year_start_month=row.fiscal_year_start_month,
        budget_method=row.budget_method,
        carry_over_surplus=row.carry_over_surplus,
        income_availability_rule=row.income_availability_rule,
        income_cutoff_day=row.income_cutoff_day,
        warning_threshold_pct=row.warning_threshold_pct,
        critical_threshold_pct=row.critical_threshold_pct,
        variance_tolerance_pct=row.variance_tolerance_pct,
        forecast_months=row.forecast_months,
        theme=row.theme,
        backup_dir=row.backup_dir,
        onboarded=row.onboarded,
    )


# ==========================================================================
# Lookups
# ==========================================================================
def category_kind_map(session: Session) -> dict[int, str]:
    rows = session.execute(select(Category.id, Category.kind)).all()
    return {row[0]: row[1] for row in rows}


def category_name_map(session: Session) -> dict[int, str]:
    """``{id: "Parent › Child"}`` for display and CSV export."""
    rows = session.execute(
        select(Category.id, Category.name, Category.parent_id)
    ).all()
    names = {row[0]: row[1] for row in rows}
    result: dict[int, str] = {}
    for cid, name, parent_id in rows:
        result[cid] = f"{names.get(parent_id)} › {name}" if parent_id in names else name
    return result


def account_name_map(session: Session) -> dict[int, str]:
    rows = session.execute(select(Account.id, Account.name)).all()
    return {row[0]: row[1] for row in rows}


def valuations_by_account(session: Session) -> dict[int, list[tuple[date, Decimal]]]:
    """All manual valuations per account, ascending by date."""
    rows = session.execute(
        select(AccountValuation).order_by(AccountValuation.as_of_date)
    ).scalars().all()
    result: dict[int, list[tuple[date, Decimal]]] = {}
    for row in rows:
        result.setdefault(row.account_id, []).append((row.as_of_date, row.value))
    return result


def latest_valuations(session: Session) -> dict[int, tuple[Decimal, date]]:
    """Most recent manual valuation per account (value, date)."""
    return {
        account_id: (entries[-1][1], entries[-1][0])
        for account_id, entries in valuations_by_account(session).items()
    }


def load_accounts(session: Session, *, include_archived: bool = False) -> list[Account]:
    stmt = select(Account).order_by(Account.sort_order, Account.name)
    if not include_archived:
        stmt = stmt.where(Account.is_archived.is_(False))
    return list(session.execute(stmt).scalars())


def load_account_infos(session: Session, *, include_archived: bool = True) -> list[AccountInfo]:
    valuations = valuations_by_account(session)
    return [
        account_info_from_orm(account, valuations.get(account.id, []))
        for account in load_accounts(session, include_archived=include_archived)
    ]


def load_cash_txns(
    session: Session,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    include_planned: bool = True,
) -> list[CashTxn]:
    """Every active transaction as a :class:`CashTxn`, ready for the maths."""
    kinds = category_kind_map(session)
    stmt = (
        select(Transaction)
        .where(Transaction.deleted_at.is_(None))
        .where(Transaction.status != TxnStatus.VOID.value)
        .order_by(Transaction.txn_date, Transaction.id)
    )
    if not include_planned:
        stmt = stmt.where(Transaction.status == TxnStatus.COMPLETED.value)
    if start is not None:
        stmt = stmt.where(Transaction.txn_date >= start)
    if end is not None:
        stmt = stmt.where(Transaction.txn_date <= end)

    result: list[CashTxn] = []
    for txn in session.execute(stmt).scalars():
        result.append(CashTxn(
            id=txn.id,
            txn_date=txn.txn_date,
            amount=txn.amount,
            kind=txn.kind,
            status=txn.status,
            actual_date=txn.actual_date,
            availability_date=txn.availability_date,
            account_id=txn.account_id,
            to_account_id=txn.to_account_id,
            category_id=txn.category_id,
            category_kind=kinds.get(txn.category_id) if txn.category_id else None,
            exclude_from_budget=txn.exclude_from_budget,
        ))
    return result


# ==========================================================================
# Recycle bin
# ==========================================================================
_SKIP_KEYS = {"created_at", "updated_at"}


def orm_to_dict(instance: Any) -> dict[str, Any]:
    """JSON-safe snapshot of an ORM row."""
    mapper = sa_inspect(instance).mapper
    payload: dict[str, Any] = {}
    for column in mapper.columns:
        value = getattr(instance, column.key, None)
        if isinstance(value, (_dt.date, _dt.datetime)):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = str(value)
        payload[column.key] = value
    return payload


def send_to_recycle_bin(session: Session, entity_type: str, instance: Any,
                        label: str = "") -> RecycleBin:
    entry = RecycleBin(
        entity_type=entity_type,
        entity_id=getattr(instance, "id", 0) or 0,
        label=label or str(getattr(instance, "name", "") or getattr(instance, "description", "")),
        payload=orm_to_dict(instance),
    )
    session.add(entry)
    return entry


def list_recycle_bin(session: Session, limit: int = 50) -> list[RecycleBin]:
    stmt = (
        select(RecycleBin)
        .where(RecycleBin.restored_at.is_(None))
        .order_by(RecycleBin.deleted_at.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


# ==========================================================================
# Misc helpers
# ==========================================================================
def apply_fields(instance: Any, payload: dict[str, Any], *, skip: Iterable[str] = ()) -> Any:
    """Copy validated payload fields onto an ORM instance."""
    skipset = set(skip) | _SKIP_KEYS
    for key, value in payload.items():
        if key in skipset or not hasattr(instance, key):
            continue
        setattr(instance, key, value)
    return instance


def ensure_exists(session: Session, model, identifier: Optional[int], what: str):
    if identifier is None:
        return None
    instance = session.get(model, identifier)
    if instance is None:
        raise NotFoundError(f"{what} #{identifier} no longer exists.")
    return instance


def coalesce_period(settings: SettingsSnapshot, period: Optional[Period],
                    year: Optional[int] = None, month: Optional[int] = None,
                    today: Optional[date] = None) -> Period:
    if period is not None:
        return period
    if year and month:
        return settings.period(year, month)
    return settings.current_period(today)
