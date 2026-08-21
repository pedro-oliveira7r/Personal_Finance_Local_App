"""Planned-vs-actual variance: sign convention, favourability, status bands."""

from __future__ import annotations

from decimal import Decimal

import pytest

from calculations.variance import (
    STATUS_NONE,
    STATUS_OK,
    STATUS_OVER,
    STATUS_SHORT,
    STATUS_UNBUDGETED,
    STATUS_UNUSED,
    STATUS_WARNING,
    approaching_limit,
    compute_variance,
    income_shortfalls,
    summarise,
    top_overspending,
    top_underspending,
    variance_table,
)
from constants import CategoryKind


def expense(planned, actual, **kwargs):
    return compute_variance(planned, actual, CategoryKind.EXPENSE.value, **kwargs)


def income(planned, actual, **kwargs):
    return compute_variance(planned, actual, CategoryKind.INCOME.value, **kwargs)


# --------------------------------------------------------------------------
# Sign convention
# --------------------------------------------------------------------------
def test_variance_is_always_actual_minus_planned():
    over = expense("1000", "1150")
    assert over.variance == Decimal("150.00")
    under = expense("1000", "850")
    assert under.variance == Decimal("-150.00")


def test_spending_under_budget_is_favourable():
    row = expense("1000", "800")
    assert row.favorable is True
    assert row.remaining == Decimal("200.00")
    assert row.consumed_pct == Decimal("80.00")
    assert row.overshoot == Decimal("0.00")


def test_spending_over_budget_is_unfavourable():
    row = expense("1000", "1200")
    assert row.favorable is False
    assert row.status == STATUS_OVER
    assert row.overshoot == Decimal("200.00")
    assert row.remaining_positive == Decimal("0.00")
    assert row.status_icon == "▲"


def test_earning_more_than_planned_is_favourable():
    row = income("5000", "5500")
    assert row.favorable is True
    assert row.variance == Decimal("500.00")
    assert row.status == STATUS_OK


def test_earning_less_than_planned_is_unfavourable():
    row = income("5000", "3000")
    assert row.favorable is False
    assert row.status == STATUS_SHORT
    assert row.overshoot == Decimal("2000.00")


def test_income_inside_tolerance_still_counts_as_on_plan():
    row = income("5000", "4900", tolerance_pct=Decimal("5"))
    assert row.status == STATUS_OK
    assert row.favorable is False  # still short, but not worth an alert


# --------------------------------------------------------------------------
# Status bands
# --------------------------------------------------------------------------
@pytest.mark.parametrize("actual,expected", [
    ("500", STATUS_OK),
    ("800", STATUS_WARNING),
    ("1000", STATUS_OK if False else STATUS_WARNING),
    ("1001", STATUS_OVER),
])
def test_expense_status_bands(actual, expected):
    row = expense("1000", actual, warning_pct=Decimal("80"),
                  critical_pct=Decimal("100"))
    assert row.status == expected


def test_nothing_planned_and_nothing_spent():
    row = expense("0", "0")
    assert row.status == STATUS_NONE
    assert row.favorable is None
    assert row.status_label == "—"


def test_spending_with_no_budget_is_unbudgeted():
    row = expense("0", "250")
    assert row.status == STATUS_UNBUDGETED
    assert row.favorable is False
    assert row.variance == Decimal("250.00")
    assert row.consumed_pct == Decimal("0.00")


def test_unplanned_income_is_a_good_surprise():
    row = income("0", "700")
    assert row.status == STATUS_UNBUDGETED
    assert row.favorable is True


def test_budgeted_but_untouched():
    row = expense("400", "0")
    assert row.status == STATUS_UNUSED
    assert row.favorable is True
    assert row.remaining == Decimal("400.00")


def test_custom_thresholds_are_respected():
    strict = expense("1000", "700", warning_pct=Decimal("60"),
                     critical_pct=Decimal("90"))
    assert strict.status == STATUS_WARNING
    relaxed = expense("1000", "1050", warning_pct=Decimal("95"),
                      critical_pct=Decimal("110"))
    assert relaxed.status == STATUS_WARNING


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def test_summarise_totals_and_counts():
    rows = [expense("1000", "1200", label="Food"),
            expense("500", "300", label="Fun"),
            income("4000", "4000", label="Salary")]
    summary = summarise(rows)
    assert summary.planned == Decimal("5500.00")
    assert summary.actual == Decimal("5500.00")
    assert summary.variance == Decimal("0.00")
    assert summary.over_count == 1
    assert summary.favorable_count == 2
    assert summary.unfavorable_count == 1


def test_top_overspending_and_underspending_ordering():
    rows = [
        expense("100", "400", label="Delivery"),
        expense("1000", "1500", label="Travel"),
        expense("800", "300", label="Fuel"),
        expense("200", "50", label="Books"),
    ]
    over = top_overspending(rows, limit=2)
    assert [row.label for row in over] == ["Travel", "Delivery"]
    under = top_underspending(rows, limit=2)
    assert [row.label for row in under] == ["Fuel", "Books"]


def test_income_shortfalls_are_ordered_worst_first():
    rows = [income("5000", "4000", label="Salary"),
            income("1000", "200", label="Freelance"),
            income("500", "700", label="Bonus")]
    shortfalls = income_shortfalls(rows)
    assert [row.label for row in shortfalls] == ["Salary", "Freelance"]


def test_approaching_limit_excludes_already_over():
    rows = [expense("1000", "850", label="Near"),
            expense("1000", "1200", label="Over"),
            expense("1000", "400", label="Fine")]
    near = approaching_limit(rows, Decimal("80"), Decimal("100"))
    assert [row.label for row in near] == ["Near"]


def test_variance_table_builds_rows_from_dicts():
    rows = variance_table([
        {"label": "Food", "kind": "expense", "planned": "500", "actual": "600"},
        {"label": "Salary", "kind": "income", "planned": "4000", "actual": "4000"},
    ])
    assert len(rows) == 2
    assert rows[0].status == STATUS_OVER
    assert rows[1].status == STATUS_OK
    assert rows[0].as_dict()["variance"] == Decimal("100.00")
