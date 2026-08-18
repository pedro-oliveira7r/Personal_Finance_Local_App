"""Builds the forward projection from budgets, rules and history."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from calculations.forecasting import (
    SOURCE_ACTUAL,
    SOURCE_AVERAGE,
    SOURCE_BUDGET,
    SOURCE_RULES,
    ForecastAlert,
    ForecastAssumption,
    ForecastRow,
    average_assumption,
    build_forecast,
    first_negative,
    forecast_alerts,
    forecast_totals,
    lowest_point,
    runway_periods,
    scenario,
)
from calculations.money import ZERO, D, money, money_sum
from calculations.periods import Period, shift_period
from constants import (
    CASH_ACCOUNT_TYPES,
    AllocationTarget,
    CategoryKind,
    TxnKind,
)
from database.models import Account, BudgetLine, Category, Goal
from services.budget_service import (
    get_period_row,
    lines_for_period,
    projected_cash_at,
)
from services.common import load_account_infos, load_cash_txns, settings_snapshot
from services.recurring_service import project_period
from services.transaction_service import actuals_for_period


@dataclass
class ForecastBundle:
    rows: list[ForecastRow] = field(default_factory=list)
    alerts: list[ForecastAlert] = field(default_factory=list)
    totals: dict[str, Decimal] = field(default_factory=dict)
    average: Optional[ForecastAssumption] = None
    start_cash: Decimal = ZERO

    @property
    def future_rows(self) -> list[ForecastRow]:
        return [row for row in self.rows if not row.is_actual]

    @property
    def actual_rows(self) -> list[ForecastRow]:
        return [row for row in self.rows if row.is_actual]

    @property
    def first_negative(self) -> Optional[ForecastRow]:
        return first_negative(self.future_rows)

    @property
    def lowest(self) -> Optional[ForecastRow]:
        return lowest_point(self.rows)

    @property
    def runway(self) -> Optional[int]:
        return runway_periods(self.future_rows)


def _cash_like_account_ids(session: Session) -> set[int]:
    rows = session.execute(
        select(Account.id, Account.type, Account.include_in_cash)
    ).all()
    return {
        row[0] for row in rows
        if row[1] in CASH_ACCOUNT_TYPES and bool(row[2])
    }


def _assumption_from_budget(
    session: Session,
    period: Period,
    cash_ids: set[int],
) -> Optional[ForecastAssumption]:
    row = get_period_row(session, period.year, period.month)
    if row is None:
        return None
    lines = lines_for_period(session, row)
    if not lines:
        return None

    goal_accounts = {
        r[0]: r[1] for r in session.execute(select(Goal.id, Goal.account_id)).all()
    }
    assumption = ForecastAssumption(period_key=period.key, source=SOURCE_BUDGET)
    for line in lines:
        amount = money(line.planned_amount)
        if line.kind == CategoryKind.INCOME.value:
            assumption.income = money(assumption.income + amount)
        elif line.kind == CategoryKind.SAVINGS.value:
            target_account = line.account_id
            if target_account is None and line.goal_id:
                target_account = goal_accounts.get(line.goal_id)
            stays_in_cash = target_account is None or target_account in cash_ids
            if stays_in_cash:
                assumption.savings_reserved = money(assumption.savings_reserved + amount)
            else:
                assumption.savings_outflow = money(assumption.savings_outflow + amount)
        elif line.kind == CategoryKind.INVESTMENT.value:
            assumption.investments = money(assumption.investments + amount)
        elif line.kind == CategoryKind.DEBT.value:
            assumption.debt_payments = money(assumption.debt_payments + amount)
        else:
            assumption.expenses = money(assumption.expenses + amount)
    return assumption


def _assumption_from_rules(session: Session, period: Period) -> Optional[ForecastAssumption]:
    projection = project_period(session, period)
    if not any([projection.income, projection.expense, projection.savings,
                projection.investment, projection.debt]):
        return None
    return ForecastAssumption(
        period_key=period.key,
        income=projection.income,
        expenses=projection.expense,
        savings_reserved=projection.savings,
        investments=projection.investment,
        debt_payments=projection.debt,
        source=SOURCE_RULES,
    )


def _assumption_from_actuals(session: Session, period: Period) -> ForecastAssumption:
    actuals = actuals_for_period(session, period)
    return ForecastAssumption(
        period_key=period.key,
        income=actuals.income_total,
        expenses=money(actuals.expense_total + actuals.uncategorised),
        savings_reserved=actuals.savings_total,
        investments=actuals.investment_total,
        debt_payments=actuals.debt_total,
        source=SOURCE_ACTUAL,
    )


def history_series(session: Session, months: int = 6,
                   today: Optional[date] = None) -> list[dict[str, Decimal]]:
    """Recent actuals shaped for :func:`average_assumption`."""
    settings = settings_snapshot(session)
    today = today or date.today()
    current = settings.current_period(today)
    output: list[dict[str, Decimal]] = []
    for offset in range(max(1, months), 0, -1):
        period = shift_period(current, -offset, settings.first_day_of_month)
        actuals = actuals_for_period(session, period)
        output.append({
            "period": period.key,
            "income": actuals.income_total,
            "expenses": money(actuals.expense_total + actuals.uncategorised),
            "savings": actuals.savings_total,
            "investments": actuals.investment_total,
            "debt_payments": actuals.debt_total,
        })
    return output


def build(
    session: Session,
    *,
    months: Optional[int] = None,
    history_months: int = 3,
    average_window: int = 6,
    today: Optional[date] = None,
    use_average_fallback: bool = True,
    low_cash_threshold: Decimal = ZERO,
) -> ForecastBundle:
    """Project ``months`` periods forward, prefixed by ``history_months`` actuals.

    Source preference per future period: an explicit budget wins, then the
    recurrence rules, then an average of recent history.
    """
    settings = settings_snapshot(session)
    today = today or date.today()
    months = months or settings.forecast_months
    months = max(1, min(int(months), config.MAX_FORECAST_MONTHS))
    history_months = max(0, min(int(history_months), 24))

    current = settings.current_period(today)
    periods = [
        shift_period(current, offset, settings.first_day_of_month)
        for offset in range(-history_months, months)
    ]
    cash_ids = _cash_like_account_ids(session)

    assumptions: dict[str, ForecastAssumption] = {}
    for period in periods:
        if period.end < current.start:
            assumptions[period.key] = _assumption_from_actuals(session, period)
            continue
        assumption = _assumption_from_budget(session, period, cash_ids)
        if assumption is None:
            assumption = _assumption_from_rules(session, period)
        if assumption is not None:
            assumptions[period.key] = assumption

    average = None
    if use_average_fallback:
        average = average_assumption(history_series(session, average_window, today),
                                     months=average_window)

    start_period = periods[0]
    start_cash = projected_cash_at(
        session, start_period.start - timedelta(days=1), today=today
    )
    reserved = _reserved_before(session, start_period, today=today)

    rows = build_forecast(
        periods, start_cash, assumptions,
        opening_reserved=reserved,
        default_assumption=average,
        today=today,
    )
    return ForecastBundle(
        rows=rows,
        alerts=forecast_alerts(rows, low_cash_threshold=low_cash_threshold),
        totals=forecast_totals([r for r in rows if not r.is_actual]),
        average=average,
        start_cash=start_cash,
    )


def _reserved_before(session: Session, period: Period,
                     today: Optional[date] = None) -> Decimal:
    """Goal money already sitting inside the cash pool when we start.

    Only goals held in cash-like accounts count: money in an investment
    account was never part of ``closing_cash`` to begin with.
    """
    from services.goal_service import earmarked_in_cash

    return earmarked_in_cash(session, today=today)


def run_scenario(
    bundle: ForecastBundle,
    *,
    income_pct: Decimal = ZERO,
    expense_pct: Decimal = ZERO,
    one_off: Optional[dict[str, Decimal]] = None,
    today: Optional[date] = None,
    low_cash_threshold: Decimal = ZERO,
) -> ForecastBundle:
    """Re-run only the future part of a projection with adjusted assumptions."""
    future = bundle.future_rows
    if not future:
        return bundle
    rows = scenario(
        future,
        income_pct=income_pct,
        expense_pct=expense_pct,
        one_off=one_off,
        opening_cash=future[0].opening_cash,
        opening_reserved=future[0].reserved - future[0].assumption.savings_reserved,
        today=today,
    )
    combined = bundle.actual_rows + rows
    return ForecastBundle(
        rows=combined,
        alerts=forecast_alerts(rows, low_cash_threshold=low_cash_threshold),
        totals=forecast_totals(rows),
        average=bundle.average,
        start_cash=bundle.start_cash,
    )


def cash_at_period(bundle: ForecastBundle, period_key: str) -> Optional[Decimal]:
    for row in bundle.rows:
        if row.period.key == period_key:
            return row.closing_cash
    return None
