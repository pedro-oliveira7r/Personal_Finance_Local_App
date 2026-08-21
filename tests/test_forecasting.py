"""Forecast chaining, sources, earmarks and alerts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from calculations.forecasting import (
    SOURCE_AVERAGE,
    SOURCE_BUDGET,
    SOURCE_RULES,
    ForecastAssumption,
    average_assumption,
    build_forecast,
    first_negative,
    forecast_alerts,
    forecast_totals,
    lowest_point,
    negative_periods,
    runway_periods,
)
from calculations.periods import make_period, period_sequence


def periods(count: int = 6, year: int = 2026, month: int = 8):
    return period_sequence(make_period(year, month), count)


def assumption(key: str, income="5000", expenses="4000", **kwargs):
    return ForecastAssumption(period_key=key, income=income, expenses=expenses,
                              source=SOURCE_BUDGET, **kwargs)


# --------------------------------------------------------------------------
# Chaining
# --------------------------------------------------------------------------
def test_closing_cash_feeds_the_next_opening():
    window = periods(3)
    rows = build_forecast(
        window, Decimal("1000"),
        {period.key: assumption(period.key) for period in window},
        today=date(2026, 8, 15),
    )
    assert [row.closing_cash for row in rows] == [
        Decimal("2000.00"), Decimal("3000.00"), Decimal("4000.00")]
    for previous, current in zip(rows, rows[1:]):
        assert current.opening_cash == previous.closing_cash


def test_missing_periods_fall_back_to_the_average():
    window = periods(3)
    average = ForecastAssumption(period_key="average", income="4000",
                                 expenses="3500", source=SOURCE_AVERAGE)
    rows = build_forecast(window, Decimal("0"),
                          {window[0].key: assumption(window[0].key)},
                          default_assumption=average, today=date(2026, 8, 15))
    assert rows[0].assumption.source == SOURCE_BUDGET
    assert rows[1].assumption.source == SOURCE_AVERAGE
    assert rows[1].net_flow == Decimal("500.00")


def test_periods_without_any_assumption_are_flat():
    window = periods(2)
    rows = build_forecast(window, Decimal("500"), {}, today=date(2026, 8, 15))
    assert all(row.net_flow == Decimal("0.00") for row in rows)
    assert rows[-1].closing_cash == Decimal("500.00")


def test_actual_and_forecast_periods_are_labelled():
    window = period_sequence(make_period(2026, 6), 4)
    rows = build_forecast(window, Decimal("0"), {}, today=date(2026, 8, 15))
    assert [row.is_actual for row in rows] == [True, True, False, False]


# --------------------------------------------------------------------------
# Earmarked money
# --------------------------------------------------------------------------
def test_savings_that_stay_in_cash_do_not_reduce_cash():
    window = periods(1)
    rows = build_forecast(
        window, Decimal("1000"),
        {window[0].key: assumption(window[0].key, income="5000", expenses="3000",
                                   savings_reserved="1000")},
        today=date(2026, 8, 15),
    )
    row = rows[0]
    assert row.net_flow == Decimal("2000.00")     # 5000 − 3000, savings stay put
    assert row.closing_cash == Decimal("3000.00")
    assert row.reserved == Decimal("1000.00")
    assert row.free_cash == Decimal("2000.00")


def test_savings_moved_out_of_cash_do_reduce_cash():
    window = periods(1)
    rows = build_forecast(
        window, Decimal("1000"),
        {window[0].key: assumption(window[0].key, income="5000", expenses="3000",
                                   savings_outflow="1000")},
        today=date(2026, 8, 15),
    )
    assert rows[0].net_flow == Decimal("1000.00")
    assert rows[0].closing_cash == Decimal("2000.00")
    assert rows[0].reserved == Decimal("0.00")


def test_opening_reserved_is_carried_in():
    window = periods(1)
    rows = build_forecast(window, Decimal("5000"), {},
                          opening_reserved=Decimal("3000"), today=date(2026, 8, 15))
    assert rows[0].free_cash == Decimal("2000.00")


def test_investments_and_debt_payments_leave_the_cash_pool():
    window = periods(1)
    rows = build_forecast(
        window, Decimal("0"),
        {window[0].key: assumption(window[0].key, income="5000", expenses="1000",
                                   investments="1500", debt_payments="800")},
        today=date(2026, 8, 15),
    )
    assert rows[0].assumption.total_outflow == Decimal("3300.00")
    assert rows[0].net_flow == Decimal("1700.00")


def test_total_allocated_includes_reserved_savings():
    item = assumption("2026-08", income="5000", expenses="3000",
                      savings_reserved="500", investments="200")
    assert item.total_outflow == Decimal("3200.00")
    assert item.total_allocated == Decimal("3700.00")


# --------------------------------------------------------------------------
# Reading the projection
# --------------------------------------------------------------------------
def test_negative_periods_and_runway():
    window = periods(4)
    rows = build_forecast(
        window, Decimal("1000"),
        {period.key: assumption(period.key, income="1000", expenses="1500")
         for period in window},
        today=date(2026, 8, 15),
    )
    assert [row.closing_cash for row in rows] == [
        Decimal("500.00"), Decimal("0.00"), Decimal("-500.00"), Decimal("-1000.00")]
    assert len(negative_periods(rows)) == 2
    assert first_negative(rows).period.key == window[2].key
    assert runway_periods(rows) == 2


def test_runway_is_none_when_cash_never_runs_out():
    window = periods(3)
    rows = build_forecast(window, Decimal("1000"),
                          {p.key: assumption(p.key) for p in window},
                          today=date(2026, 8, 15))
    assert runway_periods(rows) is None
    assert negative_periods(rows) == []


def test_lowest_point_and_totals():
    window = periods(3)
    plans = {
        window[0].key: assumption(window[0].key, income="1000", expenses="3000"),
        window[1].key: assumption(window[1].key, income="6000", expenses="1000"),
        window[2].key: assumption(window[2].key, income="2000", expenses="1000"),
    }
    rows = build_forecast(window, Decimal("2000"), plans, today=date(2026, 8, 15))
    assert lowest_point(rows).period.key == window[0].key
    totals = forecast_totals(rows)
    assert totals["income"] == Decimal("9000.00")
    assert totals["expenses"] == Decimal("5000.00")
    assert totals["net_flow"] == Decimal("4000.00")
    assert totals["closing_cash"] == Decimal("6000.00")


def test_savings_rate_on_a_forecast_row():
    window = periods(1)
    rows = build_forecast(
        window, Decimal("0"),
        {window[0].key: assumption(window[0].key, income="5000", expenses="3000",
                                   savings_reserved="500", investments="500")},
        today=date(2026, 8, 15),
    )
    assert rows[0].savings_rate == Decimal("20.00")


# --------------------------------------------------------------------------
# Averages
# --------------------------------------------------------------------------
def test_average_assumption_uses_the_last_n_entries():
    history = [
        {"income": Decimal("1000"), "expenses": Decimal("500")},
        {"income": Decimal("3000"), "expenses": Decimal("1500")},
        {"income": Decimal("5000"), "expenses": Decimal("2500")},
    ]
    average = average_assumption(history, months=2)
    assert average.income == Decimal("4000.00")
    assert average.expenses == Decimal("2000.00")
    assert "2 period" in average.note


def test_average_of_empty_history_is_zero():
    average = average_assumption([], months=6)
    assert average.income == Decimal("0.00")
    assert average.source == SOURCE_AVERAGE


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
def test_alert_on_a_projected_negative_balance():
    window = periods(2)
    rows = build_forecast(window, Decimal("100"),
                          {p.key: assumption(p.key, income="100", expenses="500")
                           for p in window},
                          today=date(2026, 8, 15))
    alerts = forecast_alerts(rows)
    assert any(alert.code == "projected_negative_balance" for alert in alerts)
    assert alerts[0].severity == "critical"


def test_low_cash_threshold_warning():
    window = periods(2)
    rows = build_forecast(window, Decimal("1200"),
                          {p.key: assumption(p.key, income="1000", expenses="1100")
                           for p in window},
                          today=date(2026, 8, 15))
    alerts = forecast_alerts(rows, low_cash_threshold=Decimal("1500"))
    assert any(alert.code == "low_cash" for alert in alerts)


def test_repeated_deficit_warning():
    window = periods(4)
    rows = build_forecast(window, Decimal("100000"),
                          {p.key: assumption(p.key, income="1000", expenses="2000")
                           for p in window},
                          today=date(2026, 8, 1))
    alerts = forecast_alerts(rows)
    assert any(alert.code == "repeated_deficit" for alert in alerts)


def test_spending_earmarked_money_is_flagged():
    window = periods(1)
    rows = build_forecast(window, Decimal("1000"),
                          {window[0].key: assumption(window[0].key, income="1000",
                                                     expenses="900")},
                          opening_reserved=Decimal("5000"),
                          today=date(2026, 8, 15))
    alerts = forecast_alerts(rows)
    assert any(alert.code == "earmarked_overlap" for alert in alerts)


def test_healthy_forecast_has_no_alerts():
    window = periods(3)
    rows = build_forecast(window, Decimal("5000"),
                          {p.key: assumption(p.key, income="5000", expenses="3000")
                           for p in window},
                          today=date(2026, 8, 15))
    assert forecast_alerts(rows) == []


# --------------------------------------------------------------------------
# Against the database
# --------------------------------------------------------------------------
def test_forecast_prefers_a_budget_over_rules(session, accounts, categories):
    from datetime import date as _date

    from services import budget_service, forecast_service, recurring_service
    from constants import Frequency, TxnKind

    today = _date(2026, 8, 15)
    recurring_service.create_rule(session, {
        "name": "Salary", "kind": TxnKind.INCOME.value, "amount": "5000",
        "frequency": Frequency.MONTHLY.value, "day_of_month": 5,
        "start_date": _date(2026, 1, 5), "category_id": categories["salary"].id,
        "account_id": accounts["Checking"].id,
    })
    session.commit()

    bundle = forecast_service.build(session, months=3, history_months=0, today=today)
    assert bundle.rows
    assert bundle.rows[0].assumption.source in {SOURCE_RULES, SOURCE_AVERAGE}

    # Now write an explicit budget for the current period: it must win.
    budget_service.upsert_line(session, 2026, 8, {
        "kind": "income", "planned_amount": "9999",
        "category_id": categories["salary"].id,
    })
    session.commit()
    bundle = forecast_service.build(session, months=3, history_months=0, today=today)
    assert bundle.rows[0].assumption.source == SOURCE_BUDGET
    assert bundle.rows[0].assumption.income == Decimal("9999.00")


def test_forecast_starts_from_real_cash(session, accounts):
    from datetime import date as _date

    from services import forecast_service

    bundle = forecast_service.build(session, months=2, history_months=0,
                                    today=_date(2026, 8, 15))
    # Checking 1000 + Savings 500 + Wallet 100, plus the seeded zero-balance accounts.
    assert bundle.start_cash == Decimal("1600.00")
