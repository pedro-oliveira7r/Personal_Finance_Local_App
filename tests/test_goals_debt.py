"""Savings goals and debt amortisation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from calculations.debt import (
    DebtInput,
    amortisation_schedule,
    compare_extra_payment,
    debt_alerts,
    interest_for_month,
    minimum_viable_payment,
    order_debts,
    simulate_strategy,
    strategy_comparison,
)
from calculations.goals import (
    compute_progress,
    goal_alerts,
    months_to_target,
    prioritise,
    projected_balance_at,
    required_contribution,
)
from constants import GoalStatus, PayoffStrategy


# ==========================================================================
# Goals
# ==========================================================================
def test_progress_percentage_and_remaining():
    progress = compute_progress(
        name="Emergency fund", target_amount="30000", current_amount="7500",
        planned_monthly="1000", today=date(2026, 8, 17),
    )
    assert progress.progress_pct == Decimal("25.00")
    assert progress.remaining == Decimal("22500.00")
    assert not progress.is_complete


def test_progress_is_capped_at_100_percent():
    progress = compute_progress(name="Trip", target_amount="1000",
                               current_amount="1500")
    assert progress.progress_pct == Decimal("100")
    assert progress.remaining == Decimal("0.00")
    assert progress.is_complete
    assert progress.on_track is True
    assert progress.status_icon == "✓"


def test_required_monthly_is_derived_from_the_target_date():
    progress = compute_progress(
        name="Car", target_amount="24000", current_amount="0",
        target_date=date(2028, 8, 17), today=date(2026, 8, 17),
    )
    assert progress.months_remaining == 24
    assert progress.required_monthly == Decimal("1000.00")


def test_target_date_in_the_past_demands_the_whole_remainder_now():
    progress = compute_progress(
        name="Late", target_amount="5000", current_amount="1000",
        target_date=date(2026, 1, 1), today=date(2026, 8, 17),
    )
    assert progress.months_remaining == 0
    assert progress.required_monthly == Decimal("4000.00")


def test_on_track_when_contributions_are_enough():
    progress = compute_progress(
        name="Trip", target_amount="12000", current_amount="0",
        planned_monthly="1000", target_date=date(2027, 9, 17),
        today=date(2026, 8, 17),
    )
    assert progress.on_track is True
    assert progress.shortfall_monthly == Decimal("0.00")
    assert progress.projected_completion is not None


def test_behind_schedule_reports_the_monthly_shortfall():
    progress = compute_progress(
        name="Trip", target_amount="12000", current_amount="0",
        planned_monthly="500", target_date=date(2027, 8, 17),
        today=date(2026, 8, 17),
    )
    assert progress.on_track is False
    assert progress.required_monthly == Decimal("1000.00")
    assert progress.shortfall_monthly == Decimal("500.00")
    assert progress.status_icon == "!"


def test_no_contributions_means_it_never_finishes():
    progress = compute_progress(
        name="Someday", target_amount="10000", current_amount="0",
        planned_monthly="0", target_date=date(2028, 1, 1), today=date(2026, 8, 17),
    )
    assert progress.projected_completion is None
    assert progress.on_track is False
    alerts = goal_alerts([progress])
    assert any("never" in message for _, message in alerts)


def test_average_contribution_is_used_when_nothing_is_planned():
    progress = compute_progress(
        name="Trip", target_amount="6000", current_amount="0",
        planned_monthly="0", average_monthly="500", today=date(2026, 8, 17),
    )
    assert progress.projected_completion is not None
    assert progress.on_track is True


def test_required_contribution_and_months_to_target():
    assert required_contribution(Decimal("1200"), 12) == Decimal("100.00")
    assert required_contribution(Decimal("1200"), 0) == Decimal("1200.00")
    assert months_to_target(Decimal("1000"), Decimal("300")) == 4  # rounds up
    assert months_to_target(Decimal("1000"), Decimal("0")) is None


def test_projected_balance_with_and_without_interest():
    plain = projected_balance_at(Decimal("1000"), Decimal("100"), 12)
    assert plain == Decimal("2200.00")
    with_interest = projected_balance_at(Decimal("1000"), Decimal("100"), 12,
                                        annual_rate_pct=Decimal("12"))
    assert with_interest > plain


def test_prioritise_funds_the_most_urgent_first():
    urgent = compute_progress(name="Urgent", target_amount="1000", current_amount="0",
                             target_date=date(2026, 10, 17), today=date(2026, 8, 17))
    later = compute_progress(name="Later", target_amount="5000", current_amount="0",
                            target_date=date(2029, 8, 17), today=date(2026, 8, 17))
    plan = prioritise([urgent, later], Decimal("600"))
    assert plan[0][0].name == "Urgent"
    assert plan[0][1] == Decimal("500.00")
    assert plan[1][1] == Decimal("100.00")


def test_prioritise_skips_completed_goals():
    done = compute_progress(name="Done", target_amount="100", current_amount="100")
    plan = prioritise([done], Decimal("500"))
    assert plan == []


def test_goal_alerts_celebrate_completion():
    done = compute_progress(name="Done", target_amount="100", current_amount="100")
    alerts = goal_alerts([done])
    assert alerts and alerts[0][0] == "success"


def test_paused_goals_are_not_alerted_on():
    paused = compute_progress(name="Paused", target_amount="1000", current_amount="0",
                             status=GoalStatus.PAUSED.value,
                             target_date=date(2027, 1, 1))
    assert goal_alerts([paused]) == []


# ==========================================================================
# Debt
# ==========================================================================
def test_interest_free_debt_divides_cleanly():
    result = amortisation_schedule(Decimal("1200"), Decimal("0"), Decimal("100"))
    assert result.months == 12
    assert result.total_interest == Decimal("0.00")
    assert result.total_paid == Decimal("1200.00")
    assert result.schedule[-1].closing_balance == Decimal("0.00")


def test_interest_bearing_debt_costs_more_than_the_principal():
    result = amortisation_schedule(Decimal("10000"), Decimal("24"), Decimal("500"))
    assert not result.never_pays_off
    assert result.total_interest > 0
    assert result.total_paid > Decimal("10000")
    assert result.schedule[-1].closing_balance == Decimal("0.00")
    assert result.interest_share_pct > 0


def test_final_payment_is_trimmed_to_land_on_zero():
    result = amortisation_schedule(Decimal("1050"), Decimal("0"), Decimal("100"))
    assert result.months == 11
    assert result.schedule[-1].payment == Decimal("50.00")
    assert result.schedule[-1].closing_balance == Decimal("0.00")


def test_payment_below_the_monthly_interest_never_clears():
    result = amortisation_schedule(Decimal("10000"), Decimal("120"), Decimal("500"))
    assert result.never_pays_off
    assert result.schedule == []
    assert result.monthly_interest_at_start == Decimal("1000.00")


def test_zero_payment_never_clears():
    result = amortisation_schedule(Decimal("5000"), Decimal("12"), Decimal("0"))
    assert result.never_pays_off


def test_already_cleared_debt_returns_immediately():
    result = amortisation_schedule(Decimal("0"), Decimal("50"), Decimal("100"),
                                   start_date=date(2026, 8, 1))
    assert result.months == 0
    assert result.payoff_date == date(2026, 8, 1)
    assert not result.never_pays_off


def test_principal_and_interest_add_up_to_the_payment():
    result = amortisation_schedule(Decimal("5000"), Decimal("18"), Decimal("300"))
    for row in result.schedule:
        assert row.principal + row.interest == row.payment
    assert result.schedule[0].interest > result.schedule[-1].interest


def test_cumulative_columns_are_consistent():
    result = amortisation_schedule(Decimal("3000"), Decimal("12"), Decimal("300"))
    assert result.schedule[-1].cumulative_interest == result.total_interest
    assert result.schedule[-1].cumulative_paid == result.total_paid


def test_due_dates_follow_the_chosen_day():
    result = amortisation_schedule(Decimal("500"), Decimal("0"), Decimal("100"),
                                   start_date=date(2026, 1, 31), due_day=31)
    dates = [row.due_date for row in result.schedule]
    assert dates[0] == date(2026, 1, 31)
    assert dates[1] == date(2026, 2, 28)


def test_extra_payment_saves_time_and_interest():
    debt = DebtInput(name="Card", balance="10000", annual_rate_pct="24",
                     planned_payment="500")
    comparison = compare_extra_payment(debt, Decimal("200"))
    assert comparison["months_saved"] > 0
    assert comparison["interest_saved"] > 0
    assert comparison["boosted"].months < comparison["base"].months


def test_effective_payment_prefers_planned_over_minimum():
    debt = DebtInput(name="Loan", balance="1000", minimum_payment="50",
                     planned_payment="200", extra_payment="30")
    assert debt.effective_payment == Decimal("230.00")
    minimum_only = DebtInput(name="Loan", balance="1000", minimum_payment="50")
    assert minimum_only.effective_payment == Decimal("50.00")


def test_minimum_viable_payment_is_the_monthly_interest():
    assert minimum_viable_payment(Decimal("10000"), Decimal("12")) == Decimal("100.00")
    assert interest_for_month(Decimal("10000"), Decimal("24")) == Decimal("200.00")


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------
def two_debts():
    return [
        DebtInput(name="Small expensive", balance="2000", annual_rate_pct="60",
                  minimum_payment="100"),
        DebtInput(name="Big cheap", balance="10000", annual_rate_pct="12",
                  minimum_payment="300"),
    ]


def test_avalanche_orders_by_rate_snowball_by_balance():
    avalanche = order_debts(two_debts(), PayoffStrategy.AVALANCHE.value)
    assert avalanche[0].name == "Small expensive"
    snowball = order_debts(two_debts(), PayoffStrategy.SNOWBALL.value)
    assert snowball[0].name == "Small expensive"

    reversed_case = [
        DebtInput(name="Small cheap", balance="500", annual_rate_pct="5",
                  minimum_payment="50"),
        DebtInput(name="Big expensive", balance="9000", annual_rate_pct="80",
                  minimum_payment="400"),
    ]
    assert order_debts(reversed_case,
                       PayoffStrategy.AVALANCHE.value)[0].name == "Big expensive"
    assert order_debts(reversed_case,
                       PayoffStrategy.SNOWBALL.value)[0].name == "Small cheap"


def test_avalanche_never_costs_more_interest_than_snowball():
    results = strategy_comparison(two_debts(), extra_pool=Decimal("400"))
    avalanche = results[PayoffStrategy.AVALANCHE.value]
    snowball = results[PayoffStrategy.SNOWBALL.value]
    assert not avalanche.never_pays_off
    assert avalanche.total_interest <= snowball.total_interest


def test_extra_payments_shorten_the_whole_plan():
    without = simulate_strategy(two_debts(), PayoffStrategy.AVALANCHE.value,
                                extra_pool=Decimal("0"))
    with_extra = simulate_strategy(two_debts(), PayoffStrategy.AVALANCHE.value,
                                   extra_pool=Decimal("500"))
    assert with_extra.months < without.months
    assert with_extra.total_interest < without.total_interest


def test_strategy_records_the_payoff_order():
    result = simulate_strategy(two_debts(), PayoffStrategy.AVALANCHE.value,
                               extra_pool=Decimal("500"))
    assert result.payoff_order[0] == "Small expensive"
    assert set(result.per_debt_months) == {"Small expensive", "Big cheap"}


def test_strategies_use_the_payment_you_actually_committed_to():
    """A planned payment above the minimum must not be simulated as the minimum.

    The demo credit card carries a 280 minimum but a 3.200 planned payment. Read
    as "minimum only", it looks like it can never be cleared; read correctly, it
    clears in months.
    """
    debts = [DebtInput(name="Card", balance="5200", annual_rate_pct="180",
                       minimum_payment="280", planned_payment="3200")]
    real = simulate_strategy(debts, PayoffStrategy.AVALANCHE.value)
    assert not real.never_pays_off
    assert real.stuck == []
    assert real.monthly_outlay == Decimal("3200.00")

    # The minimums-only baseline genuinely never clears — that is its lesson.
    baseline = simulate_strategy(debts, PayoffStrategy.MINIMUM_ONLY.value)
    assert baseline.never_pays_off
    assert baseline.stuck == ["Card"]
    assert baseline.monthly_outlay == Decimal("280.00")


def test_per_debt_extra_counts_toward_the_baseline():
    debts = [DebtInput(name="Loan", balance="1000", annual_rate_pct="0",
                       minimum_payment="50", planned_payment="100",
                       extra_payment="150")]
    result = simulate_strategy(debts, PayoffStrategy.AVALANCHE.value)
    assert result.monthly_outlay == Decimal("250.00")
    assert result.months == 4


def test_a_debt_that_outgrows_its_payment_does_not_overflow():
    """The regression: 180%/year against a minimum that only covers part of it.

    Compounded over the full 600-month horizon this produces a number with
    hundreds of digits and ``quantize`` raises InvalidOperation, taking the whole
    screen down. The simulation must notice the stall and stop.
    """
    debts = [
        DebtInput(name="Card", balance="3000", annual_rate_pct="180",
                  minimum_payment="280", planned_payment="280"),
        DebtInput(name="Car", balance="23000", annual_rate_pct="21.9",
                  minimum_payment="948", planned_payment="948"),
    ]
    for strategy in (PayoffStrategy.AVALANCHE.value, PayoffStrategy.SNOWBALL.value,
                     PayoffStrategy.MINIMUM_ONLY.value):
        result = simulate_strategy(debts, strategy)
        assert result.never_pays_off
        assert result.stuck == ["Card"]
        assert result.months < 30, "it should give up quickly, not grind to 600"


def test_strategy_comparison_survives_a_hopeless_debt():
    debts = [DebtInput(name="Trap", balance="5000", annual_rate_pct="200",
                       minimum_payment="100")]
    results = strategy_comparison(debts)          # must not raise
    assert all(result.never_pays_off for result in results.values())
    assert all(result.stuck == ["Trap"] for result in results.values())


def test_enough_extra_rescues_a_stalled_debt():
    debts = [
        DebtInput(name="Card", balance="3000", annual_rate_pct="180",
                  minimum_payment="280", planned_payment="280"),
        DebtInput(name="Car", balance="23000", annual_rate_pct="21.9",
                  minimum_payment="948", planned_payment="948"),
    ]
    rescued = simulate_strategy(debts, PayoffStrategy.AVALANCHE.value,
                                extra_pool=Decimal("500"))
    assert not rescued.never_pays_off
    assert rescued.stuck == []
    assert rescued.payoff_order[0] == "Card"


def test_minimums_only_ignores_the_extra_pool():
    """It is the do-nothing baseline — extra money and freed minimums do not apply."""
    debts = [
        DebtInput(name="A", balance="1000", annual_rate_pct="0", minimum_payment="100"),
        DebtInput(name="B", balance="2000", annual_rate_pct="0", minimum_payment="100"),
    ]
    baseline = simulate_strategy(debts, PayoffStrategy.MINIMUM_ONLY.value,
                                 extra_pool=Decimal("400"))
    rolled = simulate_strategy(debts, PayoffStrategy.SNOWBALL.value,
                               extra_pool=Decimal("400"))
    assert baseline.months == 20        # B alone takes 2000 / 100
    assert rolled.months < baseline.months
    assert baseline.monthly_outlay == Decimal("200.00")
    assert rolled.monthly_outlay == Decimal("600.00")


def test_strategy_with_no_debts_is_empty():
    result = simulate_strategy([], PayoffStrategy.AVALANCHE.value)
    assert result.months == 0
    assert not result.never_pays_off


def test_alerts_flag_a_payment_that_only_covers_interest():
    debts = [DebtInput(name="Trap", balance="10000", annual_rate_pct="120",
                       planned_payment="900")]
    alerts = debt_alerts(debts)
    assert alerts and alerts[0][0] == "critical"
    assert "never" in alerts[0][1]


def test_alerts_flag_a_missing_payment():
    debts = [DebtInput(name="Ignored", balance="5000", annual_rate_pct="10")]
    alerts = debt_alerts(debts)
    assert any("no payment planned" in message for _, message in alerts)


def test_alerts_flag_a_punitive_rate():
    debts = [DebtInput(name="Revolving", balance="1000", annual_rate_pct="180",
                       planned_payment="900")]
    alerts = debt_alerts(debts)
    assert any("annual rate" in message for _, message in alerts)


# ==========================================================================
# Against the database
# ==========================================================================
def test_goal_contribution_creates_a_transfer_and_moves_progress(session, accounts):
    from services import goal_service

    goal = goal_service.create_goal(session, {
        "name": "Trip", "target_amount": "5000", "planned_monthly": "500",
        "account_id": accounts["Savings"].id, "start_date": date(2026, 1, 1),
    })
    session.commit()

    txn = goal_service.record_contribution(
        session, goal.id, Decimal("500"), on_date=date(2026, 8, 5),
        from_account_id=accounts["Checking"].id)
    session.commit()

    assert txn.kind == "transfer"
    assert txn.goal_id == goal.id
    assert goal_service.current_amount(session, goal) == Decimal("500.00")

    progress = goal_service.progress_for(session, goal, today=date(2026, 8, 17))
    assert progress.progress_pct == Decimal("10.00")


def test_debt_payment_reduces_the_balance(session, accounts):
    from services import debt_service

    debt = debt_service.create_debt(session, {
        "name": "Loan", "principal_balance": "1000", "interest_rate": "0",
        "minimum_payment": "100", "balance_as_of": date(2026, 8, 1),
    })
    session.commit()

    result = debt_service.record_payment(
        session, debt.id, Decimal("300"), on_date=date(2026, 8, 10),
        from_account_id=accounts["Checking"].id)
    session.commit()

    assert result["new_balance"] == Decimal("700.00")
    assert result["interest_applied"] == Decimal("0.00")
    assert not result["cleared"]


def test_debt_payment_applies_accrued_interest(session, accounts):
    from services import debt_service

    debt = debt_service.create_debt(session, {
        "name": "Card", "principal_balance": "1000", "interest_rate": "12",
        "minimum_payment": "100", "balance_as_of": date(2026, 6, 1),
    })
    session.commit()

    result = debt_service.record_payment(
        session, debt.id, Decimal("100"), on_date=date(2026, 8, 1),
        from_account_id=accounts["Checking"].id)
    session.commit()

    # Two months at 1% a month, compounded: 10.00 then 10.10.
    assert result["interest_applied"] == Decimal("20.10")
    assert result["new_balance"] == Decimal("920.10")


def test_clearing_a_debt_marks_it_inactive(session, accounts):
    from services import debt_service

    debt = debt_service.create_debt(session, {
        "name": "Nearly done", "principal_balance": "200", "interest_rate": "0",
        "minimum_payment": "200", "balance_as_of": date(2026, 8, 1),
    })
    session.commit()
    result = debt_service.record_payment(
        session, debt.id, Decimal("200"), on_date=date(2026, 8, 5),
        from_account_id=accounts["Checking"].id)
    session.commit()
    assert result["cleared"]
    assert debt_service.get_debt(session, debt.id).is_active is False


def test_debt_linked_to_an_account_uses_the_account_balance(session, accounts):
    from services import debt_service
    from services import transaction_service as txs

    debt = debt_service.create_debt(session, {
        "name": "Card", "principal_balance": "0", "interest_rate": "0",
        "minimum_payment": "100", "account_id": accounts["Card"].id,
    })
    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 3), "description": "Charge",
        "amount": "450", "kind": "expense", "account_id": accounts["Card"].id,
    })
    session.commit()
    assert debt_service.effective_balance(session, debt) == Decimal("450.00")
