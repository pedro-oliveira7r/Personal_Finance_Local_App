"""Goals and debts — targets to reach, and balances to clear."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import streamlit as st

from calculations.goals import projected_balance_at
from calculations.money import ZERO, D, money, money_sum
from charts import dashboard_charts as dc
from charts import financial_charts as fc
from constants import (
    ALLOCATION_KINDS,
    DEBT_TYPE_LABELS,
    GOAL_TYPE_LABELS,
    CategoryKind,
    DebtType,
    GoalStatus,
    GoalType,
    PayoffStrategy,
)
from services import account_service, category_service, debt_service, goal_service
from ui import components as ui


def render() -> None:
    ui.page_header(
        "Goals & debts",
        "What you are saving toward, and what you are paying off.",
        icon="🚩",
    )
    tabs = st.tabs(["Goals", "Debts", "Payoff strategies"])
    with tabs[0]:
        _goals()
    with tabs[1]:
        _debts()
    with tabs[2]:
        _strategies()


# ==========================================================================
# Goals
# ==========================================================================
def _goals() -> None:
    fmt = ui.formatter()
    theme = ui.theme()
    today = date.today()

    with ui.db_read() as session:
        progresses = goal_service.all_progress(session, today=today)
        all_goals = goal_service.list_goals(session)
        totals = goal_service.totals(session, today=today)
        accounts = account_service.options_for_select(session)
        savings_cats = category_service.options_for_select(
            session, kinds=[CategoryKind.SAVINGS.value,
                            CategoryKind.INVESTMENT.value])

    if not all_goals:
        ui.empty_state(
            "No goals yet",
            "A goal turns a vague intention into a number per month. Give it a target "
            "amount and a date; the app works out what you need to put aside, tracks "
            "what you actually contribute, and tells you when you will get there.",
            icon="🚩",
        )
        _new_goal_form(accounts, savings_cats)
        return

    ui.kpi_row([
        ui.Kpi("Active goals", str(int(totals["count"])), icon="🚩"),
        ui.Kpi("Total target", fmt.money(totals["target"]), icon="🎯"),
        ui.Kpi("Saved so far", fmt.money(totals["saved"]), icon="🐖"),
        ui.Kpi("Still to go", fmt.money(totals["remaining"]), icon="📉"),
        ui.Kpi("Needed per month", fmt.money(totals["required_monthly"]), icon="📆",
               delta=f"currently paying {fmt.money(totals['planned_monthly'])}",
               delta_good=totals["planned_monthly"] >= totals["required_monthly"]),
    ])

    behind = [item for item in progresses if item.on_track is False]
    if behind:
        st.warning(
            "🟠 Behind schedule: " + ", ".join(f"**{item.name}**" for item in behind) +
            ". Either raise the monthly contribution or push the target date out.",
        )

    ui.divider()
    ui.chart(dc.goal_progress_bars(progresses, theme), key="goals_chart")
    ui.money_table(
        [{"name": f"{item.status_icon} {item.name}", "target": item.target_amount,
          "saved": item.current_amount, "remaining": item.remaining,
          "progress": item.progress_pct, "planned": item.planned_monthly,
          "required": item.required_monthly, "months": item.months_remaining,
          "target_date": item.target_date, "finish": item.projected_completion}
         for item in progresses],
        [("name", "Goal", "text"), ("target", "Target", "money"),
         ("saved", "Saved", "money"), ("remaining", "To go", "money"),
         ("progress", "Progress", "pct"), ("planned", "Paying", "money"),
         ("required", "Needs", "money"), ("months", "Months left", "int"),
         ("target_date", "Target date", "date"), ("finish", "At this rate", "date")],
        fmt,
    )

    ui.divider()
    _contribute(progresses, accounts, fmt)
    ui.divider()
    _distribute(fmt)
    ui.divider()
    _goal_projection(progresses, fmt)
    ui.divider()
    _new_goal_form(accounts, savings_cats)
    _edit_goal(all_goals, accounts, savings_cats, fmt)


def _contribute(progresses, accounts, fmt: ui.Formatter) -> None:
    ui.section("Record a contribution",
               "If the goal has its own account the money moves as a transfer, so your "
               "total cash is unchanged — it is simply earmarked.")
    options = [(item.goal_id, item.name) for item in progresses if item.goal_id]
    if not options or not accounts:
        return
    columns = st.columns([0.28, 0.2, 0.24, 0.28])
    with columns[0]:
        goal_id = st.selectbox("Goal", [item[0] for item in options],
                              format_func=lambda item: dict(options)[item],
                              key="contrib_goal")
    with columns[1]:
        amount = ui.money_input("Amount", ZERO, key="contrib_amount")
    with columns[2]:
        on_date = st.date_input("Date", value=date.today(), key="contrib_date")
    with columns[3]:
        from_account = st.selectbox("From account", [item[0] for item in accounts],
                                    format_func=lambda item: dict(accounts)[item],
                                    key="contrib_account")
    if st.button("Record the contribution", type="primary", key="contrib_go"):
        if amount <= 0:
            st.error("Enter an amount greater than zero.")
        else:
            ui.run_action(
                lambda session: goal_service.record_contribution(
                    session, goal_id, amount, on_date=on_date,
                    from_account_id=from_account),
                success=f"{money(amount)} recorded.",
            )


def _distribute(fmt: ui.Formatter) -> None:
    ui.section("Split spare money across goals",
               "Ranked by urgency: the goal with the nearest deadline and the largest "
               "monthly requirement gets funded first.")
    columns = st.columns([0.3, 0.7])
    with columns[0]:
        available = ui.money_input("Amount to split", ZERO, key="dist_amount")
    if available <= 0:
        return
    with ui.db_read() as session:
        plan = goal_service.suggest_distribution(session, available)
    if not plan:
        st.caption("No goals still need funding.")
        return
    ui.money_table(
        [{"name": progress.name, "give": give, "required": progress.required_monthly,
          "target_date": progress.target_date}
         for progress, give in plan],
        [("name", "Goal", "text"), ("give", "Suggested", "money"),
         ("required", "Needs monthly", "money"), ("target_date", "Target date", "date")],
        fmt,
    )
    st.caption("This is a suggestion — record the contributions above if you agree.")


def _goal_projection(progresses, fmt: ui.Formatter) -> None:
    ui.section("What if I contribute more?")
    options = [(item.goal_id, item.name) for item in progresses if item.goal_id]
    if not options:
        return
    columns = st.columns([0.3, 0.22, 0.22, 0.26])
    with columns[0]:
        goal_id = st.selectbox("Goal", [item[0] for item in options],
                              format_func=lambda item: dict(options)[item],
                              key="proj_goal")
    goal = next(item for item in progresses if item.goal_id == goal_id)
    with columns[1]:
        monthly = ui.money_input("Monthly contribution",
                                 goal.planned_monthly or goal.average_monthly,
                                 key="proj_monthly")
    with columns[2]:
        months = st.number_input("Over how many months", min_value=1, max_value=480,
                                 value=max(1, goal.months_remaining or 12), step=1,
                                 key="proj_months")
    with columns[3]:
        rate = ui.pct_input("Annual return %", ZERO, key="proj_rate",
                            min_value=0.0, max_value=30.0,
                            help_text="If the money sits somewhere that earns interest.")

    projected = projected_balance_at(goal.current_amount, monthly, int(months),
                                    annual_rate_pct=rate)
    reaches = projected >= goal.target_amount
    columns = st.columns(3)
    with columns[0]:
        st.metric("Balance after that", fmt.money(projected))
    with columns[1]:
        st.metric("Target", fmt.money(goal.target_amount))
    with columns[2]:
        gap = money(goal.target_amount - projected)
        st.metric("Gap", fmt.money(abs(gap)),
                  delta="target reached" if reaches else "still short",
                  delta_color="normal" if reaches else "inverse")
    if reaches:
        st.success(f"✅ {fmt.money(monthly)} a month for {int(months)} months clears "
                   f"“{goal.name}”.")
    else:
        from calculations.goals import required_contribution

        needed = required_contribution(goal.remaining, int(months))
        st.info(f"🔵 To finish “{goal.name}” in {int(months)} months you would need "
                f"**{fmt.money(needed)}** a month.")


def _new_goal_form(accounts, savings_cats) -> None:
    with st.expander("➕ New goal"):
        with st.form("new_goal", clear_on_submit=True):
            columns = st.columns([0.34, 0.33, 0.33])
            with columns[0]:
                name = st.text_input("Name", placeholder="e.g. Emergency fund")
            with columns[1]:
                goal_type = st.selectbox("Kind", list(GOAL_TYPE_LABELS),
                                        format_func=lambda item: GOAL_TYPE_LABELS[item])
            with columns[2]:
                priority = st.slider("Priority", 1, 5, 3,
                                    help="1 is most important — used when splitting "
                                         "spare money.")

            columns = st.columns(4)
            with columns[0]:
                target = ui.money_input("Target amount", ZERO, key="ng_target")
            with columns[1]:
                starting = ui.money_input("Already saved", ZERO, key="ng_start",
                                          help_text="Money set aside before you started "
                                                    "tracking here.")
            with columns[2]:
                monthly = ui.money_input("Planned monthly", ZERO, key="ng_monthly")
            with columns[3]:
                target_date = st.date_input(
                    "Target date", value=date.today() + timedelta(days=365))

            columns = st.columns([0.3, 0.3, 0.2, 0.2])
            with columns[0]:
                account_id = ui.select_with_none(
                    "Held in account", accounts, none_label="— not tied to one —",
                    key="ng_account",
                    help_text="When set, contributions are recorded as transfers.")
            with columns[1]:
                category_id = ui.select_with_none("Category for contributions",
                                                 savings_cats, none_label="— automatic —",
                                                 key="ng_cat")
            with columns[2]:
                icon = st.text_input("Icon", max_chars=2)
            with columns[3]:
                st.write("")
            notes = st.text_input("Notes (optional)")

            if st.form_submit_button("Create the goal", type="primary"):
                if not name.strip():
                    st.error("Give the goal a name.")
                elif target <= 0:
                    st.error("The target has to be greater than zero.")
                elif starting > target:
                    st.error("You have already saved more than the target — raise it.")
                else:
                    ui.run_action(
                        lambda session: goal_service.create_goal(session, {
                            "name": name, "goal_type": goal_type,
                            "target_amount": target, "starting_amount": starting,
                            "planned_monthly": monthly, "target_date": target_date,
                            "account_id": account_id, "category_id": category_id,
                            "priority": int(priority), "icon": icon or None,
                            "notes": notes or None,
                        }),
                        success=f"Goal “{name}” created.",
                    )


def _edit_goal(goals, accounts, savings_cats, fmt: ui.Formatter) -> None:
    with st.expander("✏️ Edit or close a goal"):
        options = [(g.id, f"{g.name} ({g.status})") for g in goals]
        goal_id = ui.select_with_none("Goal", options, none_label="— pick one —",
                                     key="eg_pick")
        if goal_id is None:
            return
        goal = next(g for g in goals if g.id == goal_id)

        columns = st.columns(4)
        with columns[0]:
            name = st.text_input("Name", value=goal.name, key="eg_name")
        with columns[1]:
            target = ui.money_input("Target", goal.target_amount, key="eg_target")
        with columns[2]:
            monthly = ui.money_input("Planned monthly", goal.planned_monthly,
                                     key="eg_monthly")
        with columns[3]:
            target_date = st.date_input("Target date",
                                       value=goal.target_date or date.today(),
                                       key="eg_date")
        columns = st.columns(4)
        with columns[0]:
            status = st.selectbox("Status", GoalStatus.values(),
                                 index=GoalStatus.values().index(goal.status),
                                 format_func=str.title, key="eg_status")
        with columns[1]:
            priority = st.slider("Priority", 1, 5, goal.priority, key="eg_priority")
        with columns[2]:
            account_id = ui.select_with_none("Held in account", accounts,
                                            value=goal.account_id,
                                            none_label="— none —", key="eg_account")
        with columns[3]:
            starting = ui.money_input("Already saved", goal.starting_amount,
                                      key="eg_start")

        actions = st.columns([0.4, 0.3, 0.3])
        with actions[0]:
            if st.button("Save changes", type="primary", key="eg_save",
                         **ui.wide()):
                ui.run_action(
                    lambda session: goal_service.update_goal(session, goal_id, {
                        "name": name, "target_amount": target,
                        "planned_monthly": monthly, "target_date": target_date,
                        "status": status, "priority": int(priority),
                        "account_id": account_id, "starting_amount": starting,
                    }),
                    success=f"“{name}” updated.",
                )
        with actions[1]:
            if st.button("✅ Mark as achieved", key="eg_achieve",
                         **ui.wide()):
                ui.run_action(
                    lambda session: goal_service.set_status(
                        session, goal_id, GoalStatus.ACHIEVED.value),
                    success=f"“{goal.name}” marked as achieved.",
                )
        with actions[2]:
            if ui.confirm_action(
                "🗑 Delete", f"del_goal_{goal_id}",
                prompt=f"Delete “{goal.name}”? Contributions stay in your transaction "
                       "history and a snapshot goes to the recycle bin.",
                confirm_label="Delete the goal", **ui.wide(),
            ):
                ui.run_action(
                    lambda session: goal_service.delete_goal(session, goal_id,
                                                             force=True),
                    success=f"“{goal.name}” deleted.",
                )


# ==========================================================================
# Debts
# ==========================================================================
def _debts() -> None:
    fmt = ui.formatter()
    theme = ui.theme()
    settings = ui.current_settings()
    today = date.today()
    period = settings.current_period(today)

    with ui.db_read() as session:
        views = debt_service.views(session, period=period, today=today)
        totals = debt_service.totals(session)
        alerts = debt_service.alerts(session)
        accounts = account_service.options_for_select(session)
        debt_cats = category_service.options_for_select(
            session, kinds=[CategoryKind.DEBT.value, CategoryKind.EXPENSE.value])
        payoff = debt_service.payoff_series(session, 72, today)

    if not views:
        ui.empty_state(
            "No debts tracked",
            "Add a card, a loan or a financed purchase and the app projects the payoff "
            "date, the total interest, and what an extra payment each month would save.",
            icon="⛓️",
        )
        _new_debt_form(accounts, debt_cats)
        return

    ui.kpi_row([
        ui.Kpi("Total owed", fmt.money(totals["balance"]), icon="⛓️"),
        ui.Kpi("Minimum payments", fmt.money(totals["minimum_payments"]), icon="📆"),
        ui.Kpi("You plan to pay", fmt.money(totals["planned_payments"]), icon="💸"),
        ui.Kpi("Interest per month", fmt.money(totals["monthly_interest"]), icon="🔥",
               help_text="What the debts cost you each month before any principal "
                         "is repaid."),
        ui.Kpi("Debts tracked", str(int(totals["count"])), icon="🔢"),
    ])

    for severity, message in alerts:
        (st.error if severity == "critical" else st.warning)(
            f"{'🔴' if severity == 'critical' else '🟠'} {message}")

    ui.divider()
    left, right = st.columns([0.55, 0.45])
    with left:
        ui.section("Projected balance", "At your current planned payments.")
        ui.chart(fc.debt_payoff_line(payoff, theme, height=320), key="debt_payoff")
    with right:
        ui.section("Each debt")
        ui.money_table(
            [{"name": view.debt.name,
              "type": DEBT_TYPE_LABELS.get(view.debt.debt_type, view.debt.debt_type),
              "balance": view.balance,
              "rate": f"{D(view.debt.interest_rate):.2f}%",
              "payment": money((view.debt.planned_payment or view.debt.minimum_payment)
                               + (view.debt.extra_payment or ZERO)),
              "months": (view.projection.months if not view.projection.never_pays_off
                         else None),
              "interest": (view.projection.total_interest
                           if not view.projection.never_pays_off else None),
              "paid": view.paid_this_period}
             for view in views],
            [("name", "Debt", "text"), ("type", "Type", "text"),
             ("balance", "Balance", "money"), ("rate", "Rate", "text"),
             ("payment", "Monthly", "money"), ("months", "Months left", "int"),
             ("interest", "Interest to come", "money"),
             ("paid", f"Paid in {period.short_label}", "money")],
            fmt, height=320,
        )

    linked = [view for view in views if view.linked_to_account]
    if linked:
        st.caption(
            "Balances for " + ", ".join(f"**{v.debt.name}**" for v in linked) +
            " come from their linked account, so they update themselves as charges "
            "and payments are recorded."
        )

    ui.divider()
    _debt_detail(views, theme, fmt, today)
    ui.divider()
    _record_payment(views, accounts, fmt)
    ui.divider()
    _new_debt_form(accounts, debt_cats)
    _edit_debt(views, accounts, debt_cats, fmt)


def _debt_detail(views, theme, fmt: ui.Formatter, today: date) -> None:
    ui.section("Look at one debt closely")
    options = [(view.debt.id, view.debt.name) for view in views]
    columns = st.columns([0.34, 0.33, 0.33])
    with columns[0]:
        debt_id = st.selectbox("Debt", [item[0] for item in options],
                              format_func=lambda item: dict(options)[item],
                              key="debt_detail_pick")
    with columns[1]:
        extra = ui.money_input("Extra per month", ZERO, key="debt_extra",
                               help_text="See what paying a bit more would do.")
    view = next(v for v in views if v.debt.id == debt_id)

    with ui.db_read() as session:
        comparison = debt_service.extra_payment_scenario(session, debt_id, extra, today)

    base = comparison["base"]
    boosted = comparison["boosted"]
    if base.never_pays_off:
        st.error(
            f"🔴 At {fmt.md_money(view.debt.planned_payment or view.debt.minimum_payment)} "
            f"a month this debt never clears — monthly interest alone is "
            f"{fmt.md_money(view.monthly_interest)}. Pay more than that and it starts "
            "coming down.",
        )
    else:
        columns = st.columns(4)
        with columns[0]:
            st.metric("Months to clear", base.months)
        with columns[1]:
            st.metric("Payoff date", fmt.date(base.payoff_date))
        with columns[2]:
            st.metric("Total interest", fmt.money(base.total_interest))
        with columns[3]:
            st.metric("Interest share of payments",
                      fmt.pct(base.interest_share_pct))

    if extra > 0 and not boosted.never_pays_off and not base.never_pays_off:
        saved_months = comparison["months_saved"]
        saved_interest = comparison["interest_saved"]
        st.success(
            f"✅ Paying {fmt.md_money(extra)} more each month clears it "
            f"**{saved_months} month(s) sooner** and saves "
            f"**{fmt.md_money(saved_interest)}** in interest.",
        )
        ui.chart(
            fc.debt_payoff_line(
                [{"month_index": row.month_index, "balance": row.closing_balance}
                 for row in base.schedule],
                theme, height=300,
                comparison=[{"month_index": row.month_index,
                             "balance": row.closing_balance}
                            for row in boosted.schedule],
                comparison_name=f"With {fmt.money(extra)} extra",
            ),
            key="debt_compare",
        )

    if base.schedule:
        ui.section("Where each payment goes")
        ui.chart(fc.amortisation_split_bars(base.schedule, theme, height=300, limit=36),
                 key="debt_amort")
        with st.expander("Full schedule"):
            ui.money_table(
                [{"month": row.month_index, "due": row.due_date,
                  "payment": row.payment, "interest": row.interest,
                  "principal": row.principal, "balance": row.closing_balance}
                 for row in base.schedule],
                [("month", "#", "int"), ("due", "Due", "date"),
                 ("payment", "Payment", "money"), ("interest", "Interest", "money"),
                 ("principal", "Principal", "money"),
                 ("balance", "Balance after", "money")],
                fmt, height=380,
            )


def _record_payment(views, accounts, fmt: ui.Formatter) -> None:
    ui.section("Record a payment",
               "The balance moves by accrued interest minus your payment. If your "
               "statement shows the exact principal portion, enter it for precision.")
    options = [(view.debt.id, view.debt.name) for view in views]
    if not accounts:
        return
    columns = st.columns([0.24, 0.18, 0.18, 0.2, 0.2])
    with columns[0]:
        debt_id = st.selectbox("Debt", [item[0] for item in options],
                              format_func=lambda item: dict(options)[item],
                              key="pay_debt")
    with columns[1]:
        amount = ui.money_input("Amount", ZERO, key="pay_amount")
    with columns[2]:
        principal = ui.money_input("Principal portion", ZERO, key="pay_principal",
                                   help_text="Leave at 0 to let the app estimate it.")
    with columns[3]:
        on_date = st.date_input("Date", value=date.today(), key="pay_date")
    with columns[4]:
        from_account = st.selectbox("From account", [item[0] for item in accounts],
                                    format_func=lambda item: dict(accounts)[item],
                                    key="pay_account")
    if st.button("Record the payment", type="primary", key="pay_go"):
        if amount <= 0:
            st.error("Enter an amount greater than zero.")
        else:
            def action(session):
                return debt_service.record_payment(
                    session, debt_id, amount, on_date=on_date,
                    from_account_id=from_account,
                    principal_portion=principal if principal > 0 else None,
                )

            result = ui.run_action(action, rerun=False)
            if result is not None:
                message = (f"{money(amount)} paid · interest "
                           f"{money(result['interest_applied'])} · new balance "
                           f"{money(result['new_balance'])}.")
                if result["cleared"]:
                    message += " 🎉 That debt is now clear!"
                ui.flash(message)
                st.rerun()


def _new_debt_form(accounts, debt_cats) -> None:
    with st.expander("➕ New debt"):
        with st.form("new_debt", clear_on_submit=True):
            columns = st.columns([0.34, 0.33, 0.33])
            with columns[0]:
                name = st.text_input("Name", placeholder="e.g. Car finance")
            with columns[1]:
                debt_type = st.selectbox("Kind", list(DEBT_TYPE_LABELS),
                                        format_func=lambda item: DEBT_TYPE_LABELS[item])
            with columns[2]:
                account_id = ui.select_with_none(
                    "Linked account", accounts, none_label="— not linked —",
                    key="nd_account",
                    help_text="Link the card or loan account and the balance keeps "
                              "itself up to date from your transactions.")

            columns = st.columns(4)
            with columns[0]:
                balance = ui.money_input("Current balance", ZERO, key="nd_balance")
            with columns[1]:
                original = ui.money_input("Original amount", ZERO, key="nd_original",
                                          help_text="Optional — used for the progress bar.")
            with columns[2]:
                rate = ui.pct_input("Annual interest %", ZERO, key="nd_rate",
                                    min_value=0.0, max_value=1000.0)
            with columns[3]:
                due_day = st.number_input("Due day", min_value=0, max_value=31,
                                          value=0, step=1)

            columns = st.columns(4)
            with columns[0]:
                minimum = ui.money_input("Minimum payment", ZERO, key="nd_min")
            with columns[1]:
                planned = ui.money_input("Planned payment", ZERO, key="nd_planned")
            with columns[2]:
                extra = ui.money_input("Extra payment", ZERO, key="nd_extra")
            with columns[3]:
                in_budget = st.checkbox(
                    "Give it a budget line", value=True,
                    help="Turn this OFF for a credit card whose spending you already "
                         "budget by category — otherwise the same money is allocated "
                         "twice.")
            notes = st.text_input("Notes (optional)")

            if st.form_submit_button("Create the debt", type="primary"):
                if not name.strip():
                    st.error("Give the debt a name.")
                elif balance <= 0:
                    st.error("Enter the current balance.")
                else:
                    ui.run_action(
                        lambda session: debt_service.create_debt(session, {
                            "name": name, "debt_type": debt_type,
                            "principal_balance": balance,
                            "original_principal": original if original > 0 else None,
                            "interest_rate": rate, "minimum_payment": minimum,
                            "planned_payment": planned, "extra_payment": extra,
                            "due_day": int(due_day) or None,
                            "account_id": account_id, "include_in_budget": in_budget,
                            "notes": notes or None,
                        }),
                        success=f"Debt “{name}” created.",
                    )


def _edit_debt(views, accounts, debt_cats, fmt: ui.Formatter) -> None:
    with st.expander("✏️ Edit or remove a debt"):
        options = [(view.debt.id, view.debt.name) for view in views]
        debt_id = ui.select_with_none("Debt", options, none_label="— pick one —",
                                     key="ed_pick")
        if debt_id is None:
            return
        view = next(v for v in views if v.debt.id == debt_id)
        debt = view.debt

        columns = st.columns(4)
        with columns[0]:
            name = st.text_input("Name", value=debt.name, key="ed_name")
        with columns[1]:
            rate = ui.pct_input("Annual interest %", debt.interest_rate, key="ed_rate",
                                min_value=0.0, max_value=1000.0)
        with columns[2]:
            minimum = ui.money_input("Minimum payment", debt.minimum_payment,
                                     key="ed_min")
        with columns[3]:
            planned = ui.money_input("Planned payment", debt.planned_payment,
                                     key="ed_planned")
        columns = st.columns(4)
        with columns[0]:
            extra = ui.money_input("Extra payment", debt.extra_payment, key="ed_extra")
        with columns[1]:
            balance = ui.money_input("Balance", debt.principal_balance, key="ed_balance",
                                     disabled=view.linked_to_account,
                                     help_text=("Comes from the linked account"
                                                if view.linked_to_account else ""))
        with columns[2]:
            in_budget = st.checkbox("Give it a budget line",
                                   value=debt.include_in_budget, key="ed_in_budget")
        with columns[3]:
            active = st.checkbox("Still active", value=debt.is_active, key="ed_active")

        actions = st.columns([0.4, 0.3, 0.3])
        with actions[0]:
            if st.button("Save changes", type="primary", key="ed_save",
                         **ui.wide()):
                payload = {
                    "name": name, "interest_rate": rate,
                    "minimum_payment": minimum, "planned_payment": planned,
                    "extra_payment": extra, "include_in_budget": in_budget,
                    "is_active": active,
                }
                if not view.linked_to_account:
                    payload["principal_balance"] = balance
                ui.run_action(
                    lambda session: debt_service.update_debt(session, debt_id, payload),
                    success=f"“{name}” updated.",
                )
        with actions[1]:
            if not view.linked_to_account and st.button(
                    "🔄 Reconcile balance", key="ed_reconcile",
                    **ui.wide(),
                    help="Set the balance to the figure above, dated today."):
                ui.run_action(
                    lambda session: debt_service.set_balance(session, debt_id, balance),
                    success="Balance reconciled.",
                )
        with actions[2]:
            if ui.confirm_action(
                "🗑 Delete", f"del_debt_{debt_id}",
                prompt=f"Delete “{debt.name}”? Payments stay in your transaction "
                       "history and a snapshot goes to the recycle bin.",
                confirm_label="Delete the debt", **ui.wide(),
            ):
                ui.run_action(
                    lambda session: debt_service.delete_debt(session, debt_id,
                                                             force=True),
                    success=f"“{debt.name}” deleted.",
                )


# ==========================================================================
# Strategies
# ==========================================================================
def _strategies() -> None:
    fmt = ui.formatter()
    theme = ui.theme()
    today = date.today()

    ui.section(
        "Which payoff order costs least?",
        "**Avalanche** attacks the highest interest rate first and always costs the "
        "least. **Snowball** clears the smallest balance first, which is slower but "
        "gives you a win sooner. Both keep paying every minimum, and the extra below "
        "rolls down the queue — growing as each debt clears and frees its minimum. "
        "**Minimums only** is the do-nothing baseline: no extra, nothing rolled, so "
        "you can see what changing nothing costs.",
    )
    columns = st.columns([0.3, 0.7])
    with columns[0]:
        extra = ui.money_input("Extra to share each month", ZERO, key="strat_extra")

    with ui.db_read() as session:
        debts = debt_service.list_debts(session)
        results = debt_service.compare_strategies(session, extra, today)

    if not debts:
        st.info("Add at least one debt to compare strategies.", icon="ℹ️")
        return

    # Name the blocker rather than just reporting that nothing works. Only the
    # strategies you could actually follow count as a problem; the minimums-only
    # baseline stalling is the point of showing it, not a fault.
    actionable = [results[key] for key in
                  (PayoffStrategy.AVALANCHE.value, PayoffStrategy.SNOWBALL.value)
                  if key in results]
    blocked = sorted({name for result in actionable for name in result.stuck})
    minimum_only = results.get(PayoffStrategy.MINIMUM_ONLY.value)

    if blocked:
        with ui.db_read() as session:
            views = {view.debt.name: view for view in debt_service.views(session,
                                                                        today=today)}
        lines = []
        for name in blocked:
            view = views.get(name)
            if view is None:
                continue
            payment = money((view.debt.planned_payment or view.debt.minimum_payment)
                            + (view.debt.extra_payment or ZERO))
            lines.append(
                f"- **{name}** — paying {fmt.money(payment)} a month against "
                f"{fmt.money(view.monthly_interest)} of monthly interest"
            )
        st.error(
            "🔴 **These debts grow faster than you are paying them.** The balance rises "
            "every month no matter which order you choose, so no strategy can clear "
            "them:\n\n" + "\n".join(lines) +
            "\n\nRaise the planned payment above the interest figure, or add enough "
            "extra below to cover the gap — the comparison becomes meaningful as soon "
            "as the balance starts falling.",
        )
    elif minimum_only is not None and minimum_only.stuck:
        st.warning(
            "🟠 Paying only the minimum on **" +
            "**, **".join(minimum_only.stuck) +
            "** would never clear it — the minimum does not even cover the monthly "
            "interest. Your planned payments do, which is exactly why the two rows "
            "below differ so much.",
        )

    ui.chart(fc.strategy_comparison_bars(results, theme, height=300),
             key="strat_chart")

    names = {"avalanche": "Avalanche (highest rate first)",
             "snowball": "Snowball (smallest balance first)",
             "minimum_only": "Minimums only"}
    rows = []
    for key, result in results.items():
        rows.append({
            "strategy": names.get(key, key),
            "months": result.months if not result.never_pays_off else None,
            "interest": result.total_interest if not result.never_pays_off else None,
            "paid": result.total_paid if not result.never_pays_off else None,
            "outlay": result.monthly_outlay,
            "order": " → ".join(result.payoff_order) if result.payoff_order else "—",
            "note": (f"never clears — {', '.join(result.stuck)} outgrows its payment"
                     if result.stuck else
                     ("never clears within 50 years" if result.never_pays_off else "")),
        })
    ui.money_table(
        rows,
        [("strategy", "Strategy", "text"), ("months", "Months", "int"),
         ("interest", "Total interest", "money"), ("paid", "Total paid", "money"),
         ("outlay", "Monthly outlay", "money"), ("order", "Clears in this order", "text"),
         ("note", "", "text")],
        fmt,
    )

    usable = {k: v for k, v in results.items() if not v.never_pays_off and v.months}
    if len(usable) >= 2:
        best = min(usable.items(), key=lambda item: item[1].total_interest)
        worst = max(usable.items(), key=lambda item: item[1].total_interest)
        if best[0] != worst[0]:
            st.success(
                f"✅ **{names.get(best[0], best[0])}** saves "
                f"{fmt.money(money(worst[1].total_interest - best[1].total_interest))} "
                f"in interest compared with {names.get(worst[0], worst[0])}.",
            )
