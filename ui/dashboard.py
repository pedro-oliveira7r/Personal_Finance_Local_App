"""Dashboard — the financial control centre."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import streamlit as st

from calculations.money import ZERO, D, money, money_sum
from charts import dashboard_charts as dc
from charts import financial_charts as fc
from services import (
    account_service,
    forecast_service,
    goal_service,
    reporting_service,
)
from ui import components as ui


def render() -> None:
    fmt = ui.formatter()
    settings = ui.current_settings()
    theme = ui.theme()
    today = date.today()

    ui.page_header(
        "Dashboard",
        "Everything about your money in one place — planned, actual and projected.",
        icon="📊",
    )

    period = ui.period_picker(key="dash_period")

    with ui.db_read() as session:
        snapshot = reporting_service.dashboard(session, period, today)
        history = reporting_service.trailing_history(session, 12, today)
        categories = reporting_service.category_totals(
            session, period, kinds=["expense", "savings", "investment", "debt"],
        )
        bundle = forecast_service.build(session, months=9, history_months=6, today=today)
        account_views = account_service.balance_views(session, as_of=today, period=period)
        averages = reporting_service.averages(session, 6, today)

    if not snapshot.has_data:
        _first_run(period)
        return

    _kpis(snapshot, averages, fmt, period, today)
    ui.divider()
    ui.alert_panel(snapshot.alerts, limit=10)

    tabs = st.tabs([
        "Overview", "Budget performance", "Cash flow", "Where it goes",
        "Goals", "Accounts",
    ])

    with tabs[0]:
        _overview(snapshot, bundle, history, theme, fmt)
    with tabs[1]:
        _budget_performance(snapshot, theme, fmt)
    with tabs[2]:
        _cash_flow(snapshot, history, theme, fmt, settings)
    with tabs[3]:
        _where_it_goes(categories, snapshot, theme, fmt)
    with tabs[4]:
        _goals(snapshot, theme, fmt)
    with tabs[5]:
        _accounts(account_views, theme, fmt)


# --------------------------------------------------------------------------
def _first_run(period) -> None:
    ui.empty_state(
        "Your dashboard is waiting for its first numbers",
        "Add an account and a couple of transactions, or build a budget for "
        f"{period.label}. Once anything is recorded, this screen fills in with your "
        "cash position, budget performance, alerts and a forward projection.",
        icon="📊",
        action_label="Go to Budget planning",
        action=lambda: _goto("budget"),
        secondary="Prefer to explore first? Settings → Data → Load demo data gives you "
                  "18 months of realistic sample figures.",
    )


def _goto(slug: str) -> None:
    st.session_state["_nav_page"] = slug
    st.rerun()


# --------------------------------------------------------------------------
def _kpis(snapshot, averages, fmt: ui.Formatter, period, today: date) -> None:
    income_delta = money(snapshot.income_actual - snapshot.income_planned)
    expense_delta = money(snapshot.expenses_actual - snapshot.expenses_planned)
    elapsed = int(period.elapsed_fraction(today) * 100)

    ui.kpi_row([
        ui.Kpi("Cash available", fmt.money(snapshot.cash), icon="💵",
               help_text="Checking, savings and wallet balances added together."),
        ui.Kpi("Net worth", fmt.money(snapshot.net_worth), icon="🏛️",
               help_text="Everything you own minus everything you owe."),
        ui.Kpi("Net cash flow", fmt.signed_money(snapshot.net_cash_flow), icon="🔄",
               delta=f"{elapsed}% of {period.short_label} elapsed",
               delta_good=None,
               help_text="Money in minus money out inside this period."),
        ui.Kpi("Savings rate", fmt.pct(snapshot.savings_rate_pct), icon="🐖",
               delta=f"6-month average {fmt.pct(averages.get('savings_rate', ZERO))}",
               delta_good=None,
               help_text="Share of received income that went to savings or investments."),
        ui.Kpi("Budget used", fmt.pct(snapshot.budget_utilisation_pct), icon="🧮",
               delta=f"{elapsed}% of the period gone",
               delta_good=None,
               help_text="Actual outflow against everything you allocated."),
    ])

    emergency = ("—" if snapshot.emergency_months is None
                 else f"{snapshot.emergency_months} months")
    ui.kpi_row([
        ui.Kpi("Income this period", fmt.money(snapshot.income_actual), icon="📥",
               delta=f"{fmt.signed_money(income_delta)} vs plan",
               delta_good=income_delta >= 0,
               help_text=f"Planned: {fmt.money(snapshot.income_planned)}"),
        ui.Kpi("Spending this period", fmt.money(snapshot.expenses_actual), icon="📤",
               delta=f"{fmt.signed_money(expense_delta)} vs plan",
               delta_good=expense_delta <= 0,
               help_text=f"Planned: {fmt.money(snapshot.expenses_planned)}"),
        ui.Kpi("Saved & invested", fmt.money(snapshot.savings_actual), icon="📈",
               delta=f"planned {fmt.money(snapshot.savings_planned)}",
               delta_good=None),
        ui.Kpi("Total owed", fmt.money(snapshot.total_debt), icon="⛓️",
               help_text="Credit cards, loans and any debts without their own account."),
        ui.Kpi("Emergency cover", emergency, icon="🛟",
               help_text="How long your cash would cover an average month of spending. "
                         "Three to six months is the usual comfort range."),
    ])

    if snapshot.budget is not None and snapshot.budget.has_plan:
        result = snapshot.budget.result
        pills = [(f"{result.status_label}: {fmt.money(abs(result.remaining))}",
                  "success" if result.is_balanced else
                  ("warning" if result.remaining > 0 else "critical"))]
        pills.append((f"Available to budget {fmt.money(result.available)}", "info"))
        if snapshot.budget.timing is not None and snapshot.budget.timing.late:
            pills.append((f"{fmt.money(snapshot.budget.timing.late)} arrived late",
                          "warning"))
        ui.status_pills(pills)


# --------------------------------------------------------------------------
def _overview(snapshot, bundle, history, theme, fmt: ui.Formatter) -> None:
    left, right = st.columns([0.56, 0.44])

    with left:
        ui.section("Cash over time",
                   "Solid is what happened, dashed is what your plan and rules imply.")
        ui.chart(
            dc.cash_balance_line(bundle.rows, theme, height=330),
            table=[
                {"Period": row.label,
                 "Basis": row.source_label,
                 "Income": fmt.money(row.assumption.income),
                 "Outflow": fmt.money(row.assumption.total_outflow),
                 "Closing cash": fmt.money(row.closing_cash),
                 "Free of earmarks": fmt.money(row.free_cash)}
                for row in bundle.rows
            ],
            key="dash_cash",
        )
        if bundle.first_negative is not None:
            st.error(
                f"🔴 At this rate cash turns negative in "
                f"**{bundle.first_negative.period.label}** "
                f"({fmt.money(bundle.first_negative.closing_cash)}).",
            )
        elif bundle.lowest is not None:
            st.info(
                f"🔵 Lowest projected point: **{bundle.lowest.period.label}** at "
                f"{fmt.money(bundle.lowest.closing_cash)}.",
            )

    with right:
        ui.section("Income against outflow", "Twelve months of actuals.")
        ui.chart(
            dc.income_expense_bars(history, theme, height=330, show_net_line=True),
            table=[
                {"Period": row["label"], "Income": fmt.money(row["income"]),
                 "Outflow": fmt.money(row["total_outflow"]),
                 "Net": fmt.signed_money(row["net"]),
                 "Savings rate": fmt.pct(row["savings_rate"])}
                for row in history
            ],
            key="dash_inc_exp",
        )

    ui.divider()
    left, right = st.columns(2)
    with left:
        ui.section("Coming up in the next 30 days")
        if snapshot.upcoming:
            ui.money_table(
                [{"date": txn.txn_date, "description": txn.description,
                  "kind": txn.kind.title(), "amount": txn.amount}
                 for txn in snapshot.upcoming[:12]],
                [("date", "Date", "date"), ("description", "What", "text"),
                 ("kind", "Type", "text"), ("amount", "Amount", "money")],
                fmt, height=300,
            )
        else:
            st.caption("Nothing scheduled. Add recurring rules to see what is coming.")
    with right:
        ui.section("Past due")
        if snapshot.overdue:
            st.warning(
                f"⚠️ {len(snapshot.overdue)} planned transaction(s) were never marked "
                f"as done — {fmt.money(money_sum(t.amount for t in snapshot.overdue))} "
                "in total.",
            )
            ui.money_table(
                [{"date": txn.txn_date, "description": txn.description,
                  "amount": txn.amount} for txn in snapshot.overdue[:12]],
                [("date", "Was due", "date"), ("description", "What", "text"),
                 ("amount", "Amount", "money")],
                fmt, height=260,
            )
            if st.button("Review them in Transactions", key="dash_overdue_go"):
                _goto("transactions")
        else:
            st.success("✅ Nothing overdue.")


# --------------------------------------------------------------------------
def _budget_performance(snapshot, theme, fmt: ui.Formatter) -> None:
    tracking = snapshot.tracking
    if tracking is None or not tracking.rows:
        ui.empty_state(
            "No budget for this period yet",
            "Budget planning lets you assign every unit of income a job, then this tab "
            "shows how reality compared.",
            icon="🧮", action_label="Build this period's budget",
            action=lambda: _goto("budget"),
        )
        return

    summary_pills = []
    for label, summary, favourable_low in (
        ("Income", tracking.income, False),
        ("Expenses", tracking.expenses, True),
        ("Savings", tracking.savings, False),
        ("Investments", tracking.investments, False),
        ("Debt", tracking.debt, True),
    ):
        if summary.planned == 0 and summary.actual == 0:
            continue
        good = (summary.variance <= 0) if favourable_low else (summary.variance >= 0)
        summary_pills.append((
            f"{label}: {fmt.money(summary.actual)} of {fmt.money(summary.planned)} "
            f"({fmt.signed_money(summary.variance)})",
            "success" if good else "warning",
        ))
    ui.status_pills(summary_pills)

    left, right = st.columns([0.52, 0.48])
    with left:
        ui.section("Planned against actual", "The twelve largest lines.")
        ui.chart(dc.planned_vs_actual_bars(tracking.rows, theme, height=420),
                 key="dash_pva")
    with right:
        ui.section("How much of each budget is used",
                   "The dashed line is 100% of the plan.")
        ui.chart(dc.utilisation_bullets(tracking.allocation_rows, theme, limit=11),
                 key="dash_util")

    ui.divider()
    left, right = st.columns(2)
    with left:
        ui.section("Biggest misses", "Warm bars are over budget, cool are under.")
        ui.chart(dc.variance_diverging_bars(tracking.allocation_rows, theme, height=320),
                 key="dash_var")
    with right:
        ui.section("Top overspending")
        if snapshot.top_overspend:
            ui.money_table(
                [{"label": row.label, "planned": row.planned, "actual": row.actual,
                  "over": row.overshoot, "used": row.consumed_pct}
                 for row in snapshot.top_overspend],
                [("label", "Category", "text"), ("planned", "Planned", "money"),
                 ("actual", "Actual", "money"), ("over", "Over by", "money"),
                 ("used", "Used", "pct")],
                fmt,
            )
        else:
            st.success("✅ Nothing is over budget in this period.")

        ui.section("Biggest savings against plan")
        if snapshot.top_underspend:
            ui.money_table(
                [{"label": row.label, "planned": row.planned, "actual": row.actual,
                  "under": abs(row.variance)} for row in snapshot.top_underspend],
                [("label", "Category", "text"), ("planned", "Planned", "money"),
                 ("actual", "Actual", "money"), ("under", "Under by", "money")],
                fmt,
            )
        else:
            st.caption("No categories came in under plan.")

    if tracking.unbudgeted:
        ui.divider()
        st.warning(
            f"⚠️ {len(tracking.unbudgeted)} category(ies) had activity with no budget "
            f"line — {fmt.money(money_sum(r.actual for r in tracking.unbudgeted))} in total.",
        )
        ui.money_table(
            [{"label": row.label, "actual": row.actual, "kind": row.kind.title()}
             for row in tracking.unbudgeted],
            [("label", "Category", "text"), ("kind", "Type", "text"),
             ("actual", "Spent", "money")],
            fmt,
        )


# --------------------------------------------------------------------------
def _cash_flow(snapshot, history, theme, fmt: ui.Formatter, settings) -> None:
    flow = snapshot.flow
    left, right = st.columns([0.48, 0.52])

    with left:
        ui.section("This period, step by step")
        ui.chart(dc.cashflow_waterfall(flow, theme, height=330), key="dash_waterfall")
        if flow is not None:
            rows = [
                ("Opening cash", flow.opening_cash),
                ("Income received", flow.income_received),
                ("Income available to budget", flow.income_available),
                ("Income earned (accrual)", flow.income_earned),
                ("Expenses paid", flow.expenses_paid),
                ("Transfers in", flow.transfers_in),
                ("Transfers out", flow.transfers_out),
                ("Net flow", flow.net_flow),
                ("Closing cash", flow.closing_cash),
            ]
            with st.expander("The same numbers as a list"):
                for label, value in rows:
                    st.write(f"**{label}** · {fmt.money(value)}")

    with right:
        ui.section("Where the outflow went, month by month")
        ui.chart(dc.stacked_allocation_bars(history, theme, height=330),
                 key="dash_stack")

    ui.divider()
    left, right = st.columns([0.6, 0.4])
    with left:
        ui.section("Savings rate trend", "Share of income kept, per period.")
        ui.chart(dc.savings_rate_line(history, theme, height=270,
                                      target_pct=Decimal("20")),
                 key="dash_savings_rate",
                 caption="The dashed line marks a 20% reference — a common target, "
                         "not a rule.")
    with right:
        ui.section("Income timing")
        timing = snapshot.budget.timing if snapshot.budget else None
        if timing is None:
            st.caption("No income recorded for this period.")
        else:
            st.metric("Earned in this period", fmt.money(timing.earned),
                      help="Income that belongs to this period, regardless of when the "
                           "cash arrived.")
            st.metric("Actually received", fmt.money(timing.received))
            st.metric("Available to budget here", fmt.money(timing.available),
                      help="Governed by your income availability rule: "
                           f"{settings.income_availability_rule.replace('_', ' ')}.")
            if timing.expected:
                st.metric("Still expected", fmt.money(timing.expected))
            if timing.late:
                st.warning(f"⚠️ {fmt.money(timing.late)} was earned here but the cash "
                           "landed after the period closed.")


# --------------------------------------------------------------------------
def _where_it_goes(categories, snapshot, theme, fmt: ui.Formatter) -> None:
    if not categories:
        st.caption("No spending recorded in this period yet.")
        return
    left, right = st.columns([0.55, 0.45])
    with left:
        ui.section("Spending by category")
        ui.chart(dc.category_treemap(categories, theme, height=380),
                 key="dash_treemap")
    with right:
        ui.section("Allocation of this period's plan")
        budget = snapshot.budget
        if budget is not None and budget.result.by_kind:
            slices = [(kind.title(), amount)
                      for kind, amount in budget.result.by_kind.items()]
            if budget.result.unallocated > 0:
                slices.append(("Unassigned", budget.result.unallocated))
            ui.chart(
                dc.allocation_donut(
                    slices, theme, height=330,
                    center_label="allocated",
                    center_value=fmt.money(budget.result.allocated, compact=True),
                ),
                key="dash_donut",
            )
        else:
            st.caption("Build a budget to see how the plan splits up.")

    ui.divider()
    ui.section("Every category in this period")
    ui.money_table(
        [{"label": row["label"], "kind": row["kind"].title(),
          "amount": row["amount"], "share": row["share_pct"]}
         for row in categories],
        [("label", "Category", "text"), ("kind", "Type", "text"),
         ("amount", "Spent", "money"), ("share", "Share", "pct")],
        fmt, height=340,
    )


# --------------------------------------------------------------------------
def _goals(snapshot, theme, fmt: ui.Formatter) -> None:
    progresses = snapshot.goal_progress
    if not progresses:
        ui.empty_state(
            "No goals yet",
            "Goals turn “I should save more” into a number per month. Set a target, a "
            "date, and the app works out the monthly contribution and whether you are "
            "on track.",
            icon="🚩", action_label="Create a goal",
            action=lambda: _goto("goals"),
        )
        return
    ui.chart(dc.goal_progress_bars(progresses, theme), key="dash_goals")
    ui.money_table(
        [{"name": f"{p.status_icon} {p.name}", "target": p.target_amount,
          "saved": p.current_amount, "remaining": p.remaining,
          "progress": p.progress_pct, "planned": p.planned_monthly,
          "required": p.required_monthly, "target_date": p.target_date,
          "finish": p.projected_completion}
         for p in progresses],
        [("name", "Goal", "text"), ("target", "Target", "money"),
         ("saved", "Saved", "money"), ("remaining", "To go", "money"),
         ("progress", "Progress", "pct"), ("planned", "Paying", "money"),
         ("required", "Needs", "money"), ("target_date", "Target date", "date"),
         ("finish", "At this rate", "date")],
        fmt,
    )


# --------------------------------------------------------------------------
def _accounts(views, theme, fmt: ui.Formatter) -> None:
    if not views:
        st.caption("No accounts yet.")
        return
    ui.chart(fc.account_balance_bars(views, theme, height=320), key="dash_accounts")
    ui.money_table(
        [{"name": view.name, "type": view.type_label,
          "balance": view.display_balance,
          "movement": view.movement_this_period,
          "util": view.utilisation_pct,
          "note": "owed" if view.is_liability else ""}
         for view in views],
        [("name", "Account", "text"), ("type", "Type", "text"),
         ("balance", "Balance", "money"), ("note", "", "text"),
         ("movement", "Change this period", "money"),
         ("util", "Card used", "pct")],
        fmt,
    )
