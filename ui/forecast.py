"""Forecast — where the money is heading, and what-if scenarios."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import streamlit as st

from calculations.money import ZERO, D, money, money_sum
from charts import dashboard_charts as dc
from charts import financial_charts as fc
from services import forecast_service, networth_service, reporting_service
from ui import components as ui


def render() -> None:
    fmt = ui.formatter()
    settings = ui.current_settings()
    theme = ui.theme()
    today = date.today()

    ui.page_header(
        "Forecast",
        "A projection, clearly separated from what actually happened. Each period says "
        "where its numbers came from.",
        icon="🔮",
    )

    columns = st.columns([0.24, 0.24, 0.26, 0.26])
    with columns[0]:
        months = st.slider("Months ahead", 1, 60, min(settings.forecast_months, 60),
                           key="fc_months")
    with columns[1]:
        history_months = st.slider("Months of history to show", 0, 24, 6,
                                   key="fc_history")
    with columns[2]:
        average_window = st.slider("Averaging window for gaps", 1, 12, 6,
                                   key="fc_window",
                                   help="Periods with neither a budget nor rules fall "
                                        "back to an average of this many recent months.")
    with columns[3]:
        comfort = ui.money_input("Low-cash warning level", ZERO, key="fc_comfort",
                                 help_text="Warn when projected cash dips below this.")

    with ui.db_read() as session:
        bundle = forecast_service.build(
            session, months=months, history_months=history_months,
            average_window=average_window, today=today,
            low_cash_threshold=comfort,
        )

    if not bundle.rows:
        ui.empty_state(
            "Nothing to project yet",
            "A forecast needs something to work from: a budget, some recurring rules, or "
            "a few months of history. Add any of the three and this fills in.",
            icon="🔮",
        )
        return

    _headline(bundle, fmt, months)
    ui.divider()

    tabs = st.tabs(["Cash projection", "Components", "What if…", "Net worth outlook"])
    with tabs[0]:
        _cash_tab(bundle, theme, fmt)
    with tabs[1]:
        _components_tab(bundle, theme, fmt)
    with tabs[2]:
        _scenario_tab(bundle, theme, fmt, today, comfort)
    with tabs[3]:
        _net_worth_tab(theme, fmt, today)


def _headline(bundle, fmt: ui.Formatter, months: int) -> None:
    totals = bundle.totals
    future = bundle.future_rows
    ending = future[-1] if future else None

    ui.kpi_row([
        ui.Kpi("Cash now", fmt.money(bundle.start_cash), icon="💵"),
        ui.Kpi(f"Projected in {months} month(s)",
               fmt.money(ending.closing_cash if ending else ZERO), icon="🔮",
               delta=fmt.signed_money(
                   money((ending.closing_cash if ending else ZERO) - bundle.start_cash)),
               delta_good=bool(ending and ending.closing_cash >= bundle.start_cash)),
        ui.Kpi("Projected income", fmt.money(totals.get("income", ZERO)), icon="📥"),
        ui.Kpi("Projected outflow",
               fmt.money(money(totals.get("expenses", ZERO)
                               + totals.get("investments", ZERO)
                               + totals.get("debt_payments", ZERO))), icon="📤"),
        ui.Kpi("Net over the period", fmt.signed_money(totals.get("net_flow", ZERO)),
               icon="🔄", delta_good=totals.get("net_flow", ZERO) >= 0),
    ])

    if bundle.alerts:
        for alert in bundle.alerts:
            icon = {"critical": "🔴", "warning": "🟠"}.get(alert.severity, "🔵")
            (st.error if alert.severity == "critical" else st.warning)(
                f"{icon} {alert.message}")
    else:
        st.success("✅ No cash-flow problems in the projection.")


def _cash_tab(bundle, theme, fmt: ui.Formatter) -> None:
    ui.chart(
        dc.cash_balance_line(bundle.rows, theme, height=400),
        table=[
            {"Period": row.label, "Actual or forecast":
                "actual" if row.is_actual else "forecast",
             "Basis": row.source_label,
             "Income": fmt.money(row.assumption.income),
             "Outflow": fmt.money(row.assumption.total_outflow),
             "Net": fmt.signed_money(row.net_flow),
             "Closing cash": fmt.money(row.closing_cash),
             "Earmarked": fmt.money(row.reserved),
             "Free of earmarks": fmt.money(row.free_cash)}
            for row in bundle.rows
        ],
        key="fc_cash",
        caption="The dotted line is cash free of goal earmarks — money you could "
                "actually spend without raiding a goal.",
    )

    left, right = st.columns(2)
    with left:
        if bundle.lowest is not None:
            st.metric("Lowest projected point",
                      fmt.money(bundle.lowest.closing_cash),
                      delta=bundle.lowest.period.label, delta_color="off")
    with right:
        runway = bundle.runway
        st.metric("Periods before cash runs out",
                  "never at this rate" if runway is None else str(runway))

    ui.divider()
    ui.section("Period by period")
    ui.money_table(
        [{"period": row.period.label,
          "state": "Actual" if row.is_actual else "Forecast",
          "basis": row.source_label,
          "income": row.assumption.income,
          "expenses": row.assumption.expenses,
          "savings": money(row.assumption.savings_reserved
                           + row.assumption.savings_outflow),
          "investments": row.assumption.investments,
          "debt": row.assumption.debt_payments,
          "net": row.net_flow,
          "closing": row.closing_cash,
          "free": row.free_cash}
         for row in bundle.rows],
        [("period", "Period", "text"), ("state", "", "text"),
         ("basis", "Based on", "text"), ("income", "Income", "money"),
         ("expenses", "Expenses", "money"), ("savings", "Savings", "money"),
         ("investments", "Investments", "money"), ("debt", "Debt", "money"),
         ("net", "Net flow", "money"), ("closing", "Closing cash", "money"),
         ("free", "Free cash", "money")],
        fmt, height=min(620, 60 + 36 * len(bundle.rows)),
    )


def _components_tab(bundle, theme, fmt: ui.Formatter) -> None:
    ui.section("What the projection is made of")
    ui.chart(fc.forecast_components_bars(bundle.future_rows or bundle.rows, theme,
                                        height=380),
             key="fc_components")
    st.caption(
        "Savings that stay inside a cash account are not shown as outflow — that money "
        "is earmarked, not spent, so it never leaves the cash pool."
    )

    sources: dict[str, int] = {}
    for row in bundle.rows:
        sources[row.source_label] = sources.get(row.source_label, 0) + 1
    ui.divider()
    ui.section("Where each period's numbers come from")
    for label, count in sources.items():
        st.markdown(f"- **{label}** — {count} period(s)")
    if bundle.average is not None:
        st.caption(
            f"Average fallback: income {fmt.md_money(bundle.average.income)}, expenses "
            f"{fmt.md_money(bundle.average.expenses)} — {ui.md(bundle.average.note)}."
        )
    st.info(
        "Priority order per period: an explicit budget wins, then your recurring rules, "
        "then an average of recent history. Write a budget for a month and the forecast "
        "immediately trusts it instead.",
        icon="ℹ️",
    )


def _scenario_tab(bundle, theme, fmt: ui.Formatter, today: date,
                  comfort: Decimal) -> None:
    ui.section(
        "What if…",
        "Adjust the assumptions and see the projection move. Nothing is saved — this is "
        "a sandbox.",
    )
    columns = st.columns([0.25, 0.25, 0.25, 0.25])
    with columns[0]:
        income_pct = ui.pct_input("Income changes by", ZERO, key="sc_income",
                                  min_value=-90.0, max_value=200.0)
    with columns[1]:
        expense_pct = ui.pct_input("Expenses change by", ZERO, key="sc_expense",
                                    min_value=-90.0, max_value=200.0)
    with columns[2]:
        one_off_amount = ui.money_input("One-off expense", ZERO, key="sc_oneoff")
    with columns[3]:
        future = bundle.future_rows
        options = [row.period.key for row in future]
        one_off_period = st.selectbox(
            "…in which period", options,
            format_func=lambda item: next(
                (row.period.label for row in future if row.period.key == item), item),
            key="sc_period", disabled=not options,
        ) if options else None

    if income_pct == 0 and expense_pct == 0 and one_off_amount == 0:
        st.info("Change one of the levers above to see a scenario.", icon="🎚️")
        return

    one_off = ({one_off_period: one_off_amount}
               if one_off_amount > 0 and one_off_period else None)
    scenario = forecast_service.run_scenario(
        bundle, income_pct=income_pct, expense_pct=expense_pct,
        one_off=one_off, today=today, low_cash_threshold=comfort,
    )

    base_future = bundle.future_rows
    scenario_future = scenario.future_rows
    base_end = base_future[-1].closing_cash if base_future else ZERO
    scenario_end = scenario_future[-1].closing_cash if scenario_future else ZERO
    difference = money(scenario_end - base_end)

    ui.kpi_row([
        ui.Kpi("Baseline ending cash", fmt.money(base_end), icon="📊"),
        ui.Kpi("Scenario ending cash", fmt.money(scenario_end), icon="🎚️",
               delta=fmt.signed_money(difference), delta_good=difference >= 0),
        ui.Kpi("Scenario net flow",
               fmt.signed_money(scenario.totals.get("net_flow", ZERO)), icon="🔄"),
        ui.Kpi("First problem period",
               scenario.first_negative.period.label if scenario.first_negative
               else "none", icon="⚠️"),
    ], columns=4)

    ui.chart(
        fc.scenario_comparison_line(base_future, scenario_future, theme, height=380,
                                    scenario_name="Scenario"),
        table=[{"Period": row.label,
                "Scenario income": fmt.money(row.assumption.income),
                "Scenario expenses": fmt.money(row.assumption.expenses),
                "Scenario closing cash": fmt.money(row.closing_cash)}
               for row in scenario_future],
        key="fc_scenario",
    )

    for alert in scenario.alerts:
        icon = {"critical": "🔴", "warning": "🟠"}.get(alert.severity, "🔵")
        (st.error if alert.severity == "critical" else st.warning)(
            f"{icon} {alert.message}")


def _net_worth_tab(theme, fmt: ui.Formatter, today: date) -> None:
    ui.section("Net worth outlook",
               "A straight-line projection: what you add each month, what you pay down, "
               "and optionally a return on your assets.")
    columns = st.columns(4)
    with columns[0]:
        months = st.slider("Months ahead", 6, 120, 36, key="nwp_months")
    with columns[1]:
        monthly_savings = ui.money_input("Added each month", ZERO, key="nwp_savings")
    with columns[2]:
        debt_reduction = ui.money_input("Debt cleared each month", ZERO, key="nwp_debt")
    with columns[3]:
        annual_return = ui.pct_input("Annual return on assets", ZERO, key="nwp_return",
                                     min_value=0.0, max_value=30.0)

    with ui.db_read() as session:
        averages = reporting_service.averages(session, 6, today)
        if monthly_savings == 0:
            monthly_savings = money(averages["savings"] + averages["investments"])
        if debt_reduction == 0:
            debt_reduction = averages["debt_payments"]
        points = networth_service.projection(
            session, months, monthly_savings=monthly_savings,
            monthly_debt_reduction=debt_reduction,
            annual_return_pct=annual_return, today=today,
        )

    st.caption(
        f"Defaults come from your last six months: {fmt.md_money(monthly_savings)} saved "
        f"and invested, {fmt.md_money(debt_reduction)} of debt payments per month."
    )
    ui.chart(
        fc.net_worth_chart(points[::max(1, len(points) // 24)], theme, height=380),
        table=[{"As of": point.as_of.isoformat(),
                "Assets": fmt.money(point.total_assets),
                "Liabilities": fmt.money(point.total_liabilities),
                "Net worth": fmt.money(point.net_worth)}
               for point in points],
        key="nwp_chart",
    )
    if points:
        first, last = points[0], points[-1]
        growth = money(last.net_worth - first.net_worth)
        st.metric(f"Net worth in {months} months", fmt.money(last.net_worth),
                  delta=fmt.signed_money(growth),
                  delta_color="normal" if growth >= 0 else "inverse")
    st.caption("A straight-line projection, not a market prediction. It assumes today's "
               "habits continue unchanged.")
