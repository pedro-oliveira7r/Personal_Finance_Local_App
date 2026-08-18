"""Debt amortisation, payoff projection and accelerated-repayment scenarios.

Interest is compounded monthly at ``annual_rate / 12`` — the convention used
by credit-card and consumer-loan statements. Every row of a schedule is
computed with exact decimals and the final payment is trimmed so the balance
lands on exactly zero rather than a fraction of a cent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from calculations.money import ZERO, D, money, money_sum, pct_of
from calculations.periods import shift_date_months
from constants import PayoffStrategy, Severity

#: Never build a schedule longer than this (50 years).
MAX_SCHEDULE_MONTHS = 600

#: Consecutive months of no progress before a multi-debt simulation gives up.
#: Without this, a payment below the monthly interest compounds for the full 600
#: months and produces a number too large for decimal arithmetic to quantise.
STALL_LIMIT = 3


@dataclass
class AmortRow:
    month_index: int
    due_date: date
    opening_balance: Decimal
    payment: Decimal
    interest: Decimal
    principal: Decimal
    closing_balance: Decimal
    cumulative_interest: Decimal
    cumulative_paid: Decimal


@dataclass
class PayoffResult:
    debt_name: str = ""
    schedule: list[AmortRow] = field(default_factory=list)
    months: int = 0
    payoff_date: Optional[date] = None
    total_interest: Decimal = ZERO
    total_paid: Decimal = ZERO
    original_balance: Decimal = ZERO
    #: True when the payment does not even cover the monthly interest.
    never_pays_off: bool = False
    monthly_interest_at_start: Decimal = ZERO

    @property
    def interest_share_pct(self) -> Decimal:
        return pct_of(self.total_interest, self.total_paid)


def monthly_rate(annual_rate_pct: Decimal | float | int | str) -> Decimal:
    return D(annual_rate_pct) / Decimal(1200)


def interest_for_month(balance: Decimal, annual_rate_pct: Decimal) -> Decimal:
    return money(D(balance) * monthly_rate(annual_rate_pct))


def minimum_viable_payment(balance: Decimal, annual_rate_pct: Decimal) -> Decimal:
    """Anything at or below this only services interest — the debt never ends."""
    return interest_for_month(balance, annual_rate_pct)


def amortisation_schedule(
    balance: Decimal,
    annual_rate_pct: Decimal,
    payment: Decimal,
    *,
    extra_payment: Decimal = ZERO,
    start_date: Optional[date] = None,
    due_day: Optional[int] = None,
    max_months: int = MAX_SCHEDULE_MONTHS,
    debt_name: str = "",
) -> PayoffResult:
    """Month-by-month payoff schedule.

    Returns a :class:`PayoffResult`; when ``payment + extra`` cannot beat the
    monthly interest, ``never_pays_off`` is set and the schedule is empty
    rather than 600 useless rows.
    """
    start_date = start_date or date.today()
    remaining = money(balance)
    total_payment = money(D(payment) + D(extra_payment))
    result = PayoffResult(
        debt_name=debt_name,
        original_balance=remaining,
        monthly_interest_at_start=interest_for_month(remaining, annual_rate_pct),
    )

    if remaining <= 0:
        result.payoff_date = start_date
        return result

    if total_payment <= 0 or (
        D(annual_rate_pct) > 0 and total_payment <= result.monthly_interest_at_start
    ):
        result.never_pays_off = True
        return result

    cumulative_interest = ZERO
    cumulative_paid = ZERO
    month = 0
    while remaining > 0 and month < max_months:
        month += 1
        opening = remaining
        interest = interest_for_month(opening, annual_rate_pct)
        due = money(opening + interest)
        applied = min(total_payment, due)
        principal = money(applied - interest)
        if principal <= 0:  # safety net; the guard above should prevent this
            result.never_pays_off = True
            return result
        remaining = money(opening - principal)
        if remaining < Decimal("0.01"):
            remaining = ZERO
        cumulative_interest = money(cumulative_interest + interest)
        cumulative_paid = money(cumulative_paid + applied)
        result.schedule.append(AmortRow(
            month_index=month,
            due_date=shift_date_months(start_date, month - 1, day=due_day),
            opening_balance=opening,
            payment=applied,
            interest=interest,
            principal=principal,
            closing_balance=remaining,
            cumulative_interest=cumulative_interest,
            cumulative_paid=cumulative_paid,
        ))

    result.months = len(result.schedule)
    result.total_interest = cumulative_interest
    result.total_paid = cumulative_paid
    result.payoff_date = result.schedule[-1].due_date if result.schedule else None
    if remaining > 0:
        result.never_pays_off = True
    return result


@dataclass
class DebtInput:
    """ORM-free description of one debt."""

    name: str
    balance: Decimal
    annual_rate_pct: Decimal = ZERO
    minimum_payment: Decimal = ZERO
    planned_payment: Decimal = ZERO
    extra_payment: Decimal = ZERO
    due_day: Optional[int] = None
    debt_id: Optional[int] = None

    def __post_init__(self) -> None:
        self.balance = money(self.balance)
        self.minimum_payment = money(self.minimum_payment)
        self.planned_payment = money(self.planned_payment)
        self.extra_payment = money(self.extra_payment)

    @property
    def effective_payment(self) -> Decimal:
        base = self.planned_payment if self.planned_payment > 0 else self.minimum_payment
        return money(base + self.extra_payment)

    def baseline_payment(self, minimums_only: bool = False) -> Decimal:
        """What this debt receives each month before any shared extra.

        For the *minimums only* baseline that is literally the minimum (falling
        back to the planned amount when no minimum was ever recorded). For every
        other strategy it is what you have actually committed to — planned plus
        any per-debt extra — never less than the minimum. Simulating your real
        commitment as if it were the bare minimum would make the comparison
        wrong for anyone who pays more than the statement demands.
        """
        if minimums_only:
            return money(self.minimum_payment or self.planned_payment)
        return money(max(self.effective_payment, self.minimum_payment))


def debt_input_from_orm(debt) -> DebtInput:
    return DebtInput(
        debt_id=debt.id,
        name=debt.name,
        balance=debt.principal_balance,
        annual_rate_pct=debt.interest_rate,
        minimum_payment=debt.minimum_payment,
        planned_payment=debt.planned_payment,
        extra_payment=debt.extra_payment,
        due_day=debt.due_day,
    )


def project_debt(debt: DebtInput, *, start_date: Optional[date] = None,
                 extra_override: Optional[Decimal] = None) -> PayoffResult:
    extra = debt.extra_payment if extra_override is None else money(extra_override)
    base = debt.planned_payment if debt.planned_payment > 0 else debt.minimum_payment
    return amortisation_schedule(
        debt.balance, debt.annual_rate_pct, base,
        extra_payment=extra, start_date=start_date,
        due_day=debt.due_day, debt_name=debt.name,
    )


def compare_extra_payment(
    debt: DebtInput,
    extra: Decimal,
    *,
    start_date: Optional[date] = None,
) -> dict[str, object]:
    """"What if I add X per month?" — the headline numbers."""
    base = project_debt(debt, start_date=start_date, extra_override=ZERO)
    boosted = project_debt(debt, start_date=start_date, extra_override=extra)
    months_saved = None
    if not base.never_pays_off and not boosted.never_pays_off:
        months_saved = base.months - boosted.months
    return {
        "base": base,
        "boosted": boosted,
        "extra": money(extra),
        "months_saved": months_saved,
        "interest_saved": money(base.total_interest - boosted.total_interest)
        if not base.never_pays_off and not boosted.never_pays_off else None,
    }


# --------------------------------------------------------------------------
# Multi-debt strategies
# --------------------------------------------------------------------------
def order_debts(debts: Sequence[DebtInput], strategy: str) -> list[DebtInput]:
    if strategy == PayoffStrategy.AVALANCHE.value:
        return sorted(debts, key=lambda d: (-D(d.annual_rate_pct), d.balance))
    if strategy == PayoffStrategy.SNOWBALL.value:
        return sorted(debts, key=lambda d: (d.balance, -D(d.annual_rate_pct)))
    return list(debts)


@dataclass
class StrategyResult:
    strategy: str
    months: int = 0
    total_interest: Decimal = ZERO
    total_paid: Decimal = ZERO
    payoff_order: list[str] = field(default_factory=list)
    per_debt_months: dict[str, int] = field(default_factory=dict)
    never_pays_off: bool = False
    monthly_outlay: Decimal = ZERO
    #: Debts whose payment does not even cover their own monthly interest —
    #: the reason a strategy stalls, named so the UI can be specific.
    stuck: list[str] = field(default_factory=list)


def simulate_strategy(
    debts: Sequence[DebtInput],
    strategy: str = PayoffStrategy.AVALANCHE.value,
    *,
    extra_pool: Decimal = ZERO,
    start_date: Optional[date] = None,
    max_months: int = MAX_SCHEDULE_MONTHS,
) -> StrategyResult:
    """Roll a shared extra payment down an ordered stack of debts.

    Each debt receives its minimum every month; the extra pool (plus the freed
    minimum of every debt already cleared) attacks the debt at the front of the
    queue. That is the snowball/avalanche mechanic. The *minimums only* baseline
    deliberately does neither — it shows what changing nothing costs.

    **Stall detection.** A debt whose payment does not cover its own monthly
    interest grows without limit. Iterating that for 600 months produces numbers
    with hundreds of digits, which overflows decimal arithmetic outright. So the
    simulation stops as soon as the total balance has failed to fall for
    :data:`STALL_LIMIT` consecutive months with nothing cleared: from that point
    the outcome can only get worse, and the honest answer is "this never pays
    off", named debt by debt in :attr:`StrategyResult.stuck`.
    """
    start_date = start_date or date.today()
    working = [
        DebtInput(
            name=d.name, balance=d.balance, annual_rate_pct=d.annual_rate_pct,
            minimum_payment=d.minimum_payment, planned_payment=d.planned_payment,
            extra_payment=d.extra_payment, due_day=d.due_day, debt_id=d.debt_id,
        )
        for d in debts if d.balance > 0
    ]
    result = StrategyResult(strategy=strategy)
    if not working:
        return result

    minimums_only = strategy == PayoffStrategy.MINIMUM_ONLY.value
    queue = order_debts(working, strategy)
    baseline = {d.name: d.baseline_payment(minimums_only) for d in queue}
    result.monthly_outlay = money(
        money_sum(baseline.values()) + (ZERO if minimums_only else D(extra_pool))
    )

    total_interest = ZERO
    total_paid = ZERO
    month = 0
    stalled = 0
    previous_total = money_sum(d.balance for d in queue)

    while any(d.balance > 0 for d in queue) and month < max_months:
        month += 1
        cleared_this_month = False

        # Minimums-only is the do-nothing baseline: no extra, no rolling.
        pool = ZERO if minimums_only else money(extra_pool)
        if not minimums_only:
            for debt in queue:
                if debt.balance <= 0:
                    pool = money(pool + baseline[debt.name])

        # Charge interest and pay minimums.
        for debt in queue:
            if debt.balance <= 0:
                continue
            interest = interest_for_month(debt.balance, debt.annual_rate_pct)
            debt.balance = money(debt.balance + interest)
            total_interest = money(total_interest + interest)
            applied = min(debt.balance, baseline[debt.name])
            debt.balance = money(debt.balance - applied)
            total_paid = money(total_paid + applied)
            if debt.balance <= 0 and debt.name not in result.payoff_order:
                result.payoff_order.append(debt.name)
                result.per_debt_months[debt.name] = month
                cleared_this_month = True

        # Throw the pool at the front of the queue.
        for debt in queue:
            if pool <= 0:
                break
            if debt.balance <= 0:
                continue
            applied = min(debt.balance, pool)
            debt.balance = money(debt.balance - applied)
            pool = money(pool - applied)
            total_paid = money(total_paid + applied)
            if debt.balance <= 0 and debt.name not in result.payoff_order:
                result.payoff_order.append(debt.name)
                result.per_debt_months[debt.name] = month
                cleared_this_month = True

        current_total = money_sum(d.balance for d in queue)
        if current_total >= previous_total and not cleared_this_month:
            stalled += 1
            if stalled >= STALL_LIMIT:
                break
        else:
            stalled = 0
        previous_total = current_total

    result.months = month
    result.total_interest = total_interest
    result.total_paid = total_paid
    result.never_pays_off = any(d.balance > 0 for d in queue)
    result.stuck = [
        debt.name for debt in queue
        if debt.balance > 0
        and baseline[debt.name] <= minimum_viable_payment(debt.balance,
                                                          debt.annual_rate_pct)
    ]
    return result


def strategy_comparison(
    debts: Sequence[DebtInput],
    *,
    extra_pool: Decimal = ZERO,
    start_date: Optional[date] = None,
) -> dict[str, StrategyResult]:
    return {
        strategy: simulate_strategy(
            debts, strategy, extra_pool=extra_pool, start_date=start_date
        )
        for strategy in (
            PayoffStrategy.AVALANCHE.value,
            PayoffStrategy.SNOWBALL.value,
            PayoffStrategy.MINIMUM_ONLY.value,
        )
    }


def debt_alerts(debts: Sequence[DebtInput]) -> list[tuple[str, str]]:
    alerts: list[tuple[str, str]] = []
    for debt in debts:
        if debt.balance <= 0:
            continue
        interest = minimum_viable_payment(debt.balance, debt.annual_rate_pct)
        if debt.effective_payment <= 0:
            alerts.append((
                Severity.CRITICAL.value,
                f"“{debt.name}” has no payment planned — the balance will only grow.",
            ))
        elif debt.effective_payment <= interest:
            alerts.append((
                Severity.CRITICAL.value,
                f"“{debt.name}”: the planned payment {debt.effective_payment} does not cover "
                f"monthly interest of {interest}. This debt never gets paid off.",
            ))
        elif D(debt.annual_rate_pct) >= 100:
            alerts.append((
                Severity.WARNING.value,
                f"“{debt.name}” carries a {D(debt.annual_rate_pct):.1f}% annual rate — "
                "prioritise it over savings if you can.",
            ))
    return alerts
