"""Zero-based budgeting: balance, over/under allocation, double-allocation guards."""

from __future__ import annotations

from decimal import Decimal

import pytest

from calculations.budgeting import (
    BALANCED,
    OVER_ALLOCATED,
    UNDER_ALLOCATED,
    Allocation,
    apply_to_reach_zero,
    balance_suggestions,
    budget_utilisation,
    carry_forward,
    detect_double_allocation,
    savings_rate,
    split_lines,
    zero_based_summary,
)
from constants import AllocationTarget, CategoryKind


def income(amount, label="Salary"):
    return Allocation(amount=amount, kind=CategoryKind.INCOME.value, label=label)


def expense(amount, label="Rent", **kwargs):
    return Allocation(amount=amount, kind=CategoryKind.EXPENSE.value, label=label,
                      **kwargs)


def test_balanced_budget_is_exactly_zero():
    result = zero_based_summary([
        income("5000"), expense("3000", "Living"),
        Allocation("2000", kind=CategoryKind.SAVINGS.value, label="Savings",
                   target=AllocationTarget.SAVINGS.value),
    ])
    assert result.status == BALANCED
    assert result.remaining == Decimal("0.00")
    assert result.available == Decimal("5000.00")
    assert result.allocated == Decimal("5000.00")
    assert result.is_balanced
    assert result.allocated_pct == Decimal("100.00")
    assert balance_suggestions(result) == []


def test_carry_in_counts_as_available_money():
    result = zero_based_summary([income("5000"), expense("5200")],
                                carry_in="200")
    assert result.carry_in == Decimal("200.00")
    assert result.available == Decimal("5200.00")
    assert result.status == BALANCED


def test_under_allocated_reports_the_gap():
    result = zero_based_summary([income("5000"), expense("3000")])
    assert result.status == UNDER_ALLOCATED
    assert result.unallocated == Decimal("2000.00")
    assert result.overspend == Decimal("0.00")
    codes = {w.code for w in result.warnings}
    assert "under_allocated" in codes
    assert len(balance_suggestions(result)) == 4


def test_over_allocated_reports_the_overspend():
    result = zero_based_summary([income("5000"), expense("6500")])
    assert result.status == OVER_ALLOCATED
    assert result.overspend == Decimal("1500.00")
    assert result.unallocated == Decimal("0.00")
    warnings = {w.code: w for w in result.warnings}
    assert warnings["over_allocated"].severity == "critical"


def test_sub_cent_difference_still_counts_as_balanced():
    result = zero_based_summary([income("1000.00"), expense("999.997")])
    assert result.status == BALANCED
    assert result.remaining == Decimal("0.00")


def test_zero_income_period_with_allocations_is_flagged():
    result = zero_based_summary([expense("500")])
    codes = {w.code for w in result.warnings}
    assert "no_income" in codes
    assert "no_available_money" in codes
    assert result.status == OVER_ALLOCATED


def test_empty_budget_is_balanced_but_warns_about_income():
    result = zero_based_summary([])
    assert result.status == BALANCED
    assert {w.code for w in result.warnings} == {"no_income"}


def test_breakdown_by_kind_and_target():
    result = zero_based_summary([
        income("6000"),
        expense("2000", "Housing"),
        Allocation("500", kind=CategoryKind.SAVINGS.value,
                   target=AllocationTarget.GOAL.value, label="Trip", goal_id=1),
        Allocation("1000", kind=CategoryKind.INVESTMENT.value,
                   target=AllocationTarget.INVESTMENT.value, label="ETFs"),
        Allocation("2500", kind=CategoryKind.DEBT.value,
                   target=AllocationTarget.DEBT.value, label="Car", debt_id=2),
    ])
    assert result.by_kind == {
        "expense": Decimal("2000.00"), "savings": Decimal("500.00"),
        "investment": Decimal("1000.00"), "debt": Decimal("2500.00"),
    }
    assert result.by_target["goal"] == Decimal("500.00")
    assert result.status == BALANCED


def test_split_lines_separates_income_from_allocations():
    inc, alloc = split_lines([income("100"), expense("40"), expense("60")])
    assert len(inc) == 1 and len(alloc) == 2


# --------------------------------------------------------------------------
# Double allocation
# --------------------------------------------------------------------------
def test_two_lines_for_the_same_category_are_caught():
    lines = [
        income("5000"),
        expense("800", "Groceries", category_id=10),
        expense("400", "Groceries", category_id=10),
    ]
    warnings = detect_double_allocation(lines)
    assert any(w.code == "duplicate_line" for w in warnings)
    assert any("1200.00" in (w.detail or "") for w in warnings)


def test_distinct_categories_are_not_flagged():
    lines = [expense("800", "Groceries", category_id=10),
             expense("400", "Fuel", category_id=11)]
    assert detect_double_allocation(lines) == []


def test_goal_funded_twice_is_flagged():
    lines = [
        Allocation("500", kind=CategoryKind.SAVINGS.value,
                   target=AllocationTarget.GOAL.value, label="Trip A", goal_id=7),
        Allocation("300", kind=CategoryKind.SAVINGS.value,
                   target=AllocationTarget.SAVINGS.value, label="Trip B", goal_id=7),
    ]
    warnings = detect_double_allocation(lines)
    assert any(w.code == "goal_double_funded" for w in warnings)


def test_debt_funded_twice_is_flagged():
    lines = [
        Allocation("500", kind=CategoryKind.DEBT.value,
                   target=AllocationTarget.DEBT.value, label="Card", debt_id=3),
        Allocation("100", kind=CategoryKind.EXPENSE.value,
                   target=AllocationTarget.EXPENSE.value, label="Card fee", debt_id=3),
    ]
    warnings = detect_double_allocation(lines)
    assert any(w.code == "debt_double_funded" for w in warnings)


def test_negative_and_zero_lines_are_reported():
    result = zero_based_summary([income("1000"), expense("-50", "Odd"),
                                 expense("0", "Placeholder"),
                                 expense("1050", "Rest")])
    codes = {w.code for w in result.warnings}
    assert "negative_line" in codes
    assert "zero_line" in codes


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def test_apply_to_reach_zero_suggests_a_new_amount():
    lines = [income("5000"), expense("3000", "Living", category_id=5)]
    result = zero_based_summary(lines)
    target = next(line for line in lines if line.category_id == 5)
    assert apply_to_reach_zero(lines, result, target.identity) == Decimal("5000.00")

    over = zero_based_summary([income("1000"), expense("1500", "Big", category_id=9)])
    line = expense("1500", "Big", category_id=9)
    assert apply_to_reach_zero([line], over, line.identity) == Decimal("1000.00")


def test_apply_to_reach_zero_never_goes_negative():
    lines = [income("100"), expense("900", "Huge", category_id=1)]
    result = zero_based_summary(lines)
    line = lines[1]
    assert apply_to_reach_zero(lines, result, line.identity) >= 0


def test_rate_helpers():
    assert savings_rate(Decimal("5000"), Decimal("1000")) == Decimal("20.00")
    assert savings_rate(Decimal("0"), Decimal("1000")) == Decimal("0.00")
    assert budget_utilisation(Decimal("800"), Decimal("1000")) == Decimal("125.00")


def test_carry_forward_respects_the_setting():
    result = zero_based_summary([income("1000"), expense("600")])
    assert carry_forward(result) == Decimal("400.00")
    assert carry_forward(result, carry_enabled=False) == Decimal("0.00")
