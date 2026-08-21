"""Net worth: current position, history and snapshots."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from calculations.cashflow import account_balance, accounts_in
from calculations.money import ZERO, money, money_sum
from calculations.net_worth import (
    NetWorthLine,
    NetWorthPoint,
    NetWorthSummary,
    change_between,
    liquidity_ratio,
    project_net_worth,
    summarise_net_worth,
)
from calculations.periods import Period, shift_period
from database.models import NetWorthSnapshot
from services.common import (
    NotFoundError,
    load_account_infos,
    load_accounts,
    load_cash_txns,
    settings_snapshot,
)


def current_summary(session: Session, *, as_of: Optional[date] = None,
                    currency: Optional[str] = None) -> NetWorthSummary:
    """Net worth right now (or on ``as_of``), from account balances."""
    as_of = as_of or date.today()
    code = currency.upper() if currency else None
    accounts = {a.id: a for a in load_accounts(session, include_archived=True)}
    infos = accounts_in(load_account_infos(session), currency)
    txns = load_cash_txns(session)
    lines: list[NetWorthLine] = []
    for info in infos:
        account = accounts.get(info.id)
        if account is None:
            continue
        lines.append(NetWorthLine(
            account_id=info.id,
            name=account.name,
            type=info.type,
            balance=account_balance(info, txns, as_of=as_of),
            include=account.include_in_net_worth,
        ))
    lines.extend(_unlinked_debt_lines(session, currency=code))
    return summarise_net_worth(lines, as_of=as_of)


def _unlinked_debt_lines(session: Session, *,
                         currency: Optional[str] = None) -> list[NetWorthLine]:
    """Debts with no account of their own still count against net worth."""
    from constants import AccountType
    from database.models import Debt

    rows = session.execute(
        select(Debt).where(Debt.is_active.is_(True), Debt.account_id.is_(None))
    ).scalars().all()
    return [
        NetWorthLine(
            account_id=None,
            name=debt.name,
            type=AccountType.OTHER_LIABILITY.value,
            balance=money(-debt.principal_balance),
        )
        for debt in rows
        if debt.principal_balance
        and (currency is None or (debt.currency or "").upper() == currency)
    ]


def history(session: Session, periods: Sequence[Period]) -> list[NetWorthPoint]:
    """Net worth at the end of each period, computed from real history."""
    accounts = {a.id: a for a in load_accounts(session, include_archived=True)}
    infos = load_account_infos(session)
    txns = load_cash_txns(session)
    points: list[NetWorthPoint] = []
    for period in periods:
        lines = [
            NetWorthLine(
                account_id=info.id,
                name=accounts[info.id].name,
                type=info.type,
                balance=account_balance(info, txns, as_of=period.end),
                include=accounts[info.id].include_in_net_worth,
            )
            for info in infos if info.id in accounts
        ]
        summary = summarise_net_worth(lines, as_of=period.end)
        points.append(NetWorthPoint(
            as_of=period.end,
            total_assets=summary.total_assets,
            total_liabilities=summary.total_liabilities,
            net_worth=summary.net_worth,
            label=period.short_label,
        ))
    return points


def trailing_history(session: Session, months: int = 12,
                     today: Optional[date] = None) -> list[NetWorthPoint]:
    settings = settings_snapshot(session)
    today = today or date.today()
    current = settings.current_period(today)
    periods = [
        shift_period(current, offset, settings.first_day_of_month)
        for offset in range(-max(1, months) + 1, 1)
    ]
    return history(session, periods)


def change(session: Session, months: int = 12,
           today: Optional[date] = None) -> dict[str, Decimal]:
    return change_between(trailing_history(session, months, today))


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------
def save_snapshot(session: Session, *, as_of: Optional[date] = None,
                  manual: bool = False,
                  currency: Optional[str] = None) -> NetWorthSnapshot:
    """Freeze net worth for one currency on one date.

    Stored per currency and never converted. A single combined figure would be
    frozen at whatever rate happened to be latest that day, while the live
    number kept re-valuing — so the history chart would step every time a rate
    was edited, comparing frozen-converted past against live-converted present.
    """
    from services.currency_service import active_currencies

    as_of = as_of or date.today()
    code = (currency or active_currencies(session)[0]).upper()
    summary = current_summary(session, as_of=as_of, currency=code)
    existing = session.execute(
        select(NetWorthSnapshot).where(NetWorthSnapshot.as_of_date == as_of,
                                       NetWorthSnapshot.currency == code)
    ).scalars().first()
    detail = {
        "assets": [[line.name, str(line.magnitude)] for line in summary.assets],
        "liabilities": [[line.name, str(line.magnitude)] for line in summary.liabilities],
    }
    if existing is not None:
        existing.total_assets = summary.total_assets
        existing.total_liabilities = summary.total_liabilities
        existing.net_worth = summary.net_worth
        existing.detail = detail
        existing.is_manual = manual
        session.flush()
        return existing
    snapshot = NetWorthSnapshot(
        as_of_date=as_of,
        currency=code,
        total_assets=summary.total_assets,
        total_liabilities=summary.total_liabilities,
        net_worth=summary.net_worth,
        detail=detail,
        is_manual=manual,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def save_all_snapshots(session: Session, *, as_of: Optional[date] = None,
                       manual: bool = False) -> list[NetWorthSnapshot]:
    """One snapshot per active currency."""
    from services.currency_service import active_currencies

    return [save_snapshot(session, as_of=as_of, manual=manual, currency=code)
            for code in active_currencies(session)]


def list_snapshots(session: Session, limit: int = 120,
                   currency: Optional[str] = None) -> list[NetWorthSnapshot]:
    stmt = select(NetWorthSnapshot).order_by(NetWorthSnapshot.as_of_date.desc())
    if currency is not None:
        stmt = stmt.where(NetWorthSnapshot.currency == currency.upper())
    return list(session.execute(stmt.limit(limit)).scalars())


def delete_snapshot(session: Session, snapshot_id: int) -> None:
    snapshot = session.get(NetWorthSnapshot, snapshot_id)
    if snapshot is None:
        raise NotFoundError("That snapshot no longer exists.")
    session.delete(snapshot)
    session.flush()


def snapshot_points(session: Session,
                    currency: Optional[str] = None) -> list[NetWorthPoint]:
    return [
        NetWorthPoint(
            as_of=row.as_of_date,
            total_assets=row.total_assets,
            total_liabilities=row.total_liabilities,
            net_worth=row.net_worth,
            label=row.as_of_date.isoformat(),
        )
        for row in sorted(list_snapshots(session, currency=currency),
                          key=lambda r: r.as_of_date)
    ]


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------
def projection(session: Session, months: int = 24, *,
               monthly_savings: Decimal = ZERO,
               monthly_debt_reduction: Decimal = ZERO,
               annual_return_pct: Decimal = ZERO,
               today: Optional[date] = None) -> list[NetWorthPoint]:
    summary = current_summary(session, as_of=today)
    start = NetWorthPoint(
        as_of=summary.as_of,
        total_assets=summary.total_assets,
        total_liabilities=summary.total_liabilities,
        net_worth=summary.net_worth,
    )
    return project_net_worth(
        start, monthly_savings, monthly_debt_reduction, months,
        annual_return_pct=annual_return_pct,
    )


def emergency_fund_months(session: Session, monthly_expenses: Decimal,
                          today: Optional[date] = None,
                          currency: Optional[str] = None) -> Optional[Decimal]:
    from services.account_service import balance_views, totals as account_totals

    views = balance_views(session, as_of=today)
    return liquidity_ratio(account_totals(views, currency).cash, monthly_expenses)
