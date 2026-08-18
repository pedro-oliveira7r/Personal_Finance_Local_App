"""Zero-based budget planning and planned-vs-actual tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from calculations.budgeting import (
    Allocation,
    ZeroBasedResult,
    balance_suggestions,
    zero_based_summary,
)
from calculations.cashflow import (
    IncomeTiming,
    available_income_for_period,
    cash_available,
    income_timing,
)
from calculations.money import ZERO, D, money, money_sum, pct_of
from calculations.periods import Period, period_from_key, previous_period, shift_period
from calculations.variance import (
    STATUS_UNBUDGETED,
    VarianceRow,
    VarianceSummary,
    approaching_limit,
    compute_variance,
    summarise,
    top_overspending,
    top_underspending,
)
from constants import (
    ALLOCATION_KINDS,
    AllocationTarget,
    BudgetMethod,
    CategoryKind,
    PeriodStatus,
    TxnKind,
    TxnStatus,
)
from database.models import (
    BudgetLine,
    BudgetPeriod,
    Category,
    Debt,
    Goal,
    Transaction,
    utcnow,
)
from schemas.validation import BudgetLineIn
from services.common import (
    ConflictError,
    NotFoundError,
    ServiceError,
    SettingsSnapshot,
    apply_fields,
    category_name_map,
    load_account_infos,
    load_cash_txns,
    send_to_recycle_bin,
    settings_snapshot,
)
from services.recurring_service import project_period
from services.transaction_service import ActualsByCategory, actuals_for_period


# ==========================================================================
# Periods
# ==========================================================================
def get_period_row(session: Session, year: int, month: int) -> Optional[BudgetPeriod]:
    return session.execute(
        select(BudgetPeriod).where(BudgetPeriod.year == year, BudgetPeriod.month == month)
    ).scalars().first()


def get_or_create_period(session: Session, year: int, month: int,
                         *, method: Optional[str] = None) -> BudgetPeriod:
    row = get_period_row(session, year, month)
    if row is not None:
        return row
    settings = settings_snapshot(session)
    row = BudgetPeriod(
        year=year, month=month,
        status=PeriodStatus.DRAFT.value,
        method=method or settings.budget_method,
    )
    session.add(row)
    session.flush()
    return row


def list_period_rows(session: Session, *, descending: bool = True) -> list[BudgetPeriod]:
    order = (BudgetPeriod.year.desc(), BudgetPeriod.month.desc()) if descending else (
        BudgetPeriod.year.asc(), BudgetPeriod.month.asc())
    return list(session.execute(select(BudgetPeriod).order_by(*order)).scalars())


def period_keys_with_budget(session: Session) -> list[str]:
    return [row.key for row in list_period_rows(session, descending=False)]


def set_period_status(session: Session, year: int, month: int, status: str) -> BudgetPeriod:
    row = get_or_create_period(session, year, month)
    if status not in PeriodStatus.values():
        raise ServiceError(f"Unknown period status “{status}”.")
    row.status = status
    row.closed_at = utcnow() if status == PeriodStatus.CLOSED.value else None
    session.flush()
    return row


def set_opening_override(session: Session, year: int, month: int,
                         value: Optional[Decimal]) -> BudgetPeriod:
    row = get_or_create_period(session, year, month)
    row.opening_cash_override = None if value is None else money(value)
    session.flush()
    return row


def delete_period(session: Session, year: int, month: int) -> None:
    row = get_period_row(session, year, month)
    if row is None:
        raise NotFoundError(f"No budget exists for {year}-{month:02d}.")
    send_to_recycle_bin(session, "budget_period", row, label=row.key)
    session.delete(row)
    session.flush()


# ==========================================================================
# Lines
# ==========================================================================
def lines_for_period(session: Session, period_row: BudgetPeriod) -> list[BudgetLine]:
    return list(session.execute(
        select(BudgetLine)
        .where(BudgetLine.period_id == period_row.id)
        .order_by(BudgetLine.kind, BudgetLine.id)
    ).scalars())


def _find_matching_line(session: Session, period_id: int, data: BudgetLineIn) -> Optional[BudgetLine]:
    stmt = select(BudgetLine).where(
        BudgetLine.period_id == period_id,
        BudgetLine.kind == data.kind,
    )
    if data.category_id is not None:
        stmt = stmt.where(BudgetLine.category_id == data.category_id)
    else:
        stmt = stmt.where(BudgetLine.category_id.is_(None))
    if data.goal_id is not None:
        stmt = stmt.where(BudgetLine.goal_id == data.goal_id)
    else:
        stmt = stmt.where(BudgetLine.goal_id.is_(None))
    if data.debt_id is not None:
        stmt = stmt.where(BudgetLine.debt_id == data.debt_id)
    else:
        stmt = stmt.where(BudgetLine.debt_id.is_(None))
    if data.category_id is None and data.goal_id is None and data.debt_id is None:
        stmt = stmt.where(func.lower(func.coalesce(BudgetLine.label, "")) ==
                          (data.label or "").strip().lower())
    return session.execute(stmt).scalars().first()


def upsert_line(session: Session, year: int, month: int, payload: dict[str, Any],
                *, mark_override: bool = True) -> BudgetLine:
    """Create or update the single line that funds this target.

    The uniqueness check happens here (not only in the schema) because SQLite
    treats NULLs as distinct in unique indexes — this is what actually stops
    the same category being allocated twice in one period.
    """
    period_row = get_or_create_period(session, year, month)
    if period_row.status == PeriodStatus.CLOSED.value:
        raise ConflictError(
            f"{period_row.key} is closed. Reopen it before changing the plan."
        )
    data = BudgetLineIn(**payload)
    if data.category_id:
        category = session.get(Category, data.category_id)
        if category is None:
            raise NotFoundError("That category no longer exists.")
        data.kind = category.kind
        if category.kind == CategoryKind.SAVINGS.value:
            data.target = AllocationTarget.SAVINGS.value
        elif category.kind == CategoryKind.INVESTMENT.value:
            data.target = AllocationTarget.INVESTMENT.value
        elif category.kind == CategoryKind.DEBT.value:
            data.target = AllocationTarget.DEBT.value
    if data.goal_id and not session.get(Goal, data.goal_id):
        raise NotFoundError("That goal no longer exists.")
    if data.debt_id and not session.get(Debt, data.debt_id):
        raise NotFoundError("That debt no longer exists.")

    existing = _find_matching_line(session, period_row.id, data)
    if existing is not None:
        if existing.is_locked and mark_override:
            raise ConflictError(f"“{existing.display_label}” is locked in {period_row.key}.")
        apply_fields(existing, data.model_dump(), skip={"is_override"})
        if mark_override:
            existing.is_override = True
        session.flush()
        return existing

    line = BudgetLine(period_id=period_row.id, **data.model_dump())
    if mark_override:
        line.is_override = True
    session.add(line)
    session.flush()
    return line


def delete_line(session: Session, line_id: int) -> None:
    line = session.get(BudgetLine, line_id)
    if line is None:
        raise NotFoundError("That budget line no longer exists.")
    send_to_recycle_bin(session, "budget_line", line, label=line.display_label)
    session.delete(line)
    session.flush()


def set_line_lock(session: Session, line_id: int, locked: bool) -> BudgetLine:
    line = session.get(BudgetLine, line_id)
    if line is None:
        raise NotFoundError("That budget line no longer exists.")
    line.is_locked = locked
    session.flush()
    return line


def clear_period_lines(session: Session, year: int, month: int,
                       *, keep_overrides: bool = True) -> int:
    period_row = get_or_create_period(session, year, month)
    removed = 0
    for line in lines_for_period(session, period_row):
        if keep_overrides and (line.is_override or line.is_locked):
            continue
        session.delete(line)
        removed += 1
    session.flush()
    return removed


# ==========================================================================
# Carry-in / available money
# ==========================================================================
def projected_cash_at(session: Session, on_date: date,
                      *, today: Optional[date] = None) -> Decimal:
    """Cash on a date: real history up to today, planned movements after it."""
    today = today or date.today()
    accounts = load_account_infos(session)
    txns = load_cash_txns(session)
    if on_date <= today:
        return cash_available(accounts, txns, on_date)

    base = cash_available(accounts, txns, today)
    cash_ids = {info.id for info in accounts if info.is_cash_like}
    delta = ZERO
    for txn in txns:
        if txn.status != TxnStatus.PLANNED.value:
            continue
        when = txn.effective_date
        if not (today < when <= on_date):
            continue
        if txn.kind == TxnKind.INCOME.value and txn.account_id in cash_ids:
            delta += txn.amount
        elif txn.kind == TxnKind.EXPENSE.value and txn.account_id in cash_ids:
            delta -= txn.amount
        elif txn.kind == TxnKind.TRANSFER.value:
            from_cash = txn.account_id in cash_ids
            to_cash = txn.to_account_id in cash_ids
            if from_cash and not to_cash:
                delta -= txn.amount
            elif to_cash and not from_cash:
                delta += txn.amount
    return money(base + delta)


def carry_in_for(session: Session, period: Period, *,
                 today: Optional[date] = None) -> Decimal:
    """Money genuinely free to budget when the period opens.

    Raw cash minus anything already earmarked for a goal that lives in a cash
    account — otherwise the emergency fund would look spendable every month and
    get allocated again and again.
    """
    row = get_period_row(session, period.year, period.month)
    if row is not None and row.opening_cash_override is not None:
        return money(row.opening_cash_override)
    settings = settings_snapshot(session)
    if not settings.carry_over_surplus:
        return ZERO
    from services.goal_service import earmarked_in_cash

    cash = projected_cash_at(session, period.start - timedelta(days=1), today=today)
    return money(cash - earmarked_in_cash(session, today=today))


# ==========================================================================
# Summary
# ==========================================================================
@dataclass
class BudgetSummary:
    period: Period
    row: Optional[BudgetPeriod]
    result: ZeroBasedResult
    income_lines: list[BudgetLine] = field(default_factory=list)
    allocation_lines: list[BudgetLine] = field(default_factory=list)
    timing: Optional[IncomeTiming] = None
    suggestions: list[str] = field(default_factory=list)
    label_map: dict[int, str] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return self.result.status

    @property
    def has_plan(self) -> bool:
        return bool(self.income_lines or self.allocation_lines)

    @property
    def line_count(self) -> int:
        return len(self.income_lines) + len(self.allocation_lines)


def _line_label(line: BudgetLine, names: dict[int, str],
                goals: dict[int, str], debts: dict[int, str]) -> str:
    if line.label:
        return line.label
    if line.category_id and line.category_id in names:
        return names[line.category_id]
    if line.goal_id and line.goal_id in goals:
        return f"Goal · {goals[line.goal_id]}"
    if line.debt_id and line.debt_id in debts:
        return f"Debt · {debts[line.debt_id]}"
    return "(unnamed line)"


def _allocations_from_lines(session: Session, lines: Sequence[BudgetLine]) -> list[Allocation]:
    names = category_name_map(session)
    goals = {row[0]: row[1] for row in session.execute(select(Goal.id, Goal.name)).all()}
    debts = {row[0]: row[1] for row in session.execute(select(Debt.id, Debt.name)).all()}
    return [
        Allocation(
            amount=line.planned_amount,
            kind=line.kind,
            target=line.target,
            label=_line_label(line, names, goals, debts),
            line_id=line.id,
            category_id=line.category_id,
            goal_id=line.goal_id,
            debt_id=line.debt_id,
            rule_id=line.rule_id,
            is_override=line.is_override,
        )
        for line in lines
    ]


def summarise_period(session: Session, period: Period,
                     *, today: Optional[date] = None) -> BudgetSummary:
    """Everything the Budget Planning screen needs for one period."""
    settings = settings_snapshot(session)
    row = get_period_row(session, period.year, period.month)
    lines = lines_for_period(session, row) if row is not None else []
    allocations = _allocations_from_lines(session, lines)
    carry = carry_in_for(session, period, today=today)
    result = zero_based_summary(allocations, carry)

    txns = load_cash_txns(session)
    timing = income_timing(
        txns, period, settings.income_availability_rule,
        cutoff_day=settings.income_cutoff_day,
        first_day_of_month=settings.first_day_of_month,
    )

    result.warnings.extend(_card_payment_warnings(session, period, lines))

    names = category_name_map(session)
    return BudgetSummary(
        period=period,
        row=row,
        result=result,
        income_lines=[line for line in lines if line.kind == CategoryKind.INCOME.value],
        allocation_lines=[line for line in lines if line.kind != CategoryKind.INCOME.value],
        timing=timing,
        suggestions=balance_suggestions(result),
        label_map=names,
    )


def _card_payment_warnings(session: Session, period: Period,
                          lines: Sequence[BudgetLine]) -> list:
    """Catch the classic credit-card double allocation.

    Budgeting groceries charged to a card *and* a "card payment" line allocates
    the same money twice: the payment is only moving money that the grocery
    line already claimed.
    """
    from calculations.budgeting import BudgetWarning
    from constants import AccountType, Severity
    from database.models import Account

    debt_line_ids = {line.debt_id for line in lines if line.debt_id}
    if not debt_line_ids:
        return []

    card_debts = session.execute(
        select(Debt, Account)
        .join(Account, Debt.account_id == Account.id)
        .where(Debt.id.in_(debt_line_ids),
               Account.type == AccountType.CREDIT_CARD.value)
    ).all()
    if not card_debts:
        return []

    warnings = []
    for debt, account in card_debts:
        charged = session.execute(
            select(func.count(Transaction.id)).where(
                Transaction.account_id == account.id,
                Transaction.kind == TxnKind.EXPENSE.value,
                Transaction.deleted_at.is_(None),
                Transaction.txn_date >= period.start,
                Transaction.txn_date <= period.end,
            )
        ).scalar() or 0
        if charged:
            warnings.append(BudgetWarning(
                code="card_payment_double_allocation",
                severity=Severity.WARNING.value,
                message=f"“{debt.name}” has a payment allocation and "
                        f"{charged} expense(s) charged to the same card.",
                detail="The card payment only moves money the category lines already "
                       "claimed. Turn off “include in budget” on this debt to avoid "
                       "allocating the same money twice.",
            ))
    return warnings


# ==========================================================================
# Generation
# ==========================================================================
@dataclass
class PlanGenerationReport:
    period_key: str = ""
    created: int = 0
    updated: int = 0
    skipped_overrides: int = 0
    source: str = "rules"

    def summary(self) -> str:
        parts = [f"{self.created} line(s) added", f"{self.updated} updated"]
        if self.skipped_overrides:
            parts.append(f"{self.skipped_overrides} manual override(s) preserved")
        return ", ".join(parts)


def generate_from_rules(
    session: Session,
    period: Period,
    *,
    overwrite_overrides: bool = False,
    include_goals: bool = True,
    include_debts: bool = True,
) -> PlanGenerationReport:
    """Build a period's plan from recurring rules, goals and debts.

    Lines the user has edited by hand (``is_override``) are left untouched
    unless ``overwrite_overrides`` is set, so regenerating never silently
    discards a deliberate decision.
    """
    period_row = get_or_create_period(session, period.year, period.month)
    if period_row.status == PeriodStatus.CLOSED.value:
        raise ConflictError(f"{period_row.key} is closed — reopen it to regenerate the plan.")

    report = PlanGenerationReport(period_key=period.key, source="rules")
    existing = {}
    for line in lines_for_period(session, period_row):
        key = (line.kind, line.category_id, line.goal_id, line.debt_id,
               (line.label or "").strip().lower() or None)
        existing[key] = line

    projection = project_period(session, period)
    kinds = {row[0]: row[1] for row in session.execute(select(Category.id, Category.kind)).all()}

    def put(kind: str, target: str, amount: Decimal, *,
            category_id: Optional[int] = None, goal_id: Optional[int] = None,
            debt_id: Optional[int] = None, label: Optional[str] = None,
            rule_id: Optional[int] = None) -> None:
        key = (kind, category_id, goal_id, debt_id, (label or "").strip().lower() or None)
        line = existing.get(key)
        if line is not None:
            if (line.is_override or line.is_locked) and not overwrite_overrides:
                report.skipped_overrides += 1
                return
            if line.planned_amount != money(amount):
                line.planned_amount = money(amount)
                report.updated += 1
            line.rule_id = rule_id or line.rule_id
            return
        session.add(BudgetLine(
            period_id=period_row.id, kind=kind, target=target,
            planned_amount=money(amount), category_id=category_id,
            goal_id=goal_id, debt_id=debt_id, label=label, rule_id=rule_id,
            is_override=False,
        ))
        report.created += 1

    for category_id, amount in projection.per_category.items():
        kind = kinds.get(category_id, CategoryKind.EXPENSE.value)
        target = {
            CategoryKind.SAVINGS.value: AllocationTarget.SAVINGS.value,
            CategoryKind.INVESTMENT.value: AllocationTarget.INVESTMENT.value,
            CategoryKind.DEBT.value: AllocationTarget.DEBT.value,
        }.get(kind, AllocationTarget.EXPENSE.value)
        put(kind, target, amount, category_id=category_id)

    if include_goals:
        goals = session.execute(
            select(Goal).where(Goal.status == "active", Goal.planned_monthly > 0)
        ).scalars().all()
        for goal in goals:
            put(CategoryKind.SAVINGS.value, AllocationTarget.GOAL.value,
                goal.planned_monthly, goal_id=goal.id, label=f"Goal · {goal.name}")

    if include_debts:
        debts = session.execute(
            select(Debt).where(
                Debt.is_active.is_(True), Debt.include_in_budget.is_(True)
            )
        ).scalars().all()
        for debt in debts:
            payment = debt.planned_payment or debt.minimum_payment
            if payment <= 0:
                continue
            total = money(payment + (debt.extra_payment or ZERO))
            put(CategoryKind.DEBT.value, AllocationTarget.DEBT.value,
                total, debt_id=debt.id, label=f"Debt · {debt.name}")

    period_row.generated_from_rules_at = utcnow()
    session.flush()
    return report


def copy_period(
    session: Session,
    source: Period,
    target: Period,
    *,
    growth_pct: Decimal = ZERO,
    overwrite_overrides: bool = False,
    include_income: bool = True,
) -> PlanGenerationReport:
    """Duplicate a plan into another period, optionally inflating amounts."""
    source_row = get_period_row(session, source.year, source.month)
    if source_row is None:
        raise NotFoundError(f"There is no budget for {source.label} to copy.")
    target_row = get_or_create_period(session, target.year, target.month)
    if target_row.status == PeriodStatus.CLOSED.value:
        raise ConflictError(f"{target_row.key} is closed — reopen it first.")

    report = PlanGenerationReport(period_key=target.key, source=f"copy:{source.key}")
    factor = Decimal(1) + D(growth_pct) / Decimal(100)
    existing = {
        (line.kind, line.category_id, line.goal_id, line.debt_id,
         (line.label or "").strip().lower() or None): line
        for line in lines_for_period(session, target_row)
    }

    for line in lines_for_period(session, source_row):
        if not include_income and line.kind == CategoryKind.INCOME.value:
            continue
        key = (line.kind, line.category_id, line.goal_id, line.debt_id,
               (line.label or "").strip().lower() or None)
        amount = money(line.planned_amount * factor)
        current = existing.get(key)
        if current is not None:
            if (current.is_override or current.is_locked) and not overwrite_overrides:
                report.skipped_overrides += 1
                continue
            if current.planned_amount != amount:
                current.planned_amount = amount
                report.updated += 1
            continue
        session.add(BudgetLine(
            period_id=target_row.id, kind=line.kind, target=line.target,
            planned_amount=amount, category_id=line.category_id,
            goal_id=line.goal_id, debt_id=line.debt_id, label=line.label,
            expected_day=line.expected_day, rule_id=line.rule_id,
            notes=line.notes, is_override=False,
        ))
        report.created += 1
    session.flush()
    return report


def generate_range(
    session: Session,
    start: Period,
    count: int,
    *,
    source: str = "rules",
    template_period: Optional[Period] = None,
    growth_pct: Decimal = ZERO,
    overwrite_overrides: bool = False,
) -> list[PlanGenerationReport]:
    """Plan several consecutive periods in one go.

    ``source="rules"`` derives each period from the recurrence engine (so
    annual, quarterly and seasonal items land in the right months);
    ``source="copy"`` repeats a template period with optional yearly growth.
    """
    settings = settings_snapshot(session)
    reports: list[PlanGenerationReport] = []
    count = max(1, min(int(count), 60))
    for offset in range(count):
        period = shift_period(start, offset, settings.first_day_of_month)
        if source == "copy":
            template = template_period or previous_period(start, settings.first_day_of_month)
            #: Apply growth once per completed year of distance.
            years = offset // 12
            factor = D(growth_pct) * Decimal(years)
            reports.append(copy_period(
                session, template, period,
                growth_pct=factor, overwrite_overrides=overwrite_overrides,
            ))
        else:
            reports.append(generate_from_rules(
                session, period, overwrite_overrides=overwrite_overrides
            ))
    return reports


# ==========================================================================
# Tracking (planned vs actual)
# ==========================================================================
@dataclass
class TrackingResult:
    period: Period
    rows: list[VarianceRow] = field(default_factory=list)
    income: VarianceSummary = field(default_factory=VarianceSummary)
    expenses: VarianceSummary = field(default_factory=VarianceSummary)
    savings: VarianceSummary = field(default_factory=VarianceSummary)
    investments: VarianceSummary = field(default_factory=VarianceSummary)
    debt: VarianceSummary = field(default_factory=VarianceSummary)
    unbudgeted: list[VarianceRow] = field(default_factory=list)
    uncategorised_total: Decimal = ZERO
    elapsed_fraction: float = 0.0

    @property
    def allocation_rows(self) -> list[VarianceRow]:
        return [row for row in self.rows if not row.is_income]

    @property
    def overspending(self) -> list[VarianceRow]:
        return top_overspending(self.allocation_rows)

    @property
    def underspending(self) -> list[VarianceRow]:
        return top_underspending(self.allocation_rows)

    @property
    def net_planned(self) -> Decimal:
        outflow = money(self.expenses.planned + self.savings.planned
                        + self.investments.planned + self.debt.planned)
        return money(self.income.planned - outflow)

    @property
    def net_actual(self) -> Decimal:
        outflow = money(self.expenses.actual + self.savings.actual
                        + self.investments.actual + self.debt.actual)
        return money(self.income.actual - outflow)


def track_period(
    session: Session,
    period: Period,
    *,
    today: Optional[date] = None,
    include_planned_actuals: bool = False,
) -> TrackingResult:
    """Compare the plan with what actually happened, category by category."""
    settings = settings_snapshot(session)
    today = today or date.today()
    row = get_period_row(session, period.year, period.month)
    lines = lines_for_period(session, row) if row is not None else []
    actuals = actuals_for_period(session, period, include_planned=include_planned_actuals)

    names = category_name_map(session)
    goals = {r[0]: r[1] for r in session.execute(select(Goal.id, Goal.name)).all()}
    debts = {r[0]: r[1] for r in session.execute(select(Debt.id, Debt.name)).all()}
    kinds = {r[0]: r[1] for r in session.execute(select(Category.id, Category.kind)).all()}

    result = TrackingResult(period=period, elapsed_fraction=period.elapsed_fraction(today))
    used_categories: set[int] = set()

    for line in lines:
        if line.category_id is not None:
            actual = actuals.by_category.get(line.category_id, ZERO)
            used_categories.add(line.category_id)
        elif line.goal_id is not None:
            actual = actuals.goal_contributions.get(line.goal_id, ZERO)
        elif line.debt_id is not None:
            actual = actuals.debt_payments.get(line.debt_id, ZERO)
        else:
            actual = ZERO
        result.rows.append(compute_variance(
            line.planned_amount, actual, line.kind,
            label=_line_label(line, names, goals, debts),
            key=f"line:{line.id}",
            category_id=line.category_id,
            period_key=period.key,
            warning_pct=settings.warning_threshold_pct,
            critical_pct=settings.critical_threshold_pct,
            tolerance_pct=settings.variance_tolerance_pct,
        ))

    # Money that moved in categories nobody budgeted for.
    for category_id, amount in actuals.by_category.items():
        if category_id in used_categories or amount == 0:
            continue
        kind = kinds.get(category_id, CategoryKind.EXPENSE.value)
        row_variance = compute_variance(
            ZERO, amount, kind,
            label=names.get(category_id, f"Category #{category_id}"),
            key=f"cat:{category_id}",
            category_id=category_id,
            period_key=period.key,
            warning_pct=settings.warning_threshold_pct,
            critical_pct=settings.critical_threshold_pct,
            tolerance_pct=settings.variance_tolerance_pct,
        )
        result.rows.append(row_variance)
        result.unbudgeted.append(row_variance)

    result.uncategorised_total = actuals.uncategorised
    result.income = summarise([r for r in result.rows if r.kind == CategoryKind.INCOME.value])
    result.expenses = summarise([r for r in result.rows if r.kind == CategoryKind.EXPENSE.value])
    result.savings = summarise([r for r in result.rows if r.kind == CategoryKind.SAVINGS.value])
    result.investments = summarise(
        [r for r in result.rows if r.kind == CategoryKind.INVESTMENT.value])
    result.debt = summarise([r for r in result.rows if r.kind == CategoryKind.DEBT.value])
    return result


def approaching_limits(session: Session, period: Period) -> list[VarianceRow]:
    settings = settings_snapshot(session)
    tracking = track_period(session, period)
    return approaching_limit(
        tracking.allocation_rows,
        settings.warning_threshold_pct,
        settings.critical_threshold_pct,
    )


def budget_accuracy_series(session: Session, periods: Sequence[Period]) -> list[dict[str, Any]]:
    """Accuracy of the plan for each closed period — was the budget realistic?"""
    output: list[dict[str, Any]] = []
    for period in periods:
        tracking = track_period(session, period)
        allocations = summarise(tracking.allocation_rows)
        output.append({
            "period": period.key,
            "label": period.short_label,
            "income_accuracy": tracking.income.accuracy_pct,
            "expense_accuracy": allocations.accuracy_pct,
            "planned_out": allocations.planned,
            "actual_out": allocations.actual,
            "planned_in": tracking.income.planned,
            "actual_in": tracking.income.actual,
        })
    return output
