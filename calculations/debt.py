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

from calculations.money import ZERO, D, money, pct_of
from calculations.periods import shift_date_months
from constants import Severity

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
