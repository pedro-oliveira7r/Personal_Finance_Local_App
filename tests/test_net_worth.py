"""Net worth: asset/liability split, history, projection and liquidity."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from calculations.net_worth import (
    NetWorthLine,
    NetWorthPoint,
    change_between,
    liquidity_ratio,
    project_net_worth,
    series_from_summaries,
    summarise_net_worth,
)
from constants import AccountType


def line(name, kind, balance, include=True):
    return NetWorthLine(account_id=None, name=name, type=kind,
                        balance=Decimal(balance), include=include)


def test_assets_minus_liabilities():
    summary = summarise_net_worth([
        line("Checking", AccountType.CHECKING.value, "5000"),
        line("Broker", AccountType.INVESTMENT.value, "20000"),
        line("Card", AccountType.CREDIT_CARD.value, "-1500"),
        line("Car loan", AccountType.LOAN.value, "-18000"),
    ], as_of=date(2026, 8, 17))
    assert summary.total_assets == Decimal("25000.00")
    assert summary.total_liabilities == Decimal("19500.00")
    assert summary.net_worth == Decimal("5500.00")
    assert summary.is_solvent
    assert summary.debt_to_asset_pct == Decimal("78.00")


def test_negative_net_worth_is_reported():
    summary = summarise_net_worth([
        line("Checking", AccountType.CHECKING.value, "1000"),
        line("Mortgage", AccountType.LOAN.value, "-200000"),
    ])
    assert summary.net_worth == Decimal("-199000.00")
    assert not summary.is_solvent


def test_overdrawn_asset_counts_as_a_liability():
    summary = summarise_net_worth([
        line("Checking", AccountType.CHECKING.value, "-800"),
        line("Savings", AccountType.SAVINGS.value, "2000"),
    ])
    assert summary.total_assets == Decimal("2000.00")
    assert summary.total_liabilities == Decimal("800.00")
    assert summary.net_worth == Decimal("1200.00")
    assert [item.name for item in summary.liabilities] == ["Checking"]


def test_overpaid_card_counts_as_a_small_asset():
    summary = summarise_net_worth([
        line("Card", AccountType.CREDIT_CARD.value, "150"),
    ])
    assert summary.total_assets == Decimal("150.00")
    assert summary.total_liabilities == Decimal("0.00")


def test_excluded_accounts_are_left_out():
    summary = summarise_net_worth([
        line("Checking", AccountType.CHECKING.value, "1000"),
        line("Ignore me", AccountType.OTHER_ASSET.value, "999999", include=False),
    ])
    assert summary.total_assets == Decimal("1000.00")


def test_property_counts_as_an_asset():
    summary = summarise_net_worth([
        line("Flat", AccountType.OTHER_ASSET.value, "380000"),
        line("Mortgage", AccountType.OTHER_LIABILITY.value, "-210000"),
    ])
    assert summary.net_worth == Decimal("170000.00")
    assert summary.by_type[AccountType.OTHER_ASSET.value] == Decimal("380000.00")


def test_lines_are_sorted_biggest_first():
    summary = summarise_net_worth([
        line("Small", AccountType.CHECKING.value, "100"),
        line("Large", AccountType.INVESTMENT.value, "50000"),
        line("Medium", AccountType.SAVINGS.value, "5000"),
    ])
    assert [item.name for item in summary.assets] == ["Large", "Medium", "Small"]


def test_empty_position_is_zero_and_solvent():
    summary = summarise_net_worth([])
    assert summary.net_worth == Decimal("0.00")
    assert summary.is_solvent
    assert summary.debt_to_asset_pct == Decimal("0.00")


def test_as_dict_is_serialisable():
    summary = summarise_net_worth([line("Checking", AccountType.CHECKING.value, "10")])
    payload = summary.as_dict()
    assert payload["net_worth"] == Decimal("10.00")
    assert payload["assets"] == [("Checking", Decimal("10.00"))]


# --------------------------------------------------------------------------
# Series
# --------------------------------------------------------------------------
def points():
    return [
        NetWorthPoint(as_of=date(2026, 1, 31), total_assets=Decimal("10000"),
                     total_liabilities=Decimal("4000"), net_worth=Decimal("6000")),
        NetWorthPoint(as_of=date(2026, 7, 31), total_assets=Decimal("14000"),
                     total_liabilities=Decimal("2000"), net_worth=Decimal("12000")),
    ]


def test_change_between_first_and_last():
    change = change_between(points())
    assert change["absolute"] == Decimal("6000.00")
    assert change["percent"] == Decimal("100.00")
    assert change["monthly_average"] == Decimal("1000.00")


def test_change_needs_two_points():
    assert change_between(points()[:1])["absolute"] == Decimal("0.00")
    assert change_between([])["absolute"] == Decimal("0.00")


def test_change_handles_a_zero_starting_point():
    series = [
        NetWorthPoint(as_of=date(2026, 1, 31), total_assets=Decimal("0"),
                     total_liabilities=Decimal("0"), net_worth=Decimal("0")),
        NetWorthPoint(as_of=date(2026, 2, 28), total_assets=Decimal("500"),
                     total_liabilities=Decimal("0"), net_worth=Decimal("500")),
    ]
    change = change_between(series)
    assert change["absolute"] == Decimal("500.00")
    assert change["percent"] == Decimal("0.00")


def test_series_from_summaries_is_sorted():
    later = summarise_net_worth([line("A", AccountType.CHECKING.value, "2")],
                                as_of=date(2026, 6, 1))
    earlier = summarise_net_worth([line("A", AccountType.CHECKING.value, "1")],
                                 as_of=date(2026, 1, 1))
    series = series_from_summaries([later, earlier])
    assert [point.as_of for point in series] == [date(2026, 1, 1), date(2026, 6, 1)]


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------
def test_projection_grows_assets_and_shrinks_debt():
    start = points()[0]
    series = project_net_worth(start, Decimal("500"), Decimal("200"), 12)
    assert len(series) == 13
    assert series[-1].total_assets == Decimal("16000.00")
    assert series[-1].total_liabilities == Decimal("1600.00")
    assert series[-1].net_worth == Decimal("14400.00")


def test_liabilities_never_go_below_zero():
    start = NetWorthPoint(as_of=date(2026, 1, 31), total_assets=Decimal("0"),
                         total_liabilities=Decimal("300"), net_worth=Decimal("-300"))
    series = project_net_worth(start, Decimal("0"), Decimal("500"), 6)
    assert series[-1].total_liabilities == Decimal("0.00")


def test_projection_with_compounding_beats_the_flat_case():
    start = points()[0]
    flat = project_net_worth(start, Decimal("500"), Decimal("0"), 24)
    compounded = project_net_worth(start, Decimal("500"), Decimal("0"), 24,
                                   annual_return_pct=Decimal("8"))
    assert compounded[-1].net_worth > flat[-1].net_worth


def test_zero_month_projection_returns_only_the_starting_point():
    assert len(project_net_worth(points()[0], Decimal("100"), Decimal("0"), 0)) == 1


# --------------------------------------------------------------------------
# Liquidity
# --------------------------------------------------------------------------
def test_liquidity_ratio_in_months():
    assert liquidity_ratio(Decimal("9000"), Decimal("3000")) == Decimal("3.0")
    assert liquidity_ratio(Decimal("1500"), Decimal("3000")) == Decimal("0.5")


def test_liquidity_ratio_is_none_without_expenses():
    assert liquidity_ratio(Decimal("9000"), Decimal("0")) is None


# --------------------------------------------------------------------------
# Against the database
# --------------------------------------------------------------------------
def test_current_summary_reads_real_balances(session, accounts):
    from services import networth_service
    from services import transaction_service as txs

    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 3), "description": "Card charge",
        "amount": "600", "kind": "expense", "account_id": accounts["Card"].id,
    })
    session.commit()

    summary = networth_service.current_summary(session, as_of=date(2026, 8, 31))
    # 1000 checking + 500 savings + 100 wallet + 2000 broker
    assert summary.total_assets == Decimal("3600.00")
    assert summary.total_liabilities == Decimal("600.00")
    assert summary.net_worth == Decimal("3000.00")


def test_unlinked_debts_are_added_to_liabilities(session, accounts):
    from services import debt_service, networth_service

    debt_service.create_debt(session, {
        "name": "Family loan", "principal_balance": "2500", "interest_rate": "0",
    })
    session.commit()
    summary = networth_service.current_summary(session, as_of=date(2026, 8, 31))
    assert summary.total_liabilities == Decimal("2500.00")


def test_linked_debt_is_not_counted_twice(session, accounts):
    from services import debt_service, networth_service
    from services import transaction_service as txs

    debt_service.create_debt(session, {
        "name": "Card debt", "principal_balance": "600", "interest_rate": "0",
        "account_id": accounts["Card"].id,
    })
    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 3), "description": "Charge",
        "amount": "600", "kind": "expense", "account_id": accounts["Card"].id,
    })
    session.commit()
    summary = networth_service.current_summary(session, as_of=date(2026, 8, 31))
    assert summary.total_liabilities == Decimal("600.00")


def test_snapshots_round_trip(session, accounts):
    from services import networth_service

    saved = networth_service.save_snapshot(session, as_of=date(2026, 8, 31))
    session.commit()
    assert saved.net_worth == Decimal("3600.00")

    again = networth_service.save_snapshot(session, as_of=date(2026, 8, 31))
    session.commit()
    assert again.id == saved.id  # updated in place, not duplicated
    assert len(networth_service.list_snapshots(session)) == 1


def test_history_walks_period_ends(session, accounts):
    from services import networth_service
    from services import transaction_service as txs

    txs.create_transaction(session, {
        "txn_date": date(2026, 7, 15), "description": "Bonus", "amount": "1000",
        "kind": "income", "account_id": accounts["Checking"].id,
    })
    session.commit()
    points_series = networth_service.trailing_history(session, 3, date(2026, 8, 17))
    assert len(points_series) == 3
    assert points_series[-1].net_worth == Decimal("4600.00")
    assert points_series[0].net_worth == Decimal("3600.00")
