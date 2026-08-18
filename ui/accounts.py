"""Accounts — balances, categories, valuations and interest accrual."""

from __future__ import annotations

from datetime import date

import streamlit as st

from calculations.money import ZERO, D, money, money_sum
from calculations.periods import shift_period
from charts import financial_charts as fc
from constants import (
    ACCOUNT_TYPE_LABELS,
    ALLOCATION_KINDS,
    AccountType,
    BalanceMode,
    CategoryKind,
)
from services import account_service, category_service, networth_service
from ui import components as ui


def render() -> None:
    ui.page_header(
        "Accounts",
        "Where your money actually sits — and what you owe. Transfers between accounts "
        "never count as income or spending.",
        icon="🏦",
    )

    tabs = st.tabs(["Balances", "Net worth", "Manage accounts", "Categories"])
    with tabs[0]:
        _balances()
    with tabs[1]:
        _net_worth()
    with tabs[2]:
        _manage()
    with tabs[3]:
        _categories()


# ==========================================================================
def _balances() -> None:
    fmt = ui.formatter()
    theme = ui.theme()
    settings = ui.current_settings()
    today = date.today()
    period = settings.current_period(today)

    with ui.db_read() as session:
        views = account_service.balance_views(session, as_of=today, period=period)
        totals = account_service.totals(views)
        archived = account_service.balance_views(session, as_of=today,
                                                include_archived=True)

    if not views:
        ui.empty_state(
            "No accounts yet",
            "Add the accounts you actually use — a checking account, a wallet, a credit "
            "card. Balances, net worth and cash-flow projections all build from them.",
            icon="🏦",
        )
        return

    ui.kpi_row([
        ui.Kpi("Cash available", fmt.money(totals.cash), icon="💵",
               help_text="Checking, savings and cash accounts."),
        ui.Kpi("Total assets", fmt.money(totals.assets), icon="📦"),
        ui.Kpi("Total owed", fmt.money(totals.liabilities), icon="⛓️"),
        ui.Kpi("Net worth", fmt.money(totals.net_worth), icon="🏛️"),
        ui.Kpi("Card used", fmt.money(totals.credit_used), icon="💳",
               delta=(f"of {fmt.money(totals.credit_limit)} limit"
                      if totals.credit_limit else None),
               delta_good=None),
    ])

    ui.divider()
    left, right = st.columns([0.55, 0.45])
    with left:
        ui.section("Every account")
        ui.chart(fc.account_balance_bars(views, theme, height=340), key="acct_bars")
    with right:
        ui.section("Details")
        ui.money_table(
            [{"name": f"{view.account.icon or ''} {view.name}".strip(),
              "type": view.type_label,
              "balance": view.display_balance,
              "note": "owed" if view.is_liability else "",
              "movement": view.movement_this_period,
              "util": view.utilisation_pct,
              "available": view.available_credit}
             for view in views],
            [("name", "Account", "text"), ("type", "Type", "text"),
             ("balance", "Balance", "money"), ("note", "", "text"),
             ("movement", f"Change in {period.short_label}", "money"),
             ("util", "Used", "pct"), ("available", "Credit left", "money")],
            fmt, height=340,
        )

    negatives = [view for view in views if view.balance < 0 and not view.is_liability]
    if negatives:
        st.error(
            "🔴 Overdrawn: " + ", ".join(
                f"**{view.name}** ({fmt.money(view.balance)})" for view in negatives),
        )
    high_usage = [view for view in views
                  if view.utilisation_pct is not None and view.utilisation_pct >= 70]
    if high_usage:
        st.warning(
            "🟠 High card usage: " + ", ".join(
                f"**{view.name}** at {fmt.pct(view.utilisation_pct)}"
                for view in high_usage),
        )

    ui.divider()
    _balance_history(views, fmt, theme, settings, today)

    hidden = [view for view in archived if view.account.is_archived]
    if hidden:
        with st.expander(f"📦 {len(hidden)} archived account(s)"):
            ui.money_table(
                [{"name": view.name, "type": view.type_label,
                  "balance": view.display_balance} for view in hidden],
                [("name", "Account", "text"), ("type", "Type", "text"),
                 ("balance", "Last balance", "money")],
                fmt,
            )


def _balance_history(views, fmt: ui.Formatter, theme, settings, today: date) -> None:
    ui.section("Balances over time")
    months = st.select_slider("Months", [6, 12, 18, 24, 36], value=12,
                              key="acct_hist_months")
    names = [view.name for view in views]
    chosen = st.multiselect("Accounts", names, default=names[:4],
                            key="acct_hist_pick")
    if not chosen:
        st.caption("Pick at least one account.")
        return

    current = settings.current_period(today)
    periods = [shift_period(current, offset, settings.first_day_of_month)
               for offset in range(-months + 1, 1)]
    from calculations.cashflow import account_balance
    from services.common import load_account_infos, load_cash_txns

    with ui.db_read() as session:
        infos = {info.name: info for info in load_account_infos(session)}
        txns = load_cash_txns(session)

    series: dict[str, list[tuple[str, object]]] = {}
    for name in chosen:
        info = infos.get(name)
        if info is None:
            continue
        series[name] = [
            (period.short_label, account_balance(info, txns, as_of=period.end))
            for period in periods
        ]
    ui.chart(fc.balance_history_lines(series, theme, height=330), key="acct_hist")


# ==========================================================================
def _net_worth() -> None:
    fmt = ui.formatter()
    theme = ui.theme()
    today = date.today()

    columns = st.columns([0.3, 0.7])
    with columns[0]:
        months = st.select_slider("Months of history", [6, 12, 18, 24, 36, 60],
                                  value=12, key="nw_months")

    with ui.db_read() as session:
        summary = networth_service.current_summary(session, as_of=today)
        history = networth_service.trailing_history(session, months, today)
        change = networth_service.change(session, months, today)
        snapshots = networth_service.list_snapshots(session, 40)

    ui.kpi_row([
        ui.Kpi("Net worth", fmt.money(summary.net_worth), icon="🏛️",
               delta=f"{fmt.signed_money(change['absolute'])} over {months} months",
               delta_good=change["absolute"] >= 0),
        ui.Kpi("Assets", fmt.money(summary.total_assets), icon="📦"),
        ui.Kpi("Liabilities", fmt.money(summary.total_liabilities), icon="⛓️"),
        ui.Kpi("Debt to assets", fmt.pct(summary.debt_to_asset_pct), icon="⚖️",
               help_text="Lower is safer. Above 100% means you owe more than you own."),
        ui.Kpi("Average monthly change", fmt.signed_money(change["monthly_average"]),
               icon="📈", delta_good=change["monthly_average"] >= 0),
    ])

    if not summary.is_solvent:
        st.error("🔴 Liabilities currently exceed assets — net worth is negative.")

    ui.divider()
    ui.chart(
        fc.net_worth_chart(history, theme, height=380),
        table=[{"As of": point.as_of.isoformat(),
                "Assets": fmt.money(point.total_assets),
                "Liabilities": fmt.money(point.total_liabilities),
                "Net worth": fmt.money(point.net_worth)}
               for point in history],
        key="nw_chart",
    )

    left, right = st.columns(2)
    with left:
        ui.section("What you own")
        ui.money_table(
            [{"name": line.name, "amount": line.magnitude} for line in summary.assets],
            [("name", "Asset", "text"), ("amount", "Value", "money")], fmt,
        )
    with right:
        ui.section("What you owe")
        if summary.liabilities:
            ui.money_table(
                [{"name": line.name, "amount": line.magnitude}
                 for line in summary.liabilities],
                [("name", "Liability", "text"), ("amount", "Owed", "money")], fmt,
            )
        else:
            st.success("✅ No liabilities recorded.")

    ui.divider()
    ui.section("Snapshots",
               "A saved point-in-time record. Useful before and after a big change so "
               "history stays comparable even if you restate an asset value later.")
    columns = st.columns([0.3, 0.7])
    with columns[0]:
        if st.button("📌 Save a snapshot for today", type="primary",
                     **ui.wide(), key="nw_snap"):
            ui.run_action(
                lambda session: networth_service.save_snapshot(session, as_of=today),
                success=f"Snapshot saved for {today.isoformat()}.",
            )
    if snapshots:
        ui.money_table(
            [{"as_of": row.as_of_date, "assets": row.total_assets,
              "liabilities": row.total_liabilities, "net": row.net_worth}
             for row in snapshots],
            [("as_of", "As of", "date"), ("assets", "Assets", "money"),
             ("liabilities", "Liabilities", "money"), ("net", "Net worth", "money")],
            fmt, height=280,
        )


# ==========================================================================
def _manage() -> None:
    fmt = ui.formatter()
    with ui.db_read() as session:
        accounts = account_service.list_accounts(session, include_archived=True)
        interest_accounts = account_service.interest_bearing_accounts(session)

    _new_account_form()

    if not accounts:
        return

    ui.divider()
    with st.expander("✏️ Edit an account"):
        options = [(a.id, f"{a.name}{' (archived)' if a.is_archived else ''}")
                   for a in accounts]
        account_id = ui.select_with_none("Account", options,
                                         none_label="— pick one —", key="edit_acct_pick")
        if account_id is not None:
            account = next(a for a in accounts if a.id == account_id)
            _edit_account_form(account, fmt)

    ui.divider()
    _valuations(accounts, fmt)

    if interest_accounts:
        ui.divider()
        _interest(interest_accounts, fmt)


def _new_account_form() -> None:
    with st.expander("➕ Add an account", expanded=False):
        with st.form("new_account", clear_on_submit=True):
            columns = st.columns([0.34, 0.33, 0.33])
            with columns[0]:
                name = st.text_input("Name", placeholder="e.g. Checking — Nubank")
            with columns[1]:
                acct_type = st.selectbox(
                    "Type", list(ACCOUNT_TYPE_LABELS),
                    format_func=lambda item: ACCOUNT_TYPE_LABELS[item])
            with columns[2]:
                institution = st.text_input("Institution (optional)")

            columns = st.columns([0.25, 0.25, 0.25, 0.25])
            with columns[0]:
                opening = ui.money_input("Opening balance", ZERO, key="new_acct_open",
                                         help_text="For a card or loan, the amount owed.")
            with columns[1]:
                opening_date = st.date_input("As of", value=date.today())
            with columns[2]:
                limit = ui.money_input("Credit limit", ZERO, key="new_acct_limit",
                                       help_text="Cards only. 0 means no limit set.")
            with columns[3]:
                rate = ui.pct_input("Annual interest %", ZERO, key="new_acct_rate",
                                    min_value=0.0, max_value=1000.0)

            columns = st.columns([0.2, 0.2, 0.2, 0.2, 0.2])
            with columns[0]:
                icon = st.text_input("Icon", value="", max_chars=2,
                                     help="Any emoji, purely decorative.")
            with columns[1]:
                statement_day = st.number_input("Statement day", min_value=0,
                                                max_value=31, value=0, step=1)
            with columns[2]:
                due_day = st.number_input("Due day", min_value=0, max_value=31,
                                          value=0, step=1)
            with columns[3]:
                in_cash = st.checkbox(
                    "Counts as spendable cash", value=True,
                    help="Leave on for checking, savings and wallet accounts.")
            with columns[4]:
                in_nw = st.checkbox("Counts in net worth", value=True)

            notes = st.text_input("Notes (optional)")

            if st.form_submit_button("Create the account", type="primary"):
                if not name.strip():
                    st.error("Give the account a name.")
                else:
                    payload = {
                        "name": name, "type": acct_type,
                        "institution": institution or None,
                        "opening_balance": opening, "opening_date": opening_date,
                        "credit_limit": limit if limit > 0 else None,
                        "interest_rate": rate if rate > 0 else None,
                        "statement_day": int(statement_day) or None,
                        "due_day": int(due_day) or None,
                        "icon": icon or None, "notes": notes or None,
                        "include_in_cash": in_cash, "include_in_net_worth": in_nw,
                    }
                    ui.run_action(
                        lambda session: account_service.create_account(session, payload),
                        success=f"Account “{name}” created.",
                    )


def _edit_account_form(account, fmt: ui.Formatter) -> None:
    columns = st.columns([0.34, 0.33, 0.33])
    with columns[0]:
        name = st.text_input("Name", value=account.name, key="ea_name")
    with columns[1]:
        acct_type = st.selectbox(
            "Type", list(ACCOUNT_TYPE_LABELS),
            index=list(ACCOUNT_TYPE_LABELS).index(account.type),
            format_func=lambda item: ACCOUNT_TYPE_LABELS[item], key="ea_type")
    with columns[2]:
        institution = st.text_input("Institution", value=account.institution or "",
                                    key="ea_inst")

    columns = st.columns(4)
    with columns[0]:
        opening = ui.money_input("Opening balance", account.opening_balance,
                                 key="ea_open")
    with columns[1]:
        opening_date = st.date_input("As of", value=account.opening_date, key="ea_date")
    with columns[2]:
        limit = ui.money_input("Credit limit", account.credit_limit or ZERO,
                               key="ea_limit")
    with columns[3]:
        rate = ui.pct_input("Annual interest %", account.interest_rate or ZERO,
                            key="ea_rate", min_value=0.0, max_value=1000.0)

    columns = st.columns(4)
    with columns[0]:
        in_cash = st.checkbox("Spendable cash", value=account.include_in_cash,
                              key="ea_cash")
    with columns[1]:
        in_nw = st.checkbox("In net worth", value=account.include_in_net_worth,
                            key="ea_nw")
    with columns[2]:
        manual = st.checkbox(
            "Value it manually", value=account.balance_mode == BalanceMode.MANUAL.value,
            key="ea_manual",
            help="For property or market-priced holdings: the balance comes from the "
                 "latest valuation rather than from transactions.")
    with columns[3]:
        archived = st.checkbox("Archived", value=account.is_archived, key="ea_arch")

    actions = st.columns([0.34, 0.33, 0.33])
    with actions[0]:
        if st.button("Save changes", type="primary", key="ea_save",
                     **ui.wide()):
            payload = {
                "name": name, "type": acct_type, "institution": institution or None,
                "opening_balance": opening, "opening_date": opening_date,
                "credit_limit": limit if limit > 0 else None,
                "interest_rate": rate if rate > 0 else None,
                "include_in_cash": in_cash, "include_in_net_worth": in_nw,
                "balance_mode": (BalanceMode.MANUAL.value if manual
                                 else BalanceMode.TRANSACTIONS.value),
            }
            def action(session):
                account_service.update_account(session, account.id, payload)
                if archived != account.is_archived:
                    account_service.archive_account(session, account.id, archived)
                return True

            ui.run_action(action, success=f"“{name}” updated.")
    with actions[1]:
        label = "Un-archive" if account.is_archived else "Archive"
        if st.button(f"📦 {label}", key="ea_arch_btn", **ui.wide(),
                     help="Archiving hides the account but keeps every transaction."):
            ui.run_action(
                lambda session: account_service.archive_account(
                    session, account.id, not account.is_archived),
                success=f"“{account.name}” {label.lower()}d.",
            )
    with actions[2]:
        with ui.db_read() as session:
            usage = account_service.usage_count(session, account.id)
        if ui.confirm_action(
            "🗑 Delete", f"del_acct_{account.id}",
            prompt=f"Delete “{account.name}”? It has {usage['transactions']} "
                   "transaction(s), which would be detached but kept. Archiving is "
                   "almost always the better choice.",
            confirm_label="Delete anyway", require_text="DELETE",
            **ui.wide(),
        ):
            ui.run_action(
                lambda session: account_service.delete_account(
                    session, account.id, force=True),
                success=f"“{account.name}” deleted — a snapshot is in the recycle bin.",
            )


def _valuations(accounts, fmt: ui.Formatter) -> None:
    manual = [a for a in accounts if a.balance_mode == BalanceMode.MANUAL.value]
    ui.section(
        "Manual valuations",
        "For an apartment, a car or a fund whose value moves on its own rather than "
        "through transactions. Each valuation applies from its date onwards.",
    )
    candidates = [(a.id, a.name) for a in accounts if not a.is_archived]
    if not candidates:
        return
    columns = st.columns([0.3, 0.25, 0.25, 0.2])
    with columns[0]:
        account_id = ui.select_with_none("Account", candidates,
                                         value=manual[0].id if manual else None,
                                         none_label="— pick one —", key="val_acct")
    with columns[1]:
        value = ui.money_input("Value", ZERO, key="val_value")
    with columns[2]:
        as_of = st.date_input("As of", value=date.today(), key="val_date")
    with columns[3]:
        st.write("")
        if account_id is not None and st.button("Record valuation", type="primary",
                                                **ui.wide(), key="val_go"):
            ui.run_action(
                lambda session: account_service.add_valuation(
                    session, account_id, value, as_of),
                success="Valuation recorded — the account now uses manual valuation.",
            )

    if account_id is not None:
        with ui.db_read() as session:
            rows = account_service.list_valuations(session, account_id)
        if rows:
            ui.money_table(
                [{"as_of": row.as_of_date, "value": row.value, "notes": row.notes or ""}
                 for row in rows],
                [("as_of", "As of", "date"), ("value", "Value", "money"),
                 ("notes", "Notes", "text")],
                fmt, height=220,
            )


def _interest(accounts, fmt: ui.Formatter) -> None:
    ui.section(
        "Post monthly interest",
        "A financed car does not shrink by the payment — it shrinks by the payment minus "
        "interest. This posts the interest each month so the balance behaves like the "
        "real loan. Already-posted months are never posted twice.",
    )
    options = [(a.id, f"{a.name} · {D(a.interest_rate):.2f}% a year") for a in accounts]
    columns = st.columns([0.4, 0.2, 0.2, 0.2])
    with columns[0]:
        account_id = ui.select_with_none("Account", options, value=options[0][0],
                                         none_label="— pick one —", key="int_acct")
    with columns[1]:
        through = st.date_input("Through", value=date.today(), key="int_through")
    with columns[2]:
        day = st.number_input("Post on day", min_value=1, max_value=28, value=1,
                              step=1, key="int_day")
    with columns[3]:
        st.write("")
        preview = st.button("Preview", **ui.wide(), key="int_preview")

    if account_id is None:
        return
    if preview:
        with ui.db_read() as session:
            result = account_service.accrue_interest(
                session, account_id, through=through, day_of_month=int(day),
                dry_run=True)
        if result["posted"]:
            st.info(f"Would post {result['posted']} interest charge(s) totalling "
                    f"{fmt.money(result['total'])}.", icon="🧾")
            ui.money_table(
                [{"date": entry["date"], "amount": entry["amount"],
                  "balance": entry["balance_before"]}
                 for entry in result["entries"]],
                [("date", "Date", "date"), ("amount", "Interest", "money"),
                 ("balance", "Balance before", "money")],
                fmt, height=240,
            )
        else:
            st.success("✅ Nothing to post — interest is already up to date.")

    if ui.confirm_action(
        "🧾 Post the interest", f"int_post_{account_id}",
        prompt="Post interest transactions for every month not yet covered? They are "
               "flagged as excluded from the budget, since the cost is already inside "
               "the payment you budget.",
        confirm_label="Post them", button_type="primary",
    ):
        def action(session):
            return account_service.accrue_interest(
                session, account_id, through=through, day_of_month=int(day))

        result = ui.run_action(action, rerun=False)
        if result is not None:
            ui.flash(f"{result['posted']} interest charge(s) posted "
                     f"({money(result['total'])}).")
            st.rerun()


# ==========================================================================
def _categories() -> None:
    fmt = ui.formatter()
    ui.section(
        "Categories",
        "Two levels deep: a top-level category and its subcategories. The *type* "
        "decides how the budget treats it — expenses consume money, savings and "
        "investments set it aside, debt lines repay it.",
    )

    with ui.db_read() as session:
        counts = category_service.counts_by_kind(session)
        trees = {
            kind: category_service.category_tree(session, kind=kind)
            for kind in [CategoryKind.INCOME.value, *ALLOCATION_KINDS]
        }
        parent_options = category_service.options_for_select(session)

    ui.kpi_row([
        ui.Kpi("Income", str(counts.get("income", 0)), icon="📥"),
        ui.Kpi("Expense", str(counts.get("expense", 0)), icon="📤"),
        ui.Kpi("Savings", str(counts.get("savings", 0)), icon="🐖"),
        ui.Kpi("Investment", str(counts.get("investment", 0)), icon="📈"),
        ui.Kpi("Debt", str(counts.get("debt", 0)), icon="⛓️"),
    ])

    ui.divider()
    _new_category_form(parent_options)

    ui.divider()
    labels = {"income": "Income", "expense": "Expenses", "savings": "Savings",
              "investment": "Investments", "debt": "Debt"}
    tabs = st.tabs([labels[kind] for kind in trees])
    for tab, (kind, nodes) in zip(tabs, trees.items()):
        with tab:
            if not nodes:
                st.caption("No categories of this type yet.")
                continue
            for node in nodes:
                children = ", ".join(child.name for child in node.children)
                icon = node.category.icon or "•"
                st.markdown(f"**{icon} {node.name}**")
                if children:
                    st.caption(children)
            _edit_category_panel(kind, nodes)


def _new_category_form(parent_options) -> None:
    with st.expander("➕ Add a category"):
        with st.form("new_category", clear_on_submit=True):
            columns = st.columns([0.32, 0.24, 0.28, 0.16])
            with columns[0]:
                name = st.text_input("Name", placeholder="e.g. Childcare")
            with columns[1]:
                kind = st.selectbox(
                    "Type", [CategoryKind.INCOME.value, *ALLOCATION_KINDS],
                    index=1, format_func=str.title)
            with columns[2]:
                parent_id = ui.select_with_none(
                    "Inside (optional)", parent_options,
                    none_label="— top level —", key="new_cat_parent",
                    help_text="Pick a top-level category to make this a subcategory.")
            with columns[3]:
                icon = st.text_input("Icon", max_chars=2)
            if st.form_submit_button("Create the category", type="primary"):
                if not name.strip():
                    st.error("Give the category a name.")
                else:
                    ui.run_action(
                        lambda session: category_service.create_category(session, {
                            "name": name, "kind": kind, "parent_id": parent_id,
                            "icon": icon or None,
                        }),
                        success=f"Category “{name}” created.",
                    )


def _edit_category_panel(kind: str, nodes) -> None:
    flat: list[tuple[int, str]] = []
    for node in nodes:
        flat.append((node.id, node.name))
        for child in node.children:
            flat.append((child.id, f"{node.name} › {child.name}"))

    with st.expander(f"✏️ Rename, archive or delete a {kind} category"):
        category_id = ui.select_with_none("Category", flat, none_label="— pick one —",
                                         key=f"edit_cat_{kind}")
        if category_id is None:
            return
        with ui.db_read() as session:
            category = category_service.get_category(session, category_id)
            usage = category_service.usage_count(session, category_id)

        columns = st.columns([0.34, 0.16, 0.16, 0.34])
        with columns[0]:
            name = st.text_input("Name", value=category.name, key=f"cat_name_{kind}")
        with columns[1]:
            icon = st.text_input("Icon", value=category.icon or "", max_chars=2,
                                key=f"cat_icon_{kind}")
        with columns[2]:
            archived = st.checkbox("Archived", value=category.is_archived,
                                  key=f"cat_arch_{kind}")
        with columns[3]:
            st.caption(
                f"Used by {usage['transactions']} transaction(s), "
                f"{usage['budget_lines']} budget line(s), {usage['rules']} rule(s)."
            )

        actions = st.columns([0.34, 0.33, 0.33])
        with actions[0]:
            if st.button("Save", type="primary", key=f"cat_save_{kind}",
                         **ui.wide()):
                def action(session):
                    category_service.update_category(session, category_id, {
                        "name": name, "icon": icon or None,
                    })
                    if archived != category.is_archived:
                        category_service.archive_category(session, category_id, archived)
                    return True

                ui.run_action(action, success=f"“{name}” updated.")
        with actions[1]:
            if st.button("📦 Archive instead", key=f"cat_arch_btn_{kind}",
                         **ui.wide(),
                         help="Keeps every historical figure but hides it from pickers."):
                ui.run_action(
                    lambda session: category_service.archive_category(
                        session, category_id, True),
                    success=f"“{category.name}” archived.",
                )
        with actions[2]:
            in_use = usage["transactions"] + usage["budget_lines"] + usage["rules"]
            if ui.confirm_action(
                "🗑 Delete", f"del_cat_{category_id}",
                prompt=(f"Delete “{category.full_name}”? It is used by {in_use} record(s), "
                        "which stay in your history but become uncategorised."
                        if in_use else
                        f"Delete “{category.full_name}”? A snapshot goes to the recycle bin."),
                confirm_label="Delete it", **ui.wide(),
                require_text="DELETE" if in_use else None,
            ):
                ui.run_action(
                    lambda session: category_service.delete_category(
                        session, category_id, force=True),
                    success=f"“{category.name}” deleted.",
                )
