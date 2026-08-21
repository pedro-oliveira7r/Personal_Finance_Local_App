"""Forecast — where the money is heading."""

from __future__ import annotations

from datetime import date

import streamlit as st

from calculations.money import ZERO, money
from charts import dashboard_charts as dc
from charts import financial_charts as fc
from services import forecast_service, networth_service, reporting_service
from services.common import ServiceError
from ui import components as ui


def render() -> None:
    settings = ui.current_settings()
    today = date.today()

    ui.page_header(
        "Forecast",
        "A projection, clearly separated from what actually happened.",
        icon="🔮",
    )

    book = ui.currency_book()
    # Forecast is the one screen that opens combined: a projection of "how much
    # money will I have" is the question least served by looking at one
    # currency at a time.
    currency = ui.currency_picker(key="fc_currency", default=ui.ALL_CURRENCIES)

    if currency is None:
        missing = [c for c in book.active if not book.has_rate(c)]
        if missing:
            st.error(
                f"No exchange rate on file for {', '.join(missing)}. Set one on the "
                "Dashboard, or pick a single currency to project it on its own.",
                icon="💱",
            )
            return
        ui.converted_notice(book)

    display = currency or book.primary
    fmt = ui.formatter(display)
    theme = ui.theme(display)

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
                                 currency=display,
                                 help_text="Warn when projected cash dips below this.")

    with ui.db_read() as session:
        options = dict(months=months, history_months=history_months,
                       average_window=average_window, today=today,
                       low_cash_threshold=comfort)
        try:
            bundle = (forecast_service.build_combined(session, book=book, **options)
                      if currency is None
                      else forecast_service.build(session, currency=currency, **options))
        except ServiceError as exc:
            st.error(str(exc), icon="💱")
            return

    if not bundle.rows:
        ui.empty_state(
            "Nothing to project yet",
            "A forecast needs something to work from: a budget, some recurring rules, or "
            "a few months of history. Add any of the three and this fills in.",
            icon="🔮",
        )
        return

    _headline(bundle, fmt, months)
    if bundle.converted:
        _per_currency_breakdown(bundle, book)
    ui.divider()

    tabs = st.tabs(["Cash projection", "Net worth outlook"])
    with tabs[0]:
        _cash_tab(bundle, theme, fmt)
    with tabs[1]:
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


def _per_currency_breakdown(bundle, book) -> None:
    """Each currency's own closing cash, unconverted.

    A converted headline is only trustworthy if you can see what it was made
    of — this is the cheapest way to let someone check the arithmetic, and to
    notice a rate that has drifted out of date.
    """
    if not bundle.parts:
        return
    with st.expander("Show the per-currency breakdown"):
        rows = []
        for code, part in bundle.parts.items():
            ending = part.future_rows[-1] if part.future_rows else None
            rows.append({
                "currency": f"{book.symbol(code)} {code}",
                "start": ui.formatter(code).money(part.start_cash),
                "end": ui.formatter(code).money(
                    ending.closing_cash if ending else ZERO),
                "rate": ("—" if code == book.primary
                         else ui.formatter(book.primary).money(
                             book.rate_to_primary(code))),
            })
        st.dataframe(
            [{"Currency": r["currency"], "Cash now": r["start"],
              "Projected": r["end"], f"1 unit = ({book.primary})": r["rate"]}
             for r in rows],
            hide_index=True, **ui.wide(),
        )
        st.caption("These are each currency's own figures, with nothing converted.")
