"""Historical analysis, trends and the dashboard snapshot.

This is the read-only layer the Dashboard and Reports screens sit on. It never
writes, so it is safe to call as often as a rerun needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from calculations.budgeting import savings_rate
from calculations.cashflow import (
    accounts_in,
    PeriodCashflow,
    cash_available,
    cashflow_series,
    income_timing,
    period_cashflow,
    upcoming_outflows,
)
from calculations.money import ZERO, money, money_sum, pct_of
from calculations.periods import Period, quarter_of_month, shift_period
from calculations.variance import (
    VarianceRow,
    approaching_limit,
    income_shortfalls,
    summarise,
    top_overspending,
    top_underspending,
)
from constants import CategoryKind, Severity, TxnKind
from database.models import Category, Transaction
from services import account_service, debt_service, goal_service, networth_service
from services.budget_service import (
    BudgetSummary,
    TrackingResult,
    carry_in_for,
    summarise_period,
    track_period,
)
from services.common import (
    category_name_map,
    load_account_infos,
    load_cash_txns,
    settings_snapshot,
)
from services.transaction_service import (
    actuals_for_period,
    overdue_planned,
    upcoming_planned,
)


# ==========================================================================
# History
# ==========================================================================
def period_history(session: Session, periods: Sequence[Period],
                   *, today: Optional[date] = None,
                   currency: Optional[str] = None) -> list[dict[str, Any]]:
    """One row per period with the headline figures, oldest first."""
    settings = settings_snapshot(session)
    today = today or date.today()
    accounts = accounts_in(load_account_infos(session), currency)
    txns = load_cash_txns(session)
    flows = cashflow_series(
        periods, accounts, txns,
        availability_rule=settings.income_availability_rule,
        cutoff_day=settings.income_cutoff_day,
        first_day_of_month=settings.first_day_of_month,
        today=today,
    )
    rows: list[dict[str, Any]] = []
    for period, flow in zip(periods, flows):
        actuals = actuals_for_period(session, period)
        expenses = money(actuals.expense_total + actuals.uncategorised)
        saved = money(actuals.savings_total + actuals.investment_total)
        rows.append({
            "period": period.key,
            "label": period.short_label,
            "year": period.year,
            "month": period.month,
            "quarter": quarter_of_month(period.month, settings.fiscal_year_start_month),
            "income": actuals.income_total,
            "expenses": expenses,
            "savings": actuals.savings_total,
            "investments": actuals.investment_total,
            "debt_payments": actuals.debt_total,
            "total_outflow": money(expenses + saved + actuals.debt_total),
            "net": money(actuals.income_total - expenses - saved - actuals.debt_total),
            "cash_flow": flow.net_flow,
            "opening_cash": flow.opening_cash,
            "closing_cash": flow.closing_cash,
            "savings_rate": savings_rate(actuals.income_total, saved),
            "is_actual": flow.is_actual,
            "txn_count": flow.txn_count,
        })
    return rows


def trailing_periods(session: Session, months: int = 12,
                     today: Optional[date] = None,
                     *, include_current: bool = True) -> list[Period]:
    settings = settings_snapshot(session)
    today = today or date.today()
    current = settings.current_period(today)
    end_offset = 0 if include_current else -1
    start_offset = end_offset - max(1, months) + 1
    return [
        shift_period(current, offset, settings.first_day_of_month)
        for offset in range(start_offset, end_offset + 1)
    ]


def trailing_history(session: Session, months: int = 12,
                     today: Optional[date] = None,
                     currency: Optional[str] = None) -> list[dict[str, Any]]:
    return period_history(session, trailing_periods(session, months, today),
                          today=today, currency=currency)


def averages(session: Session, months: int = 6,
             today: Optional[date] = None,
             currency: Optional[str] = None) -> dict[str, Decimal]:
    rows = [row for row in trailing_history(session, months + 1, today, currency)][:-1] or []
    if not rows:
        return {key: ZERO for key in
                ("income", "expenses", "savings", "investments", "debt_payments", "net")}
    count = Decimal(len(rows))
    keys = ("income", "expenses", "savings", "investments", "debt_payments", "net")
    result = {key: money(money_sum(row[key] for row in rows) / count) for key in keys}
    result["savings_rate"] = savings_rate(
        result["income"], money(result["savings"] + result["investments"])
    )
    return result


# ==========================================================================
# Category analysis
# ==========================================================================
def category_totals(session: Session, period: Period, *,
                    kinds: Optional[Sequence[str]] = None,
                    roll_up: bool = True, limit: Optional[int] = None,
                    currency: Optional[str] = None) -> list[dict[str, Any]]:
    """Spending per category inside a period, biggest first."""
    actuals = actuals_for_period(session, period, currency=currency)
    names = category_name_map(session)
    rows = session.execute(select(Category.id, Category.kind, Category.parent_id,
                                 Category.name, Category.color)).all()
    meta = {row[0]: {"kind": row[1], "parent_id": row[2], "name": row[3], "color": row[4]}
            for row in rows}

    bucket: dict[int, Decimal] = {}
    source = actuals.by_parent if roll_up else actuals.by_category
    for category_id, amount in source.items():
        info = meta.get(category_id)
        if info is None:
            continue
        if kinds and info["kind"] not in kinds:
            continue
        bucket[category_id] = money(bucket.get(category_id, ZERO) + amount)

    total = money_sum(bucket.values())
    output = [
        {
            "category_id": category_id,
            "label": meta[category_id]["name"] if roll_up else names.get(category_id, "?"),
            "kind": meta[category_id]["kind"],
            "color": meta[category_id]["color"],
            "amount": amount,
            "share_pct": pct_of(amount, total),
        }
        for category_id, amount in bucket.items() if amount != 0
    ]
    output.sort(key=lambda row: row["amount"], reverse=True)
    if actuals.uncategorised and (not kinds or CategoryKind.EXPENSE.value in kinds):
        output.append({
            "category_id": None,
            "label": "Uncategorised",
            "kind": CategoryKind.EXPENSE.value,
            "color": "#898781",
            "amount": actuals.uncategorised,
            "share_pct": pct_of(actuals.uncategorised, total),
        })
    return output[:limit] if limit else output


def category_trend(session: Session, category_ids: Sequence[int],
                   periods: Sequence[Period]) -> list[dict[str, Any]]:
    """A time series per category, for the trend chart."""
    names = category_name_map(session)
    child_map: dict[int, list[int]] = {}
    for parent_id in category_ids:
        children = [
            row[0] for row in session.execute(
                select(Category.id).where(Category.parent_id == parent_id)
            ).all()
        ]
        child_map[parent_id] = [parent_id, *children]

    output: list[dict[str, Any]] = []
    for period in periods:
        actuals = actuals_for_period(session, period)
        for parent_id, ids in child_map.items():
            amount = money_sum(actuals.by_category.get(cid, ZERO) for cid in ids)
            output.append({
                "period": period.key,
                "label": period.short_label,
                "category_id": parent_id,
                "category": names.get(parent_id, f"#{parent_id}"),
                "amount": amount,
            })
    return output


# ==========================================================================
# Dashboard snapshot
# ==========================================================================
@dataclass
class Alert:
    severity: str
    code: str
    message: str
    detail: Optional[str] = None

    @property
    def icon(self) -> str:
        from constants import SEVERITY_ICONS
        return SEVERITY_ICONS.get(self.severity, "•")


@dataclass
class DashboardSnapshot:
    period: Period
    today: date
    cash: Decimal = ZERO
    net_worth: Decimal = ZERO
    total_assets: Decimal = ZERO
    total_liabilities: Decimal = ZERO
    total_savings: Decimal = ZERO
    total_investments: Decimal = ZERO
    total_debt: Decimal = ZERO

    income_actual: Decimal = ZERO
    income_planned: Decimal = ZERO
    expenses_actual: Decimal = ZERO
    expenses_planned: Decimal = ZERO
    savings_actual: Decimal = ZERO
    savings_planned: Decimal = ZERO
    net_cash_flow: Decimal = ZERO
    savings_rate_pct: Decimal = ZERO
    budget_utilisation_pct: Decimal = ZERO
    available_to_budget: Decimal = ZERO
    unallocated: Decimal = ZERO
    emergency_months: Optional[Decimal] = None

    budget: Optional[BudgetSummary] = None
    tracking: Optional[TrackingResult] = None
    flow: Optional[PeriodCashflow] = None
    alerts: list[Alert] = field(default_factory=list)
    top_overspend: list[VarianceRow] = field(default_factory=list)
    top_underspend: list[VarianceRow] = field(default_factory=list)
    near_limit: list[VarianceRow] = field(default_factory=list)
    upcoming: list[Transaction] = field(default_factory=list)
    overdue: list[Transaction] = field(default_factory=list)
    goal_progress: list = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.flow and self.flow.txn_count) or bool(
            self.budget and self.budget.has_plan)


#: Snapshot fields that are money in the filtered currency and therefore
#: convertible. Percentages, counts and month-spans are recomputed, not summed.
_CONVERTIBLE_FIELDS = (
    "cash", "net_worth", "total_assets", "total_liabilities", "total_savings",
    "total_investments", "total_debt", "income_actual", "income_planned",
    "expenses_actual", "expenses_planned", "savings_actual", "savings_planned",
    "net_cash_flow", "available_to_budget", "unallocated",
)

#: The same idea for one period's cash flow.
_CONVERTIBLE_FLOW_FIELDS = (
    "opening_cash", "income_received", "income_available", "income_earned",
    "expenses_paid", "transfers_in", "transfers_out", "savings_contributed",
    "investments_contributed", "debt_paid", "net_flow", "closing_cash",
)


def dashboard_combined(session: Session, period: Optional[Period] = None,
                       today: Optional[date] = None,
                       *, book=None) -> DashboardSnapshot:
    """Every currency, converted into the primary and added up.

    Built by computing a real per-currency snapshot and converting it, rather
    than by converting transactions on the way in. Both give the same answer
    while the rate is uniform, and this way each currency's own figures stay
    exact and auditable — the conversion happens once per headline number.

    Ratios (savings rate, budget utilisation) are **recomputed** from the
    converted totals. Averaging percentages across currencies would weight a
    small euro balance the same as a large real one.
    """
    from services.currency_service import book as currency_book

    settings = settings_snapshot(session)
    today = today or date.today()
    period = period or settings.current_period(today)
    book = book or currency_book(session)

    parts = [(code, dashboard(session, period, today, currency=code))
             for code in book.active]
    combined = dashboard(session, period, today, currency=book.primary)

    for name in _CONVERTIBLE_FIELDS:
        setattr(combined, name, money_sum(
            book.convert(getattr(part, name), code, book.primary)
            for code, part in parts
        ))
    if combined.flow is not None:
        for name in _CONVERTIBLE_FLOW_FIELDS:
            setattr(combined.flow, name, money_sum(
                book.convert(getattr(part.flow, name), code, book.primary)
                for code, part in parts if part.flow is not None
            ))

    combined.savings_rate_pct = savings_rate(
        combined.income_actual, combined.savings_actual)
    planned_out = money_sum(
        book.convert(
            money(part.expenses_planned + part.savings_planned), code, book.primary)
        for code, part in parts
    )
    actual_out = money_sum(
        book.convert(
            money(part.expenses_actual + part.savings_actual), code, book.primary)
        for code, part in parts
    )
    combined.budget_utilisation_pct = pct_of(actual_out, planned_out)

    # Alerts are per-currency statements of fact; concatenating them keeps each
    # one true in its own denomination rather than inventing a converted one.
    combined.alerts = [alert for _, part in parts for alert in part.alerts]
    combined.upcoming = [t for _, part in parts for t in part.upcoming]
    combined.overdue = [t for _, part in parts for t in part.overdue]
    return combined


def dashboard(session: Session, period: Optional[Period] = None,
              today: Optional[date] = None,
              currency: Optional[str] = None) -> DashboardSnapshot:
    """Everything the Dashboard needs, computed once.

    Every figure on the snapshot is a scalar, so on a mixed book they are only
    meaningful once ``currency`` narrows them to one denomination. ``None``
    keeps the pre-multi-currency behaviour and is correct for a single-currency
    book.
    """
    settings = settings_snapshot(session)
    today = today or date.today()
    period = period or settings.current_period(today)

    accounts = accounts_in(load_account_infos(session), currency)
    txns = load_cash_txns(session)
    views = account_service.balance_views(session, as_of=today)
    account_totals = account_service.totals(views, currency)

    flow = period_cashflow(
        period, accounts, txns,
        availability_rule=settings.income_availability_rule,
        cutoff_day=settings.income_cutoff_day,
        first_day_of_month=settings.first_day_of_month,
    )
    budget = summarise_period(session, period, today=today, currency=currency)
    tracking = track_period(session, period, today=today, currency=currency)
    debt_summary = debt_service.totals(session, currency=currency)
    goals = goal_service.all_progress(session, today=today, currency=currency)

    snapshot = DashboardSnapshot(period=period, today=today, flow=flow,
                                budget=budget, tracking=tracking)
    # Debts linked to an account are already counted through that account's
    # balance; only unlinked debts are added on top, so nothing is double-counted.
    snapshot.cash = account_totals.cash
    snapshot.total_assets = account_totals.assets
    snapshot.total_liabilities = money(
        account_totals.liabilities + debt_summary.get("unlinked_balance", ZERO)
    )
    snapshot.net_worth = money(snapshot.total_assets - snapshot.total_liabilities)
    snapshot.total_savings = account_totals.savings
    snapshot.total_investments = account_totals.investments
    snapshot.total_debt = snapshot.total_liabilities

    snapshot.income_actual = tracking.income.actual
    snapshot.income_planned = tracking.income.planned
    snapshot.expenses_actual = tracking.expenses.actual
    snapshot.expenses_planned = tracking.expenses.planned
    snapshot.savings_actual = money(tracking.savings.actual + tracking.investments.actual)
    snapshot.savings_planned = money(tracking.savings.planned + tracking.investments.planned)
    snapshot.net_cash_flow = flow.net_flow
    snapshot.savings_rate_pct = savings_rate(snapshot.income_actual, snapshot.savings_actual)
    planned_out = money(tracking.expenses.planned + tracking.savings.planned
                        + tracking.investments.planned + tracking.debt.planned)
    actual_out = money(tracking.expenses.actual + tracking.savings.actual
                       + tracking.investments.actual + tracking.debt.actual)
    snapshot.budget_utilisation_pct = pct_of(actual_out, planned_out)
    snapshot.available_to_budget = budget.result.available
    snapshot.unallocated = budget.result.unallocated

    monthly_average = averages(session, 6, today, currency)
    snapshot.emergency_months = networth_service.emergency_fund_months(
        session, monthly_average["expenses"], today, currency
    )

    snapshot.top_overspend = top_overspending(tracking.allocation_rows)
    snapshot.top_underspend = top_underspending(tracking.allocation_rows)
    snapshot.near_limit = approaching_limit(
        tracking.allocation_rows,
        settings.warning_threshold_pct,
        settings.critical_threshold_pct,
    )
    snapshot.upcoming = upcoming_planned(session, 30, today)
    snapshot.overdue = overdue_planned(session, today)
    snapshot.goal_progress = goals
    snapshot.alerts = build_alerts(
        session, snapshot, settings=settings, today=today, monthly_average=monthly_average
    )
    return snapshot


def build_alerts(session: Session, snapshot: DashboardSnapshot, *,
                 settings=None, today: Optional[date] = None,
                 monthly_average: Optional[dict[str, Decimal]] = None) -> list[Alert]:
    """Turn the snapshot into a prioritised list of things to act on."""
    settings = settings or settings_snapshot(session)
    today = today or date.today()
    alerts: list[Alert] = []
    tracking = snapshot.tracking
    budget = snapshot.budget

    # 1. Cash position
    if snapshot.cash < 0:
        alerts.append(Alert(Severity.CRITICAL.value, "negative_cash",
                            "Your cash accounts are overdrawn.",
                            f"Combined balance: {snapshot.cash}."))

    # 2. Budget balance
    if budget is not None and budget.has_plan:
        if budget.result.status == "over_allocated":
            alerts.append(Alert(Severity.CRITICAL.value, "over_allocated",
                                f"{snapshot.period.label} allocates "
                                f"{budget.result.overspend} more than is available.",
                                "Trim an allocation or revise expected income."))
        elif budget.result.status == "under_allocated":
            alerts.append(Alert(Severity.WARNING.value, "under_allocated",
                                f"{budget.result.unallocated} in "
                                f"{snapshot.period.label} has no job yet.",
                                "Zero-based budgeting assigns every unit of currency."))

    # 3. Overspending categories
    if tracking is not None:
        for row in snapshot.top_overspend[:3]:
            alerts.append(Alert(Severity.CRITICAL.value, "category_over",
                                f"“{row.label}” is {row.overshoot} over its "
                                f"{row.planned} budget.",
                                f"Consumed {row.consumed_pct}% of the plan."))
        for row in snapshot.near_limit[:3]:
            alerts.append(Alert(Severity.WARNING.value, "category_near_limit",
                                f"“{row.label}” has used {row.consumed_pct}% "
                                f"of its budget.",
                                f"{row.remaining_positive} left."))
        for row in income_shortfalls(tracking.rows)[:2]:
            alerts.append(Alert(Severity.WARNING.value, "income_short",
                                f"“{row.label}” is {abs(row.variance)} below plan.",
                                f"Received {row.actual} of {row.planned} expected."))
        if tracking.unbudgeted:
            total = money_sum(row.actual for row in tracking.unbudgeted)
            alerts.append(Alert(Severity.WARNING.value, "unbudgeted_spend",
                                f"{total} was spent in "
                                f"{len(tracking.unbudgeted)} unbudgeted category(ies).",
                                "Add them to the plan so next period is realistic."))
        if tracking.uncategorised_total > 0:
            alerts.append(Alert(Severity.INFO.value, "uncategorised",
                                f"{tracking.uncategorised_total} of activity has no category.",
                                "Categorise it to keep reports meaningful."))

    # 4. Overdue and upcoming
    if snapshot.overdue:
        total = money_sum(txn.amount for txn in snapshot.overdue)
        alerts.append(Alert(Severity.WARNING.value, "overdue_planned",
                            f"{len(snapshot.overdue)} planned transaction(s) "
                            f"are past due ({total}).",
                            "Mark them done or reschedule them."))
    large_upcoming = [txn for txn in snapshot.upcoming
                      if txn.amount >= (monthly_average or {}).get("expenses", ZERO) / Decimal(4)
                      and txn.kind == TxnKind.EXPENSE.value]
    if large_upcoming:
        nearest = large_upcoming[0]
        alerts.append(Alert(Severity.INFO.value, "large_upcoming",
                            f"Large expense coming up: {nearest.description} "
                            f"({nearest.amount}) on {nearest.txn_date.isoformat()}.",
                            f"{len(large_upcoming)} sizeable payment(s) in the next 30 days."))

    # 5. Emergency fund
    if snapshot.emergency_months is not None and snapshot.emergency_months < 3:
        alerts.append(Alert(Severity.WARNING.value, "thin_emergency_fund",
                            f"Cash covers about {snapshot.emergency_months} month(s) "
                            "of average spending.",
                            "Three to six months is the usual comfort range."))

    # 6. Goals and debts
    for severity, message in goal_service.alerts(session, today=today):
        alerts.append(Alert(severity, "goal", message))
    for severity, message in debt_service.alerts(session):
        alerts.append(Alert(severity, "debt", message))

    # 7. Forecast
    try:
        from services import forecast_service

        bundle = forecast_service.build(session, months=6, history_months=0, today=today)
        for alert in bundle.alerts:
            alerts.append(Alert(alert.severity, alert.code, alert.message))
    except Exception:  # pragma: no cover - the dashboard must never crash on this
        pass

    order = {Severity.CRITICAL.value: 0, Severity.WARNING.value: 1,
             Severity.INFO.value: 2, Severity.SUCCESS.value: 3}
    alerts.sort(key=lambda item: order.get(item.severity, 9))
    return alerts
