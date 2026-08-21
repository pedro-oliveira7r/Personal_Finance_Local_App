"""Budget planning — build a zero-based plan for one or many periods."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import streamlit as st

from calculations.money import ZERO, D, money, money_sum, to_float
from calculations.periods import shift_period
from calculations.recurrence import describe
from charts import dashboard_charts as dc
from constants import (
    ALLOCATION_KINDS,
    FREQUENCY_LABELS,
    PAYMENT_METHODS,
    AllocationTarget,
    BusinessDayRule,
    CategoryKind,
    Frequency,
    PeriodStatus,
    TxnKind,
)
from services import (
    account_service,
    budget_service,
    category_service,
    debt_service,
    goal_service,
    recurring_service,
)
from ui import components as ui


def render() -> None:
    ui.page_header(
        "Budget planning",
        "Give every unit of income a job. Available money − allocations = zero.",
        icon="🧮",
    )
    picker, period_col = st.columns([0.32, 0.68])
    with picker:
        # No "All": zero-based balance is a per-currency identity, and a
        # converted total is not something you can hand out to categories.
        currency = ui.currency_picker(key="budget_currency", include_all=False)
    with period_col:
        period = ui.period_picker(key="budget_period")

    tabs = st.tabs(["This period", "Recurring rules"])
    with tabs[0]:
        _single_period(period, currency)
    with tabs[1]:
        _rules()


# ==========================================================================
# One period
# ==========================================================================
def _single_period(period, currency: str | None = None) -> None:
    fmt = ui.formatter(currency)
    settings = ui.current_settings()
    theme = ui.theme(currency)

    with ui.db_read() as session:
        summary = budget_service.summarise_period(session, period, currency=currency)
        income_options = category_service.options_for_select(
            session, kinds=[CategoryKind.INCOME.value])
        allocation_options = category_service.options_for_select(
            session, kinds=list(ALLOCATION_KINDS))
        goal_options = [(g.id, g.name) for g in goal_service.active_goals(session)]
        debt_options = [(d.id, d.name) for d in debt_service.list_debts(session)]
        account_options = account_service.options_for_select(session)

    result = summary.result
    status_severity = ("success" if result.is_balanced
                       else ("warning" if result.remaining > 0 else "critical"))

    ui.kpi_row([
        ui.Kpi("Cash carried in", fmt.money(result.carry_in), icon="↪️",
               help_text="Free cash at the start of the period — your balances minus "
                         "money already earmarked for goals."),
        ui.Kpi("Expected income", fmt.money(result.planned_income), icon="📥"),
        ui.Kpi("Available to budget", fmt.money(result.available), icon="💵"),
        ui.Kpi("Allocated", fmt.money(result.allocated), icon="🧾",
               delta=f"{fmt.pct(result.allocated_pct)} of available",
               delta_good=None),
        ui.Kpi(result.status_label, fmt.money(abs(result.remaining)), icon="🎯",
               help_text="Zero-based budgeting is balanced when this is zero."),
    ])
    ui.status_pills([(f"{result.status_label}: {fmt.money(abs(result.remaining))}",
                      status_severity)])

    if result.warnings:
        with st.expander(f"{len(result.warnings)} note(s) about this plan",
                         expanded=any(w.severity == "critical" for w in result.warnings)):
            for warning in result.warnings:
                icon = {"critical": "🔴", "warning": "🟠"}.get(warning.severity, "🔵")
                st.markdown(f"{icon} **{warning.message}**")
                if warning.detail:
                    st.caption(warning.detail)

    if not summary.has_plan:
        _no_plan_yet(period, summary)
        return

    _period_toolbar(period, summary)
    ui.divider()

    left, right = st.columns([0.62, 0.38])
    with left:
        _income_editor(period, summary, income_options, fmt)
        _allocation_editor(period, summary, fmt)
    with right:
        _balance_helper(period, summary, fmt, goal_options, debt_options)
        ui.divider()
        _allocation_chart(summary, theme, fmt)

    ui.divider()
    _add_line_form(period, income_options, allocation_options,
                   goal_options, debt_options, account_options)


def _no_plan_yet(period, summary) -> None:
    ui.empty_state(
        f"No plan for {period.label} yet",
        "Start from your recurring rules — salary, rent, subscriptions, annual "
        "insurance, the July holiday — and the app fills the period with the right "
        "amounts for the right months. You can then override any line by hand.",
        icon="🧮",
    )
    left, middle, right = st.columns(3)
    with left:
        if st.button("Generate from recurring rules", type="primary",
                     **ui.wide(), key="gen_rules_empty"):
            _generate(period)
    with middle:
        previous = shift_period(period, -1, ui.current_settings().first_day_of_month)
        if st.button(f"Copy {previous.short_label}", **ui.wide(),
                     key="copy_prev_empty"):
            _copy(previous, period)
    with right:
        if st.button("Start from scratch", **ui.wide(),
                     key="scratch_empty"):
            ui.run_action(
                lambda session: budget_service.get_or_create_period(
                    session, period.year, period.month),
                success=f"Empty budget created for {period.label}.",
            )


def _period_toolbar(period, summary) -> None:
    settings = ui.current_settings()
    row = summary.row
    status = row.status if row is not None else PeriodStatus.DRAFT.value

    columns = st.columns([0.22, 0.22, 0.2, 0.18, 0.18])
    with columns[0]:
        if st.button("↻ Regenerate from rules", **ui.wide(),
                     key="gen_rules", help="Overrides you made by hand are kept."):
            _generate(period)
    with columns[1]:
        previous = shift_period(period, -1, settings.first_day_of_month)
        if st.button(f"⧉ Copy {previous.short_label}", **ui.wide(),
                     key="copy_prev"):
            _copy(previous, period)
    with columns[2]:
        options = [PeriodStatus.DRAFT.value, PeriodStatus.ACTIVE.value,
                   PeriodStatus.CLOSED.value]
        labels = {"draft": "Draft", "active": "Active", "closed": "Closed"}
        chosen = st.selectbox("Period status", options,
                             index=options.index(status) if status in options else 0,
                             format_func=lambda item: labels[item],
                             key="period_status",
                             help="Closing a period locks its plan against accidental edits.")
        if chosen != status:
            ui.run_action(
                lambda session: budget_service.set_period_status(
                    session, period.year, period.month, chosen),
                success=f"{period.label} is now {labels[chosen].lower()}.",
            )
    with columns[3]:
        with st.popover("Opening cash", **ui.wide()):
            st.caption(
                "By default the plan carries in your real free cash. Override it if you "
                "want to plan from a specific figure."
            )
            override = row.opening_cash_override if row is not None else None
            value = ui.money_input("Carry in", override if override is not None
                                   else summary.result.carry_in, key="carry_override")
            left, right = st.columns(2)
            with left:
                if st.button("Use this figure", key="carry_set", type="primary",
                             **ui.wide()):
                    ui.run_action(
                        lambda session: budget_service.set_opening_override(
                            session, period.year, period.month, value),
                        success="Opening cash overridden.",
                    )
            with right:
                if st.button("Back to automatic", key="carry_clear",
                             **ui.wide()):
                    ui.run_action(
                        lambda session: budget_service.set_opening_override(
                            session, period.year, period.month, None),
                        success="Opening cash back to automatic.",
                    )
    with columns[4]:
        if ui.confirm_action(
            "🗑 Delete plan", f"del_period_{period.key}",
            prompt=f"Delete the whole budget for {period.label}? "
                   "Transactions are not touched, and the plan goes to the recycle bin.",
            confirm_label="Delete the plan", **ui.wide(),
        ):
            ui.run_action(
                lambda session: budget_service.delete_period(
                    session, period.year, period.month),
                success=f"Budget for {period.label} deleted.",
            )


def _generate(period) -> None:
    def action(session):
        return budget_service.generate_from_rules(session, period)

    report = ui.run_action(action, rerun=False)
    if report is not None:
        ui.flash(f"{period.label}: {report.summary()}.")
        st.rerun()


def _copy(source, target) -> None:
    def action(session):
        return budget_service.copy_period(session, source, target)

    report = ui.run_action(action, rerun=False)
    if report is not None:
        ui.flash(f"Copied {source.label} into {target.label}: {report.summary()}.")
        st.rerun()


# --------------------------------------------------------------------------
def _income_editor(period, summary, income_options, fmt: ui.Formatter) -> None:
    ui.section("Expected income",
               "What you expect to receive. Availability rules decide which period "
               "each payment can actually fund.")
    lines = summary.income_lines
    if not lines:
        st.caption("No income lines yet — add one below.")
        return
    _editable_lines(period, lines, "income", fmt)


def _allocation_editor(period, summary, fmt: ui.Formatter) -> None:
    groups = {
        CategoryKind.EXPENSE.value: ("Expenses", "🧾"),
        CategoryKind.SAVINGS.value: ("Savings & goals", "🐖"),
        CategoryKind.INVESTMENT.value: ("Investments", "📈"),
        CategoryKind.DEBT.value: ("Debt repayment", "⛓️"),
    }
    for kind, (label, icon) in groups.items():
        lines = [line for line in summary.allocation_lines if line.kind == kind]
        if not lines:
            continue
        total = money_sum(line.planned_amount for line in lines)
        ui.section(f"{icon} {label} — {fmt.money(total)}")
        _editable_lines(period, lines, kind, fmt)


def _editable_lines(period, lines, group_key: str, fmt: ui.Formatter) -> None:
    """Inline editing of planned amounts via a data editor."""
    rows = [
        {
            "id": line.id,
            "Line": line.display_label,
            "Planned": to_float(line.planned_amount),
            "Day": line.expected_day or 0,
            "Locked": bool(line.is_locked),
            "Manual": bool(line.is_override),
            "Remove": False,
        }
        for line in lines
    ]
    editor_key = f"editor_{period.key}_{group_key}"
    edited = st.data_editor(
        rows,
        key=editor_key,
        **ui.wide(),
        hide_index=True,
        column_order=["Line", "Planned", "Day", "Locked", "Manual", "Remove"],
        column_config={
            "id": None,
            "Line": st.column_config.TextColumn(disabled=True, width="large"),
            "Planned": st.column_config.NumberColumn(
                format="%.2f", min_value=0.0, step=25.0,
                help="Edit the amount and press Save."),
            "Day": st.column_config.NumberColumn(
                "Due day", min_value=0, max_value=31, step=1,
                help="Day of the month you expect it. 0 means unspecified."),
            "Locked": st.column_config.CheckboxColumn(
                help="Locked lines are never touched by regeneration."),
            "Manual": st.column_config.CheckboxColumn(
                disabled=True, help="Set automatically once you edit a generated line."),
            "Remove": st.column_config.CheckboxColumn(help="Tick, then press Save."),
        },
    )

    changed = _diff(rows, edited)
    if changed:
        left, right = st.columns([0.3, 0.7])
        with left:
            if st.button(f"Save {len(changed)} change(s)", key=f"save_{editor_key}",
                         type="primary", **ui.wide()):
                _apply_line_changes(period, lines, changed)
        with right:
            st.caption("Unsaved edits are highlighted above. Nothing is written until "
                       "you press Save.")


def _diff(original: list[dict], edited) -> list[dict]:
    """Rows whose editable fields differ from what was loaded."""
    by_id = {row["id"]: row for row in original}
    changes: list[dict] = []
    records = edited if isinstance(edited, list) else edited.to_dict("records")
    for record in records:
        base = by_id.get(record.get("id"))
        if base is None:
            continue
        if (abs(float(record.get("Planned", 0)) - float(base["Planned"])) > 0.004
                or int(record.get("Day") or 0) != int(base["Day"] or 0)
                or bool(record.get("Locked")) != bool(base["Locked"])
                or bool(record.get("Remove"))):
            changes.append(record)
    return changes


def _apply_line_changes(period, lines, changes: list[dict]) -> None:
    by_id = {line.id: line for line in lines}

    def action(session):
        applied = 0
        for record in changes:
            line = by_id.get(record.get("id"))
            if line is None:
                continue
            if record.get("Remove"):
                budget_service.delete_line(session, line.id)
                applied += 1
                continue
            day = int(record.get("Day") or 0) or None
            budget_service.upsert_line(session, period.year, period.month, {
                "kind": line.kind,
                "target": line.target,
                "planned_amount": money(record.get("Planned", 0)),
                "category_id": line.category_id,
                "goal_id": line.goal_id,
                "debt_id": line.debt_id,
                "account_id": line.account_id,
                "label": line.label,
                "expected_day": day,
                "notes": line.notes,
            })
            if bool(record.get("Locked")) != bool(line.is_locked):
                budget_service.set_line_lock(session, line.id, bool(record.get("Locked")))
            applied += 1
        return applied

    count = ui.run_action(action, rerun=False)
    if count:
        ui.flash(f"{count} budget line(s) updated.")
        st.rerun()


# --------------------------------------------------------------------------
def _balance_helper(period, summary, fmt: ui.Formatter,
                    goal_options, debt_options) -> None:
    result = summary.result
    ui.section("Reaching zero")
    if result.is_balanced:
        st.success("✅ This budget balances — every unit of currency has a job.")
        return

    if result.remaining > 0:
        st.warning(
            f"🟠 **{fmt.money(result.unallocated)}** still has no job.",
        )
    else:
        st.error(
            f"🔴 The plan promises **{fmt.money(result.overspend)}** more than exists.",
        )

    for suggestion in result and summary.suggestions[:4]:
        st.markdown(f"- {suggestion}")

    if result.remaining > 0 and (goal_options or debt_options):
        st.caption("Send the remainder somewhere in one click:")
        targets: list[tuple[str, str, int]] = []
        targets += [("goal", name, gid) for gid, name in goal_options[:4]]
        targets += [("debt", name, did) for did, name in debt_options[:3]]
        for kind, name, identifier in targets:
            if st.button(f"→ {fmt.money(result.unallocated)} to {name}",
                         key=f"assign_{kind}_{identifier}", **ui.wide()):
                _assign_remainder(period, kind, identifier, name, result.unallocated)


def _assign_remainder(period, kind: str, identifier: int, name: str,
                      amount: Decimal) -> None:
    def action(session):
        existing = None
        row = budget_service.get_period_row(session, period.year, period.month)
        if row is not None:
            for line in budget_service.lines_for_period(session, row):
                if (kind == "goal" and line.goal_id == identifier) or \
                   (kind == "debt" and line.debt_id == identifier):
                    existing = line
                    break
        current = existing.planned_amount if existing is not None else ZERO
        payload = {
            "kind": (CategoryKind.SAVINGS.value if kind == "goal"
                     else CategoryKind.DEBT.value),
            "target": (AllocationTarget.GOAL.value if kind == "goal"
                       else AllocationTarget.DEBT.value),
            "planned_amount": money(current + amount),
            "label": f"{'Goal' if kind == 'goal' else 'Debt'} · {name}",
        }
        payload["goal_id" if kind == "goal" else "debt_id"] = identifier
        return budget_service.upsert_line(session, period.year, period.month, payload)

    ui.run_action(action, success=f"{money(amount)} assigned to {name}.")


def _allocation_chart(summary, theme, fmt: ui.Formatter) -> None:
    result = summary.result
    if not result.by_kind:
        return
    ui.section("How the plan splits up")
    slices = [(kind.title(), amount) for kind, amount in result.by_kind.items()]
    if result.unallocated > 0:
        slices.append(("Unassigned", result.unallocated))
    ui.chart(
        dc.allocation_donut(slices, theme, height=300,
                            center_label="allocated",
                            center_value=fmt.money(result.allocated, compact=True)),
        table=[{"Bucket": label, "Amount": fmt.money(amount)} for label, amount in slices],
        key="budget_donut",
    )


# --------------------------------------------------------------------------
def _add_line_form(period, income_options, allocation_options,
                   goal_options, debt_options, account_options) -> None:
    with st.expander("➕ Add a budget line"):
        kind_choice = st.radio(
            "What is this line?",
            ["Expected income", "Category allocation", "Goal contribution",
             "Debt payment", "Custom allocation"],
            horizontal=True, key="add_line_kind",
        )
        with st.form("add_budget_line", clear_on_submit=True):
            category_id = goal_id = debt_id = account_id = None
            label = None
            kind = CategoryKind.EXPENSE.value
            target = AllocationTarget.EXPENSE.value

            if kind_choice == "Expected income":
                kind = CategoryKind.INCOME.value
                target = AllocationTarget.OTHER.value
                category_id = ui.select_with_none("Income category", income_options,
                                                  none_label="— pick a category —")
            elif kind_choice == "Category allocation":
                category_id = ui.select_with_none("Category", allocation_options,
                                                  none_label="— pick a category —")
            elif kind_choice == "Goal contribution":
                kind = CategoryKind.SAVINGS.value
                target = AllocationTarget.GOAL.value
                goal_id = ui.select_with_none("Goal", goal_options,
                                              none_label="— pick a goal —")
            elif kind_choice == "Debt payment":
                kind = CategoryKind.DEBT.value
                target = AllocationTarget.DEBT.value
                debt_id = ui.select_with_none("Debt", debt_options,
                                              none_label="— pick a debt —")
            else:
                target = AllocationTarget.OTHER.value
                label = st.text_input("Label", placeholder="e.g. Wedding fund")

            columns = st.columns([0.4, 0.3, 0.3])
            with columns[0]:
                amount = ui.money_input("Planned amount", ZERO, key="add_line_amount")
            with columns[1]:
                day = st.number_input("Expected day", min_value=0, max_value=31,
                                      value=0, step=1,
                                      help="0 if it does not matter.")
            with columns[2]:
                account_id = ui.select_with_none("Account (optional)", account_options,
                                                 none_label="— any —")
            notes = st.text_input("Note (optional)", placeholder="Why this amount?")

            if st.form_submit_button("Add the line", type="primary"):
                if not any([category_id, goal_id, debt_id, label]):
                    st.error("Pick a category, goal or debt, or give the line a label.")
                elif amount <= 0 and kind_choice != "Category allocation":
                    st.error("Enter an amount greater than zero.")
                else:
                    payload = {
                        "kind": kind, "target": target,
                        "planned_amount": amount,
                        "category_id": category_id, "goal_id": goal_id,
                        "debt_id": debt_id, "account_id": account_id,
                        "label": label or None,
                        "expected_day": int(day) or None,
                        "notes": notes or None,
                    }
                    ui.run_action(
                        lambda session: budget_service.upsert_line(
                            session, period.year, period.month, payload),
                        success="Budget line saved.",
                    )


# ==========================================================================
# Recurring rules
# ==========================================================================
def _rules() -> None:
    fmt = ui.formatter()
    with ui.db_read() as session:
        rules = recurring_service.list_rules(session)
        stats = recurring_service.rule_summary(session)
        upcoming = recurring_service.upcoming(session, 45)
        income_cats = category_service.options_for_select(
            session, kinds=[CategoryKind.INCOME.value])
        expense_cats = category_service.options_for_select(
            session, kinds=list(ALLOCATION_KINDS))
        accounts = account_service.options_for_select(session)
        goals = [(g.id, g.name) for g in goal_service.active_goals(session)]
        debts = [(d.id, d.name) for d in debt_service.list_debts(session)]
        descriptions = {rule.id: recurring_service.describe_rule(rule) for rule in rules}

    ui.section(
        "Recurring rules",
        "One rule per repeating item. Rules handle growth (“+5% every January”), "
        "seasonality (“electricity 50% higher in summer”), quarterly and annual "
        "timing, weekend adjustment, and the gap between a due date and the day cash "
        "actually moves.",
    )

    ui.kpi_row([
        ui.Kpi("Active rules", str(stats["active"]), icon="🔁"),
        ui.Kpi("Income rules", str(stats["income"]), icon="📥"),
        ui.Kpi("Expense rules", str(stats["expense"]), icon="📤"),
        ui.Kpi("With growth", str(stats["with_growth"]), icon="📈"),
        ui.Kpi("Seasonal", str(stats["seasonal"]), icon="🌦️"),
    ])

    columns = st.columns([0.3, 0.3, 0.4])
    with columns[0]:
        horizon = st.number_input("Generate months ahead", min_value=1, max_value=60,
                                  value=12, step=1, key="gen_horizon")
    with columns[1]:
        backfill = st.checkbox("Also fill in past occurrences", value=False,
                               key="gen_backfill",
                               help="Useful right after importing history.")
    with columns[2]:
        if st.button("Generate planned transactions", type="primary",
                     **ui.wide(), key="gen_planned"):
            def action(session):
                return recurring_service.generate_planned(
                    session, horizon_months=int(horizon), backfill=backfill)

            report = ui.run_action(action, rerun=False,
                                   spinner="Generating planned transactions…")
            if report is not None:
                ui.flash(f"{report.summary()}. Nothing was duplicated.")
                st.rerun()

    if not rules:
        ui.empty_state(
            "No recurring rules yet",
            "Add your salary, rent and subscriptions once, and every future budget and "
            "forecast builds itself from them.",
            icon="🔁",
        )
    else:
        ui.money_table(
            [{"name": ("✅ " if rule.is_active else "⏸ ") + rule.name,
              "kind": rule.kind.title(),
              "amount": rule.amount,
              "schedule": descriptions.get(rule.id, ""),
              "start": rule.start_date,
              "end": rule.end_date}
             for rule in rules],
            [("name", "Rule", "text"), ("kind", "Type", "text"),
             ("amount", "Base amount", "money"), ("schedule", "Schedule", "text"),
             ("start", "From", "date"), ("end", "Until", "date")],
            fmt, height=min(520, 60 + 36 * len(rules)),
        )
        _edit_rule_panel(rules, income_cats, expense_cats, accounts, goals, debts, fmt)

    ui.divider()
    _new_rule_form(income_cats, expense_cats, accounts, goals, debts)

    if upcoming:
        ui.divider()
        ui.section("Next 45 days from your rules")
        ui.money_table(
            [{"date": item.occurrence.due_date, "name": item.rule.name,
              "kind": item.rule.kind.title(), "amount": item.occurrence.amount,
              "cash": item.occurrence.cash_date}
             for item in upcoming[:40]],
            [("date", "Due", "date"), ("name", "Rule", "text"),
             ("kind", "Type", "text"), ("amount", "Amount", "money"),
             ("cash", "Cash moves", "date")],
            fmt, height=340,
        )


def _edit_rule_panel(rules, income_cats, expense_cats, accounts, goals, debts,
                     fmt: ui.Formatter) -> None:
    with st.expander("✏️ Edit or remove a rule"):
        options = [(rule.id, rule.name) for rule in rules]
        rule_id = ui.select_with_none("Rule", options, none_label="— pick a rule —",
                                      key="edit_rule_pick")
        if rule_id is None:
            return
        rule = next((item for item in rules if item.id == rule_id), None)
        if rule is None:
            return

        columns = st.columns([0.3, 0.3, 0.4])
        with columns[0]:
            new_amount = ui.money_input("Base amount", rule.amount, key="edit_rule_amount")
        with columns[1]:
            new_growth = ui.pct_input("Growth per step", rule.growth_pct,
                                      key="edit_rule_growth")
        with columns[2]:
            active = st.checkbox("Rule is active", value=rule.is_active,
                                 key="edit_rule_active")

        preview_months = st.slider("Preview months", 3, 36, 12, key="edit_rule_preview")
        occurrences = recurring_service.preview_rule({
            "name": rule.name, "kind": rule.kind, "amount": new_amount,
            "frequency": rule.frequency, "interval": rule.interval,
            "start_date": rule.start_date, "end_date": rule.end_date,
            "day_of_month": rule.day_of_month, "weekday": rule.weekday,
            "month_of_year": rule.month_of_year,
            "growth_pct": new_growth,
            "growth_every_months": rule.growth_every_months,
            "growth_anchor_month": rule.growth_anchor_month,
            "seasonal_factors": rule.seasonal_factors,
            "business_day_rule": rule.business_day_rule,
            "settlement_offset_days": rule.settlement_offset_days,
            "account_id": rule.account_id, "to_account_id": rule.to_account_id,
        }, months=preview_months)
        if occurrences:
            ui.money_table(
                [{"due": occ.due_date, "cash": occ.cash_date, "amount": occ.amount,
                  "growth": f"×{occ.growth_steps}" if occ.growth_steps else "—",
                  "season": (f"×{occ.seasonal_factor}"
                             if occ.seasonal_factor != 1 else "—")}
                 for occ in occurrences],
                [("due", "Due", "date"), ("cash", "Cash moves", "date"),
                 ("amount", "Amount", "money"), ("growth", "Growth steps", "text"),
                 ("season", "Season factor", "text")],
                fmt, height=260,
            )
            st.caption(f"Total over the preview: "
                       f"**{fmt.money(money_sum(o.amount for o in occurrences))}**")

        left, right = st.columns(2)
        with left:
            if st.button("Save changes", type="primary", key="edit_rule_save",
                         **ui.wide()):
                ui.run_action(
                    lambda session: recurring_service.update_rule(session, rule_id, {
                        "amount": new_amount, "growth_pct": new_growth,
                        "is_active": active,
                    }),
                    success=f"“{rule.name}” updated.",
                )
        with right:
            if ui.confirm_action(
                "🗑 Delete rule", f"del_rule_{rule_id}",
                prompt=f"Delete “{rule.name}”? Planned transactions it created are "
                       "removed too; anything already completed stays in your history.",
                confirm_label="Delete the rule", **ui.wide(),
            ):
                ui.run_action(
                    lambda session: recurring_service.delete_rule(session, rule_id),
                    success=f"“{rule.name}” deleted.",
                )


def _new_rule_form(income_cats, expense_cats, accounts, goals, debts) -> None:
    with st.expander("➕ New recurring rule"):
        kind = st.radio("Type", [TxnKind.INCOME.value, TxnKind.EXPENSE.value,
                                 TxnKind.TRANSFER.value],
                        format_func=lambda item: item.title(), horizontal=True,
                        key="new_rule_kind")
        with st.form("new_rule", clear_on_submit=False):
            columns = st.columns([0.42, 0.28, 0.3])
            with columns[0]:
                name = st.text_input("Name", placeholder="e.g. Rent")
            with columns[1]:
                amount = ui.money_input("Amount", ZERO, key="new_rule_amount")
            with columns[2]:
                frequency = st.selectbox(
                    "Frequency", list(FREQUENCY_LABELS),
                    index=list(FREQUENCY_LABELS).index(Frequency.MONTHLY.value),
                    format_func=lambda item: FREQUENCY_LABELS[item],
                )

            columns = st.columns(4)
            with columns[0]:
                start = st.date_input("Starts", value=date.today())
            with columns[1]:
                use_end = st.checkbox("Has an end date", value=False)
                end = st.date_input("Ends", value=date.today(),
                                    disabled=not use_end)
            with columns[2]:
                interval = st.number_input("Every N intervals", min_value=1,
                                           max_value=60, value=1, step=1)
            with columns[3]:
                day = st.number_input("Day of month", min_value=0, max_value=31,
                                      value=0, step=1,
                                      help="0 uses the start date's day.")

            columns = st.columns(3)
            with columns[0]:
                if kind == TxnKind.INCOME.value:
                    category_id = ui.select_with_none("Category", income_cats,
                                                      none_label="— none —",
                                                      key="new_rule_cat_in")
                elif kind == TxnKind.EXPENSE.value:
                    category_id = ui.select_with_none("Category", expense_cats,
                                                      none_label="— none —",
                                                      key="new_rule_cat_out")
                else:
                    category_id = None
                    st.caption("Transfers do not use a category — money simply moves.")
            with columns[1]:
                account_id = ui.select_with_none(
                    "Account" if kind != TxnKind.TRANSFER.value else "From account",
                    accounts, none_label="— pick one —", key="new_rule_acct")
            with columns[2]:
                to_account_id = (
                    ui.select_with_none("To account", accounts,
                                        none_label="— pick one —", key="new_rule_to")
                    if kind == TxnKind.TRANSFER.value else None
                )

            st.markdown("**Real-world behaviour** (all optional)")
            columns = st.columns(4)
            with columns[0]:
                growth = ui.pct_input("Grows by", ZERO, key="new_rule_growth",
                                      help_text="e.g. 5 for a 5% rise")
            with columns[1]:
                growth_every = st.number_input("…every N months", min_value=1,
                                               max_value=120, value=12, step=1)
            with columns[2]:
                anchor = st.selectbox(
                    "…anchored to", [0] + list(range(1, 13)),
                    format_func=lambda item: ("no anchor" if item == 0
                                              else date(2000, item, 1).strftime("%B")),
                    help="Pick January for “+5% every January”.",
                )
            with columns[3]:
                month_of_year = st.selectbox(
                    "Only in month", [0] + list(range(1, 13)),
                    format_func=lambda item: ("any month" if item == 0
                                              else date(2000, item, 1).strftime("%B")),
                    help="For annual or quarterly items — e.g. insurance every March.",
                )

            columns = st.columns(3)
            with columns[0]:
                weekend = st.selectbox(
                    "Weekend handling",
                    [rule.value for rule in BusinessDayRule],
                    format_func=lambda item: item.replace("_", " ").title(),
                )
            with columns[1]:
                settlement = st.number_input(
                    "Cash moves N days later", min_value=-60, max_value=180,
                    value=0, step=1,
                    help="A salary earned on the 30th that lands on the 2nd is +3.",
                )
            with columns[2]:
                payment_method = st.selectbox("Payment method", [""] + PAYMENT_METHODS)

            seasonal_text = st.text_input(
                "Seasonal factors",
                placeholder="1:1.5, 2:1.4, 7:0.8",
                help="month:multiplier pairs. 1:1.5 means January is 50% higher.",
            )

            columns = st.columns(3)
            with columns[0]:
                goal_id = ui.select_with_none("Counts toward goal", goals,
                                              none_label="— none —", key="new_rule_goal")
            with columns[1]:
                debt_id = ui.select_with_none("Counts toward debt", debts,
                                              none_label="— none —", key="new_rule_debt")
            with columns[2]:
                in_budget = st.checkbox("Include in generated budgets", value=True)

            if st.form_submit_button("Create the rule", type="primary"):
                payload = {
                    "name": name, "kind": kind, "amount": amount,
                    "frequency": frequency, "interval": int(interval),
                    "start_date": start,
                    "end_date": end if use_end else None,
                    "day_of_month": int(day) or None,
                    "month_of_year": month_of_year or None,
                    "category_id": category_id, "account_id": account_id,
                    "to_account_id": to_account_id,
                    "goal_id": goal_id, "debt_id": debt_id,
                    "growth_pct": growth, "growth_every_months": int(growth_every),
                    "growth_anchor_month": anchor or None,
                    "seasonal_factors": _parse_seasonal(seasonal_text),
                    "business_day_rule": weekend,
                    "settlement_offset_days": int(settlement),
                    "payment_method": payment_method or None,
                    "include_in_budget": in_budget,
                }
                if not name.strip():
                    st.error("Give the rule a name.")
                elif amount <= 0:
                    st.error("Enter an amount greater than zero.")
                else:
                    ui.run_action(
                        lambda session: recurring_service.create_rule(session, payload),
                        success=f"Rule “{name}” created.",
                    )


def _parse_seasonal(text: str):
    if not text.strip():
        return None
    factors: dict[str, float] = {}
    for chunk in text.replace(";", ",").split(","):
        if ":" not in chunk:
            continue
        month, _, value = chunk.partition(":")
        try:
            index = int(month.strip())
            multiplier = float(value.strip().replace(",", "."))
        except ValueError:
            continue
        if 1 <= index <= 12:
            factors[str(index)] = multiplier
    return factors or None
