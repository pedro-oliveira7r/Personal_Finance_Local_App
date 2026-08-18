"""Settings — preferences, budgeting rules, backup, restore and data management."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import streamlit as st

import config
from calculations.money import ZERO, format_money, money
from constants import (
    AVAILABILITY_RULE_LABELS,
    DATE_FORMATS,
    MONTH_NAMES,
    SUPPORTED_CURRENCIES,
    AvailabilityRule,
    BudgetMethod,
)
from services import settings_service
from ui import components as ui


def render() -> None:
    ui.page_header(
        "Settings",
        "How the app behaves, and everything about where your data lives.",
        icon="⚙️",
    )
    tabs = st.tabs([
        "Preferences", "Budgeting rules", "Backup & restore", "Data", "Privacy & storage",
    ])
    with tabs[0]:
        _preferences()
    with tabs[1]:
        _budget_rules()
    with tabs[2]:
        _backup()
    with tabs[3]:
        _data()
    with tabs[4]:
        _privacy()


# ==========================================================================
def _preferences() -> None:
    settings = ui.current_settings()

    ui.section("Money and dates")
    columns = st.columns(4)
    with columns[0]:
        currency = st.selectbox(
            "Base currency", SUPPORTED_CURRENCIES,
            index=SUPPORTED_CURRENCIES.index(settings.base_currency)
            if settings.base_currency in SUPPORTED_CURRENCIES else 0,
            key="set_currency",
            help="Changes formatting everywhere. Existing amounts are not converted.")
    with columns[1]:
        date_format = st.selectbox(
            "Date format", list(DATE_FORMATS),
            index=list(DATE_FORMATS).index(settings.date_format)
            if settings.date_format in DATE_FORMATS else 0,
            key="set_dateformat")
    with columns[2]:
        show_cents = st.checkbox("Show cents", value=settings.show_cents,
                                 key="set_cents",
                                 help="Turn off for a cleaner look on large figures.")
    with columns[3]:
        theme_choice = st.selectbox(
            "Theme", ["auto", "light", "dark"],
            index=["auto", "light", "dark"].index(settings.theme)
            if settings.theme in ("auto", "light", "dark") else 0,
            key="set_theme",
            help="Charts follow this. 'auto' matches your system or Streamlit theme.")

    st.caption("Preview: " + ui.md(" · ".join([
        format_money(1234.5, currency, places=2 if show_cents else 0),
        format_money(-98765.43, currency, places=2 if show_cents else 0),
        format_money(1234567.89, currency, compact=True),
        date.today().strftime(DATE_FORMATS[date_format]),
    ])))

    ui.divider()
    ui.section("Period boundaries",
               "If you are paid on the 5th and budget from the 5th to the 4th, set the "
               "first day of the month accordingly — every calculation follows.")
    columns = st.columns(3)
    with columns[0]:
        first_day = st.number_input(
            "First day of the budgeting month", min_value=1, max_value=28,
            value=settings.first_day_of_month, step=1, key="set_firstday")
    with columns[1]:
        fiscal_month = st.selectbox(
            "Fiscal year starts in", list(range(1, 13)),
            index=settings.fiscal_year_start_month - 1,
            format_func=lambda item: MONTH_NAMES[item - 1], key="set_fiscal")
    with columns[2]:
        forecast_months = st.number_input(
            "Default forecast length (months)", min_value=1, max_value=120,
            value=settings.forecast_months, step=1, key="set_forecast")

    from calculations.periods import make_period

    example = make_period(date.today().year, date.today().month, int(first_day))
    st.caption(f"With that setting, the current period runs "
               f"**{example.start.isoformat()} → {example.end.isoformat()}** "
               f"({example.days} days).")

    if st.button("Save preferences", type="primary", key="save_prefs"):
        def action(session):
            return settings_service.update_settings(session, {
                "base_currency": currency, "date_format": date_format,
                "show_cents": show_cents, "theme": theme_choice,
                "first_day_of_month": int(first_day),
                "fiscal_year_start_month": int(fiscal_month),
                "forecast_months": int(forecast_months),
            })

        ui.invalidate_settings()
        ui.run_action(action, success="Preferences saved.")


# ==========================================================================
def _budget_rules() -> None:
    settings = ui.current_settings()

    ui.section(
        "When does income become spendable?",
        "The single most useful setting for real life. A salary earned on 31 January can "
        "fund January, February, or whichever period the deposit actually landed in — "
        "and a single payment can always override this from the Transactions screen.",
    )
    rules = list(AVAILABILITY_RULE_LABELS)
    availability = st.radio(
        "Availability rule", rules,
        index=rules.index(settings.income_availability_rule)
        if settings.income_availability_rule in rules else 0,
        format_func=lambda item: AVAILABILITY_RULE_LABELS[item],
        key="set_avail",
    )
    cutoff = settings.income_cutoff_day
    if availability == AvailabilityRule.CUTOFF_DAY.value:
        cutoff = st.number_input(
            "Cut-off day of the month", min_value=1, max_value=31,
            value=settings.income_cutoff_day, step=1, key="set_cutoff",
            help="Money arriving after this day funds the following period.")

    ui.divider()
    ui.section("Carry-over and method")
    columns = st.columns(3)
    with columns[0]:
        method = st.selectbox(
            "Budgeting method",
            [BudgetMethod.ZERO_BASED.value, BudgetMethod.CATEGORY_LIMITS.value],
            index=0 if settings.budget_method == BudgetMethod.ZERO_BASED.value else 1,
            format_func=lambda item: ("Zero-based (assign every unit)"
                                      if item == "zero_based"
                                      else "Category limits (spending caps)"),
            key="set_method")
    with columns[1]:
        carry = st.checkbox(
            "Carry leftover cash into the next period",
            value=settings.carry_over_surplus, key="set_carry",
            help="On: the cash you start a period with counts as available to budget "
                 "(minus anything earmarked for goals). Off: each period budgets only "
                 "its own income.")
    with columns[2]:
        st.caption(
            "Goal money held in a cash account is always subtracted before working out "
            "what is free to budget — otherwise your emergency fund would look "
            "spendable every single month."
        )

    ui.divider()
    ui.section("Warning thresholds",
               "When tracking should start nudging you about a category.")
    columns = st.columns(3)
    with columns[0]:
        warning = ui.pct_input("Warning at", settings.warning_threshold_pct,
                               key="set_warn", min_value=1.0, max_value=200.0,
                               help_text="Percentage of a category's budget consumed.")
    with columns[1]:
        critical = ui.pct_input("Over budget above", settings.critical_threshold_pct,
                                key="set_crit", min_value=1.0, max_value=300.0)
    with columns[2]:
        tolerance = ui.pct_input("Treat as on-plan within",
                                 settings.variance_tolerance_pct, key="set_tol",
                                 min_value=0.0, max_value=50.0,
                                 help_text="Small variances are noise, not a problem.")

    if critical < warning:
        st.error("The over-budget threshold has to be at or above the warning threshold.")
    elif st.button("Save budgeting rules", type="primary", key="save_rules"):
        def action(session):
            return settings_service.update_settings(session, {
                "income_availability_rule": availability,
                "income_cutoff_day": int(cutoff),
                "budget_method": method,
                "carry_over_surplus": carry,
                "warning_threshold_pct": warning,
                "critical_threshold_pct": critical,
                "variance_tolerance_pct": tolerance,
            })

        ui.invalidate_settings()
        ui.run_action(action, success="Budgeting rules saved.")


# ==========================================================================
def _backup() -> None:
    from import_export import backup

    fmt = ui.formatter()
    settings = ui.current_settings()

    ui.section(
        "Backups",
        "Two formats. A **SQLite snapshot** is an exact copy — restore that for a "
        "byte-for-byte return. A **JSON dump** is readable and survives future schema "
        "changes. The combined archive holds both.",
    )

    with ui.db_read() as session:
        directory = settings_service.resolve_backup_dir(session)
        files = backup.list_backups(session)
        json_data = backup.json_bytes(session)

    st.caption(f"Backup folder: `{directory}`")

    columns = st.columns(3)
    with columns[0]:
        if st.button("💾 Snapshot now (SQLite)", type="primary",
                     **ui.wide(), key="bk_db"):
            def action(session):
                return backup.create_sqlite_backup(target_dir=directory)

            path = ui.run_action(action, rerun=False)
            if path is not None:
                ui.flash(f"Backup written to `{path.name}`.")
                st.rerun()
    with columns[1]:
        if st.button("🗂 Combined archive (.zip)", **ui.wide(), key="bk_zip"):
            def action(session):
                return backup.create_zip_backup(session, target_dir=directory)

            path = ui.run_action(action, rerun=False)
            if path is not None:
                ui.flash(f"Archive written to `{path.name}`.")
                st.rerun()
    with columns[2]:
        st.download_button("⬇ Download a JSON dump", json_data,
                           file_name=f"finance-{date.today().isoformat()}.json",
                           mime="application/json", **ui.wide())

    new_dir = st.text_input("Change the backup folder", value=str(directory),
                            key="bk_dir",
                            help="Point this at a synced folder if you want off-machine "
                                 "copies — that is your choice, not a default.")
    if new_dir.strip() and new_dir.strip() != str(directory):
        if st.button("Use that folder", key="bk_dir_save"):
            ui.run_action(
                lambda session: settings_service.update_settings(
                    session, {"backup_dir": new_dir.strip()}),
                success="Backup folder updated.",
            )

    if files:
        ui.divider()
        ui.section(f"{len(files)} backup(s) on disk")
        ui.money_table(
            [{"name": item.name, "kind": item.kind.upper(),
              "size": item.size_label, "when": item.modified}
             for item in files],
            [("name", "File", "text"), ("kind", "Format", "text"),
             ("size", "Size", "text"), ("when", "Created", "date")],
            fmt, height=min(320, 60 + 36 * len(files)),
        )
        columns = st.columns([0.5, 0.5])
        with columns[0]:
            options = [(index, item.name) for index, item in enumerate(files)]
            chosen = ui.select_with_none("Restore from", options,
                                        none_label="— pick a backup —",
                                        key="bk_restore_pick")
        with columns[1]:
            st.write("")
            if chosen is not None:
                item = files[chosen]
                if ui.confirm_action(
                    f"♻️ Restore {item.name}", "bk_restore",
                    prompt=f"Replace your current data with **{item.name}** "
                           f"({item.size_label}, {item.modified:%Y-%m-%d %H:%M})? "
                           "Your current database is copied aside first, so this is "
                           "itself reversible.",
                    confirm_label="Restore it", require_text="RESTORE",
                    **ui.wide(),
                ):
                    _do_restore(item.path)

        if len(files) > 10:
            if ui.confirm_action(
                "🧹 Keep only the 10 newest", "bk_prune",
                prompt=f"Delete {len(files) - 10} older backup file(s) permanently?",
                confirm_label="Prune them",
            ):
                removed = backup.prune_backups(10, target_dir=directory)
                ui.flash(f"{removed} old backup(s) removed.", "warning")
                st.rerun()

    ui.divider()
    ui.section("Restore from a file you have")
    uploaded = st.file_uploader("Backup file (.db, .json or .zip)",
                                type=["db", "sqlite", "json", "zip"], key="bk_upload")
    if uploaded is not None:
        st.warning(
            f"**{uploaded.name}** will replace everything currently in the app. Your "
            "present database is snapshotted first.", icon="⚠️",
        )
        if ui.confirm_action(
            "♻️ Restore from this file", "bk_upload_restore",
            prompt="Are you sure? Everything currently stored will be replaced.",
            confirm_label="Restore now", require_text="RESTORE",
        ):
            try:
                report = backup.restore_from_upload(uploaded.name, uploaded.getvalue())
            except Exception as exc:
                st.error(f"Restore failed: {exc}")
            else:
                _after_restore(report)


def _do_restore(path: Path) -> None:
    from import_export import backup

    try:
        if path.suffix.lower() == ".json":
            report = backup.restore_json(path.read_bytes())
        elif path.suffix.lower() == ".zip":
            report = backup.restore_zip(path)
        else:
            report = backup.restore_sqlite(path)
    except Exception as exc:
        st.error(f"Restore failed: {exc}")
        return
    _after_restore(report)


def _after_restore(report) -> None:
    st.session_state.pop("_bootstrapped", None)
    ui.invalidate_settings()
    message = report.summary()
    if report.previous_saved_to:
        message += f" Your previous data was saved to `{report.previous_saved_to}`."
    ui.flash(message)
    st.rerun()


# ==========================================================================
def _data() -> None:
    from database.database import database_stats, vacuum

    fmt = ui.formatter()
    ui.section("What is in your database")
    stats = database_stats()
    size_mb = stats["size_bytes"] / (1024 * 1024)

    interesting = ["transactions", "budget_lines", "budget_periods", "recurring_rules",
                   "categories", "accounts", "goals", "debts",
                   "net_worth_snapshots", "recycle_bin"]
    tables = stats["tables"]
    ui.kpi_row([
        ui.Kpi("Transactions", f"{tables.get('transactions') or 0:,}".replace(",", "."),
               icon="💳"),
        ui.Kpi("Budget lines", str(tables.get("budget_lines") or 0), icon="🧮"),
        ui.Kpi("Recurring rules", str(tables.get("recurring_rules") or 0), icon="🔁"),
        ui.Kpi("Categories", str(tables.get("categories") or 0), icon="🏷️"),
        ui.Kpi("Database size", f"{size_mb:.2f} MB", icon="🗄️"),
    ])
    with st.expander("Every table"):
        st.dataframe(
            [{"Table": name, "Rows": count} for name, count in sorted(tables.items())],
            **ui.wide(), hide_index=True,
        )

    ui.divider()
    ui.section("Demo data",
               "Realistic sample figures generated from the same engine the app uses. "
               "Loading it never overwrites anything you already entered.")
    with ui.db_read() as session:
        from demo import demo_data

        has_demo = demo_data.has_demo_data(session)
    columns = st.columns(2)
    with columns[0]:
        if has_demo:
            st.info("Demo data is present in this database.", icon="🧪")
        elif st.button("🧪 Load the demo dataset", **ui.wide(),
                       key="load_demo"):
            def action(session):
                from demo.demo_data import load_demo_data

                return load_demo_data(session)

            report = ui.run_action(action, rerun=False, spinner="Generating 18 months…")
            if report is not None:
                ui.flash("Demo data loaded: " + report.summary())
                st.rerun()
    with columns[1]:
        st.caption("18 months of history plus 6 months of plan: salary with a January "
                   "rise, seasonal electricity, annual insurance, quarterly tax, a "
                   "July holiday, four goals and two debts.")

    ui.divider()
    ui.section("Maintenance")
    columns = st.columns(3)
    with columns[0]:
        if st.button("🧹 Compact the database (VACUUM)", **ui.wide(),
                     key="do_vacuum"):
            vacuum()
            ui.flash("Database compacted.")
            st.rerun()
    with columns[1]:
        if st.button("🔁 Regenerate planned transactions", **ui.wide(),
                     key="regen_planned"):
            from services import recurring_service

            def action(session):
                return recurring_service.generate_planned(session, horizon_months=12)

            report = ui.run_action(action, rerun=False)
            if report is not None:
                ui.flash(report.summary())
                st.rerun()
    with columns[2]:
        if st.button("✅ Close achieved goals", **ui.wide(),
                     key="close_goals"):
            from services import goal_service

            def action(session):
                return goal_service.auto_close_achieved(session)

            changed = ui.run_action(action, rerun=False)
            if changed is not None:
                ui.flash(f"{len(changed)} goal(s) marked as achieved."
                         if changed else "No goals are fully funded yet.")
                st.rerun()

    ui.divider()
    ui.section("Danger zone")
    st.warning(
        "These actions are permanent. Take a backup first — the button is one tab to "
        "the left.", icon="⚠️",
    )
    columns = st.columns(2)
    with columns[0]:
        if ui.confirm_action(
            "🗑 Clear all financial data", "clear_data",
            prompt="Delete every transaction, budget, rule, goal, debt and snapshot? "
                   "Your accounts and categories are kept so you can start clean "
                   "without rebuilding your setup. This cannot be undone.",
            confirm_label="Clear it all", require_text="CLEAR",
            **ui.wide(),
        ):
            def action(session):
                from demo.demo_data import clear_all_data

                return clear_all_data(session, keep_accounts=True, keep_categories=True)

            counts = ui.run_action(action, rerun=False,
                                   spinner="Clearing…")
            if counts is not None:
                total = sum(counts.values())
                ui.flash(f"{total} record(s) removed. Accounts and categories kept.",
                         "warning")
                st.rerun()
    with columns[1]:
        if ui.confirm_action(
            "💣 Reset everything to factory defaults", "reset_all",
            prompt="Delete all data **and** your accounts and categories, then restore "
                   "the default category tree. This is a complete reset and cannot be "
                   "undone.",
            confirm_label="Reset everything", require_text="RESET",
            **ui.wide(),
        ):
            def action(session):
                from demo.demo_data import clear_all_data

                return clear_all_data(session, keep_accounts=False,
                                      keep_categories=False)

            counts = ui.run_action(action, rerun=False, spinner="Resetting…")
            if counts is not None:
                st.session_state.pop("_bootstrapped", None)
                ui.flash("Everything reset. Default categories restored.", "warning")
                st.rerun()


# ==========================================================================
def _privacy() -> None:
    ui.section("Where your data lives")
    st.markdown(
        f"""
Your financial data is in a single SQLite file on this computer:

```
{config.db_path()}
```

Supporting folders:

- backups — `{config.BACKUP_DIR}`
- exports — `{config.EXPORT_DIR}`

Move the whole lot by setting the `PFA_DATA_DIR` environment variable before
launching, or point just the backups elsewhere from the Backup tab.
        """
    )

    ui.divider()
    ui.section("What this app does not do")
    st.markdown(
        """
- **No network calls with your data.** No sync service, no cloud account, no API keys.
- **No analytics or telemetry.** Nothing is measured, counted or phoned home.
- **No credentials stored.** The app never asks for a bank login, because it never
  connects to one — you bring data in by CSV.
- **No third-party scripts.** Charts are rendered by the Plotly library bundled with
  the app.

Imported CSV data is validated and sanitised before it reaches the database, and
every write goes through a validation layer that rejects impossible values.
        """
    )

    ui.divider()
    ui.section("Sensible precautions")
    st.markdown(
        """
- Take a backup before big changes; restores keep the previous database aside anyway.
- If you use a synced folder for backups, be aware the sync provider then holds a copy.
- The SQLite file is not encrypted. If the machine is shared, rely on your operating
  system's user accounts and full-disk encryption.
        """
    )

    ui.divider()
    ui.section("Version")
    st.caption(f"{config.APP_NAME} v{config.APP_VERSION} · Python "
               f"{__import__('sys').version.split()[0]} · Streamlit "
               f"{st.__version__}")
