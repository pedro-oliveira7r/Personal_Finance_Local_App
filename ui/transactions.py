"""Transactions — entry, search, editing, transfers, import and export."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import streamlit as st

from calculations.money import ZERO, money, money_sum
from constants import (
    ALLOCATION_KINDS,
    PAYMENT_METHODS,
    CategoryKind,
    TxnKind,
    TxnStatus,
)
from services import account_service, category_service, debt_service, goal_service
from services import transaction_service as txs
from ui import components as ui

PAGE_SIZE = 50


def render() -> None:
    ui.page_header(
        "Transactions",
        "Every movement of money — recorded, planned, or imported from your bank.",
        icon="💳",
    )

    tabs = st.tabs(["Browse", "Add", "Transfer", "Import CSV", "Export", "Recycle bin"])
    with tabs[0]:
        _browse()
    with tabs[1]:
        _add_form()
    with tabs[2]:
        _transfer_form()
    with tabs[3]:
        _import_tab()
    with tabs[4]:
        _export_tab()
    with tabs[5]:
        _recycle_bin()


# ==========================================================================
# Browse / edit
# ==========================================================================
def _browse() -> None:
    fmt = ui.formatter()
    settings = ui.current_settings()
    today = date.today()

    with ui.db_read() as session:
        accounts = account_service.options_for_select(session, include_archived=True)
        categories = category_service.options_for_select(session)
        tags = txs.all_tags(session)
        bounds = txs.date_bounds(session)

    with st.expander("🔎 Filters", expanded=True):
        row1 = st.columns([0.24, 0.24, 0.26, 0.26])
        with row1[0]:
            preset = st.selectbox(
                "Range", ["This period", "Last 30 days", "Last 90 days",
                          "This year", "Everything", "Custom"],
                key="txn_preset",
            )
        start, end = _resolve_range(preset, settings, today, bounds)
        with row1[1]:
            if preset == "Custom":
                start = st.date_input("From", value=start or today - timedelta(days=30),
                                      key="txn_start")
            else:
                st.text_input("From", value=start.isoformat() if start else "—",
                              disabled=True, key="txn_start_display")
        with row1[2]:
            if preset == "Custom":
                end = st.date_input("To", value=end or today, key="txn_end")
            else:
                st.text_input("To", value=end.isoformat() if end else "—",
                              disabled=True, key="txn_end_display")
        with row1[3]:
            search = st.text_input("Search", placeholder="description, note or tag",
                                  key="txn_search")

        row2 = st.columns([0.22, 0.22, 0.2, 0.18, 0.18])
        with row2[0]:
            kinds = st.multiselect("Type", TxnKind.values(), format_func=str.title,
                                   key="txn_kinds", placeholder="All types")
        with row2[1]:
            statuses = st.multiselect("Status", TxnStatus.values(), format_func=str.title,
                                      key="txn_statuses", placeholder="All statuses")
        with row2[2]:
            account_ids = st.multiselect(
                "Account", [item[0] for item in accounts],
                format_func=lambda item: dict(accounts).get(item, str(item)),
                key="txn_accounts", placeholder="All accounts")
        with row2[3]:
            category_ids = st.multiselect(
                "Category", [item[0] for item in categories],
                format_func=lambda item: dict(categories).get(item, str(item)),
                key="txn_categories", placeholder="All categories")
        with row2[4]:
            chosen_tags = st.multiselect("Tags", tags, key="txn_tags",
                                         placeholder="Any tag")

        row3 = st.columns([0.22, 0.22, 0.28, 0.28])
        with row3[0]:
            min_amount = st.number_input("Minimum amount", min_value=0.0, value=0.0,
                                         step=50.0, key="txn_min")
        with row3[1]:
            max_amount = st.number_input("Maximum amount", min_value=0.0, value=0.0,
                                         step=50.0, key="txn_max",
                                         help="0 means no upper limit.")
        with row3[2]:
            planned_flag = st.selectbox(
                "Planned or not", ["Any", "Was planned", "Unplanned"],
                key="txn_planned")
        with row3[3]:
            use_effective = st.checkbox(
                "Filter by the date cash actually moved", value=False,
                key="txn_effective",
                help="Off: uses the entry date. On: uses the payment date.")

    flt = txs.TxnFilter(
        start=start, end=end,
        kinds=kinds or None, statuses=statuses or None,
        account_ids=account_ids or None, category_ids=category_ids or None,
        tags=chosen_tags or None,
        search=search or None,
        min_amount=Decimal(str(min_amount)) if min_amount else None,
        max_amount=Decimal(str(max_amount)) if max_amount else None,
        planned_flag=(True if planned_flag == "Was planned"
                      else (False if planned_flag == "Unplanned" else None)),
        use_effective_date=use_effective,
    )

    page = st.session_state.get("_txn_page", 0)
    with ui.db_read() as session:
        total = txs.count_transactions(session, flt)
        flt.limit = PAGE_SIZE
        flt.offset = page * PAGE_SIZE
        rows = txs.list_transactions(session, flt)
        category_names = category_service.options_for_select(session,
                                                            include_archived=True)
        name_map = dict(category_names)
        account_map = dict(accounts)

    totals_in = money_sum(t.amount for t in rows if t.kind == TxnKind.INCOME.value)
    totals_out = money_sum(t.amount for t in rows if t.kind == TxnKind.EXPENSE.value)
    ui.kpi_row([
        ui.Kpi("Matching transactions", f"{total:,}".replace(",", "."), icon="🔢"),
        ui.Kpi("Income on this page", fmt.money(totals_in), icon="📥"),
        ui.Kpi("Expenses on this page", fmt.money(totals_out), icon="📤"),
        ui.Kpi("Net on this page", fmt.signed_money(money(totals_in - totals_out)),
               icon="🔄"),
    ], columns=4)

    if not rows:
        ui.empty_state(
            "Nothing matches those filters",
            "Widen the date range or clear a filter. If this is a brand new book, add "
            "your first transaction in the Add tab.",
            icon="🔍",
        )
        return

    status_icon = {"completed": "✅", "planned": "🕒", "void": "🚫"}
    kind_icon = {"income": "📥", "expense": "📤", "transfer": "🔄"}
    table = [
        {
            "id": txn.id,
            "": status_icon.get(txn.status, ""),
            "Date": fmt.date(txn.txn_date),
            "Paid": fmt.date(txn.actual_date) if txn.actual_date else "—",
            "Description": txn.description,
            "Type": f"{kind_icon.get(txn.kind, '')} {txn.kind.title()}",
            "Category": name_map.get(txn.category_id, "—") if txn.category_id else "—",
            "Account": account_map.get(txn.account_id, "—") if txn.account_id else "—",
            "Amount": fmt.money(txn.amount),
            "Tags": txn.tags or "",
        }
        for txn in rows
    ]
    st.dataframe(table, **ui.wide(), hide_index=True,
                 column_order=["", "Date", "Paid", "Description", "Type",
                               "Category", "Account", "Amount", "Tags"],
                 height=min(640, 60 + 34 * len(table)))

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    nav = st.columns([0.15, 0.2, 0.15, 0.5])
    with nav[0]:
        if st.button("‹ Previous", disabled=page <= 0, key="txn_prev",
                     **ui.wide()):
            st.session_state["_txn_page"] = max(0, page - 1)
            st.rerun()
    with nav[1]:
        st.caption(f"Page {page + 1} of {pages}")
    with nav[2]:
        if st.button("Next ›", disabled=page + 1 >= pages, key="txn_next",
                     **ui.wide()):
            st.session_state["_txn_page"] = page + 1
            st.rerun()

    ui.divider()
    _edit_panel(rows, fmt)
    _bulk_panel(rows, fmt)


def _resolve_range(preset: str, settings, today: date, bounds):
    if preset == "This period":
        period = settings.current_period(today)
        return period.start, period.end
    if preset == "Last 30 days":
        return today - timedelta(days=30), today
    if preset == "Last 90 days":
        return today - timedelta(days=90), today
    if preset == "This year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    if preset == "Everything":
        return bounds[0], bounds[1] or today
    return st.session_state.get("txn_start", today - timedelta(days=30)), \
        st.session_state.get("txn_end", today)


def _edit_panel(rows, fmt: ui.Formatter) -> None:
    with st.expander("✏️ Edit or delete one transaction"):
        options = [
            (txn.id, f"{txn.txn_date.isoformat()} · {txn.description} · "
                     f"{fmt.money(txn.amount)}")
            for txn in rows
        ]
        txn_id = ui.select_with_none("Transaction", options,
                                     none_label="— pick one from the page above —",
                                     key="edit_txn_pick")
        if txn_id is None:
            return
        txn = next((item for item in rows if item.id == txn_id), None)
        if txn is None:
            return

        with ui.db_read() as session:
            income_cats = category_service.options_for_select(
                session, kinds=[CategoryKind.INCOME.value])
            other_cats = category_service.options_for_select(
                session, kinds=list(ALLOCATION_KINDS))
            accounts = account_service.options_for_select(session, include_archived=True)

        columns = st.columns([0.28, 0.24, 0.24, 0.24])
        with columns[0]:
            description = st.text_input("Description", value=txn.description,
                                        key="edit_desc")
        with columns[1]:
            amount = ui.money_input("Amount", txn.amount, key="edit_amount")
        with columns[2]:
            txn_date = st.date_input("Date", value=txn.txn_date, key="edit_date")
        with columns[3]:
            status = st.selectbox("Status", TxnStatus.values(),
                                 index=TxnStatus.values().index(txn.status),
                                 format_func=str.title, key="edit_status")

        columns = st.columns([0.3, 0.3, 0.2, 0.2])
        with columns[0]:
            pool = income_cats if txn.kind == TxnKind.INCOME.value else other_cats
            category_id = ui.select_with_none(
                "Category", pool, value=txn.category_id, none_label="— none —",
                key="edit_cat",
                disabled=txn.kind == TxnKind.TRANSFER.value)
        with columns[1]:
            account_id = ui.select_with_none("Account", accounts,
                                             value=txn.account_id,
                                             none_label="— none —", key="edit_acct")
        with columns[2]:
            method = st.selectbox("Payment method", [""] + PAYMENT_METHODS,
                                 index=([""] + PAYMENT_METHODS).index(txn.payment_method)
                                 if txn.payment_method in PAYMENT_METHODS else 0,
                                 key="edit_method")
        with columns[3]:
            actual = st.date_input("Paid on", value=txn.actual_date or txn.txn_date,
                                  key="edit_actual",
                                  disabled=status != TxnStatus.COMPLETED.value)

        columns = st.columns([0.4, 0.3, 0.3])
        with columns[0]:
            tags = st.text_input("Tags", value=txn.tags or "", key="edit_tags",
                                help="Comma separated.")
        with columns[1]:
            exclude = st.checkbox("Exclude from budget maths",
                                 value=txn.exclude_from_budget, key="edit_exclude",
                                 help="For reimbursed costs or internal bookkeeping.")
        with columns[2]:
            availability = st.date_input(
                "Available for budgeting from",
                value=txn.availability_date or txn.txn_date,
                key="edit_avail",
                disabled=txn.kind != TxnKind.INCOME.value,
                help="Overrides the global availability rule for this one payment.")
        notes = st.text_area("Notes", value=txn.notes or "", key="edit_notes", height=70)

        actions = st.columns([0.25, 0.25, 0.25, 0.25])
        with actions[0]:
            if st.button("Save changes", type="primary", key="edit_save",
                         **ui.wide()):
                payload = {
                    "description": description, "amount": amount,
                    "txn_date": txn_date, "status": status,
                    "category_id": category_id, "account_id": account_id,
                    "payment_method": method or None, "tags": tags or None,
                    "notes": notes or None, "exclude_from_budget": exclude,
                    "actual_date": (actual if status == TxnStatus.COMPLETED.value
                                    else None),
                }
                if txn.kind == TxnKind.INCOME.value and availability != txn_date:
                    payload["availability_date"] = availability
                ui.run_action(
                    lambda session: txs.update_transaction(session, txn_id, payload),
                    success="Transaction updated.",
                )
        with actions[1]:
            if txn.status == TxnStatus.PLANNED.value:
                if st.button("Mark as completed", key="edit_complete",
                             **ui.wide()):
                    ui.run_action(
                        lambda session: txs.complete_transaction(session, txn_id),
                        success="Marked as completed.",
                    )
            elif st.button("Back to planned", key="edit_replan",
                           **ui.wide()):
                ui.run_action(
                    lambda session: txs.revert_to_planned(session, txn_id),
                    success="Set back to planned.",
                )
        with actions[2]:
            if st.button("Void it", key="edit_void", **ui.wide(),
                         help="Keeps the record but removes it from every calculation."):
                ui.run_action(
                    lambda session: txs.void_transaction(session, txn_id),
                    success="Transaction voided.",
                )
        with actions[3]:
            if ui.confirm_action(
                "🗑 Delete", f"del_txn_{txn_id}",
                prompt="Delete this transaction? It goes to the recycle bin and can be "
                       "restored from the Recycle bin tab.",
                confirm_label="Delete it", **ui.wide(),
            ):
                ui.run_action(
                    lambda session: txs.delete_transaction(session, txn_id),
                    success="Transaction deleted — restorable from the recycle bin.",
                )


def _bulk_panel(rows, fmt: ui.Formatter) -> None:
    with st.expander("⚡ Bulk actions on this page"):
        with ui.db_read() as session:
            categories = category_service.options_for_select(session)
        labels = {
            txn.id: f"{txn.txn_date.isoformat()} · {txn.description} · "
                    f"{fmt.money(txn.amount)}"
            for txn in rows
        }
        chosen = st.multiselect("Transactions", list(labels),
                                format_func=lambda item: labels[item],
                                key="bulk_pick")
        if not chosen:
            st.caption("Pick one or more transactions to act on.")
            return
        st.caption(f"{len(chosen)} selected.")

        columns = st.columns([0.3, 0.35, 0.35])
        with columns[0]:
            if st.button("Mark as completed", key="bulk_complete",
                         **ui.wide()):
                ui.run_action(
                    lambda session: txs.bulk_complete(session, chosen),
                    success=f"{len(chosen)} transaction(s) completed.",
                )
        with columns[1]:
            target = ui.select_with_none("Move to category", categories,
                                         none_label="— pick a category —",
                                         key="bulk_cat")
            if target is not None and st.button("Recategorise", key="bulk_recat",
                                                **ui.wide()):
                def action(session):
                    return txs.bulk_recategorise(session, chosen, target)

                count = ui.run_action(action, rerun=False)
                if count is not None:
                    ui.flash(f"{count} transaction(s) recategorised "
                             f"({len(chosen) - count} skipped as incompatible).")
                    st.rerun()
        with columns[2]:
            if ui.confirm_action(
                f"🗑 Delete {len(chosen)}", "bulk_delete",
                prompt=f"Delete {len(chosen)} transaction(s)? They all go to the recycle "
                       "bin and can be restored.",
                confirm_label="Delete them", **ui.wide(),
            ):
                ui.run_action(
                    lambda session: txs.bulk_delete(session, chosen),
                    success=f"{len(chosen)} transaction(s) deleted.",
                )


# ==========================================================================
# Add / transfer
# ==========================================================================
def _add_form() -> None:
    with ui.db_read() as session:
        income_cats = category_service.options_for_select(
            session, kinds=[CategoryKind.INCOME.value])
        other_cats = category_service.options_for_select(
            session, kinds=list(ALLOCATION_KINDS))
        accounts = account_service.options_for_select(session)
        goals = [(g.id, g.name) for g in goal_service.active_goals(session)]
        debts = [(d.id, d.name) for d in debt_service.list_debts(session)]
        recent = txs.recent_descriptions(session, 60)

    if not accounts:
        ui.empty_state(
            "You need an account first",
            "A transaction has to come from or go to somewhere. Create a checking "
            "account, a wallet, or a card and come back.",
            icon="🏦", action_label="Go to Accounts",
            action=lambda: _goto("accounts"),
        )
        return

    kind = st.radio("What happened?",
                    [TxnKind.EXPENSE.value, TxnKind.INCOME.value],
                    format_func=lambda item: ("I spent money" if item == "expense"
                                              else "I received money"),
                    horizontal=True, key="add_kind")

    with st.form("add_transaction", clear_on_submit=True):
        columns = st.columns([0.34, 0.22, 0.22, 0.22])
        with columns[0]:
            description = st.text_input("Description", placeholder="e.g. Supermarket")
        with columns[1]:
            amount = ui.money_input("Amount", ZERO, key="add_amount")
        with columns[2]:
            txn_date = st.date_input("Date", value=date.today())
        with columns[3]:
            status = st.selectbox(
                "Status", [TxnStatus.COMPLETED.value, TxnStatus.PLANNED.value],
                format_func=lambda item: ("Already happened" if item == "completed"
                                          else "Planned for later"))

        columns = st.columns([0.3, 0.3, 0.2, 0.2])
        with columns[0]:
            pool = income_cats if kind == TxnKind.INCOME.value else other_cats
            category_id = ui.select_with_none("Category", pool,
                                              none_label="— uncategorised —",
                                              key="add_cat")
        with columns[1]:
            account_id = ui.select_with_none("Account", accounts,
                                             value=accounts[0][0],
                                             none_label="— pick one —", key="add_acct")
        with columns[2]:
            method = st.selectbox("Payment method", [""] + PAYMENT_METHODS)
        with columns[3]:
            planned_flag = st.checkbox(
                "This was part of the plan", value=True,
                help="Untick for a surprise — tracking then flags it as unplanned.")

        columns = st.columns([0.34, 0.22, 0.22, 0.22])
        with columns[0]:
            tags = st.text_input("Tags", placeholder="essential, work")
        with columns[1]:
            goal_id = (ui.select_with_none("Counts toward goal", goals,
                                           none_label="— none —", key="add_goal")
                       if goals else None)
        with columns[2]:
            debt_id = (ui.select_with_none("Counts toward debt", debts,
                                           none_label="— none —", key="add_debt")
                       if debts else None)
        with columns[3]:
            allow_duplicate = st.checkbox(
                "Save even if it looks like a duplicate", value=False,
                help="The app blocks identical entries by default.")
        notes = st.text_area("Notes", height=70, placeholder="Anything worth remembering")

        submitted = st.form_submit_button("Save the transaction", type="primary")

    if recent:
        st.caption("Recently used: " + " · ".join(recent[:8]))

    if submitted:
        if not description.strip():
            st.error("Give the transaction a description.")
            return
        if amount <= 0:
            st.error("The amount has to be greater than zero.")
            return
        payload = {
            "txn_date": txn_date, "description": description, "amount": amount,
            "kind": kind, "status": status,
            "actual_date": txn_date if status == TxnStatus.COMPLETED.value else None,
            "category_id": category_id, "account_id": account_id,
            "payment_method": method or None, "tags": tags or None,
            "notes": notes or None, "is_planned": planned_flag,
            "goal_id": goal_id, "debt_id": debt_id,
        }
        ui.run_action(
            lambda session: txs.create_transaction(
                session, payload, allow_duplicate=allow_duplicate),
            success=f"Saved: {description} · {money(amount)}.",
        )


def _transfer_form() -> None:
    ui.section(
        "Move money between your own accounts",
        "Transfers are never income or an expense — they only relocate money, so your "
        "totals stay honest. Paying a credit card or funding savings belongs here.",
    )
    with ui.db_read() as session:
        accounts = account_service.options_for_select(session)
        goals = [(g.id, g.name) for g in goal_service.active_goals(session)]
        debts = [(d.id, d.name) for d in debt_service.list_debts(session)]

    if len(accounts) < 2:
        st.info("You need at least two accounts to make a transfer.", icon="ℹ️")
        return

    with st.form("add_transfer", clear_on_submit=True):
        columns = st.columns([0.28, 0.28, 0.22, 0.22])
        with columns[0]:
            from_id = st.selectbox("From", [item[0] for item in accounts],
                                   format_func=lambda item: dict(accounts)[item])
        with columns[1]:
            to_id = st.selectbox("To", [item[0] for item in accounts],
                                 index=min(1, len(accounts) - 1),
                                 format_func=lambda item: dict(accounts)[item])
        with columns[2]:
            amount = ui.money_input("Amount", ZERO, key="transfer_amount")
        with columns[3]:
            txn_date = st.date_input("Date", value=date.today())

        columns = st.columns([0.4, 0.3, 0.3])
        with columns[0]:
            description = st.text_input("Description", value="Transfer")
        with columns[1]:
            goal_id = (ui.select_with_none("Counts toward goal", goals,
                                           none_label="— none —", key="transfer_goal")
                       if goals else None)
        with columns[2]:
            debt_id = (ui.select_with_none("Counts toward debt", debts,
                                           none_label="— none —", key="transfer_debt")
                       if debts else None)
        notes = st.text_input("Notes", placeholder="optional")

        if st.form_submit_button("Record the transfer", type="primary"):
            if from_id == to_id:
                st.error("Pick two different accounts.")
            elif amount <= 0:
                st.error("The amount has to be greater than zero.")
            else:
                payload = {
                    "txn_date": txn_date, "description": description or "Transfer",
                    "amount": amount, "kind": TxnKind.TRANSFER.value,
                    "status": TxnStatus.COMPLETED.value, "actual_date": txn_date,
                    "account_id": from_id, "to_account_id": to_id,
                    "notes": notes or None, "goal_id": goal_id, "debt_id": debt_id,
                }
                ui.run_action(
                    lambda session: txs.create_transaction(session, payload),
                    success=f"Transferred {money(amount)}.",
                )


def _goto(slug: str) -> None:
    st.session_state["_nav_page"] = slug
    st.rerun()


# ==========================================================================
# Import
# ==========================================================================
def _import_tab() -> None:
    from import_export import csv_handler

    fmt = ui.formatter()
    ui.section(
        "Import a CSV",
        "Nothing is written until you review the preview and press Import. Rows that "
        "match an existing transaction exactly are flagged and left out by default.",
    )

    st.download_button("⬇ Download a template CSV", csv_handler.template_csv(),
                       file_name="transactions-template.csv", mime="text/csv")

    uploaded = st.file_uploader("CSV file", type=["csv", "txt"], key="import_file")
    if uploaded is None:
        with st.expander("Which columns does it understand?"):
            st.markdown(
                "- **date** — required. Many formats work (`2026-08-05`, `05/08/2026`).\n"
                "- **description** — required.\n"
                "- **amount** — required. `1.234,56`, `1,234.56` and `-284.90` all parse.\n"
                "- **kind** — optional (`income`/`expense`/`transfer`, or Portuguese "
                "equivalents). Without it, negative amounts are read as expenses.\n"
                "- **category**, **subcategory**, **account**, **to_account**, "
                "**payment_method**, **tags**, **notes**, **status** — all optional.\n\n"
                "Column names in English or Brazilian Portuguese are matched "
                "automatically; you can correct the mapping before importing."
            )
        _import_history()
        return

    raw = uploaded.getvalue()
    with ui.db_read() as session:
        headers, _ = csv_handler.read_rows(raw)
        detected = csv_handler.detect_mapping(headers)
        accounts = account_service.options_for_select(session)

    ui.section("1 · Check the column mapping")
    mapping: dict[str, str | None] = {}
    fields = list(csv_handler.COLUMN_ALIASES)
    for start in range(0, len(fields), 4):
        columns = st.columns(4)
        for col, field_name in zip(columns, fields[start:start + 4]):
            with col:
                options = ["—"] + headers
                current = detected.get(field_name)
                index = options.index(current) if current in options else 0
                chosen = st.selectbox(field_name.replace("_", " ").title(), options,
                                      index=index, key=f"map_{field_name}")
                mapping[field_name] = None if chosen == "—" else chosen

    ui.section("2 · Set the defaults")
    columns = st.columns([0.28, 0.24, 0.24, 0.24])
    with columns[0]:
        default_account = ui.select_with_none(
            "Default account", accounts, value=accounts[0][0] if accounts else None,
            none_label="— none —", key="import_account",
            help_text="Used for rows with no account column.")
    with columns[1]:
        negative_expense = st.checkbox(
            "Negative amounts are expenses", value=True, key="import_negative",
            help="The convention most bank exports use.")
    with columns[2]:
        create_categories = st.checkbox("Create missing categories", value=True,
                                        key="import_make_cats")
    with columns[3]:
        create_accounts = st.checkbox("Create missing accounts", value=False,
                                      key="import_make_accts")

    with ui.db_read() as session:
        preview = csv_handler.build_preview(
            session, raw, source_name=uploaded.name, mapping=mapping,
            default_account_id=default_account,
            negative_is_expense=negative_expense,
        )

    ui.section("3 · Review the preview")
    ui.kpi_row([
        ui.Kpi("Rows read", str(len(preview.rows)), icon="📄"),
        ui.Kpi("Ready to import", str(len(preview.importable)), icon="✅"),
        ui.Kpi("Look like duplicates", str(len(preview.duplicate_rows)), icon="👯"),
        ui.Kpi("Have problems", str(len(preview.error_rows)), icon="🚫"),
        ui.Kpi("Net effect", fmt.signed_money(money(preview.total_in - preview.total_out)),
               icon="🔄",
               help_text=f"In {fmt.money(preview.total_in)} · "
                         f"out {fmt.money(preview.total_out)}"),
    ])

    if preview.error_rows:
        with st.expander(f"🚫 {len(preview.error_rows)} row(s) cannot be imported",
                         expanded=True):
            for row in preview.error_rows[:25]:
                st.markdown(f"- **Row {row.index}** — {row.error}")
            if len(preview.error_rows) > 25:
                st.caption(f"…and {len(preview.error_rows) - 25} more.")

    if preview.duplicate_rows:
        with st.expander(f"👯 {len(preview.duplicate_rows)} possible duplicate(s)"):
            for row in preview.duplicate_rows[:25]:
                st.markdown(
                    f"- **Row {row.index}** — {row.payload['description']} · "
                    f"{fmt.money(row.payload['amount'])} → matches "
                    f"{row.duplicate_label}"
                )

    if preview.new_categories:
        st.info("New categories that would be created: " +
                ", ".join(preview.new_categories[:12]) +
                ("…" if len(preview.new_categories) > 12 else ""), icon="🏷️")
    if preview.new_accounts:
        st.warning("Accounts named in the file that do not exist yet: " +
                   ", ".join(preview.new_accounts[:8]) +
                   (". Tick “Create missing accounts” to add them."
                    if not create_accounts else ""), icon="🏦")

    sample = preview.valid_rows[:200]
    if sample:
        st.dataframe(
            [{"Row": row.index,
              "Date": row.payload["txn_date"],
              "Description": row.payload["description"],
              "Type": row.payload["kind"].title(),
              "Amount": fmt.money(row.payload["amount"]),
              "Category": row.new_category or ("mapped" if row.payload["category_id"]
                                               else "—"),
              "Duplicate": row.duplicate_label or "",
              "Will import": "yes" if row.include else "no"}
             for row in sample],
            **ui.wide(), hide_index=True, height=340,
        )

    ui.section("4 · Import")
    include_dupes = st.checkbox(
        "Import the flagged duplicates too", value=False, key="import_dupes",
        help="Off by default. Turn it on only if you know they are genuinely separate.")
    if not preview.importable and not include_dupes:
        st.warning("There is nothing to import with the current settings.", icon="⚠️")
        return
    count = len(preview.importable) + (len(preview.duplicate_rows) if include_dupes else 0)
    if ui.confirm_action(
        f"📥 Import {count} transaction(s)", "do_import",
        prompt=f"Import {count} transaction(s) from **{uploaded.name}**? "
               "Existing data is never overwritten, and the whole batch can be rolled "
               "back afterwards.",
        confirm_label="Import now", button_type="primary",
    ):
        def action(session):
            return csv_handler.commit(
                session, preview,
                create_missing_categories=create_categories,
                create_missing_accounts=create_accounts,
                include_duplicates=include_dupes,
            )

        result = ui.run_action(action, rerun=False, spinner="Importing…")
        if result is not None:
            ui.flash(result.summary() + " You can undo this from the import history.")
            if result.errors:
                ui.flash(f"{len(result.errors)} row(s) failed — see the import history.",
                         "warning")
            st.rerun()

    _import_history()


def _import_history() -> None:
    from import_export import csv_handler

    with ui.db_read() as session:
        batches = csv_handler.list_batches(session)
    if not batches:
        return
    ui.divider()
    ui.section("Import history", "Any batch can be rolled back in one action.")
    for batch in batches[:8]:
        columns = st.columns([0.42, 0.2, 0.18, 0.2])
        with columns[0]:
            st.markdown(f"**{batch.source_name}**")
            st.caption(batch.created_at.strftime("%Y-%m-%d %H:%M"))
        with columns[1]:
            st.caption(f"{batch.imported_count} imported · {batch.skipped_count} skipped")
        with columns[2]:
            st.caption("rolled back" if batch.rolled_back_at else "active")
        with columns[3]:
            if batch.rolled_back_at is None and ui.confirm_action(
                "↩ Roll back", f"rollback_{batch.id}",
                prompt=f"Roll back “{batch.source_name}”? Its "
                       f"{batch.imported_count} transaction(s) go to the recycle bin.",
                confirm_label="Roll it back", **ui.wide(),
            ):
                def action(session):
                    return csv_handler.rollback(session, batch.id)

                count = ui.run_action(action, rerun=False)
                if count is not None:
                    ui.flash(f"{count} transaction(s) rolled back.")
                    st.rerun()


# ==========================================================================
# Export
# ==========================================================================
def _export_tab() -> None:
    from import_export import csv_handler, excel_handler

    fmt = ui.formatter()
    settings = ui.current_settings()
    today = date.today()

    ui.section("Export your data", "Plain files you own, on your own disk.")
    columns = st.columns([0.3, 0.35, 0.35])
    with columns[0]:
        months = st.select_slider("Months to include", [1, 3, 6, 12, 24, 36, 60],
                                  value=12, key="export_months")
    with columns[1]:
        include_planned = st.checkbox("Include planned transactions", value=True,
                                      key="export_planned")
    with columns[2]:
        delimiter = st.selectbox("CSV delimiter", [",", ";", "\t"],
                                 format_func=lambda item: {"," : "comma",
                                                           ";": "semicolon",
                                                           "\t": "tab"}[item],
                                 key="export_delim")

    from calculations.periods import shift_period

    current = settings.current_period(today)
    start = shift_period(current, -(months - 1), settings.first_day_of_month)
    statuses = None if include_planned else [TxnStatus.COMPLETED.value]

    with ui.db_read() as session:
        rows = txs.list_transactions(session, txs.TxnFilter(
            start=start.start, end=current.end, statuses=statuses, order_desc=False))
        csv_text = csv_handler.transactions_to_csv(session, rows, delimiter=delimiter)

    st.caption(f"{len(rows)} transaction(s) from {start.label} to {current.label}.")
    columns = st.columns(2)
    with columns[0]:
        st.download_button(
            "⬇ Transactions as CSV", csv_text,
            file_name=f"transactions-{today.isoformat()}.csv",
            mime="text/csv", **ui.wide(),
        )
    with columns[1]:
        if st.button("📊 Build the full Excel workbook", **ui.wide(),
                     key="export_xlsx"):
            with st.spinner("Building the workbook…"):
                with ui.db_read() as session:
                    data = excel_handler.workbook_bytes(session, months=months,
                                                        today=today)
            st.session_state["_xlsx_bytes"] = data
    if st.session_state.get("_xlsx_bytes"):
        st.download_button(
            "⬇ Download the Excel workbook", st.session_state["_xlsx_bytes"],
            file_name=f"personal-finance-{today.isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            **ui.wide(),
        )
        st.caption("Nine sheets: transactions, budget, history, accounts, goals, debts, "
                   "net worth and forecast — with live SUM formulas.")


# ==========================================================================
# Recycle bin
# ==========================================================================
def _recycle_bin() -> None:
    fmt = ui.formatter()
    ui.section(
        "Recycle bin",
        "Deleting never destroys anything straight away. Restore what you need, or "
        "purge it permanently when you are sure.",
    )
    with ui.db_read() as session:
        deleted = txs.list_transactions(session, txs.TxnFilter(
            only_deleted=True, limit=200))
        from services.common import list_recycle_bin

        others = [entry for entry in list_recycle_bin(session, 60)
                  if entry.entity_type != "transaction"]

    if deleted:
        st.markdown(f"**{len(deleted)} deleted transaction(s)**")
        labels = {
            txn.id: f"{txn.txn_date.isoformat()} · {txn.description} · "
                    f"{fmt.money(txn.amount)}"
            for txn in deleted
        }
        chosen = st.multiselect("Restore which ones?", list(labels),
                                format_func=lambda item: labels[item],
                                key="restore_pick")
        columns = st.columns([0.3, 0.7])
        with columns[0]:
            if chosen and st.button(f"↩ Restore {len(chosen)}", type="primary",
                                    **ui.wide(), key="restore_go"):
                def action(session):
                    for txn_id in chosen:
                        txs.restore_transaction(session, txn_id)
                    return len(chosen)

                ui.run_action(action, success=f"{len(chosen)} transaction(s) restored.")
        with columns[1]:
            if ui.confirm_action(
                "🔥 Purge everything in the bin", "purge_bin",
                prompt=f"Permanently delete {len(deleted)} transaction(s)? "
                       "This cannot be undone.",
                confirm_label="Purge permanently", require_text="PURGE",
            ):
                def action(session):
                    return txs.purge_deleted(session)

                count = ui.run_action(action, rerun=False)
                if count is not None:
                    ui.flash(f"{count} transaction(s) permanently removed.", "warning")
                    st.rerun()
    else:
        st.success("✅ The recycle bin is empty.")

    if others:
        ui.divider()
        st.markdown(f"**{len(others)} other deleted record(s)**")
        ui.money_table(
            [{"type": entry.entity_type.replace("_", " ").title(),
              "label": entry.label, "when": entry.deleted_at}
             for entry in others],
            [("type", "Kind", "text"), ("label", "What", "text"),
             ("when", "Deleted", "date")],
            fmt, height=260,
        )
        st.caption("Categories, accounts, goals, debts and budget lines are snapshotted "
                   "here as JSON before deletion, so nothing is silently lost.")
