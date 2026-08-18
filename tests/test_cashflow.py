"""Account balances, transfers and income-availability (late-income) rules."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from calculations.cashflow import (
    AccountInfo,
    CashTxn,
    account_balance,
    account_movement,
    available_income_for_period,
    balances_as_of,
    carry_in_for_period,
    cash_available,
    cashflow_series,
    credit_utilisation,
    income_timing,
    period_cashflow,
    resolve_availability_date,
    upcoming_outflows,
)
from calculations.periods import make_period
from constants import AccountType, AvailabilityRule, BalanceMode, TxnKind, TxnStatus


CHECKING = AccountInfo(id=1, type=AccountType.CHECKING.value, name="Checking",
                       opening_balance=Decimal("1000"), opening_date=date(2026, 1, 1))
SAVINGS = AccountInfo(id=2, type=AccountType.SAVINGS.value, name="Savings",
                      opening_balance=Decimal("500"), opening_date=date(2026, 1, 1))
CARD = AccountInfo(id=3, type=AccountType.CREDIT_CARD.value, name="Card",
                   opening_balance=Decimal("200"), opening_date=date(2026, 1, 1),
                   include_in_cash=False, credit_limit=Decimal("2000"))
BROKER = AccountInfo(id=4, type=AccountType.INVESTMENT.value, name="Broker",
                     opening_balance=Decimal("3000"), opening_date=date(2026, 1, 1),
                     include_in_cash=False)
ALL = [CHECKING, SAVINGS, CARD, BROKER]


def txn(**kwargs) -> CashTxn:
    base = dict(txn_date=date(2026, 8, 10), amount=Decimal("100"),
                kind=TxnKind.EXPENSE.value, account_id=1)
    base.update(kwargs)
    return CashTxn(**base)


# --------------------------------------------------------------------------
# Balances
# --------------------------------------------------------------------------
def test_income_and_expense_move_an_asset_account():
    movements = [
        txn(kind=TxnKind.INCOME.value, amount=Decimal("2000")),
        txn(kind=TxnKind.EXPENSE.value, amount=Decimal("300")),
    ]
    assert account_balance(CHECKING, movements) == Decimal("2700.00")


def test_expense_on_a_credit_card_increases_what_you_owe():
    movements = [txn(account_id=3, amount=Decimal("450"))]
    balance = account_balance(CARD, movements)
    assert balance == Decimal("-650.00")  # 200 opening + 450 charged
    assert credit_utilisation(CARD, balance) == Decimal("32.50")


def test_paying_the_card_reduces_debt_and_cash_by_the_same_amount():
    movements = [
        txn(kind=TxnKind.TRANSFER.value, amount=Decimal("200"),
            account_id=1, to_account_id=3),
    ]
    assert account_balance(CHECKING, movements) == Decimal("800.00")
    assert account_balance(CARD, movements) == Decimal("0.00")


def test_transfer_between_cash_accounts_leaves_total_cash_unchanged():
    before = cash_available(ALL, [])
    movements = [txn(kind=TxnKind.TRANSFER.value, amount=Decimal("400"),
                     account_id=1, to_account_id=2)]
    assert cash_available(ALL, movements) == before
    assert account_balance(CHECKING, movements) == Decimal("600.00")
    assert account_balance(SAVINGS, movements) == Decimal("900.00")


def test_transfer_to_a_non_cash_account_reduces_cash():
    movements = [txn(kind=TxnKind.TRANSFER.value, amount=Decimal("500"),
                     account_id=1, to_account_id=4)]
    # 1000 checking + 500 savings, less the 500 that left the cash pool.
    assert cash_available(ALL, movements) == Decimal("1000.00")
    assert account_balance(BROKER, movements) == Decimal("3500.00")


def test_transfers_are_never_income_or_expense():
    movements = [txn(kind=TxnKind.TRANSFER.value, amount=Decimal("300"),
                     account_id=1, to_account_id=2)]
    flow = period_cashflow(make_period(2026, 8), ALL, movements)
    assert flow.income_received == Decimal("0.00")
    assert flow.expenses_paid == Decimal("0.00")
    assert flow.net_flow == Decimal("0.00")


def test_planned_transactions_are_excluded_from_real_balances():
    movements = [txn(status=TxnStatus.PLANNED.value, amount=Decimal("900"))]
    assert account_balance(CHECKING, movements) == Decimal("1000.00")
    assert account_balance(CHECKING, movements, include_planned=True) == Decimal("100.00")


def test_void_transactions_are_ignored_everywhere():
    movements = [txn(status=TxnStatus.VOID.value, amount=Decimal("900"))]
    assert account_balance(CHECKING, movements, include_planned=True) == Decimal("1000.00")


def test_balance_as_of_a_date_ignores_later_movements():
    movements = [
        txn(txn_date=date(2026, 8, 5), actual_date=date(2026, 8, 5),
            amount=Decimal("100")),
        txn(txn_date=date(2026, 8, 25), actual_date=date(2026, 8, 25),
            amount=Decimal("200")),
    ]
    assert account_balance(CHECKING, movements, as_of=date(2026, 8, 10)) == \
        Decimal("900.00")
    assert account_balance(CHECKING, movements, as_of=date(2026, 8, 31)) == \
        Decimal("700.00")


def test_opening_balance_only_counts_from_its_own_date():
    late = AccountInfo(id=9, type=AccountType.CHECKING.value,
                       opening_balance=Decimal("800"), opening_date=date(2026, 6, 1))
    assert account_balance(late, [], as_of=date(2026, 1, 1)) == Decimal("0.00")
    assert account_balance(late, [], as_of=date(2026, 6, 1)) == Decimal("800.00")


def test_negative_cash_balance_is_reported_not_clamped():
    movements = [txn(amount=Decimal("1500"))]
    assert account_balance(CHECKING, movements) == Decimal("-500.00")
    # An overdrawn account offsets a positive one rather than being floored at zero.
    assert cash_available(ALL, movements) == Decimal("0.00")


def test_manual_valuation_wins_and_respects_its_date():
    property_account = AccountInfo(
        id=10, type=AccountType.OTHER_ASSET.value, name="Flat",
        opening_balance=Decimal("0"), opening_date=date(2026, 1, 1),
        balance_mode=BalanceMode.MANUAL.value, include_in_cash=False,
        valuations=[(date(2026, 1, 31), Decimal("300000")),
                    (date(2026, 6, 30), Decimal("320000"))],
    )
    assert account_balance(property_account, []) == Decimal("320000.00")
    assert account_balance(property_account, [], as_of=date(2026, 3, 1)) == \
        Decimal("300000.00")
    assert account_balance(property_account, [], as_of=date(2025, 12, 1)) == \
        Decimal("0.00")


def test_manual_valuation_on_a_liability_is_negative():
    loan = AccountInfo(id=11, type=AccountType.LOAN.value,
                       balance_mode=BalanceMode.MANUAL.value,
                       valuations=[(date(2026, 1, 1), Decimal("15000"))])
    assert account_balance(loan, []) == Decimal("-15000.00")


def test_balances_as_of_returns_every_account():
    result = balances_as_of(ALL, [])
    assert set(result) == {1, 2, 3, 4}
    assert result[3] == Decimal("-200.00")


def test_movement_helper_is_asset_style():
    movements = [txn(kind=TxnKind.INCOME.value, amount=Decimal("50")),
                 txn(kind=TxnKind.EXPENSE.value, amount=Decimal("20"))]
    assert account_movement(movements, 1) == Decimal("30.00")


# --------------------------------------------------------------------------
# Income availability — the late paycheck problem
# --------------------------------------------------------------------------
def january_salary(actual_day: int = 31) -> CashTxn:
    return CashTxn(
        txn_date=date(2026, 1, 31), amount=Decimal("5000"),
        kind=TxnKind.INCOME.value, account_id=1,
        actual_date=date(2026, 1, actual_day),
    )


def test_earned_period_rule_funds_the_month_it_belongs_to():
    salary = january_salary()
    assert resolve_availability_date(
        salary, AvailabilityRule.EARNED_PERIOD.value) == date(2026, 1, 31)
    assert available_income_for_period(
        [salary], make_period(2026, 1), AvailabilityRule.EARNED_PERIOD.value
    ) == Decimal("5000.00")
    assert available_income_for_period(
        [salary], make_period(2026, 2), AvailabilityRule.EARNED_PERIOD.value
    ) == Decimal("0.00")


def test_next_period_rule_pushes_the_paycheck_forward():
    salary = january_salary()
    resolved = resolve_availability_date(salary, AvailabilityRule.NEXT_PERIOD.value)
    assert resolved == date(2026, 2, 1)
    assert available_income_for_period(
        [salary], make_period(2026, 2), AvailabilityRule.NEXT_PERIOD.value
    ) == Decimal("5000.00")
    assert available_income_for_period(
        [salary], make_period(2026, 1), AvailabilityRule.NEXT_PERIOD.value
    ) == Decimal("0.00")


def test_actual_date_rule_follows_the_deposit():
    late = CashTxn(txn_date=date(2026, 1, 31), amount=Decimal("5000"),
                   kind=TxnKind.INCOME.value, account_id=1,
                   actual_date=date(2026, 2, 3))
    assert resolve_availability_date(
        late, AvailabilityRule.ACTUAL_DATE.value) == date(2026, 2, 3)
    assert available_income_for_period(
        [late], make_period(2026, 2), AvailabilityRule.ACTUAL_DATE.value
    ) == Decimal("5000.00")


def test_cutoff_rule_splits_on_the_chosen_day():
    early = january_salary(actual_day=20)
    late = january_salary(actual_day=28)
    rule = AvailabilityRule.CUTOFF_DAY.value
    assert available_income_for_period([early], make_period(2026, 1), rule,
                                       cutoff_day=25) == Decimal("5000.00")
    assert available_income_for_period([late], make_period(2026, 1), rule,
                                       cutoff_day=25) == Decimal("0.00")
    assert available_income_for_period([late], make_period(2026, 2), rule,
                                       cutoff_day=25) == Decimal("5000.00")


def test_per_transaction_override_beats_the_global_rule():
    salary = january_salary()
    salary.availability_date = date(2026, 3, 5)
    for rule in AvailabilityRule.values():
        assert resolve_availability_date(salary, rule) == date(2026, 3, 5)
    assert available_income_for_period(
        [salary], make_period(2026, 3), AvailabilityRule.EARNED_PERIOD.value
    ) == Decimal("5000.00")


def test_income_excluded_from_budget_is_not_available():
    salary = january_salary()
    salary.exclude_from_budget = True
    assert available_income_for_period(
        [salary], make_period(2026, 1), AvailabilityRule.EARNED_PERIOD.value
    ) == Decimal("0.00")


def test_planned_income_can_be_left_out_of_availability():
    expected = CashTxn(txn_date=date(2026, 1, 20), amount=Decimal("1000"),
                       kind=TxnKind.INCOME.value, account_id=1,
                       status=TxnStatus.PLANNED.value)
    period = make_period(2026, 1)
    rule = AvailabilityRule.EARNED_PERIOD.value
    assert available_income_for_period([expected], period, rule) == Decimal("1000.00")
    assert available_income_for_period([expected], period, rule,
                                       include_planned=False) == Decimal("0.00")


def test_income_timing_distinguishes_earned_received_available_and_late():
    period = make_period(2026, 1)
    on_time = CashTxn(txn_date=date(2026, 1, 10), amount=Decimal("1000"),
                      kind=TxnKind.INCOME.value, account_id=1,
                      actual_date=date(2026, 1, 10))
    late = CashTxn(txn_date=date(2026, 1, 31), amount=Decimal("5000"),
                   kind=TxnKind.INCOME.value, account_id=1,
                   actual_date=date(2026, 2, 3))
    expected = CashTxn(txn_date=date(2026, 1, 25), amount=Decimal("800"),
                       kind=TxnKind.INCOME.value, account_id=1,
                       status=TxnStatus.PLANNED.value)

    timing = income_timing([on_time, late, expected], period,
                           AvailabilityRule.ACTUAL_DATE.value)
    assert timing.earned == Decimal("6800.00")
    assert timing.received == Decimal("1000.00")
    assert timing.late == Decimal("5000.00")
    assert timing.expected == Decimal("800.00")
    # Received cash (1000) plus the payment still expected inside the period (800):
    # a planned payment dated in January is budgetable in January.
    assert timing.available == Decimal("1800.00")


# --------------------------------------------------------------------------
# Period cash flow
# --------------------------------------------------------------------------
def test_period_cashflow_chains_opening_to_closing():
    movements = [
        CashTxn(txn_date=date(2026, 8, 5), actual_date=date(2026, 8, 5),
                amount=Decimal("4000"), kind=TxnKind.INCOME.value, account_id=1),
        CashTxn(txn_date=date(2026, 8, 10), actual_date=date(2026, 8, 10),
                amount=Decimal("1500"), kind=TxnKind.EXPENSE.value, account_id=1),
    ]
    flow = period_cashflow(make_period(2026, 8), ALL, movements)
    assert flow.opening_cash == Decimal("1500.00")  # 1000 + 500 wallet-less
    assert flow.income_received == Decimal("4000.00")
    assert flow.expenses_paid == Decimal("1500.00")
    assert flow.net_flow == Decimal("2500.00")
    assert flow.closing_cash == Decimal("4000.00")


def test_card_spending_does_not_reduce_cash_in_the_period():
    movements = [CashTxn(txn_date=date(2026, 8, 6), actual_date=date(2026, 8, 6),
                         amount=Decimal("600"), kind=TxnKind.EXPENSE.value,
                         account_id=3)]
    flow = period_cashflow(make_period(2026, 8), ALL, movements)
    assert flow.expenses_paid == Decimal("0.00")
    assert flow.net_flow == Decimal("0.00")


def test_transaction_crossing_a_period_boundary_lands_by_cash_date():
    movement = CashTxn(txn_date=date(2026, 7, 31), actual_date=date(2026, 8, 2),
                       amount=Decimal("900"), kind=TxnKind.EXPENSE.value, account_id=1)
    july = period_cashflow(make_period(2026, 7), ALL, [movement])
    august = period_cashflow(make_period(2026, 8), ALL, [movement])
    assert july.expenses_paid == Decimal("0.00")
    assert august.expenses_paid == Decimal("900.00")


def test_savings_and_investment_categories_are_tracked_separately():
    movements = [
        CashTxn(txn_date=date(2026, 8, 3), actual_date=date(2026, 8, 3),
                amount=Decimal("500"), kind=TxnKind.EXPENSE.value, account_id=1,
                category_id=50, category_kind="savings"),
        CashTxn(txn_date=date(2026, 8, 4), actual_date=date(2026, 8, 4),
                amount=Decimal("300"), kind=TxnKind.EXPENSE.value, account_id=1,
                category_id=51, category_kind="investment"),
        CashTxn(txn_date=date(2026, 8, 5), actual_date=date(2026, 8, 5),
                amount=Decimal("2000"), kind=TxnKind.INCOME.value, account_id=1),
    ]
    flow = period_cashflow(make_period(2026, 8), ALL, movements)
    assert flow.savings_contributed == Decimal("500.00")
    assert flow.investments_contributed == Decimal("300.00")
    assert flow.core_expenses == Decimal("0.00")
    assert flow.savings_rate == Decimal("40.00")


def test_zero_income_period_has_a_zero_savings_rate():
    flow = period_cashflow(make_period(2026, 8), ALL, [])
    assert flow.income_received == Decimal("0.00")
    assert flow.savings_rate == Decimal("0.00")
    assert flow.closing_cash == flow.opening_cash


def test_cashflow_series_chains_and_marks_actual_versus_forecast():
    periods = [make_period(2026, 7), make_period(2026, 8), make_period(2026, 9)]
    movements = [
        CashTxn(txn_date=date(2026, 7, 5), actual_date=date(2026, 7, 5),
                amount=Decimal("1000"), kind=TxnKind.INCOME.value, account_id=1),
        CashTxn(txn_date=date(2026, 9, 5), amount=Decimal("2000"),
                kind=TxnKind.INCOME.value, account_id=1,
                status=TxnStatus.PLANNED.value),
    ]
    series = cashflow_series(periods, ALL, movements, today=date(2026, 8, 15))
    assert [row.is_actual for row in series] == [True, False, False]
    for previous, current in zip(series, series[1:]):
        assert current.opening_cash == previous.closing_cash
    assert series[-1].closing_cash == Decimal("4500.00")


def test_carry_in_is_the_balance_the_day_before_the_period():
    movements = [CashTxn(txn_date=date(2026, 7, 20), actual_date=date(2026, 7, 20),
                         amount=Decimal("250"), kind=TxnKind.INCOME.value,
                         account_id=1)]
    assert carry_in_for_period(make_period(2026, 8), ALL, movements) == Decimal("1750.00")


def test_upcoming_outflows_are_sorted_by_size():
    movements = [
        CashTxn(txn_date=date(2026, 8, 20), amount=Decimal("100"),
                kind=TxnKind.EXPENSE.value, account_id=1,
                status=TxnStatus.PLANNED.value),
        CashTxn(txn_date=date(2026, 8, 25), amount=Decimal("900"),
                kind=TxnKind.EXPENSE.value, account_id=1,
                status=TxnStatus.PLANNED.value),
        CashTxn(txn_date=date(2026, 12, 1), amount=Decimal("500"),
                kind=TxnKind.EXPENSE.value, account_id=1,
                status=TxnStatus.PLANNED.value),
    ]
    upcoming = upcoming_outflows(movements, date(2026, 8, 15), days=30)
    assert [item.amount for item in upcoming] == [Decimal("900.00"), Decimal("100.00")]
