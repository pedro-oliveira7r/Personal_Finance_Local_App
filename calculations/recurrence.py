"""The recurrence engine.

Turns a rule ("rent, monthly, on the 5th, +6% every January, paid one business
day earlier when the 5th is a weekend") into a deterministic list of
occurrences with the correct amount for each date.

Two dates are produced per occurrence and they are not always the same:

``due_date``
    the nominal date the money is owed/earned, after weekend adjustment.
``cash_date``
    when the money actually moves — ``due_date`` plus the settlement offset.
    This is what drives cash-flow and income-availability logic.

Every occurrence carries a stable :attr:`Occurrence.key` derived only from the
due date, which is what makes regeneration idempotent: re-running the engine
updates the existing planned transaction instead of creating a twin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Iterator, Optional

from calculations.money import D, ONE, money
from calculations.periods import (
    add_months,
    adjust_business_day,
    clamp_day,
    month_diff,
    safe_date,
)
from constants import MONTH_STEP, BusinessDayRule, Frequency

#: Refuse to enumerate more than this many occurrences in one call — a guard
#: against a daily rule with a 50-year window silently eating all memory.
MAX_OCCURRENCES = 5000


@dataclass(frozen=True)
class Occurrence:
    seq: int
    due_date: date
    cash_date: date
    amount: Decimal
    growth_steps: int = 0
    seasonal_factor: Decimal = ONE

    @property
    def key(self) -> str:
        """Stable identity for de-duplication (date-based, not index-based)."""
        return self.due_date.isoformat()


@dataclass
class RecurrenceSpec:
    """Everything the engine needs, decoupled from the ORM."""

    frequency: str
    start_date: date
    amount: Decimal = Decimal("0")
    interval: int = 1
    end_date: Optional[date] = None
    max_occurrences: Optional[int] = None
    day_of_month: Optional[int] = None
    weekday: Optional[int] = None            # 0 = Monday
    month_of_year: Optional[int] = None
    growth_pct: Decimal = Decimal("0")
    growth_every_months: int = 12
    growth_anchor_month: Optional[int] = None
    seasonal_factors: Optional[dict] = field(default=None)
    business_day_rule: str = BusinessDayRule.NONE.value
    settlement_offset_days: int = 0

    def __post_init__(self) -> None:
        self.amount = D(self.amount)
        self.growth_pct = D(self.growth_pct)
        self.interval = max(1, int(self.interval or 1))
        self.growth_every_months = max(1, int(self.growth_every_months or 12))
        self.settlement_offset_days = int(self.settlement_offset_days or 0)


def spec_from_rule(rule) -> RecurrenceSpec:
    """Build a :class:`RecurrenceSpec` from a ``RecurringRule`` ORM row."""
    return RecurrenceSpec(
        frequency=rule.frequency,
        start_date=rule.start_date,
        amount=rule.amount,
        interval=rule.interval or 1,
        end_date=rule.end_date,
        max_occurrences=rule.max_occurrences,
        day_of_month=rule.day_of_month,
        weekday=rule.weekday,
        month_of_year=rule.month_of_year,
        growth_pct=rule.growth_pct or Decimal("0"),
        growth_every_months=rule.growth_every_months or 12,
        growth_anchor_month=rule.growth_anchor_month,
        seasonal_factors=rule.seasonal_factors,
        business_day_rule=rule.business_day_rule or BusinessDayRule.NONE.value,
        settlement_offset_days=rule.settlement_offset_days or 0,
    )


# --------------------------------------------------------------------------
# Amount modifiers
# --------------------------------------------------------------------------
def growth_steps_for(spec: RecurrenceSpec, target: date) -> int:
    """How many times the growth percentage has kicked in by ``target``.

    With an anchor month the increase happens *on* that month ("+5% every
    January"). Without one it happens every ``growth_every_months`` months
    counted from the start date ("+2% every 6 months").
    """
    if spec.growth_pct == 0:
        return 0
    months = month_diff(spec.start_date, target)
    if months <= 0:
        return 0
    if spec.growth_anchor_month is None:
        return months // spec.growth_every_months

    anchor = int(spec.growth_anchor_month)
    step = spec.growth_every_months
    start_index = spec.start_date.year * 12 + (spec.start_date.month - 1)
    target_index = target.year * 12 + (target.month - 1)
    count = 0
    for index in range(start_index + 1, target_index + 1):
        month = index % 12 + 1
        if (month - anchor) % step == 0:
            count += 1
    return count


def seasonal_factor_for(spec: RecurrenceSpec, target: date) -> Decimal:
    """Month multiplier, e.g. electricity 1.35x in summer."""
    factors = spec.seasonal_factors or {}
    if not factors:
        return ONE
    for key in (str(target.month), target.month, f"{target.month:02d}"):
        if key in factors:
            value = D(factors[key])
            return value if value > 0 else ONE
    return ONE


def amount_for_date(spec: RecurrenceSpec, target: date) -> Decimal:
    """The amount this rule produces on ``target``, growth and season applied."""
    amount = D(spec.amount)
    steps = growth_steps_for(spec, target)
    if steps:
        factor = ONE + spec.growth_pct / Decimal(100)
        for _ in range(steps):
            amount *= factor
    amount *= seasonal_factor_for(spec, target)
    return money(amount)


# --------------------------------------------------------------------------
# Date generation
# --------------------------------------------------------------------------
def _nominal_dates(spec: RecurrenceSpec, until: date) -> Iterator[date]:
    """Raw schedule dates (no weekend adjustment, no settlement offset)."""
    freq = spec.frequency
    start = spec.start_date

    if freq == Frequency.ONE_TIME.value:
        yield start
        return

    if freq in (Frequency.DAILY.value, Frequency.CUSTOM_DAYS.value):
        step = timedelta(days=spec.interval)
        current = start
        while current <= until:
            yield current
            current += step
        return

    if freq in (Frequency.WEEKLY.value, Frequency.BIWEEKLY.value):
        weeks = spec.interval * (2 if freq == Frequency.BIWEEKLY.value else 1)
        current = start
        if spec.weekday is not None:
            shift = (int(spec.weekday) - start.weekday()) % 7
            current = start + timedelta(days=shift)
        step = timedelta(weeks=weeks)
        while current <= until:
            yield current
            current += step
        return

    # Month-based frequencies.
    step_months = MONTH_STEP.get(freq, spec.interval if freq == Frequency.CUSTOM_MONTHS.value else 1)
    if freq == Frequency.CUSTOM_MONTHS.value:
        step_months = spec.interval
    else:
        step_months = step_months * spec.interval

    day = spec.day_of_month or start.day
    year, month = start.year, start.month

    if spec.month_of_year:
        # Anchor an annual/quarterly series to a specific month.
        month = int(spec.month_of_year)
        year = start.year
        if safe_date(year, month, day) < start:
            year, month = add_months(year, month, step_months)

    current = safe_date(year, month, day)
    if current < start and not spec.month_of_year:
        year, month = add_months(year, month, step_months)
        current = safe_date(year, month, day)

    while current <= until:
        yield current
        year, month = add_months(year, month, step_months)
        current = safe_date(year, month, day)


def generate_occurrences(
    spec: RecurrenceSpec,
    window_start: date,
    window_end: date,
) -> list[Occurrence]:
    """Every occurrence whose ``due_date`` falls in ``[window_start, window_end]``.

    ``seq`` counts from the rule's own start, so it stays stable no matter
    which window you ask for.
    """
    if window_end < window_start:
        return []
    horizon = window_end
    if spec.end_date and spec.end_date < horizon:
        horizon = spec.end_date
    if horizon < spec.start_date:
        return []

    results: list[Occurrence] = []
    for seq, nominal in enumerate(_nominal_dates(spec, horizon)):
        if seq >= MAX_OCCURRENCES:
            break
        if spec.max_occurrences is not None and seq >= spec.max_occurrences:
            break
        if spec.end_date and nominal > spec.end_date:
            break
        due = adjust_business_day(nominal, spec.business_day_rule)
        if due < window_start or due > window_end:
            continue
        cash = due + timedelta(days=spec.settlement_offset_days)
        results.append(Occurrence(
            seq=seq,
            due_date=due,
            cash_date=cash,
            amount=amount_for_date(spec, nominal),
            growth_steps=growth_steps_for(spec, nominal),
            seasonal_factor=seasonal_factor_for(spec, nominal),
        ))
    return results


def occurrences_in_period(spec: RecurrenceSpec, period) -> list[Occurrence]:
    """Convenience wrapper around a :class:`~calculations.periods.Period`."""
    return generate_occurrences(spec, period.start, period.end)


def period_total(spec: RecurrenceSpec, period) -> Decimal:
    """Sum of everything this rule contributes to one period."""
    return money(sum((occ.amount for occ in occurrences_in_period(spec, period)), Decimal("0")))


def next_occurrence(spec: RecurrenceSpec, after: date, lookahead_days: int = 400) -> Optional[Occurrence]:
    """The first occurrence strictly after ``after``, if any."""
    window_end = after + timedelta(days=lookahead_days)
    if spec.end_date and spec.end_date < window_end:
        window_end = spec.end_date
    upcoming = generate_occurrences(spec, after + timedelta(days=1), window_end)
    return upcoming[0] if upcoming else None


def describe(spec: RecurrenceSpec) -> str:
    """Human summary used in list views and tooltips."""
    from constants import FREQUENCY_LABELS

    label = FREQUENCY_LABELS.get(spec.frequency, spec.frequency)
    parts = [label]
    if spec.frequency in (Frequency.CUSTOM_DAYS.value, Frequency.CUSTOM_MONTHS.value):
        unit = "days" if spec.frequency == Frequency.CUSTOM_DAYS.value else "months"
        parts = [f"Every {spec.interval} {unit}"]
    elif spec.interval > 1:
        parts.append(f"(every {spec.interval} intervals)")
    if spec.day_of_month:
        parts.append(f"on day {spec.day_of_month}")
    if spec.month_of_year:
        from constants import MONTH_NAMES
        parts.append(f"in {MONTH_NAMES[int(spec.month_of_year) - 1]}")
    if spec.growth_pct:
        every = spec.growth_every_months
        when = ""
        if spec.growth_anchor_month:
            from constants import MONTH_NAMES
            when = f" each {MONTH_NAMES[int(spec.growth_anchor_month) - 1]}"
        elif every != 1:
            when = f" every {every} months"
        parts.append(f"· {spec.growth_pct:+.2f}%{when}")
    if spec.seasonal_factors:
        parts.append("· seasonal")
    if spec.settlement_offset_days:
        parts.append(f"· cash {spec.settlement_offset_days:+d}d")
    if spec.end_date:
        parts.append(f"· until {spec.end_date.isoformat()}")
    return " ".join(parts)


def total_between(specs: Iterable[RecurrenceSpec], start: date, end: date) -> Decimal:
    total = Decimal("0")
    for spec in specs:
        for occ in generate_occurrences(spec, start, end):
            total += occ.amount
    return money(total)
