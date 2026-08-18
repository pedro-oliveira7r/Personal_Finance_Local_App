"""Transaction service against a real database: validation, duplicates, undo."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from constants import TxnKind, TxnStatus
from services import account_service, category_service
from services import transaction_service as txs
from services.common import ConflictError, NotFoundError, ServiceError


def make(session, **kwargs):
    payload = dict(txn_date=date(2026, 8, 10), description="Supermarket",
                   amount=Decimal("150"), kind=TxnKind.EXPENSE.value)
    payload.update(kwargs)
    return txs.create_transaction(session, payload)


# --------------------------------------------------------------------------
# Storage integrity
# --------------------------------------------------------------------------
def test_amount_round_trips_exactly(session, accounts):
    created = make(session, amount="1234.56", account_id=accounts["Checking"].id)
    session.commit()
    fetched = txs.get_transaction(session, created.id)
    assert fetched.amount == Decimal("1234.56")
    assert isinstance(fetched.amount, Decimal)


def test_many_small_amounts_sum_exactly(session, accounts):
    for _ in range(100):
        make(session, amount="0.07", account_id=accounts["Checking"].id,
             description=f"Coffee {_}")
    session.commit()
    rows = txs.list_transactions(session, txs.TxnFilter())
    total = sum((row.amount for row in rows), Decimal("0"))
    assert total == Decimal("7.00")


def test_amount_must_be_positive(session, accounts):
    with pytest.raises(Exception):
        make(session, amount="-50", account_id=accounts["Checking"].id)
    with pytest.raises(Exception):
        make(session, amount="0", account_id=accounts["Checking"].id)


def test_account_is_required(session):
    with pytest.raises(Exception):
        make(session, account_id=None)


# --------------------------------------------------------------------------
# Category coherence
# --------------------------------------------------------------------------
def test_income_category_cannot_be_used_for_an_expense(session, accounts, categories):
    with pytest.raises(ServiceError):
        make(session, account_id=accounts["Checking"].id,
             category_id=categories["salary"].id)


def test_expense_category_cannot_be_used_for_income(session, accounts, categories):
    with pytest.raises(ServiceError):
        make(session, kind=TxnKind.INCOME.value, account_id=accounts["Checking"].id,
             category_id=categories["groceries"].id)


def test_matching_category_is_accepted(session, accounts, categories):
    created = make(session, account_id=accounts["Checking"].id,
                   category_id=categories["groceries"].id)
    assert created.category_id == categories["groceries"].id


# --------------------------------------------------------------------------
# Duplicate protection
# --------------------------------------------------------------------------
def test_identical_transaction_is_blocked(session, accounts):
    make(session, account_id=accounts["Checking"].id)
    session.commit()
    with pytest.raises(ConflictError):
        make(session, account_id=accounts["Checking"].id)


def test_duplicate_can_be_forced_through(session, accounts):
    make(session, account_id=accounts["Checking"].id)
    session.commit()
    second = txs.create_transaction(session, {
        "txn_date": date(2026, 8, 10), "description": "Supermarket",
        "amount": Decimal("150"), "kind": TxnKind.EXPENSE.value,
        "account_id": accounts["Checking"].id,
    }, allow_duplicate=True)
    assert second.id is not None


def test_near_duplicates_are_surfaced_without_blocking(session, accounts):
    make(session, account_id=accounts["Checking"].id)
    session.commit()
    matches = txs.find_duplicates(session, date(2026, 8, 12), Decimal("150"),
                                  "Different text", accounts["Checking"].id,
                                  TxnKind.EXPENSE.value)
    assert len(matches) == 1
    # A different amount on a nearby date is not a duplicate.
    assert txs.find_duplicates(session, date(2026, 8, 12), Decimal("151"),
                               "Supermarket", accounts["Checking"].id,
                               TxnKind.EXPENSE.value) == []


def test_fingerprint_ignores_case_accents_and_punctuation(session):
    a = txs.fingerprint(date(2026, 1, 1), Decimal("10"), "Café  São Paulo!",
                        1, TxnKind.EXPENSE.value)
    b = txs.fingerprint(date(2026, 1, 1), Decimal("10"), "cafe sao paulo", 1,
                        TxnKind.EXPENSE.value)
    assert a == b


def test_different_accounts_are_not_duplicates(session, accounts):
    make(session, account_id=accounts["Checking"].id)
    session.commit()
    other = make(session, account_id=accounts["Wallet"].id)
    assert other.id is not None


# --------------------------------------------------------------------------
# Transfers
# --------------------------------------------------------------------------
def test_transfer_requires_two_distinct_accounts(session, accounts):
    with pytest.raises(Exception):
        txs.create_transfer(session, {
            "txn_date": date(2026, 8, 1), "amount": Decimal("100"),
            "from_account_id": accounts["Checking"].id,
            "to_account_id": accounts["Checking"].id,
        })


def test_transfer_has_no_category(session, accounts, categories):
    transfer = txs.create_transfer(session, {
        "txn_date": date(2026, 8, 1), "amount": Decimal("300"),
        "from_account_id": accounts["Checking"].id,
        "to_account_id": accounts["Savings"].id,
        "description": "To savings",
    })
    assert transfer.kind == TxnKind.TRANSFER.value
    assert transfer.category_id is None
    assert transfer.to_account_id == accounts["Savings"].id


def test_transfer_is_excluded_from_income_and_expense_actuals(session, accounts,
                                                             categories):
    from calculations.periods import make_period

    txs.create_transfer(session, {
        "txn_date": date(2026, 8, 1), "amount": Decimal("300"),
        "from_account_id": accounts["Checking"].id,
        "to_account_id": accounts["Savings"].id,
    })
    make(session, account_id=accounts["Checking"].id, amount="100",
         category_id=categories["groceries"].id)
    session.commit()

    actuals = txs.actuals_for_period(session, make_period(2026, 8))
    assert actuals.expense_total == Decimal("100.00")
    assert actuals.income_total == Decimal("0.00")


# --------------------------------------------------------------------------
# Planned → completed
# --------------------------------------------------------------------------
def test_completing_a_planned_transaction(session, accounts):
    planned = make(session, status=TxnStatus.PLANNED.value,
                   account_id=accounts["Checking"].id)
    session.commit()
    assert planned.actual_date is None

    completed = txs.complete_transaction(session, planned.id,
                                         actual_date=date(2026, 8, 12))
    assert completed.status == TxnStatus.COMPLETED.value
    assert completed.actual_date == date(2026, 8, 12)


def test_completing_with_a_corrected_amount(session, accounts):
    planned = make(session, status=TxnStatus.PLANNED.value,
                   account_id=accounts["Checking"].id)
    session.commit()
    completed = txs.complete_transaction(session, planned.id,
                                         actual_amount=Decimal("172.40"))
    assert completed.amount == Decimal("172.40")


def test_reverting_to_planned_clears_the_payment_date(session, accounts):
    created = make(session, account_id=accounts["Checking"].id)
    session.commit()
    reverted = txs.revert_to_planned(session, created.id)
    assert reverted.status == TxnStatus.PLANNED.value
    assert reverted.actual_date is None


def test_voiding_keeps_the_record_but_drops_it_from_lists(session, accounts):
    created = make(session, account_id=accounts["Checking"].id)
    session.commit()
    txs.void_transaction(session, created.id)
    session.commit()
    assert txs.count_transactions(session, txs.TxnFilter(
        statuses=[TxnStatus.COMPLETED.value])) == 0
    assert txs.get_transaction(session, created.id).status == TxnStatus.VOID.value


# --------------------------------------------------------------------------
# Soft delete and undo
# --------------------------------------------------------------------------
def test_delete_is_reversible(session, accounts):
    created = make(session, account_id=accounts["Checking"].id)
    session.commit()

    txs.delete_transaction(session, created.id)
    session.commit()
    assert txs.count_transactions(session, txs.TxnFilter()) == 0
    assert txs.count_transactions(session, txs.TxnFilter(only_deleted=True)) == 1

    txs.restore_transaction(session, created.id)
    session.commit()
    assert txs.count_transactions(session, txs.TxnFilter()) == 1


def test_delete_files_a_recycle_bin_entry(session, accounts):
    from services.common import list_recycle_bin

    created = make(session, account_id=accounts["Checking"].id)
    session.commit()
    txs.delete_transaction(session, created.id)
    session.commit()
    entries = list_recycle_bin(session)
    assert any(entry.entity_type == "transaction" for entry in entries)


def test_purge_permanently_removes(session, accounts):
    created = make(session, account_id=accounts["Checking"].id)
    session.commit()
    txs.delete_transaction(session, created.id)
    session.commit()
    assert txs.purge_deleted(session) == 1
    session.commit()
    with pytest.raises(NotFoundError):
        txs.get_transaction(session, created.id)


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------
@pytest.fixture()
def dataset(session, accounts, categories):
    make(session, txn_date=date(2026, 7, 5), description="July food", amount="200",
         account_id=accounts["Checking"].id, category_id=categories["groceries"].id,
         tags="essential")
    make(session, txn_date=date(2026, 8, 5), description="August food", amount="300",
         account_id=accounts["Checking"].id, category_id=categories["groceries"].id,
         tags="essential, weekly")
    make(session, txn_date=date(2026, 8, 20), description="Salary", amount="5000",
         kind=TxnKind.INCOME.value, account_id=accounts["Checking"].id,
         category_id=categories["salary"].id)
    make(session, txn_date=date(2026, 8, 25), description="Wallet snack", amount="15",
         account_id=accounts["Wallet"].id)
    session.commit()
    return True


def test_date_filter(session, dataset):
    august = txs.list_transactions(session, txs.TxnFilter(
        start=date(2026, 8, 1), end=date(2026, 8, 31)))
    assert len(august) == 3


def test_kind_and_amount_filters(session, dataset):
    income = txs.list_transactions(session, txs.TxnFilter(kinds=[TxnKind.INCOME.value]))
    assert len(income) == 1
    big = txs.list_transactions(session, txs.TxnFilter(min_amount=Decimal("250")))
    assert {row.description for row in big} == {"August food", "Salary"}
    small = txs.list_transactions(session, txs.TxnFilter(max_amount=Decimal("20")))
    assert [row.description for row in small] == ["Wallet snack"]


def test_search_and_tag_filters(session, dataset):
    found = txs.list_transactions(session, txs.TxnFilter(search="food"))
    assert len(found) == 2
    tagged = txs.list_transactions(session, txs.TxnFilter(tags=["weekly"]))
    assert [row.description for row in tagged] == ["August food"]


def test_account_and_category_filters(session, dataset, accounts, categories):
    wallet = txs.list_transactions(session, txs.TxnFilter(
        account_ids=[accounts["Wallet"].id]))
    assert len(wallet) == 1
    groceries = txs.list_transactions(session, txs.TxnFilter(
        category_ids=[categories["groceries"].id]))
    assert len(groceries) == 2


def test_pagination(session, dataset):
    page = txs.list_transactions(session, txs.TxnFilter(limit=2, offset=0))
    assert len(page) == 2
    assert txs.count_transactions(session, txs.TxnFilter()) == 4


def test_all_tags_are_collected(session, dataset):
    assert txs.all_tags(session) == ["essential", "weekly"]


def test_date_bounds(session, dataset):
    first, last = txs.date_bounds(session)
    assert first == date(2026, 7, 5)
    assert last == date(2026, 8, 25)


# --------------------------------------------------------------------------
# Bulk operations
# --------------------------------------------------------------------------
def test_bulk_complete_and_delete(session, accounts):
    ids = []
    for index in range(3):
        created = make(session, description=f"Bill {index}",
                       status=TxnStatus.PLANNED.value,
                       account_id=accounts["Checking"].id)
        ids.append(created.id)
    session.commit()

    assert txs.bulk_complete(session, ids, date(2026, 8, 15)) == 3
    session.commit()
    assert txs.bulk_delete(session, ids) == 3
    session.commit()
    assert txs.count_transactions(session, txs.TxnFilter()) == 0


def test_bulk_recategorise_skips_incompatible_rows(session, accounts, categories):
    expense_id = make(session, description="Food", account_id=accounts["Checking"].id).id
    income_id = make(session, description="Pay", kind=TxnKind.INCOME.value,
                     amount="1000", account_id=accounts["Checking"].id,
                     category_id=categories["salary"].id).id
    session.commit()

    moved = txs.bulk_recategorise(session, [expense_id, income_id],
                                  categories["groceries"].id)
    assert moved == 1
    session.commit()
    assert txs.get_transaction(session, income_id).category_id == categories["salary"].id


# --------------------------------------------------------------------------
# Overdue / upcoming
# --------------------------------------------------------------------------
def test_overdue_and_upcoming_planned(session, accounts):
    make(session, txn_date=date(2026, 8, 1), description="Late bill",
         status=TxnStatus.PLANNED.value, account_id=accounts["Checking"].id)
    make(session, txn_date=date(2026, 8, 25), description="Future bill",
         status=TxnStatus.PLANNED.value, account_id=accounts["Checking"].id)
    session.commit()

    overdue = txs.overdue_planned(session, today=date(2026, 8, 15))
    assert [row.description for row in overdue] == ["Late bill"]
    upcoming = txs.upcoming_planned(session, days=30, today=date(2026, 8, 15))
    assert [row.description for row in upcoming] == ["Future bill"]


def test_actuals_split_by_category_kind(session, accounts, categories):
    from calculations.periods import make_period

    make(session, txn_date=date(2026, 8, 3), description="Food",
         amount="400", account_id=accounts["Checking"].id,
         category_id=categories["groceries"].id)
    make(session, txn_date=date(2026, 8, 4), description="Reserve",
         amount="600", account_id=accounts["Checking"].id,
         category_id=categories["emergency"].id)
    make(session, txn_date=date(2026, 8, 5), description="ETF",
         amount="700", account_id=accounts["Checking"].id,
         category_id=categories["investments"].id)
    make(session, txn_date=date(2026, 8, 6), description="Pay",
         kind=TxnKind.INCOME.value, amount="5000",
         account_id=accounts["Checking"].id, category_id=categories["salary"].id)
    make(session, txn_date=date(2026, 8, 7), description="Mystery",
         amount="90", account_id=accounts["Checking"].id)
    session.commit()

    actuals = txs.actuals_for_period(session, make_period(2026, 8))
    assert actuals.expense_total == Decimal("400.00")
    assert actuals.savings_total == Decimal("600.00")
    assert actuals.investment_total == Decimal("700.00")
    assert actuals.income_total == Decimal("5000.00")
    assert actuals.uncategorised == Decimal("90.00")


def test_excluded_transactions_stay_out_of_actuals(session, accounts, categories):
    from calculations.periods import make_period

    created = make(session, txn_date=date(2026, 8, 3), amount="500",
                   account_id=accounts["Checking"].id,
                   category_id=categories["groceries"].id)
    session.commit()
    txs.update_transaction(session, created.id, {"exclude_from_budget": True})
    session.commit()
    actuals = txs.actuals_for_period(session, make_period(2026, 8))
    assert actuals.expense_total == Decimal("0.00")
