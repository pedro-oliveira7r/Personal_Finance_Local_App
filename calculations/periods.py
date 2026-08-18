"""Budget period arithmetic.

A *period* is the atomic budgeting unit: one month, optionally starting on a
day other than the 1st (people paid on the 5th often budget 5th-to-4th).
Quarters and years are aggregations of periods, never separate entities —
that keeps every downstream calculation working on a single grain.

All functions are pure and total: they never raise on month-end overflow
(31 February becomes 28/29 February) and they handle leap years correctly.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator, Sequence

from constants import MONTH_ABBR, MONTH_NAMES


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------
def is_leap_year(year: int) -> bool:
    return calendar.isleap(year)


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def month_end(year: int, month: int) -> date:
    return date(year, month, days_in_month(year, month))


def clamp_day(year: int, month: int, day: int) -> int:
    """Squeeze ``day`` into a month that may be shorter. 31 Feb -> 28/29 Feb."""
    return max(1, min(int(day), days_in_month(year, month)))


def safe_date(year: int, month: int, day: int) -> date:
    """Build a date, clamping the day to the month length."""
    year, month = normalise_month(year, month)
    return date(year, month, clamp_day(year, month, day))


def normalise_month(year: int, month: int) -> tuple[int, int]:
    """Fold a month number outside 1-12 into the right year."""
    index = (year * 12) + (month - 1)
    return index // 12, (index % 12) + 1


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    return normalise_month(year, month + delta)


def shift_date_months(anchor: date, delta: int, *, day: int | None = None) -> date:
    """Move a date by whole months, keeping (or overriding) the day-of-month."""
    year, month = add_months(anchor.year, anchor.month, delta)
    return safe_date(year, month, day if day is not None else anchor.day)


def month_diff(a: date, b: date) -> int:
    """Whole months from ``a`` to ``b`` (negative if ``b`` precedes ``a``)."""
    return (b.year - a.year) * 12 + (b.month - a.month)


def is_business_day(value: date) -> bool:
    return value.weekday() < 5


def adjust_business_day(value: date, rule: str) -> date:
    """Nudge a date off the weekend according to ``rule``.

    Public holidays are deliberately not modelled — they vary by country and
    guessing them wrong is worse than not adjusting at all.
    """
    from constants import BusinessDayRule

    if rule == BusinessDayRule.NONE.value or is_business_day(value):
        return value
    if rule == BusinessDayRule.NEXT.value:
        while not is_business_day(value):
            value += timedelta(days=1)
        return value
    if rule == BusinessDayRule.PREVIOUS.value:
        while not is_business_day(value):
            value -= timedelta(days=1)
        return value
    if rule == BusinessDayRule.NEAREST.value:
        # Saturday -> Friday, Sunday -> Monday.
        return value - timedelta(days=1) if value.weekday() == 5 else value + timedelta(days=1)
    return value


# --------------------------------------------------------------------------
# Period
# --------------------------------------------------------------------------
@dataclass(frozen=True, order=True)
class Period:
    """A single budgeting month with concrete calendar boundaries."""

    year: int
    month: int
    start: date
    end: date

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def label(self) -> str:
        return f"{MONTH_NAMES[self.month - 1]} {self.year}"

    @property
    def short_label(self) -> str:
        return f"{MONTH_ABBR[self.month - 1]}/{self.year % 100:02d}"

    @property
    def index(self) -> int:
        """Absolute month index — handy for sorting and offset arithmetic."""
        return self.year * 12 + (self.month - 1)

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def quarter(self) -> int:
        return (self.month - 1) // 3 + 1

    def contains(self, value: date) -> bool:
        return self.start <= value <= self.end

    def clip(self, value: date) -> date:
        return min(max(value, self.start), self.end)

    def elapsed_fraction(self, today: date) -> float:
        """0.0 before the period, 1.0 after, proportional inside."""
        if today < self.start:
            return 0.0
        if today > self.end:
            return 1.0
        return ((today - self.start).days + 1) / self.days

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.label


def make_period(year: int, month: int, first_day: int = 1) -> Period:
    """Build the period identified by ``year``/``month``.

    With ``first_day=1`` this is the calendar month. With ``first_day=5`` the
    August 2026 period runs 2026-08-05 .. 2026-09-04.
    """
    year, month = normalise_month(year, month)
    first_day = max(1, min(int(first_day or 1), 28))
    start = safe_date(year, month, first_day)
    if first_day == 1:
        end = month_end(year, month)
    else:
        next_year, next_month = add_months(year, month, 1)
        end = safe_date(next_year, next_month, first_day) - timedelta(days=1)
    return Period(year=year, month=month, start=start, end=end)


def period_for_date(value: date, first_day: int = 1) -> Period:
    """Which period does this calendar date fall into?"""
    first_day = max(1, min(int(first_day or 1), 28))
    if first_day == 1:
        return make_period(value.year, value.month, 1)
    boundary = clamp_day(value.year, value.month, first_day)
    if value.day >= boundary:
        return make_period(value.year, value.month, first_day)
    year, month = add_months(value.year, value.month, -1)
    return make_period(year, month, first_day)


def parse_period_key(key: str) -> tuple[int, int]:
    """``"2026-08"`` -> ``(2026, 8)``."""
    text = str(key).strip()
    parts = text.replace("/", "-").split("-")
    if len(parts) < 2:
        raise ValueError(f"Invalid period key: {key!r}")
    year, month = int(parts[0]), int(parts[1])
    if not 1 <= month <= 12:
        raise ValueError(f"Invalid month in period key: {key!r}")
    return year, month


def period_from_key(key: str, first_day: int = 1) -> Period:
    year, month = parse_period_key(key)
    return make_period(year, month, first_day)


def next_period(period: Period, first_day: int = 1) -> Period:
    year, month = add_months(period.year, period.month, 1)
    return make_period(year, month, first_day)


def previous_period(period: Period, first_day: int = 1) -> Period:
    year, month = add_months(period.year, period.month, -1)
    return make_period(year, month, first_day)


def shift_period(period: Period, delta: int, first_day: int = 1) -> Period:
    year, month = add_months(period.year, period.month, delta)
    return make_period(year, month, first_day)


def period_sequence(start: Period, count: int, first_day: int = 1) -> list[Period]:
    """``count`` consecutive periods beginning at ``start`` (inclusive)."""
    if count <= 0:
        return []
    return [shift_period(start, offset, first_day) for offset in range(count)]


def periods_between(start: Period, end: Period, first_day: int = 1) -> list[Period]:
    """Inclusive range; returns ``[]`` when ``end`` precedes ``start``."""
    span = end.index - start.index
    if span < 0:
        return []
    return period_sequence(start, span + 1, first_day)


def iter_periods(start: Period, count: int, first_day: int = 1) -> Iterator[Period]:
    for offset in range(max(0, count)):
        yield shift_period(start, offset, first_day)


# --------------------------------------------------------------------------
# Quarters, years, fiscal years
# --------------------------------------------------------------------------
def quarter_of_month(month: int, fiscal_start_month: int = 1) -> int:
    """1-4, relative to the fiscal year start."""
    offset = (month - fiscal_start_month) % 12
    return offset // 3 + 1


def fiscal_year_of(value: date, fiscal_start_month: int = 1) -> int:
    """The fiscal year label a date belongs to."""
    if fiscal_start_month <= 1:
        return value.year
    return value.year + 1 if value.month >= fiscal_start_month else value.year


def quarter_periods(year: int, quarter: int, first_day: int = 1,
                    fiscal_start_month: int = 1) -> list[Period]:
    """The three periods making up fiscal quarter ``quarter`` of ``year``."""
    quarter = max(1, min(int(quarter), 4))
    start_year, start_month = add_months(year, fiscal_start_month, (quarter - 1) * 3)
    return period_sequence(make_period(start_year, start_month, first_day), 3, first_day)


def year_periods(year: int, first_day: int = 1, fiscal_start_month: int = 1) -> list[Period]:
    start = make_period(year, fiscal_start_month, first_day)
    return period_sequence(start, 12, first_day)


def group_label(period: Period, granularity: str, fiscal_start_month: int = 1) -> str:
    """Bucket label used by the reporting layer."""
    if granularity == "month":
        return period.short_label
    if granularity == "quarter":
        return f"Q{quarter_of_month(period.month, fiscal_start_month)} {period.year}"
    if granularity == "year":
        return str(period.year)
    return period.key


# --------------------------------------------------------------------------
# Ranges
# --------------------------------------------------------------------------
def date_range_of_periods(periods: Sequence[Period]) -> tuple[date, date] | tuple[None, None]:
    if not periods:
        return None, None
    return min(p.start for p in periods), max(p.end for p in periods)


def month_index_to_period(index: int, first_day: int = 1) -> Period:
    return make_period(index // 12, index % 12 + 1, first_day)


def format_date(value: date | None, pattern: str = "%d/%m/%Y") -> str:
    return value.strftime(pattern) if value else ""
