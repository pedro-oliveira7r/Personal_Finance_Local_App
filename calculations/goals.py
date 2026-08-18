"""Savings-goal progress and completion maths."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

from calculations.money import ZERO, D, money, pct_of
from calculations.periods import Period, month_diff, shift_date_months
from constants import GoalStatus, Severity


@dataclass
class GoalProgress:
    goal_id: Optional[int]
    name: str
    target_amount: Decimal
    current_amount: Decimal
    planned_monthly: Decimal = ZERO
    target_date: Optional[date] = None
    actual_last_period: Decimal = ZERO
    average_monthly: Decimal = ZERO
    status: str = GoalStatus.ACTIVE.value

    # derived
    remaining: Decimal = ZERO
    progress_pct: Decimal = ZERO
    months_remaining: Optional[int] = None
    required_monthly: Decimal = ZERO
    projected_completion: Optional[date] = None
    on_track: Optional[bool] = None
    shortfall_monthly: Decimal = ZERO

    @property
    def is_complete(self) -> bool:
        return self.current_amount >= self.target_amount

    @property
    def severity(self) -> str:
        if self.is_complete:
            return Severity.SUCCESS.value
        if self.on_track is False:
            return Severity.WARNING.value
        return Severity.INFO.value

    @property
    def status_icon(self) -> str:
        if self.is_complete:
            return "✓"
        if self.on_track is False:
            return "!"
        return "→"

    def as_dict(self) -> dict:
        return {
            "goal_id": self.goal_id,
            "name": self.name,
            "target": self.target_amount,
            "current": self.current_amount,
            "remaining": self.remaining,
            "progress_pct": self.progress_pct,
            "planned_monthly": self.planned_monthly,
            "required_monthly": self.required_monthly,
            "months_remaining": self.months_remaining,
            "target_date": self.target_date,
            "projected_completion": self.projected_completion,
            "on_track": self.on_track,
        }


def compute_progress(
    *,
    name: str,
    target_amount: Decimal,
    current_amount: Decimal,
    planned_monthly: Decimal = ZERO,
    target_date: Optional[date] = None,
    average_monthly: Decimal = ZERO,
    actual_last_period: Decimal = ZERO,
    goal_id: Optional[int] = None,
    status: str = GoalStatus.ACTIVE.value,
    today: Optional[date] = None,
) -> GoalProgress:
    """Everything the UI needs to render one goal."""
    today = today or date.today()
    progress = GoalProgress(
        goal_id=goal_id,
        name=name,
        target_amount=money(target_amount),
        current_amount=money(current_amount),
        planned_monthly=money(planned_monthly),
        target_date=target_date,
        actual_last_period=money(actual_last_period),
        average_monthly=money(average_monthly),
        status=status,
    )

    progress.remaining = money(max(ZERO, progress.target_amount - progress.current_amount))
    progress.progress_pct = min(
        Decimal("100"), pct_of(progress.current_amount, progress.target_amount)
    )

    if target_date is not None:
        months = month_diff(today, target_date)
        # A target date inside the current month still leaves this month to pay.
        progress.months_remaining = max(0, months)
        if progress.remaining <= 0:
            progress.required_monthly = ZERO
        elif progress.months_remaining <= 0:
            progress.required_monthly = progress.remaining
        else:
            progress.required_monthly = money(
                progress.remaining / Decimal(progress.months_remaining)
            )

    #: What we believe will actually be contributed each month.
    contribution = progress.planned_monthly or progress.average_monthly
    if progress.remaining <= 0:
        progress.projected_completion = today
        progress.on_track = True
        progress.shortfall_monthly = ZERO
        return progress

    if contribution > 0:
        months_needed = int((progress.remaining / contribution).to_integral_value(rounding="ROUND_CEILING"))
        progress.projected_completion = shift_date_months(today, months_needed)
        if target_date is not None:
            progress.on_track = progress.projected_completion <= target_date
            progress.shortfall_monthly = money(
                max(ZERO, progress.required_monthly - contribution)
            )
        else:
            progress.on_track = True
    else:
        progress.projected_completion = None
        progress.on_track = False if target_date is not None else None
        progress.shortfall_monthly = progress.required_monthly

    return progress


def required_contribution(remaining: Decimal, months: int) -> Decimal:
    """Flat monthly amount needed to close a gap in ``months`` months."""
    if months <= 0:
        return money(remaining)
    return money(D(remaining) / Decimal(months))


def months_to_target(remaining: Decimal, monthly: Decimal) -> Optional[int]:
    if D(monthly) <= 0:
        return None
    return int((D(remaining) / D(monthly)).to_integral_value(rounding="ROUND_CEILING"))


def projected_balance_at(
    current: Decimal,
    monthly: Decimal,
    months: int,
    *,
    annual_rate_pct: Decimal = ZERO,
) -> Decimal:
    """Future value of a goal with regular contributions.

    Interest is optional and compounded monthly — useful for an emergency fund
    parked in an interest-bearing account.
    """
    balance = D(current)
    monthly_rate = D(annual_rate_pct) / Decimal(1200)
    for _ in range(max(0, int(months))):
        balance = balance * (Decimal(1) + monthly_rate) + D(monthly)
    return money(balance)


def prioritise(goals: Sequence[GoalProgress], available: Decimal) -> list[tuple[GoalProgress, Decimal]]:
    """Split ``available`` across goals, most urgent first.

    Urgency = the required monthly contribution. Fully-funded goals are
    skipped and anything left over is returned against a ``None`` goal by the
    caller if needed.
    """
    ranked = sorted(
        [g for g in goals if g.remaining > 0],
        key=lambda g: (
            g.months_remaining if g.months_remaining is not None else 9999,
            -g.required_monthly,
        ),
    )
    pot = money(available)
    plan: list[tuple[GoalProgress, Decimal]] = []
    for goal in ranked:
        if pot <= 0:
            plan.append((goal, ZERO))
            continue
        need = goal.required_monthly or goal.planned_monthly or goal.remaining
        give = min(pot, money(need))
        plan.append((goal, give))
        pot = money(pot - give)
    return plan


def goal_alerts(progresses: Sequence[GoalProgress]) -> list[tuple[str, str]]:
    """``(severity, message)`` pairs for the dashboard alert panel."""
    alerts: list[tuple[str, str]] = []
    for goal in progresses:
        if goal.status != GoalStatus.ACTIVE.value:
            continue
        if goal.is_complete:
            alerts.append((Severity.SUCCESS.value, f"Goal “{goal.name}” is fully funded."))
            continue
        if goal.on_track is False and goal.target_date:
            if goal.projected_completion is None:
                alerts.append((
                    Severity.WARNING.value,
                    f"“{goal.name}” has no contributions — it will never reach its target.",
                ))
            else:
                alerts.append((
                    Severity.WARNING.value,
                    f"“{goal.name}” is behind: needs {goal.required_monthly}/month "
                    f"but is getting {goal.planned_monthly or goal.average_monthly}.",
                ))
        elif goal.actual_last_period == 0 and goal.planned_monthly > 0:
            alerts.append((
                Severity.WARNING.value,
                f"No contribution recorded for “{goal.name}” last period.",
            ))
    return alerts
