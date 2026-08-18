"""Zero-based budgeting arithmetic.

The single rule the whole module serves:

    available money  −  planned allocations  =  0

``available money`` is the cash carried into the period plus the income that
becomes *available* during it (see :mod:`calculations.cashflow` — income earned
in a period is not necessarily spendable in it). ``allocations`` are every
planned outflow: expenses, savings, investments, debt payments, goal funding.

All amounts are :class:`~decimal.Decimal`. Nothing here touches the database or
Streamlit, so every branch is unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from calculations.money import EPSILON, ZERO, D, is_zero, money, money_sum, pct_of
from constants import AllocationTarget, CategoryKind, Severity

BALANCED = "balanced"
UNDER_ALLOCATED = "under_allocated"
OVER_ALLOCATED = "over_allocated"

STATUS_LABELS = {
    BALANCED: "Balanced",
    UNDER_ALLOCATED: "Under-allocated",
    OVER_ALLOCATED: "Over-allocated",
}


@dataclass
class Allocation:
    """One planned line in a budget, decoupled from the ORM."""

    amount: Decimal
    kind: str = CategoryKind.EXPENSE.value
    target: str = AllocationTarget.EXPENSE.value
    label: str = ""
    line_id: Optional[int] = None
    category_id: Optional[int] = None
    goal_id: Optional[int] = None
    debt_id: Optional[int] = None
    rule_id: Optional[int] = None
    is_override: bool = False

    def __post_init__(self) -> None:
        self.amount = money(self.amount)

    @property
    def is_income(self) -> bool:
        return self.kind == CategoryKind.INCOME.value

    @property
    def identity(self) -> tuple:
        """What this line funds — used to spot double allocation."""
        return (self.kind, self.category_id, self.goal_id, self.debt_id,
                (self.label or "").strip().lower() or None)


@dataclass
class BudgetWarning:
    code: str
    message: str
    severity: str = Severity.WARNING.value
    detail: Optional[str] = None


@dataclass
class ZeroBasedResult:
    carry_in: Decimal
    planned_income: Decimal
    available: Decimal
    allocated: Decimal
    remaining: Decimal
    status: str
    by_kind: dict[str, Decimal] = field(default_factory=dict)
    by_target: dict[str, Decimal] = field(default_factory=dict)
    income_count: int = 0
    allocation_count: int = 0
    warnings: list[BudgetWarning] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def is_balanced(self) -> bool:
        return self.status == BALANCED

    @property
    def allocated_pct(self) -> Decimal:
        """How much of the available money has been given a job."""
        return pct_of(self.allocated, self.available)

    @property
    def unallocated(self) -> Decimal:
        """Positive when money still needs a job; zero when over-allocated."""
        return self.remaining if self.remaining > 0 else ZERO

    @property
    def overspend(self) -> Decimal:
        """Positive when the plan promises money that does not exist."""
        return -self.remaining if self.remaining < 0 else ZERO


def split_lines(lines: Iterable[Allocation]) -> tuple[list[Allocation], list[Allocation]]:
    """Separate expected income from allocations of that income."""
    income, allocations = [], []
    for line in lines:
        (income if line.is_income else allocations).append(line)
    return income, allocations


def zero_based_summary(
    lines: Sequence[Allocation],
    carry_in: Decimal | float | int | str = ZERO,
    *,
    tolerance: Decimal = EPSILON,
    detect_duplicates: bool = True,
) -> ZeroBasedResult:
    """Evaluate a period's plan against the zero-based rule."""
    income_lines, allocation_lines = split_lines(lines)

    carry = money(carry_in)
    planned_income = money_sum(line.amount for line in income_lines)
    available = money(carry + planned_income)
    allocated = money_sum(line.amount for line in allocation_lines)
    remaining = money(available - allocated)

    if is_zero(remaining, tolerance):
        status = BALANCED
        remaining = ZERO
    elif remaining > 0:
        status = UNDER_ALLOCATED
    else:
        status = OVER_ALLOCATED

    by_kind: dict[str, Decimal] = {}
    by_target: dict[str, Decimal] = {}
    for line in allocation_lines:
        by_kind[line.kind] = money(by_kind.get(line.kind, ZERO) + line.amount)
        by_target[line.target] = money(by_target.get(line.target, ZERO) + line.amount)

    result = ZeroBasedResult(
        carry_in=carry,
        planned_income=planned_income,
        available=available,
        allocated=allocated,
        remaining=remaining,
        status=status,
        by_kind=by_kind,
        by_target=by_target,
        income_count=len(income_lines),
        allocation_count=len(allocation_lines),
    )
    result.warnings = collect_warnings(result, lines, detect_duplicates=detect_duplicates)
    return result


def collect_warnings(
    result: ZeroBasedResult,
    lines: Sequence[Allocation],
    *,
    detect_duplicates: bool = True,
) -> list[BudgetWarning]:
    """Everything worth telling the user about this plan."""
    warnings: list[BudgetWarning] = []

    if result.status == OVER_ALLOCATED:
        warnings.append(BudgetWarning(
            code="over_allocated",
            severity=Severity.CRITICAL.value,
            message=f"You have allocated {result.overspend} more than is available.",
            detail="Reduce an allocation or add expected income before the period starts.",
        ))
    elif result.status == UNDER_ALLOCATED and result.available > 0:
        warnings.append(BudgetWarning(
            code="under_allocated",
            severity=Severity.WARNING.value,
            message=f"{result.unallocated} still has no job.",
            detail="Zero-based budgeting means every unit of currency is assigned — "
                   "send the rest to savings, a goal, or debt.",
        ))

    if result.available <= 0 and result.allocation_count:
        warnings.append(BudgetWarning(
            code="no_available_money",
            severity=Severity.CRITICAL.value,
            message="This period has no available money but does have allocations.",
            detail="Check expected income and the cash carried in from last period.",
        ))

    if result.income_count == 0:
        funded_by_carry_in = result.carry_in > 0
        warnings.append(BudgetWarning(
            code="no_income",
            severity=Severity.INFO.value if funded_by_carry_in else Severity.WARNING.value,
            message="No expected income recorded for this period.",
            detail=(f"The plan is funded entirely by the {result.carry_in} carried in."
                    if funded_by_carry_in else
                    "Add the income you expect, or this period has nothing to allocate."),
        ))

    if detect_duplicates:
        warnings.extend(detect_double_allocation(lines))

    negatives = [line for line in lines if line.amount < 0]
    if negatives:
        warnings.append(BudgetWarning(
            code="negative_line",
            severity=Severity.WARNING.value,
            message=f"{len(negatives)} line(s) have a negative amount.",
            detail="Budget lines should be positive; direction comes from the line type.",
        ))

    zero_lines = [line for line in lines if line.amount == 0]
    if zero_lines:
        warnings.append(BudgetWarning(
            code="zero_line",
            severity=Severity.INFO.value,
            message=f"{len(zero_lines)} line(s) are set to zero.",
            detail="Zero lines are kept so the category still shows in tracking.",
        ))

    return warnings


def detect_double_allocation(lines: Iterable[Allocation]) -> list[BudgetWarning]:
    """Catch the same money being promised twice.

    Two shapes of mistake are found:

    1. Two lines funding the identical target (same category, or same goal).
    2. A goal funded both directly *and* through a category line tagged to it.
    """
    warnings: list[BudgetWarning] = []
    seen: dict[tuple, list[Allocation]] = {}
    for line in lines:
        seen.setdefault(line.identity, []).append(line)

    for identity, group in seen.items():
        if len(group) > 1:
            label = group[0].label or "this target"
            warnings.append(BudgetWarning(
                code="duplicate_line",
                severity=Severity.CRITICAL.value,
                message=f"{len(group)} lines allocate to “{label}”.",
                detail=f"Total promised: {money_sum(g.amount for g in group)}. "
                       "Merge them so the same money is not counted twice.",
            ))

    goal_direct = {line.goal_id for line in lines
                   if line.goal_id and line.target == AllocationTarget.GOAL.value}
    goal_indirect = {line.goal_id for line in lines
                     if line.goal_id and line.target != AllocationTarget.GOAL.value}
    for goal_id in sorted(goal_direct & goal_indirect):
        warnings.append(BudgetWarning(
            code="goal_double_funded",
            severity=Severity.WARNING.value,
            message="A goal is funded by both a dedicated line and a category line.",
            detail=f"Goal #{goal_id} — confirm this is intentional, otherwise you are "
                   "double-counting the contribution.",
        ))

    debt_direct = {line.debt_id for line in lines
                   if line.debt_id and line.target == AllocationTarget.DEBT.value}
    debt_indirect = {line.debt_id for line in lines
                     if line.debt_id and line.target != AllocationTarget.DEBT.value}
    for debt_id in sorted(debt_direct & debt_indirect):
        warnings.append(BudgetWarning(
            code="debt_double_funded",
            severity=Severity.WARNING.value,
            message="A debt is funded by both a dedicated line and a category line.",
            detail=f"Debt #{debt_id} — check you are not paying it twice on paper.",
        ))

    return warnings


def balance_suggestions(result: ZeroBasedResult) -> list[str]:
    """Concrete next actions to reach zero."""
    if result.is_balanced:
        return []
    if result.status == UNDER_ALLOCATED:
        amount = result.unallocated
        return [
            f"Send {amount} to savings or an emergency fund.",
            f"Add {amount} to a financial goal.",
            f"Make an extra debt payment of {amount}.",
            f"Increase a variable category (food, leisure) by {amount}.",
        ]
    amount = result.overspend
    return [
        f"Cut {amount} from discretionary categories (leisure, shopping, delivery).",
        f"Reduce a savings or investment allocation by {amount} this period only.",
        f"Move {amount} of spending to the next period if it can wait.",
        f"Add {amount} of realistic extra income (freelance, sale of unused items).",
    ]


def apply_to_reach_zero(
    lines: Sequence[Allocation],
    result: ZeroBasedResult,
    target_identity: tuple,
) -> Decimal:
    """New amount for ``target_identity`` that would balance the budget.

    Returns the suggested amount (never below zero).
    """
    for line in lines:
        if line.identity == target_identity:
            proposed = money(line.amount + result.remaining)
            return proposed if proposed > 0 else ZERO
    return money(result.remaining) if result.remaining > 0 else ZERO


# --------------------------------------------------------------------------
# Rate metrics used by the dashboard
# --------------------------------------------------------------------------
def savings_rate(income: Decimal, saved: Decimal) -> Decimal:
    """Share of income put aside, as a percentage."""
    return pct_of(saved, income)


def budget_utilisation(planned: Decimal, actual: Decimal) -> Decimal:
    """How much of the plan has been consumed, as a percentage."""
    return pct_of(actual, planned)


def discretionary_share(by_kind: dict[str, Decimal], discretionary_total: Decimal) -> Decimal:
    total = money_sum(by_kind.values())
    return pct_of(discretionary_total, total)


def carry_forward(result: ZeroBasedResult, *, carry_enabled: bool = True) -> Decimal:
    """Cash a balanced-on-paper period hands to the next one."""
    if not carry_enabled:
        return ZERO
    return money(result.remaining)
