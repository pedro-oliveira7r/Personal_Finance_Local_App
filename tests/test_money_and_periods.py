"""Money arithmetic, formatting and period/date maths."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from calculations.money import (
    D,
    ZERO,
    allocate,
    apply_pct,
    format_money,
    format_pct,
    is_zero,
    money,
    money_sum,
    parse_money,
    pct_of,
    safe_div,
)
from calculations.periods import (
    Period,
    add_months,
    adjust_business_day,
    clamp_day,
    days_in_month,
    fiscal_year_of,
    is_leap_year,
    make_period,
    month_diff,
    month_end,
    period_for_date,
    period_sequence,
    periods_between,
    quarter_of_month,
    quarter_periods,
    safe_date,
    shift_date_months,
    shift_period,
    year_periods,
)
from constants import BusinessDayRule


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------
def test_float_input_does_not_leak_binary_noise():
    assert D(0.1) == Decimal("0.1")
    assert money(0.1 + 0.2) == Decimal("0.30")
    assert money_sum([0.1] * 10) == Decimal("1.00")


def test_rounding_is_half_up_not_bankers():
    assert money("2.345") == Decimal("2.35")
    assert money("2.355") == Decimal("2.36")
    assert money("-2.345") == Decimal("-2.35")


def test_allocate_never_loses_a_cent():
    parts = allocate("100.00", [1, 1, 1])
    assert sum(parts) == Decimal("100.00")
    assert parts == [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")]

    weighted = allocate("1000.00", [3, 1, 1])
    assert sum(weighted) == Decimal("1000.00")

    negative = allocate("-10.00", [1, 1, 1])
    assert sum(negative) == Decimal("-10.00")

    zero_weights = allocate("9.00", [0, 0, 0])
    assert sum(zero_weights) == Decimal("9.00")

    assert allocate("50.00", []) == []


def test_percentages_and_safe_division():
    assert pct_of("25", "200") == Decimal("12.50")
    assert pct_of("25", "0") == ZERO
    # A negative denominator must not flip the sign of the ratio.
    assert pct_of("25", "-200") == Decimal("12.50")
    assert safe_div(1, 0, Decimal("7")) == Decimal("7")
    assert apply_pct("1000", "5") == Decimal("1050.00")
    assert apply_pct("1000", "-10") == Decimal("900.00")


def test_quantising_never_raises_on_absurd_magnitudes():
    """A runaway calculation must saturate, not take a screen down with it."""
    from calculations.money import MAX_MONEY

    assert money(Decimal(10) ** 400) == MAX_MONEY
    assert money(-(Decimal(10) ** 400)) == -MAX_MONEY
    assert money(Decimal("NaN")) == ZERO
    assert money(Decimal("Infinity")) == ZERO
    assert money(Decimal("-Infinity")) == ZERO
    assert money(float("nan")) == ZERO
    # Ordinary values are untouched by the guard.
    assert money("1234.565") == Decimal("1234.57")
    assert money(MAX_MONEY) == MAX_MONEY


def test_is_zero_tolerance():
    assert is_zero(Decimal("0.004"))
    assert not is_zero(Decimal("0.01"))


@pytest.mark.parametrize("currency,value,expected", [
    ("BRL", "1234.5", "R$ 1.234,50"),
    ("BRL", "-1234.5", "-R$ 1.234,50"),
    ("USD", "1234.5", "$1,234.50"),
    ("EUR", "1000000", "€ 1.000.000,00"),
])
def test_currency_formatting(currency, value, expected):
    assert format_money(Decimal(value), currency) == expected


def test_compact_and_percent_formatting():
    assert format_money(Decimal("1234567.89"), "BRL", compact=True) == "R$ 1,2 M"
    assert format_money(Decimal("2500"), "USD", compact=True) == "$2.5 k"
    assert format_pct(Decimal("12.345")) == "12.3%"
    assert format_pct(Decimal("12.345"), signed=True) == "+12.3%"


@pytest.mark.parametrize("text,expected", [
    ("R$ 1.234,56", "1234.56"),
    ("1,234.56", "1234.56"),
    ("1234,56", "1234.56"),
    ("(1.234,56)", "-1234.56"),
    ("-284.90", "-284.90"),
    ("1.234", "1234"),
    ("1,234", "1234"),
    ("", "0"),
    ("nonsense", "0"),
])
def test_parse_money_handles_real_world_input(text, expected):
    assert parse_money(text) == Decimal(expected)


# --------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------
def test_calendar_month_period():
    period = make_period(2026, 8)
    assert (period.start, period.end) == (date(2026, 8, 1), date(2026, 8, 31))
    assert period.days == 31
    assert period.key == "2026-08"
    assert period.label == "August 2026"
    assert period.quarter == 3


def test_leap_year_february():
    assert is_leap_year(2024) and not is_leap_year(2023)
    assert make_period(2024, 2).end == date(2024, 2, 29)
    assert make_period(2023, 2).end == date(2023, 2, 28)
    assert make_period(2024, 2).days == 29
    assert make_period(2100, 2).end == date(2100, 2, 28)  # century, not a leap year


def test_month_end_overflow_is_clamped_not_an_error():
    assert safe_date(2026, 2, 31) == date(2026, 2, 28)
    assert safe_date(2024, 2, 31) == date(2024, 2, 29)
    assert clamp_day(2026, 4, 31) == 30
    assert month_end(2026, 11) == date(2026, 11, 30)


def test_custom_first_day_periods():
    period = make_period(2026, 8, 5)
    assert (period.start, period.end) == (date(2026, 8, 5), date(2026, 9, 4))
    assert period_for_date(date(2026, 9, 4), 5).key == "2026-08"
    assert period_for_date(date(2026, 9, 5), 5).key == "2026-09"
    assert period_for_date(date(2026, 8, 4), 5).key == "2026-07"


def test_first_day_28_across_february():
    period = make_period(2026, 2, 28)
    assert period.start == date(2026, 2, 28)
    assert period.end == date(2026, 3, 27)
    assert period_for_date(date(2026, 2, 27), 28).key == "2026-01"


def test_period_arithmetic_crosses_years():
    assert add_months(2026, 12, 1) == (2027, 1)
    assert add_months(2026, 1, -1) == (2025, 12)
    assert shift_period(make_period(2026, 12), 1).key == "2027-01"
    assert shift_period(make_period(2026, 1), -1).key == "2025-12"
    assert month_diff(date(2026, 1, 31), date(2026, 3, 1)) == 2
    assert shift_date_months(date(2026, 1, 31), 1) == date(2026, 2, 28)


def test_sequences_and_ranges():
    sequence = period_sequence(make_period(2026, 11), 4)
    assert [p.key for p in sequence] == ["2026-11", "2026-12", "2027-01", "2027-02"]
    inclusive = periods_between(make_period(2026, 1), make_period(2026, 3))
    assert len(inclusive) == 3
    assert periods_between(make_period(2026, 3), make_period(2026, 1)) == []
    assert period_sequence(make_period(2026, 1), 0) == []


def test_elapsed_fraction_is_bounded():
    period = make_period(2026, 8)
    assert period.elapsed_fraction(date(2026, 7, 1)) == 0.0
    assert period.elapsed_fraction(date(2026, 9, 1)) == 1.0
    middle = period.elapsed_fraction(date(2026, 8, 16))
    assert 0.5 < middle < 0.55


def test_quarters_and_fiscal_years():
    assert quarter_of_month(1) == 1 and quarter_of_month(12) == 4
    # A fiscal year starting in July puts July in Q1.
    assert quarter_of_month(7, fiscal_start_month=7) == 1
    assert quarter_of_month(6, fiscal_start_month=7) == 4
    assert fiscal_year_of(date(2026, 8, 1), 7) == 2027
    assert fiscal_year_of(date(2026, 5, 1), 7) == 2026
    assert [p.key for p in quarter_periods(2026, 2)] == ["2026-04", "2026-05", "2026-06"]
    assert len(year_periods(2026)) == 12


def test_business_day_adjustment():
    saturday = date(2026, 8, 8)
    assert saturday.weekday() == 5
    assert adjust_business_day(saturday, BusinessDayRule.NEXT.value) == date(2026, 8, 10)
    assert adjust_business_day(saturday, BusinessDayRule.PREVIOUS.value) == date(2026, 8, 7)
    assert adjust_business_day(saturday, BusinessDayRule.NEAREST.value) == date(2026, 8, 7)
    sunday = date(2026, 8, 9)
    assert adjust_business_day(sunday, BusinessDayRule.NEAREST.value) == date(2026, 8, 10)
    weekday = date(2026, 8, 12)
    assert adjust_business_day(weekday, BusinessDayRule.NEXT.value) == weekday
    assert adjust_business_day(saturday, BusinessDayRule.NONE.value) == saturday


def test_periods_are_orderable_and_hashable():
    a, b = make_period(2026, 1), make_period(2026, 2)
    assert a < b
    assert len({a, b, make_period(2026, 1)}) == 2
    assert b.index - a.index == 1
