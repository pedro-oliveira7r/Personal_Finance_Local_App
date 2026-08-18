"""Budget tracking — how the plan is holding up, and quick completion of planned items."""

from __future__ import annotations

from datetime import date

import streamlit as st

from calculations.money import ZERO, money, money_sum
from calculations.variance import pace_projection
from charts import dashboard_charts as dc
from constants import TxnStatus
from services import budget_service, transaction_service
from ui import components as ui


def render() -> None:
    fmt = ui.formatter()
    theme = ui.theme()
    today = date.today()

    ui.page_header(
        "Budget tracking",
        "Plan against reality, line by line — plus a fast way to tick off what has "
        "actually happened.",
        icon="🎯",
    )
    period = ui.period_picker(key="track_period")

    with ui.db_read() as session:
        tracking = budget_service.track_period(session, period, today=today)
        planned = transaction_service.list_transactions(
            session,
            transaction_service.TxnFilter(
                start=period.start, end=period.end,
                statuses=[TxnStatus.PLANNED.value], order_desc=False,
            ),
        )
        overdue = transaction_service.overdue_planned(session, today)

    if not tracking.rows:
        ui.empty_state(
            f"No budget lines for {period.label}",
            "Tracking compares a plan with reality, so it needs a plan first. Build one "
            "from your recurring rules and come back — it takes a few seconds.",
            icon="🎯", action_label="Go to Budget planning",
            action=lambda: _goto("budget"),
        )
        return

    elapsed_pct = int(tracking.elapsed_fraction * 100)
    _summary(tracking, fmt, elapsed_pct)
    ui.divider()

    tabs = st.tabs(["Line by line", "Charts", "Mark things done", "Pace check"])
    with tabs[0]:
        _lines(tracking, fmt)
    with tabs[1]:
        _charts(tracking, theme)
    with tabs[2]:
        _complete_planned(planned, overdue, fmt, period)
    with tabs[3]:
        _pace(tracking, fmt, elapsed_pct)


def _goto(slug: str) -> None:
    st.session_state["_nav_page"] = slug
    st.rerun()


def _summary(tracking, fmt: ui.Formatter, elapsed_pct: int) -> None:
    allocations = [row for row in tracking.rows if not row.is_income]
    planned_out = money_sum(row.planned for row in allocations)
    actual_out = money_sum(row.actual for row in allocations)

    ui.kpi_row([
        ui.Kpi("Income", fmt.money(tracking.income.actual), icon="📥",
               delta=f"{fmt.signed_money(tracking.income.variance)} vs plan",
               delta_good=tracking.income.variance >= 0,
               help_text=f"Planned {fmt.money(tracking.income.planned)}"),
        ui.Kpi("Total outflow", fmt.money(actual_out), icon="📤",
               delta=f"{fmt.signed_money(money(actual_out - planned_out))} vs plan",
               delta_good=actual_out <= planned_out,
               help_text=f"Planned {fmt.money(planned_out)}"),
        ui.Kpi("Net (planned)", fmt.signed_money(tracking.net_planned), icon="🧮"),
        ui.Kpi("Net (actual so far)", fmt.signed_money(tracking.net_actual), icon="📊"),
        ui.Kpi("Period elapsed", f"{elapsed_pct}%", icon="⏳",
               help_text="Compare this with how much of the budget is used — if "
                         "spending is ahead of the calendar, you are on pace to overrun."),
    ])

    pills = []
    if tracking.expenses.over_count:
        pills.append((f"{tracking.expenses.over_count} category(ies) over budget",
                      "critical"))
    if tracking.unbudgeted:
        pills.append((f"{len(tracking.unbudgeted)} unbudgeted category(ies)", "warning"))
    if tracking.uncategorised_total:
        pills.append((f"{fmt.money(tracking.uncategorised_total)} uncategorised",
                      "warning"))
    if not pills:
        pills.append(("Everything inside its plan", "success"))
    ui.status_pills(pills)


def _lines(tracking, fmt: ui.Formatter) -> None:
    filters = st.columns([0.3, 0.3, 0.4])
    with filters[0]:
        kinds = st.multiselect(
            "Type", ["income", "expense", "savings", "investment", "debt"],
            default=[], format_func=str.title, key="track_kinds",
            placeholder="All types",
        )
    with filters[1]:
        statuses = st.multiselect(
            "Status", ["ok", "warning", "over", "short", "unbudgeted", "unused", "none"],
            default=[], key="track_status", placeholder="All statuses",
            format_func=lambda item: item.replace("_", " ").title(),
        )
    with filters[2]:
        search = st.text_input("Find a line", placeholder="Type part of a category name",
                              key="track_search")

    rows = tracking.rows
    if kinds:
        rows = [row for row in rows if row.kind in kinds]
    if statuses:
        rows = [row for row in rows if row.status in statuses]
    if search.strip():
        needle = search.strip().lower()
        rows = [row for row in rows if needle in row.label.lower()]

    rows = sorted(rows, key=lambda row: (row.kind != "income", -abs(row.actual)))
    st.caption(f"{len(rows)} line(s). Variance is Actual − Planned; ✓ means favourable, "
               "▲ means it went the wrong way.")
    ui.variance_table(rows, fmt, height=min(620, 60 + 36 * max(1, len(rows))))

    csv = _rows_to_csv(rows, fmt)
    st.download_button("⬇ Download this table as CSV", csv,
                       file_name=f"tracking-{tracking.period.key}.csv", mime="text/csv")


def _rows_to_csv(rows, fmt: ui.Formatter) -> str:
    from import_export.csv_handler import rows_to_csv

    return rows_to_csv([
        {"line": row.label, "type": row.kind, "planned": row.planned,
         "actual": row.actual, "variance": row.variance,
         "variance_pct": row.variance_pct, "consumed_pct": row.consumed_pct,
         "status": row.status_label}
        for row in rows
    ])


def _charts(tracking, theme) -> None:
    left, right = st.columns(2)
    with left:
        ui.section("Planned against actual")
        ui.chart(dc.planned_vs_actual_bars(tracking.rows, theme, height=440, limit=14),
                 key="track_pva")
    with right:
        ui.section("Budget consumed")
        ui.chart(dc.utilisation_bullets(tracking.allocation_rows, theme, limit=12),
                 key="track_util")
    ui.divider()
    ui.section("Distance from plan")
    ui.chart(dc.variance_diverging_bars(tracking.allocation_rows, theme, height=360,
                                        limit=14),
             key="track_var")


def _complete_planned(planned, overdue, fmt: ui.Formatter, period) -> None:
    ui.section(
        "Planned transactions in this period",
        "Tick what has actually happened. Completing a line feeds straight into the "
        "actual column — you can also correct the amount if it came out different.",
    )
    if overdue:
        st.warning(
            f"⚠️ {len(overdue)} planned transaction(s) from earlier periods are still "
            f"open ({fmt.money(money_sum(t.amount for t in overdue))}).",
        )

    if not planned:
        st.success("✅ Nothing planned is still open in this period.")
        return

    rows = [
        {
            "id": txn.id,
            "Date": txn.txn_date,
            "Description": txn.description,
            "Type": txn.kind.title(),
            "Amount": float(txn.amount),
            "Done": False,
        }
        for txn in planned
    ]
    edited = st.data_editor(
        rows, key=f"complete_{period.key}", **ui.wide(), hide_index=True,
        column_order=["Date", "Description", "Type", "Amount", "Done"],
        column_config={
            "id": None,
            "Date": st.column_config.DateColumn(disabled=True),
            "Description": st.column_config.TextColumn(disabled=True, width="large"),
            "Type": st.column_config.TextColumn(disabled=True, width="small"),
            "Amount": st.column_config.NumberColumn(
                format="%.2f", min_value=0.0, step=10.0,
                help="Change it if the real amount differed."),
            "Done": st.column_config.CheckboxColumn(help="Tick, then press Confirm."),
        },
    )
    records = edited if isinstance(edited, list) else edited.to_dict("records")
    selected = [record for record in records if record.get("Done")]
    by_id = {txn.id: txn for txn in planned}

    left, right = st.columns([0.32, 0.68])
    with left:
        actual_date = st.date_input("Completed on", value=date.today(),
                                    key="complete_date")
    with right:
        st.caption(f"{len(selected)} selected. "
                   f"{fmt.money(money_sum(money(r['Amount']) for r in selected))} total.")
        if selected and st.button(f"Confirm {len(selected)} transaction(s)",
                                  type="primary", key="complete_go"):
            def action(session):
                count = 0
                for record in selected:
                    txn = by_id.get(record["id"])
                    if txn is None:
                        continue
                    new_amount = money(record["Amount"])
                    transaction_service.complete_transaction(
                        session, txn.id, actual_date=actual_date,
                        actual_amount=new_amount if new_amount != txn.amount else None,
                    )
                    count += 1
                return count

            count = ui.run_action(action, rerun=False)
            if count:
                ui.flash(f"{count} transaction(s) marked as completed.")
                st.rerun()


def _pace(tracking, fmt: ui.Formatter, elapsed_pct: int) -> None:
    ui.section(
        "Are you on pace?",
        "For each budgeted category, where the current rate of spending would land by "
        "the end of the period. Only meaningful part-way through a period.",
    )
    if tracking.elapsed_fraction <= 0:
        st.info("This period has not started yet.", icon="🗓️")
        return
    if tracking.elapsed_fraction >= 1:
        st.info("This period is complete — the actual column is the final answer.",
                icon="✅")

    rows = []
    for row in tracking.allocation_rows:
        if row.planned <= 0:
            continue
        projected = pace_projection(row, tracking.elapsed_fraction)
        overrun = money(projected - row.planned)
        rows.append({
            "label": f"{row.status_icon} {row.label}",
            "planned": row.planned,
            "actual": row.actual,
            "projected": projected,
            "overrun": overrun,
            "verdict": "on pace" if overrun <= 0 else "heading over",
        })
    rows.sort(key=lambda item: item["overrun"], reverse=True)

    if not rows:
        st.caption("No budgeted categories to project.")
        return
    st.caption(f"{elapsed_pct}% of the period has passed.")
    ui.money_table(
        rows,
        [("label", "Category", "text"), ("planned", "Planned", "money"),
         ("actual", "So far", "money"), ("projected", "On pace for", "money"),
         ("overrun", "Would exceed by", "money"), ("verdict", "Verdict", "text")],
        fmt, height=min(560, 60 + 36 * len(rows)),
    )
