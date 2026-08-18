"""The recurrence engine: schedules, growth, seasonality, settlement, idempotency."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from calculations.periods import make_period, period_from_key
from calculations.recurrence import (
    MAX_OCCURRENCES,
    RecurrenceSpec,
    amount_for_date,
    describe,
    generate_occurrences,
    growth_steps_for,
    next_occurrence,
    occurrences_in_period,
    period_total,
    seasonal_factor_for,
    total_between,
)
from constants import BusinessDayRule, Frequency


def spec(**kwargs) -> RecurrenceSpec:
    base = dict(frequency=Frequency.MONTHLY.value, start_date=date(2026, 1, 10),
                amount=Decimal("100"))
    base.update(kwargs)
    return RecurrenceSpec(**base)


# --------------------------------------------------------------------------
# Schedules
# --------------------------------------------------------------------------
def test_monthly_on_a_fixed_day():
    rule = spec(day_of_month=5, start_date=date(2026, 1, 1))
    dates = [o.due_date for o in generate_occurrences(rule, date(2026, 1, 1),
                                                      date(2026, 4, 30))]
    assert dates == [date(2026, 1, 5), date(2026, 2, 5), date(2026, 3, 5),
                     date(2026, 4, 5)]


def test_monthly_on_the_31st_clamps_in_short_months():
    rule = spec(day_of_month=31, start_date=date(2026, 1, 31))
    dates = [o.due_date for o in generate_occurrences(rule, date(2026, 1, 1),
                                                      date(2026, 5, 1))]
    assert dates == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31),
                     date(2026, 4, 30)]


def test_monthly_on_the_29th_in_a_leap_year():
    rule = spec(day_of_month=29, start_date=date(2024, 1, 29))
    dates = [o.due_date for o in generate_occurrences(rule, date(2024, 1, 1),
                                                      date(2024, 3, 31))]
    assert dates == [date(2024, 1, 29), date(2024, 2, 29), date(2024, 3, 29)]


def test_quarterly_anchored_to_a_month():
    rule = spec(frequency=Frequency.QUARTERLY.value, month_of_year=1, day_of_month=20,
                start_date=date(2026, 1, 1))
    dates = [o.due_date for o in generate_occurrences(rule, date(2026, 1, 1),
                                                      date(2026, 12, 31))]
    assert [d.month for d in dates] == [1, 4, 7, 10]


def test_annual_in_a_specific_month():
    rule = spec(frequency=Frequency.ANNUAL.value, month_of_year=3, day_of_month=15,
                start_date=date(2026, 1, 1))
    dates = [o.due_date for o in generate_occurrences(rule, date(2026, 1, 1),
                                                      date(2029, 1, 1))]
    assert dates == [date(2026, 3, 15), date(2027, 3, 15), date(2028, 3, 15)]


def test_semiannual_steps_six_months():
    rule = spec(frequency=Frequency.SEMIANNUAL.value, start_date=date(2026, 2, 10))
    dates = [o.due_date for o in generate_occurrences(rule, date(2026, 1, 1),
                                                      date(2027, 12, 31))]
    assert dates == [date(2026, 2, 10), date(2026, 8, 10), date(2027, 2, 10),
                     date(2027, 8, 10)]


def test_weekly_and_biweekly():
    weekly = spec(frequency=Frequency.WEEKLY.value, start_date=date(2026, 8, 3))
    dates = [o.due_date for o in generate_occurrences(weekly, date(2026, 8, 1),
                                                      date(2026, 8, 31))]
    assert dates[:3] == [date(2026, 8, 3), date(2026, 8, 10), date(2026, 8, 17)]

    fortnight = spec(frequency=Frequency.BIWEEKLY.value, start_date=date(2026, 8, 3))
    dates = [o.due_date for o in generate_occurrences(fortnight, date(2026, 8, 1),
                                                      date(2026, 9, 30))]
    assert dates == [date(2026, 8, 3), date(2026, 8, 17), date(2026, 8, 31),
                     date(2026, 9, 14), date(2026, 9, 28)]


def test_weekly_aligns_to_a_chosen_weekday():
    rule = spec(frequency=Frequency.WEEKLY.value, start_date=date(2026, 8, 3),
                weekday=4)  # Friday
    dates = [o.due_date for o in generate_occurrences(rule, date(2026, 8, 1),
                                                      date(2026, 8, 31))]
    assert all(d.weekday() == 4 for d in dates)
    assert dates[0] == date(2026, 8, 7)


def test_daily_and_custom_intervals():
    daily = spec(frequency=Frequency.DAILY.value, start_date=date(2026, 8, 1))
    assert len(generate_occurrences(daily, date(2026, 8, 1), date(2026, 8, 31))) == 31

    every_ten = spec(frequency=Frequency.CUSTOM_DAYS.value, interval=10,
                     start_date=date(2026, 8, 1))
    dates = [o.due_date for o in generate_occurrences(every_ten, date(2026, 8, 1),
                                                      date(2026, 8, 31))]
    assert dates == [date(2026, 8, 1), date(2026, 8, 11), date(2026, 8, 21),
                     date(2026, 8, 31)]

    every_two_months = spec(frequency=Frequency.CUSTOM_MONTHS.value, interval=2,
                            start_date=date(2026, 1, 15))
    dates = [o.due_date for o in generate_occurrences(every_two_months, date(2026, 1, 1),
                                                      date(2026, 12, 31))]
    assert [d.month for d in dates] == [1, 3, 5, 7, 9, 11]


def test_one_time_fires_once():
    rule = spec(frequency=Frequency.ONE_TIME.value, start_date=date(2026, 6, 15))
    assert len(generate_occurrences(rule, date(2026, 1, 1), date(2027, 1, 1))) == 1


def test_end_date_and_max_occurrences_stop_the_series():
    bounded = spec(day_of_month=1, start_date=date(2026, 1, 1),
                   end_date=date(2026, 3, 31))
    assert len(generate_occurrences(bounded, date(2026, 1, 1), date(2026, 12, 31))) == 3

    capped = spec(day_of_month=1, start_date=date(2026, 1, 1), max_occurrences=2)
    assert len(generate_occurrences(capped, date(2026, 1, 1), date(2026, 12, 31))) == 2


def test_window_before_the_rule_starts_is_empty():
    rule = spec(start_date=date(2026, 6, 1))
    assert generate_occurrences(rule, date(2026, 1, 1), date(2026, 5, 31)) == []
    assert generate_occurrences(rule, date(2026, 12, 1), date(2026, 1, 1)) == []


def test_sequence_numbers_are_stable_across_windows():
    rule = spec(day_of_month=1, start_date=date(2026, 1, 1))
    full = generate_occurrences(rule, date(2026, 1, 1), date(2026, 6, 30))
    partial = generate_occurrences(rule, date(2026, 4, 1), date(2026, 6, 30))
    assert [o.seq for o in partial] == [3, 4, 5]
    assert partial[0].key == full[3].key


def test_occurrence_key_is_date_based_for_idempotency():
    rule = spec(day_of_month=9, start_date=date(2026, 1, 9))
    first = generate_occurrences(rule, date(2026, 1, 1), date(2026, 2, 28))
    again = generate_occurrences(rule, date(2026, 1, 1), date(2026, 2, 28))
    assert [o.key for o in first] == [o.key for o in again]
    assert first[0].key == "2026-01-09"


def test_runaway_series_is_capped():
    rule = spec(frequency=Frequency.DAILY.value, start_date=date(1990, 1, 1))
    generated = generate_occurrences(rule, date(1990, 1, 1), date(2050, 1, 1))
    assert len(generated) <= MAX_OCCURRENCES


# --------------------------------------------------------------------------
# Amounts
# --------------------------------------------------------------------------
def test_growth_anchored_to_january():
    rule = spec(amount=Decimal("5000"), day_of_month=5,
                start_date=date(2025, 6, 5), growth_pct=Decimal("5"),
                growth_anchor_month=1)
    assert period_total(rule, period_from_key("2025-12")) == Decimal("5000.00")
    assert period_total(rule, period_from_key("2026-01")) == Decimal("5250.00")
    assert period_total(rule, period_from_key("2026-12")) == Decimal("5250.00")
    assert period_total(rule, period_from_key("2027-01")) == Decimal("5512.50")
    assert growth_steps_for(rule, date(2028, 1, 5)) == 3


def test_growth_every_n_months_without_an_anchor():
    rule = spec(amount=Decimal("100"), start_date=date(2026, 1, 10),
                growth_pct=Decimal("10"), growth_every_months=6)
    assert amount_for_date(rule, date(2026, 1, 10)) == Decimal("100.00")
    assert amount_for_date(rule, date(2026, 6, 10)) == Decimal("100.00")
    assert amount_for_date(rule, date(2026, 7, 10)) == Decimal("110.00")
    assert amount_for_date(rule, date(2027, 1, 10)) == Decimal("121.00")


def test_negative_growth_shrinks_the_amount():
    rule = spec(amount=Decimal("200"), start_date=date(2026, 1, 1),
                growth_pct=Decimal("-10"), growth_every_months=12)
    assert amount_for_date(rule, date(2027, 1, 1)) == Decimal("180.00")


def test_seasonal_factors_apply_per_month():
    rule = spec(amount=Decimal("200"), day_of_month=10,
                start_date=date(2026, 1, 10),
                seasonal_factors={"1": 1.4, "7": 0.8})
    assert period_total(rule, period_from_key("2026-01")) == Decimal("280.00")
    assert period_total(rule, period_from_key("2026-04")) == Decimal("200.00")
    assert period_total(rule, period_from_key("2026-07")) == Decimal("160.00")
    assert seasonal_factor_for(rule, date(2026, 3, 1)) == Decimal("1")


def test_growth_and_seasonality_compose():
    rule = spec(amount=Decimal("100"), day_of_month=1, start_date=date(2026, 1, 1),
                growth_pct=Decimal("10"), growth_anchor_month=1,
                seasonal_factors={"1": 2.0})
    # 2027-01: one growth step (110) then doubled by the season factor.
    assert amount_for_date(rule, date(2027, 1, 1)) == Decimal("220.00")


def test_zero_and_invalid_seasonal_factors_are_ignored():
    rule = spec(amount=Decimal("100"), seasonal_factors={"5": 0})
    assert seasonal_factor_for(rule, date(2026, 5, 1)) == Decimal("1")


# --------------------------------------------------------------------------
# Dates that move
# --------------------------------------------------------------------------
def test_weekend_adjustment_moves_the_due_date():
    saturday_rule = spec(day_of_month=8, start_date=date(2026, 8, 1),
                         business_day_rule=BusinessDayRule.PREVIOUS.value)
    first = generate_occurrences(saturday_rule, date(2026, 8, 1), date(2026, 8, 31))[0]
    assert date(2026, 8, 8).weekday() == 5
    assert first.due_date == date(2026, 8, 7)


def test_settlement_offset_separates_due_date_from_cash_date():
    rule = spec(day_of_month=30, start_date=date(2026, 1, 30),
                settlement_offset_days=3)
    occurrence = generate_occurrences(rule, date(2026, 1, 1), date(2026, 1, 31))[0]
    assert occurrence.due_date == date(2026, 1, 30)
    assert occurrence.cash_date == date(2026, 2, 2)


def test_negative_settlement_offset_pays_early():
    rule = spec(day_of_month=10, start_date=date(2026, 3, 10),
                settlement_offset_days=-2)
    occurrence = generate_occurrences(rule, date(2026, 3, 1), date(2026, 3, 31))[0]
    assert occurrence.cash_date == date(2026, 3, 8)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def test_next_occurrence_looks_forward_only():
    rule = spec(day_of_month=15, start_date=date(2026, 1, 15))
    upcoming = next_occurrence(rule, date(2026, 3, 15))
    assert upcoming is not None and upcoming.due_date == date(2026, 4, 15)


def test_next_occurrence_returns_none_after_the_end():
    rule = spec(day_of_month=1, start_date=date(2026, 1, 1),
                end_date=date(2026, 2, 1))
    assert next_occurrence(rule, date(2026, 3, 1)) is None


def test_totals_across_rules():
    a = spec(amount=Decimal("100"), day_of_month=1, start_date=date(2026, 1, 1))
    b = spec(amount=Decimal("50"), day_of_month=15, start_date=date(2026, 1, 1))
    assert total_between([a, b], date(2026, 1, 1), date(2026, 2, 28)) == Decimal("300.00")


def test_occurrences_in_period_respects_custom_first_day():
    rule = spec(day_of_month=2, start_date=date(2026, 1, 2))
    period = make_period(2026, 8, 5)  # 5 Aug - 4 Sep
    inside = occurrences_in_period(rule, period)
    assert [o.due_date for o in inside] == [date(2026, 9, 2)]


def test_describe_is_human_readable():
    rule = spec(day_of_month=5, growth_pct=Decimal("5"), growth_anchor_month=1,
                seasonal_factors={"1": 1.2}, settlement_offset_days=2)
    text = describe(rule)
    assert "Monthly" in text and "day 5" in text and "January" in text
    assert "seasonal" in text and "+2d" in text
