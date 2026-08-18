"""Reports — historical analysis, trends and comparisons."""

from __future__ import annotations

from datetime import date

import streamlit as st

from calculations.money import ZERO, D, money, money_sum
from calculations.periods import shift_period
from charts import dashboard_charts as dc
from charts import financial_charts as fc
from constants import ALLOCATION_KINDS, CategoryKind
from services import category_service, reporting_service
from services.common import category_name_map
from ui import components as ui


def render() -> None:
    ui.page_header(
        "Reports",
        "What actually happened, over time — trends, comparisons and how realistic your "
        "budgets have been.",
        icon="📈",
    )

    tabs = st.tabs([
        "Overview", "Category trends", "Compare periods", "Patterns",
        "Budget accuracy", "Yearly summary",
    ])
    with tabs[0]:
        _overview()
    with tabs[1]:
        _trends()
    with tabs[2]:
        _compare()
    with tabs[3]:
        _patterns()
    with tabs[4]:
        _accuracy()
    with tabs[5]:
        _yearly()


# ==========================================================================
def _overview() -> None:
    fmt = ui.formatter()
    theme = ui.theme()
    today = date.today()

    columns = st.columns([0.3, 0.7])
    with columns[0]:
        months = st.select_slider("Months of history", [3, 6, 12, 18, 24, 36, 60],
                                  value=12, key="rep_months")

    with ui.db_read() as session:
        history = reporting_service.trailing_history(session, months, today)
        averages = reporting_service.averages(session, min(months, 12), today)

    if not history:
        ui.empty_state("Nothing to report yet",
                       "Record a few transactions and this page fills with trends.",
                       icon="📈")
        return

    actual = [row for row in history if row["is_actual"]] or history
    ui.kpi_row([
        ui.Kpi("Average income", fmt.money(averages["income"]), icon="📥"),
        ui.Kpi("Average expenses", fmt.money(averages["expenses"]), icon="📤"),
        ui.Kpi("Average saved", fmt.money(money(averages["savings"]
                                               + averages["investments"])), icon="🐖"),
        ui.Kpi("Average net", fmt.signed_money(averages["net"]), icon="🔄",
               delta_good=averages["net"] >= 0),
        ui.Kpi("Average savings rate", fmt.pct(averages.get("savings_rate", ZERO)),
               icon="📊"),
    ])

    ui.divider()
    left, right = st.columns(2)
    with left:
        ui.section("Income against outflow")
        ui.chart(dc.income_expense_bars(history, theme, height=340, show_net_line=True),
                 key="rep_inc_exp")
    with right:
        ui.section("Where the outflow went")
        ui.chart(dc.stacked_allocation_bars(history, theme, height=340),
                 key="rep_stack")

    ui.divider()
    left, right = st.columns([0.6, 0.4])
    with left:
        ui.section("Savings rate over time")
        ui.chart(dc.savings_rate_line(history, theme, height=280), key="rep_rate")
    with right:
        ui.section("Best and worst months")
        best = max(actual, key=lambda row: D(row["net"]))
        worst = min(actual, key=lambda row: D(row["net"]))
        st.metric(f"Best · {best['label']}", fmt.signed_money(best["net"]))
        st.metric(f"Worst · {worst['label']}", fmt.signed_money(worst["net"]))
        top_saver = max(actual, key=lambda row: D(row["savings_rate"]))
        st.metric(f"Highest savings rate · {top_saver['label']}",
                  fmt.pct(top_saver["savings_rate"]))

    ui.divider()
    ui.section("The whole table")
    ui.money_table(
        [{"period": row["label"], "income": row["income"], "expenses": row["expenses"],
          "savings": row["savings"], "investments": row["investments"],
          "debt": row["debt_payments"], "net": row["net"],
          "closing": row["closing_cash"], "rate": row["savings_rate"],
          "state": "actual" if row["is_actual"] else "in progress"}
         for row in history],
        [("period", "Period", "text"), ("income", "Income", "money"),
         ("expenses", "Expenses", "money"), ("savings", "Savings", "money"),
         ("investments", "Investments", "money"), ("debt", "Debt", "money"),
         ("net", "Net", "money"), ("closing", "Closing cash", "money"),
         ("rate", "Savings rate", "pct"), ("state", "", "text")],
        fmt, height=min(600, 60 + 36 * len(history)),
    )
    _download(history, "history")


def _download(rows, name: str) -> None:
    from import_export.csv_handler import rows_to_csv

    if not rows:
        return
    st.download_button(f"⬇ Download {name} as CSV", rows_to_csv(rows),
                       file_name=f"{name}-{date.today().isoformat()}.csv",
                       mime="text/csv", key=f"dl_{name}")


# ==========================================================================
def _trends() -> None:
    fmt = ui.formatter()
    theme = ui.theme()
    today = date.today()

    with ui.db_read() as session:
        parents = category_service.list_categories(
            session, kinds=list(ALLOCATION_KINDS), parents_only=True)
    if not parents:
        st.caption("No categories to chart.")
        return

    columns = st.columns([0.3, 0.7])
    with columns[0]:
        months = st.select_slider("Months", [6, 12, 18, 24, 36], value=12,
                                  key="trend_months")
    with columns[1]:
        options = [(cat.id, cat.name) for cat in parents]
        default = [cat.id for cat in parents[:5]]
        chosen = st.multiselect("Categories", [item[0] for item in options],
                                default=default,
                                format_func=lambda item: dict(options)[item],
                                key="trend_cats")
    if not chosen:
        st.caption("Pick at least one category.")
        return

    with ui.db_read() as session:
        periods = reporting_service.trailing_periods(session, months, today)
        rows = reporting_service.category_trend(session, chosen, periods)

    view = st.radio("View", ["Lines", "Stacked bars", "Heatmap"], horizontal=True,
                    key="trend_view")
    if view == "Lines":
        ui.chart(fc.category_trend_lines(rows, theme, height=380), key="trend_lines")
    elif view == "Stacked bars":
        ui.chart(fc.stacked_category_bars(rows, theme, height=380), key="trend_bars")
    else:
        ui.chart(fc.spending_heatmap(rows, theme, height=420), key="trend_heat")

    totals: dict[str, list] = {}
    for row in rows:
        totals.setdefault(row["category"], []).append(D(row["amount"]))
    summary = [
        {"category": name,
         "total": money_sum(values),
         "average": money(money_sum(values) / max(1, len(values))),
         "highest": max(values) if values else ZERO,
         "lowest": min(values) if values else ZERO}
        for name, values in totals.items()
    ]
    summary.sort(key=lambda item: item["total"], reverse=True)
    ui.money_table(
        summary,
        [("category", "Category", "text"), ("total", "Total", "money"),
         ("average", "Monthly average", "money"), ("highest", "Highest month", "money"),
         ("lowest", "Lowest month", "money")],
        fmt,
    )
    _download(rows, "category-trends")


# ==========================================================================
def _compare() -> None:
    fmt = ui.formatter()
    theme = ui.theme()
    today = date.today()
    settings = ui.current_settings()

    mode = st.radio("Comparison", ["Month over month", "Year over year", "Pick two"],
                    horizontal=True, key="cmp_mode")

    period = settings.current_period(today)
    if mode == "Month over month":
        with ui.db_read() as session:
            data = reporting_service.month_over_month(session, period, today)
        previous_label, current_label = "Previous month", "This month"
    elif mode == "Year over year":
        with ui.db_read() as session:
            data = reporting_service.year_over_year(session, period, today)
        previous_label, current_label = "Same month last year", "This month"
    else:
        columns = st.columns(2)
        options = [shift_period(period, offset, settings.first_day_of_month)
                   for offset in range(-36, 13)]
        keys = [item.key for item in options]
        with columns[0]:
            first_key = st.selectbox("Compare", keys,
                                     index=keys.index(shift_period(
                                         period, -1, settings.first_day_of_month).key),
                                     format_func=lambda item: next(
                                         p.label for p in options if p.key == item),
                                     key="cmp_a")
        with columns[1]:
            second_key = st.selectbox("With", keys, index=keys.index(period.key),
                                      format_func=lambda item: next(
                                          p.label for p in options if p.key == item),
                                      key="cmp_b")
        first = next(p for p in options if p.key == first_key)
        second = next(p for p in options if p.key == second_key)
        with ui.db_read() as session:
            data = reporting_service.compare_periods(session, second, first)
        previous_label, current_label = first.label, second.label

    if not data or not data.get("metrics"):
        st.caption("Not enough history to compare yet.")
        return

    metrics = data["metrics"]
    st.caption(f"**{data['previous_label']}** against **{data['current_label']}**")
    ui.chart(
        fc.comparison_bars(metrics, theme, height=340,
                           previous_label=previous_label,
                           current_label=current_label),
        key="cmp_chart",
    )

    names = {"income": "Income", "expenses": "Expenses", "savings": "Savings",
             "investments": "Investments", "debt_payments": "Debt payments",
             "net": "Net"}
    rows = []
    for key, values in metrics.items():
        change = values["change"]
        good = change >= 0 if key in {"income", "savings", "investments", "net"} \
            else change <= 0
        rows.append({
            "metric": names.get(key, key),
            "previous": values["previous"],
            "current": values["current"],
            "change": change,
            "change_pct": values["change_pct"],
            "verdict": ("✓ better" if good else "▲ worse") if change else "· flat",
        })
    ui.money_table(
        rows,
        [("metric", "Metric", "text"), ("previous", previous_label, "money"),
         ("current", current_label, "money"), ("change", "Change", "money"),
         ("change_pct", "Change %", "pct"), ("verdict", "", "text")],
        fmt,
    )


# ==========================================================================
def _patterns() -> None:
    fmt = ui.formatter()
    today = date.today()
    settings = ui.current_settings()
    period = settings.current_period(today)

    ui.section(
        "Your fixed cost base",
        "Categories you spend on almost every month. The volatility column shows how "
        "much they wobble — high volatility means the average is a poor planning figure.",
    )
    columns = st.columns([0.3, 0.7])
    with columns[0]:
        months = st.select_slider("Months to analyse", [3, 6, 9, 12, 18, 24], value=6,
                                  key="pat_months")

    with ui.db_read() as session:
        patterns = reporting_service.recurring_patterns(session, months, today)
        unusual = reporting_service.unusual_expenses(session, period, months=months,
                                                    today=today)
        # Shape the rows while the session is open: reading a relationship after
        # it closes would raise DetachedInstanceError.
        names = category_name_map(session)
        biggest = [
            {"date": txn.txn_date, "description": txn.description,
             "amount": txn.amount,
             "category": names.get(txn.category_id, "—") if txn.category_id else "—"}
            for txn in reporting_service.biggest_transactions(session, period, 12)
        ]

    if patterns:
        total = money_sum(row["average"] for row in patterns)
        st.metric("Recurring monthly base", fmt.money(total),
                  help="Sum of the averages below — roughly what you spend before any "
                       "discretionary decision.")
        ui.money_table(
            patterns,
            [("label", "Category", "text"), ("months_present", "Months seen", "int"),
             ("average", "Average", "money"), ("minimum", "Lowest", "money"),
             ("maximum", "Highest", "money"), ("spread", "Spread", "money"),
             ("volatility_pct", "Volatility", "pct")],
            fmt, height=min(520, 60 + 36 * len(patterns)),
        )
        _download(patterns, "recurring-patterns")
    else:
        st.caption("Not enough history to detect patterns yet.")

    ui.divider()
    ui.section(f"Unusually high in {period.label}",
               "Categories at least 50% above their own recent average.")
    if unusual:
        ui.money_table(
            unusual,
            [("label", "Category", "text"), ("amount", "This period", "money"),
             ("average", "Usual", "money"), ("change_pct", "Above usual", "pct")],
            fmt,
        )
    else:
        st.success("✅ Nothing stands out this period.")

    ui.divider()
    ui.section(f"Largest single expenses in {period.label}")
    if biggest:
        ui.money_table(
            biggest,
            [("date", "Date", "date"), ("description", "What", "text"),
             ("category", "Category", "text"), ("amount", "Amount", "money")],
            fmt,
        )
    else:
        st.caption("No expenses recorded in this period.")


# ==========================================================================
def _accuracy() -> None:
    fmt = ui.formatter()
    theme = ui.theme()
    today = date.today()

    ui.section(
        "How realistic were your budgets?",
        "100% means the plan matched reality exactly. Consistently low accuracy usually "
        "means a category needs a bigger allowance rather than more willpower.",
    )
    columns = st.columns([0.3, 0.7])
    with columns[0]:
        months = st.select_slider("Closed periods to review", [3, 6, 12, 18, 24],
                                  value=12, key="acc_months")

    with ui.db_read() as session:
        rows = reporting_service.budget_accuracy(session, months, today)

    rows = [row for row in rows if row["planned_out"] or row["planned_in"]]
    if not rows:
        st.caption("No completed periods with a budget to measure yet.")
        return

    average_expense = money(money_sum(D(row["expense_accuracy"]) for row in rows)
                            / len(rows))
    average_income = money(money_sum(D(row["income_accuracy"]) for row in rows)
                           / len(rows))
    columns = st.columns(2)
    with columns[0]:
        st.metric("Average spending-plan accuracy", fmt.pct(average_expense))
    with columns[1]:
        st.metric("Average income-plan accuracy", fmt.pct(average_income))

    ui.chart(fc.budget_accuracy_bars(rows, theme, height=340), key="acc_chart")
    ui.money_table(
        rows,
        [("label", "Period", "text"), ("planned_in", "Planned income", "money"),
         ("actual_in", "Actual income", "money"),
         ("income_accuracy", "Income accuracy", "pct"),
         ("planned_out", "Planned outflow", "money"),
         ("actual_out", "Actual outflow", "money"),
         ("expense_accuracy", "Spending accuracy", "pct")],
        fmt,
    )
    _download(rows, "budget-accuracy")


# ==========================================================================
def _yearly() -> None:
    fmt = ui.formatter()
    theme = ui.theme()

    with ui.db_read() as session:
        years = reporting_service.available_years(session)
    if not years:
        st.caption("No data yet.")
        return

    year = st.selectbox("Year", sorted(years, reverse=True), key="year_pick")
    with ui.db_read() as session:
        summary = reporting_service.annual_summary(session, year)

    ui.kpi_row([
        ui.Kpi("Income", fmt.money(summary["income"]), icon="📥"),
        ui.Kpi("Expenses", fmt.money(summary["expenses"]), icon="📤"),
        ui.Kpi("Saved", fmt.money(summary["savings"]), icon="🐖"),
        ui.Kpi("Invested", fmt.money(summary["investments"]), icon="📈"),
        ui.Kpi("Net", fmt.signed_money(summary["net"]), icon="🔄",
               delta_good=summary["net"] >= 0),
    ])

    ui.divider()
    ui.chart(dc.income_expense_bars(summary["rows"], theme, height=340,
                                    show_net_line=True),
             key="year_chart")
    ui.money_table(
        [{"period": row["label"], "income": row["income"], "expenses": row["expenses"],
          "savings": row["savings"], "investments": row["investments"],
          "debt": row["debt_payments"], "net": row["net"], "rate": row["savings_rate"]}
         for row in summary["rows"]],
        [("period", "Month", "text"), ("income", "Income", "money"),
         ("expenses", "Expenses", "money"), ("savings", "Savings", "money"),
         ("investments", "Investments", "money"), ("debt", "Debt", "money"),
         ("net", "Net", "money"), ("rate", "Savings rate", "pct")],
        fmt,
    )
    _download(summary["rows"], f"summary-{year}")
