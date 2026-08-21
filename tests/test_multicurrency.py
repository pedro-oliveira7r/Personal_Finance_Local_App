"""Multi-currency: FX transfers, conversion, and the guards around both.

The invariant under all of this is that money is never silently re-labelled.
A figure either stays in the currency it was recorded in, or it passes through
``currency_service.convert`` — there is no third path, and no rate defaults to
one.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from constants import AccountType, TxnKind
from services import account_service, currency_service as cur
from services import transaction_service as txs
from services.common import ConflictError, ServiceError


def balances(session) -> dict[str, Decimal]:
    return {v.account.name: v.balance
            for v in account_service.balance_views(session)}


# ==========================================================================
# Cross-currency transfers
# ==========================================================================
def test_fx_transfer_moves_a_different_magnitude_on_each_side(
        session, accounts, eur_book):
    before = balances(session)
    txs.create_transfer(session, {
        "txn_date": date(2026, 8, 20), "amount": "1000", "to_amount": "161.29",
        "from_account_id": accounts["Checking"].id,
        "to_account_id": eur_book["account"].id,
    })
    session.commit()

    after = balances(session)
    assert after["Checking"] - before["Checking"] == Decimal("-1000.00")
    assert after["Euro savings"] - before["Euro savings"] == Decimal("161.29")


def test_same_currency_transfer_still_moves_one_amount(session, accounts):
    """The regression guard on ``account_movement``.

    Both legs share a magnitude only when they share a currency, and that is
    the overwhelmingly common case — it must not have been disturbed.
    """
    before = balances(session)
    txn = txs.create_transfer(session, {
        "txn_date": date(2026, 8, 20), "amount": "250",
        "from_account_id": accounts["Checking"].id,
        "to_account_id": accounts["Savings"].id,
    })
    session.commit()

    assert txn.to_amount is None
    assert txn.fx_rate is None
    assert txn.is_fx is False
    after = balances(session)
    assert after["Checking"] - before["Checking"] == Decimal("-250.00")
    assert after["Savings"] - before["Savings"] == Decimal("250.00")


def test_fx_rate_is_always_derived_never_taken_from_the_payload(
        session, accounts, eur_book):
    txn = txs.create_transfer(session, {
        "txn_date": date(2026, 8, 20), "amount": "1000", "to_amount": "160",
        "from_account_id": accounts["Checking"].id,
        "to_account_id": eur_book["account"].id,
    })
    session.commit()
    assert txn.fx_rate == Decimal("6.250000")


def test_crossing_currencies_without_the_arrived_amount_is_refused(
        session, accounts, eur_book):
    with pytest.raises(ServiceError, match="amount that arrived"):
        txs.create_transfer(session, {
            "txn_date": date(2026, 8, 20), "amount": "1000",
            "from_account_id": accounts["Checking"].id,
            "to_account_id": eur_book["account"].id,
        })


def test_same_currency_transfer_rejects_a_mismatched_arrived_amount(
        session, accounts):
    with pytest.raises(ServiceError, match="must match"):
        txs.create_transfer(session, {
            "txn_date": date(2026, 8, 20), "amount": "250", "to_amount": "99",
            "from_account_id": accounts["Checking"].id,
            "to_account_id": accounts["Savings"].id,
        })


def test_editing_a_transfer_keeps_its_fx_legs(session, accounts, eur_book):
    """``update_transaction`` merges from a hand-written whitelist.

    A field missing from that dict is silently dropped rather than raising,
    because the schema ignores unknown keys — so an unrelated edit would blank
    the FX pair and quietly re-price the transfer at parity.
    """
    txn = txs.create_transfer(session, {
        "txn_date": date(2026, 8, 20), "amount": "1000", "to_amount": "161.29",
        "from_account_id": accounts["Checking"].id,
        "to_account_id": eur_book["account"].id,
    })
    session.commit()

    txs.update_transaction(session, txn.id, {"description": "Renamed only"})
    session.commit()

    assert txn.description == "Renamed only"
    assert txn.to_amount == Decimal("161.29")
    assert txn.fx_rate == Decimal("6.200012")


def test_completing_an_fx_transfer_rederives_the_rate(session, accounts, eur_book):
    txn = txs.create_transfer(session, {
        "txn_date": date(2026, 8, 20), "amount": "1000", "to_amount": "160",
        "status": "planned",
        "from_account_id": accounts["Checking"].id,
        "to_account_id": eur_book["account"].id,
    })
    session.commit()

    txs.complete_transaction(session, txn.id, actual_to_amount="158.40")
    session.commit()
    assert txn.to_amount == Decimal("158.40")
    assert txn.fx_rate == cur.derive_fx_rate(Decimal("1000"), Decimal("158.40"))


# ==========================================================================
# Conversion
# ==========================================================================
def test_same_currency_conversion_needs_no_rate_at_all(session):
    """A single-currency book must never be able to raise here."""
    book = cur.book(session)
    assert book.convert("1234.56", "BRL", "BRL") == Decimal("1234.56")


def test_conversion_round_trips_within_a_cent(session, eur_book):
    book = cur.book(session)
    there = book.convert("100", "EUR", "BRL")
    back = book.convert(there, "BRL", "EUR")
    assert there == Decimal("620.00")
    assert abs(back - Decimal("100")) <= Decimal("0.01")


def test_a_missing_rate_raises_rather_than_assuming_parity(session):
    book = cur.book(session)
    with pytest.raises(cur.MissingRateError):
        book.convert("100", "EUR", "BRL")


def test_the_latest_rate_wins_and_an_older_one_changes_nothing(session, eur_book):
    cur.set_rate(session, "EUR", "6.50", as_of=date(2026, 8, 15))
    session.commit()
    assert cur.book(session).convert("100", "EUR", "BRL") == Decimal("650.00")

    # A backdated correction is recorded but must not displace the newest quote.
    cur.set_rate(session, "EUR", "5.00", as_of=date(2026, 7, 1))
    session.commit()
    assert cur.book(session).convert("100", "EUR", "BRL") == Decimal("650.00")


def test_a_new_rate_revalues_history(session, eur_book):
    """Pins the trade-off the user accepted: conversion uses the latest rate.

    Converted history therefore moves when a rate is entered. That is intended;
    the test exists so nobody later "fixes" it into a date-based lookup without
    a deliberate decision.
    """
    book = cur.book(session)
    assert book.convert("100", "EUR", "BRL") == Decimal("620.00")

    cur.set_rate(session, "EUR", "7.00", as_of=date(2026, 8, 25))
    session.commit()
    assert cur.book(session).convert("100", "EUR", "BRL") == Decimal("700.00")


def test_deriving_a_rate_refuses_a_zero_denominator(session):
    with pytest.raises(ServiceError):
        cur.derive_fx_rate("1000", "0")


def test_the_primary_currency_cannot_be_given_a_rate(session):
    with pytest.raises(ServiceError):
        cur.set_rate(session, "BRL", "1.5")


# ==========================================================================
# Debts across currencies
# ==========================================================================
def test_debt_payment_moves_the_balance_in_the_debts_own_currency(
        session, accounts, eur_book):
    """Paying a euro debt from a real account must not subtract reais from it."""
    from services import debt_service

    loan = account_service.create_account(session, {
        "name": "Euro loan", "type": AccountType.LOAN.value, "currency": "EUR",
        "opening_balance": "5000", "opening_date": date(2026, 1, 1),
        "include_in_cash": False,
    })
    debt = debt_service.create_debt(session, {
        "name": "Euro loan", "principal_balance": "5000", "interest_rate": "0",
        "minimum_payment": "100", "currency": "EUR", "account_id": loan.id,
    })
    session.commit()

    debt_service.record_payment(
        session, debt.id, Decimal("620"), to_amount=Decimal("100"),
        from_account_id=accounts["Checking"].id, on_date=date(2026, 8, 20),
    )
    session.commit()

    # The debt is genuinely denominated in EUR — if the schema dropped the
    # field this would be BRL and the balance maths below would be a
    # coincidence rather than a result.
    assert debt.currency == "EUR"
    # 100 EUR reached the loan, not 620 of anything.
    assert debt.principal_balance == Decimal("4900.00")


# ==========================================================================
# Guards
# ==========================================================================
def test_fingerprint_is_unchanged_for_every_pre_existing_row(session):
    """A shifted hash would silently disable duplicate detection on all history."""
    without = txs.fingerprint(date(2026, 8, 20), Decimal("100"), "Coffee", 1, "expense")
    explicit_none = txs.fingerprint(
        date(2026, 8, 20), Decimal("100"), "Coffee", 1, "expense", None)
    assert without == explicit_none
    assert without == "04d506535c444e0459dd8fdf2a11e4ef91da0914"

    with_leg = txs.fingerprint(
        date(2026, 8, 20), Decimal("100"), "Coffee", 1, "transfer", Decimal("16"))
    assert with_leg != without


def test_a_book_may_not_hold_more_than_three_currencies(session):
    with pytest.raises(ServiceError):
        cur.set_active_currencies(session, ["EUR", "USD", "GBP"])


def test_a_currency_still_held_by_an_account_cannot_be_removed(session, eur_book):
    """Archived accounts still hold money, so they still hold a currency."""
    account_service.archive_account(session, eur_book["account"].id, True)
    session.commit()
    with pytest.raises(ServiceError, match="still hold"):
        cur.set_active_currencies(session, [])


def test_an_account_with_transactions_cannot_be_re_denominated(
        session, accounts, categories, eur_book):
    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 20), "description": "Groceries", "amount": "80",
        "kind": TxnKind.EXPENSE.value, "account_id": accounts["Checking"].id,
        "category_id": categories["groceries"].id,
    })
    session.commit()
    with pytest.raises(ConflictError, match="re-label"):
        account_service.update_account(
            session, accounts["Checking"].id, {"currency": "EUR"})


def test_an_empty_account_may_still_change_currency(session, accounts, eur_book):
    account_service.update_account(session, accounts["Broker"].id, {"currency": "EUR"})
    session.commit()
    assert accounts["Broker"].currency == "EUR"


def test_a_new_book_stamps_its_own_primary_not_a_hardcoded_default(session):
    """The pydantic default is the literal "BRL" and has no session.

    Without the service filling it in, every goal and debt of a euro-based book
    would silently be recorded as reais.
    """
    from services import goal_service, settings_service

    settings_service.update_settings(session, {"base_currency": "EUR",
                                               "active_currencies": ["EUR"]})
    session.commit()
    goal = goal_service.create_goal(session, {
        "name": "Trip", "target_amount": "3000", "start_date": date(2026, 1, 1)})
    session.commit()
    assert goal.currency == "EUR"


def test_no_transfer_anywhere_crosses_currencies_without_its_far_leg(session):
    """The class of bug, not one instance of it.

    Three separate places build a transfer ``Transaction``: the service door,
    recurring-rule generation, and the demo generator. Only the first validates.
    A transfer that crosses currencies with ``to_amount`` unset does not fail —
    it silently credits the destination with the source currency's magnitude.
    """
    from sqlalchemy import select
    from database.models import Account, Transaction
    from demo.demo_data import load_demo_data

    load_demo_data(session, months_back=6, months_forward=2, today=date(2026, 8, 17))
    session.commit()

    currencies = {a.id: a.currency for a in session.execute(select(Account)).scalars()}
    offenders = [
        t for t in session.execute(
            select(Transaction).where(Transaction.kind == TxnKind.TRANSFER.value)
        ).scalars()
        if t.account_id and t.to_account_id
        and currencies.get(t.account_id) != currencies.get(t.to_account_id)
        and t.to_amount is None
    ]
    assert not offenders, (
        f"{len(offenders)} cross-currency transfer(s) have no to_amount and will "
        f"credit the destination in the wrong currency"
    )


def test_the_demo_book_actually_exercises_a_cross_currency_transfer(session):
    """Guards the guard: the test above passes trivially if none exist."""
    from sqlalchemy import select
    from database.models import Transaction
    from demo.demo_data import load_demo_data

    load_demo_data(session, months_back=6, months_forward=2, today=date(2026, 8, 17))
    session.commit()
    fx = session.execute(
        select(Transaction).where(Transaction.to_amount.is_not(None))
    ).scalars().all()
    assert fx, "the demo book no longer contains an FX transfer"
    assert all(t.fx_rate and t.fx_rate > 0 for t in fx)


def test_an_empty_book_may_still_choose_its_primary_currency(session):
    """A new user must be able to leave the default behind.

    The seeded starter accounts are created in the default currency, so a rule
    of "never change the primary" would strand every new book on BRL forever.
    """
    from services import settings_service

    settings_service.update_settings(session, {"base_currency": "EUR"})
    session.commit()

    snapshot = settings_service.get_settings(session)
    assert snapshot.base_currency == "EUR"
    assert snapshot.active_currencies == ("EUR",)


def test_a_book_holding_real_money_cannot_change_its_primary(
        session, accounts, eur_book):
    """Rates are quoted against the primary and amounts are never converted."""
    from services import settings_service

    with pytest.raises(ServiceError, match="only change while the book is empty"):
        settings_service.update_settings(session, {"base_currency": "EUR"})
