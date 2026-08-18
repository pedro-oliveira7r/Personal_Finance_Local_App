"""Historical analysis, trends and the dashboard snapshot.

This is the read-only layer the Dashboard and Reports screens sit on. It never
writes, so it is safe to call as often as a rerun needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from calculations.budgeting import savings_rate
from calculations.cashflow import (
    PeriodCashflow,
    cash_available,
    cashflow_series,
    income_timing,
    period_cashflow,
    upcoming_outflows,
)
from calculations.money import ZERO, D, money, money_sum, pct_of
from calculations.periods import Period, quarter_of_month, shift_period
from calculations.variance import (
    VarianceRow,
    approaching_limit,
    income_shortfalls,
    summarise,
    top_overspending,
    top_underspending,
)
from constants import CategoryKind, Severity, TxnKind, TxnStatus
from database.models import Category, Transaction
from services import account_service, debt_service, goal_service, networth_service
from services.budget_service import (
    BudgetSummary,
    TrackingResult,
    budget_accuracy_series,
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
    TxnFilter,
    actuals_for_period,
    list_transactions,
    overdue_planned,
    upcoming_planned,
)


# ==========================================================================
# History
# ==========================================================================
def period_history(session: Session, periods: Sequence[Period],
                   *, today: Optional[date] = None) -> list[dict[str, Any]]:
    """One row per period with the headline figures, oldest first."""
    settings = settings_snapshot(session)
    today = today or date.today()
    accounts = load_account_infos(session)
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
                     today: Optional[date] = None) -> list[dict[str, Any]]:
    return period_history(session, trailing_periods(session, months, today), today=today)


def averages(session: Session, months: int = 6,
             today: Optional[date] = None) -> dict[str, Decimal]:
    rows = [row for row in trailing_history(session, months + 1, today)][:-1] or []
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


def compare_periods(session: Session, current: Period, previous: Period) -> dict[str, Any]:
    """Side-by-side of two periods with deltas — used for MoM and YoY."""
    rows = period_history(session, [previous, current])
    if len(rows) < 2:
        return {}
    before, after = rows[0], rows[1]
    keys = ("income", "expenses", "savings", "investments", "debt_payments", "net")
    deltas = {
        key: {
            "previous": before[key],
            "current": after[key],
            "change": money(D(after[key]) - D(before[key])),
            "change_pct": pct_of(money(D(after[key]) - D(before[key])), before[key]),
        }
        for key in keys
    }
    return {
        "current_label": current.label,
        "previous_label": previous.label,
        "metrics": deltas,
    }


def month_over_month(session: Session, period: Optional[Period] = None,
                     today: Optional[date] = None) -> dict[str, Any]:
    settings = settings_snapshot(session)
    period = period or settings.current_period(today or date.today())
    return compare_periods(session, period,
                           shift_period(period, -1, settings.first_day_of_month))


def year_over_year(session: Session, period: Optional[Period] = None,
                   today: Optional[date] = None) -> dict[str, Any]:
    settings = settings_snapshot(session)
    period = period or settings.current_period(today or date.today())
    return compare_periods(session, period,
                           shift_period(period, -12, settings.first_day_of_month))


# ==========================================================================
# Category analysis
# ==========================================================================
def category_totals(session: Session, period: Period, *,
                    kinds: Optional[Sequence[str]] = None,
                    roll_up: bool = True, limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Spending per category inside a period, biggest first."""
    actuals = actuals_for_period(session, period)
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


def recurring_patterns(session: Session, months: int = 6,
                       today: Optional[date] = None) -> list[dict[str, Any]]:
    """Categories you spend on nearly every month — the true fixed cost base."""
    periods = trailing_periods(session, months, today, include_current=False)
    if not periods:
        return []
    names = category_name_map(session)
    tallies: dict[int, list[Decimal]] = {}
    for period in periods:
        actuals = actuals_for_period(session, period)
        for category_id, amount in actuals.by_category.items():
            tallies.setdefault(category_id, []).append(amount)

    output: list[dict[str, Any]] = []
    for category_id, amounts in tallies.items():
        appearances = len(amounts)
        if appearances < max(2, len(periods) - 1):
            continue
        average = money(money_sum(amounts) / Decimal(appearances))
        spread = money(max(amounts) - min(amounts))
        output.append({
            "category_id": category_id,
            "label": names.get(category_id, f"#{category_id}"),
            "months_present": appearances,
            "months_analysed": len(periods),
            "average": average,
            "minimum": min(amounts),
            "maximum": max(amounts),
            "spread": spread,
            "volatility_pct": pct_of(spread, average),
        })
    output.sort(key=lambda row: row["average"], reverse=True)
    return output


def biggest_transactions(session: Session, period: Period, limit: int = 10) -> list[Transaction]:
    txns = list_transactions(session, TxnFilter(
        start=period.start, end=period.end,
        kinds=[TxnKind.EXPENSE.value],
        statuses=[TxnStatus.COMPLETED.value],
        use_effective_date=True,
    ))
    return sorted(txns, key=lambda txn: txn.amount, reverse=True)[:limit]


def unusual_expenses(session: Session, period: Period, *, months: int = 6,
                     threshold_pct: Decimal = Decimal("50"),
                     today: Optional[date] = None) -> list[dict[str, Any]]:
    """Categories markedly above their own recent average."""
    baseline = recurring_patterns(session, months, today)
    lookup = {row["category_id"]: row for row in baseline}
    actuals = actuals_for_period(session, period)
    names = category_name_map(session)
    output: list[dict[str, Any]] = []
    for category_id, amount in actuals.by_category.items():
        base = lookup.get(category_id)
        if base is None or base["average"] <= 0:
            continue
        change = pct_of(money(amount - base["average"]), base["average"])
        if change >= D(threshold_pct):
            output.append({
                "category_id": category_id,
                "label": names.get(category_id, f"#{category_id}"),
                "amount": amount,
                "average": base["average"],
                "change_pct": change,
            })
    output.sort(key=lambda row: row["change_pct"], reverse=True)
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


def dashboard(session: Session, period: Optional[Period] = None,
              today: Optional[date] = None) -> DashboardSnapshot:
    """Everything the Dashboard needs, computed once."""
    settings = settings_snapshot(session)
    today = today or date.today()
    period = period or settings.current_period(today)

    accounts = load_account_infos(session)
    txns = load_cash_txns(session)
    views = account_service.balance_views(session, as_of=today)
    account_totals = account_service.totals(views)

    flow = period_cashflow(
        period, accounts, txns,
        availability_rule=settings.income_availability_rule,
        cutoff_day=settings.income_cutoff_day,
        first_day_of_month=settings.first_day_of_month,
    )
    budget = summarise_period(session, period, today=today)
    tracking = track_period(session, period, today=today)
    debt_summary = debt_service.totals(session)
    goals = goal_service.all_progress(session, today=today)

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

    monthly_average = averages(session, 6, today)
    snapshot.emergency_months = networth_service.emergency_fund_months(
        session, monthly_average["expenses"], today
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


# ==========================================================================
# Report tables
# ==========================================================================
def budget_accuracy(session: Session, months: int = 12,
                    today: Optional[date] = None) -> list[dict[str, Any]]:
    periods = trailing_periods(session, months, today, include_current=False)
    return budget_accuracy_series(session, periods)


def annual_summary(session: Session, year: int) -> dict[str, Any]:
    settings = settings_snapshot(session)
    periods = [settings.period(year, month) for month in range(1, 13)]
    rows = period_history(session, periods)
    return {
        "year": year,
        "rows": rows,
        "income": money_sum(row["income"] for row in rows),
        "expenses": money_sum(row["expenses"] for row in rows),
        "savings": money_sum(row["savings"] for row in rows),
        "investments": money_sum(row["investments"] for row in rows),
        "debt_payments": money_sum(row["debt_payments"] for row in rows),
        "net": money_sum(row["net"] for row in rows),
    }


def available_years(session: Session) -> list[int]:
    rows = session.execute(
        select(func.min(Transaction.txn_date), func.max(Transaction.txn_date))
        .where(Transaction.deleted_at.is_(None))
    ).first()
    today = date.today()
    if not rows or rows[0] is None:
        return [today.year]
    return list(range(rows[0].year, max(rows[1].year, today.year) + 1))
