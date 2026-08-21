"""End-to-end integrity: budget generation, tracking, import/export, backup, edge cases."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from calculations.periods import make_period, shift_period
from constants import CategoryKind, Frequency, PeriodStatus, TxnKind, TxnStatus
from services import (
    account_service,
    budget_service,
    category_service,
    debt_service,
    goal_service,
    recurring_service,
    reporting_service,
    settings_service,
)
from services import transaction_service as txs
from database.models import Account
from services.common import ConflictError


# ==========================================================================
# Budget generation from rules
# ==========================================================================
@pytest.fixture()
def rules(session, accounts, categories):
    """Salary with a January rise, rent, seasonal power, annual insurance."""
    recurring_service.create_rule(session, {
        "name": "Salary", "kind": TxnKind.INCOME.value, "amount": "5000",
        "frequency": Frequency.MONTHLY.value, "day_of_month": 5,
        "start_date": date(2026, 1, 5), "growth_pct": "5",
        "growth_anchor_month": 1, "category_id": categories["salary"].id,
        "account_id": accounts["Checking"].id,
    })
    recurring_service.create_rule(session, {
        "name": "Rent", "kind": TxnKind.EXPENSE.value, "amount": "1500",
        "frequency": Frequency.MONTHLY.value, "day_of_month": 10,
        "start_date": date(2026, 1, 10), "category_id": categories["rent"].id,
        "account_id": accounts["Checking"].id,
    })
    power = category_service.resolve_path(session, "Utilities › Electricity",
                                          kind="expense")
    recurring_service.create_rule(session, {
        "name": "Electricity", "kind": TxnKind.EXPENSE.value, "amount": "200",
        "frequency": Frequency.MONTHLY.value, "day_of_month": 14,
        "start_date": date(2026, 1, 14), "category_id": power.id,
        "account_id": accounts["Checking"].id,
        "seasonal_factors": {"1": 1.5, "7": 0.5},
    })
    insurance = category_service.resolve_path(
        session, "Transportation › Vehicle insurance", kind="expense")
    recurring_service.create_rule(session, {
        "name": "Car insurance", "kind": TxnKind.EXPENSE.value, "amount": "2400",
        "frequency": Frequency.ANNUAL.value, "month_of_year": 3, "day_of_month": 15,
        "start_date": date(2026, 1, 1), "category_id": insurance.id,
        "account_id": accounts["Checking"].id,
    })
    session.commit()
    return True


def test_annual_expense_only_lands_in_its_own_month(session, rules):
    march = budget_service.generate_from_rules(session, make_period(2026, 3))
    april = budget_service.generate_from_rules(session, make_period(2026, 4))
    session.commit()
    assert march.created > april.created

    march_summary = budget_service.summarise_period(session, make_period(2026, 3))
    april_summary = budget_service.summarise_period(session, make_period(2026, 4))
    march_labels = {line.display_label for line in march_summary.allocation_lines}
    april_labels = {line.display_label for line in april_summary.allocation_lines}
    assert any("insurance" in label.lower() for label in march_labels)
    assert not any("insurance" in label.lower() for label in april_labels)


def test_seasonal_amounts_differ_by_month(session, rules):
    for month in (1, 7):
        budget_service.generate_from_rules(session, make_period(2026, month))
    session.commit()

    def power_amount(month: int) -> Decimal:
        summary = budget_service.summarise_period(session, make_period(2026, month))
        line = next(item for item in summary.allocation_lines
                    if "Electricity" in item.display_label)
        return line.planned_amount

    assert power_amount(1) == Decimal("300.00")
    assert power_amount(7) == Decimal("100.00")


def test_salary_growth_shows_up_the_following_january(session, rules):
    for key in ((2026, 12), (2027, 1)):
        budget_service.generate_from_rules(session, make_period(*key))
    session.commit()

    def income(year: int, month: int) -> Decimal:
        summary = budget_service.summarise_period(session, make_period(year, month))
        return summary.result.planned_income

    assert income(2026, 12) == Decimal("5000.00")
    assert income(2027, 1) == Decimal("5250.00")


def test_manual_override_survives_regeneration(session, rules):
    period = make_period(2026, 5)
    budget_service.generate_from_rules(session, period)
    session.commit()

    summary = budget_service.summarise_period(session, period)
    rent_line = next(item for item in summary.allocation_lines
                     if "Rent" in item.display_label)
    budget_service.upsert_line(session, 2026, 5, {
        "kind": rent_line.kind, "target": rent_line.target,
        "category_id": rent_line.category_id, "planned_amount": "1750",
    })
    session.commit()

    report = budget_service.generate_from_rules(session, period)
    session.commit()
    assert report.skipped_overrides >= 1

    summary = budget_service.summarise_period(session, period)
    rent_line = next(item for item in summary.allocation_lines
                     if "Rent" in item.display_label)
    assert rent_line.planned_amount == Decimal("1750.00")


def test_forced_overwrite_replaces_an_override(session, rules):
    period = make_period(2026, 5)
    budget_service.generate_from_rules(session, period)
    session.commit()
    summary = budget_service.summarise_period(session, period)
    rent_line = next(item for item in summary.allocation_lines
                     if "Rent" in item.display_label)
    budget_service.upsert_line(session, 2026, 5, {
        "kind": rent_line.kind, "target": rent_line.target,
        "category_id": rent_line.category_id, "planned_amount": "9999",
    })
    session.commit()

    budget_service.generate_from_rules(session, period, overwrite_overrides=True)
    session.commit()
    summary = budget_service.summarise_period(session, period)
    rent_line = next(item for item in summary.allocation_lines
                     if "Rent" in item.display_label)
    assert rent_line.planned_amount == Decimal("1500.00")


def test_upserting_the_same_category_twice_updates_rather_than_duplicating(
        session, categories):
    for amount in ("500", "600"):
        budget_service.upsert_line(session, 2026, 8, {
            "kind": CategoryKind.EXPENSE.value,
            "category_id": categories["groceries"].id,
            "planned_amount": amount,
        })
    session.commit()
    summary = budget_service.summarise_period(session, make_period(2026, 8))
    grocery_lines = [item for item in summary.allocation_lines
                     if item.category_id == categories["groceries"].id]
    assert len(grocery_lines) == 1
    assert grocery_lines[0].planned_amount == Decimal("600.00")


def test_copying_a_period_with_growth(session, rules):
    source = make_period(2026, 2)
    budget_service.generate_from_rules(session, source)
    session.commit()
    target = make_period(2026, 9)
    budget_service.copy_period(session, source, target, growth_pct=Decimal("10"))
    session.commit()

    before = budget_service.summarise_period(session, source)
    after = budget_service.summarise_period(session, target)
    assert after.result.allocated == (before.result.allocated * Decimal("1.1")
                                      ).quantize(Decimal("0.01"))


def test_closed_periods_reject_edits(session, categories):
    budget_service.upsert_line(session, 2026, 8, {
        "kind": CategoryKind.EXPENSE.value,
        "category_id": categories["groceries"].id, "planned_amount": "100",
    })
    budget_service.set_period_status(session, 2026, 8, PeriodStatus.CLOSED.value)
    session.commit()

    with pytest.raises(ConflictError):
        budget_service.upsert_line(session, 2026, 8, {
            "kind": CategoryKind.EXPENSE.value,
            "category_id": categories["groceries"].id, "planned_amount": "200",
        })


def test_locked_lines_are_never_regenerated(session, rules):
    period = make_period(2026, 6)
    budget_service.generate_from_rules(session, period)
    session.commit()
    summary = budget_service.summarise_period(session, period)
    line = summary.allocation_lines[0]
    budget_service.set_line_lock(session, line.id, True)
    session.commit()

    report = budget_service.generate_from_rules(session, period,
                                                overwrite_overrides=False)
    session.commit()
    assert report.skipped_overrides >= 1


# ==========================================================================
# Tracking
# ==========================================================================
def test_tracking_matches_plan_against_actuals(session, accounts, categories, rules):
    period = make_period(2026, 8)
    budget_service.generate_from_rules(session, period)
    session.commit()

    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 10), "description": "Rent August",
        "amount": "1500", "kind": TxnKind.EXPENSE.value,
        "account_id": accounts["Checking"].id, "category_id": categories["rent"].id,
    })
    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 5), "description": "Salary August",
        "amount": "4800", "kind": TxnKind.INCOME.value,
        "account_id": accounts["Checking"].id, "category_id": categories["salary"].id,
    })
    session.commit()

    tracking = budget_service.track_period(session, period, today=date(2026, 8, 31))
    rent_row = next(row for row in tracking.rows if "Rent" in row.label)
    assert rent_row.planned == Decimal("1500.00")
    assert rent_row.actual == Decimal("1500.00")
    assert rent_row.variance == Decimal("0.00")
    assert rent_row.status == "warning"  # exactly at 100% of the plan

    income_row = next(row for row in tracking.rows if row.kind == "income")
    assert income_row.actual == Decimal("4800.00")
    assert income_row.favorable is False


def test_unbudgeted_spending_is_surfaced(session, accounts, categories):
    period = make_period(2026, 8)
    budget_service.upsert_line(session, 2026, 8, {
        "kind": CategoryKind.EXPENSE.value, "category_id": categories["rent"].id,
        "planned_amount": "1500",
    })
    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 12), "description": "Surprise groceries",
        "amount": "320", "kind": TxnKind.EXPENSE.value,
        "account_id": accounts["Checking"].id,
        "category_id": categories["groceries"].id,
    })
    session.commit()

    tracking = budget_service.track_period(session, period, today=date(2026, 8, 31))
    assert len(tracking.unbudgeted) == 1
    assert tracking.unbudgeted[0].actual == Decimal("320.00")
    assert tracking.unbudgeted[0].status == "unbudgeted"


def test_zero_income_period_is_handled(session, accounts, categories):
    period = make_period(2026, 9)
    budget_service.upsert_line(session, 2026, 9, {
        "kind": CategoryKind.EXPENSE.value, "category_id": categories["rent"].id,
        "planned_amount": "1500",
    })
    session.commit()
    summary = budget_service.summarise_period(session, period, today=date(2026, 9, 1))
    assert summary.result.planned_income == Decimal("0.00")
    codes = {warning.code for warning in summary.result.warnings}
    assert "no_income" in codes


def test_carry_in_excludes_money_earmarked_for_goals(session, accounts):
    goal = goal_service.create_goal(session, {
        "name": "Emergency", "target_amount": "10000", "starting_amount": "400",
        "account_id": accounts["Savings"].id, "start_date": date(2026, 1, 1),
    })
    session.commit()
    period = make_period(2026, 8)
    carry = budget_service.carry_in_for(session, period, today=date(2026, 8, 1))
    # 1600 cash across the fixture accounts, less the 400 already earmarked.
    assert carry == Decimal("1200.00")


def test_opening_cash_override_wins(session, accounts):
    budget_service.set_opening_override(session, 2026, 8, Decimal("777"))
    session.commit()
    assert budget_service.carry_in_for(session, make_period(2026, 8)) == \
        Decimal("777.00")
    budget_service.set_opening_override(session, 2026, 8, None)
    session.commit()
    assert budget_service.carry_in_for(session, make_period(2026, 8)) != \
        Decimal("777.00")


def test_carry_over_can_be_switched_off(session, accounts):
    settings_service.update_settings(session, {"carry_over_surplus": False})
    session.commit()
    assert budget_service.carry_in_for(session, make_period(2026, 8)) == Decimal("0.00")


# ==========================================================================
# Recurrence generation against the database
# ==========================================================================
def test_generation_is_idempotent(session, accounts, categories, rules):
    first = recurring_service.generate_planned(
        session, horizon_months=6, today=date(2026, 8, 1))
    session.commit()
    assert first.created > 0

    second = recurring_service.generate_planned(
        session, horizon_months=6, today=date(2026, 8, 1))
    session.commit()
    assert second.created == 0
    assert second.updated == 0


def test_generation_never_touches_completed_occurrences(session, accounts,
                                                       categories, rules):
    recurring_service.generate_planned(session, horizon_months=3,
                                       today=date(2026, 8, 1))
    session.commit()
    planned = txs.list_transactions(session, txs.TxnFilter(
        statuses=[TxnStatus.PLANNED.value], order_desc=False))
    target = planned[0]
    txs.complete_transaction(session, target.id, actual_amount=Decimal("4321"))
    session.commit()

    report = recurring_service.generate_planned(session, horizon_months=3,
                                                today=date(2026, 8, 1))
    session.commit()
    assert report.skipped_completed >= 1
    assert txs.get_transaction(session, target.id).amount == Decimal("4321.00")


def test_deleting_an_occurrence_is_respected_on_regeneration(session, accounts,
                                                            categories, rules):
    recurring_service.generate_planned(session, horizon_months=2,
                                       today=date(2026, 8, 1))
    session.commit()
    planned = txs.list_transactions(session, txs.TxnFilter(
        statuses=[TxnStatus.PLANNED.value], order_desc=False))
    txs.delete_transaction(session, planned[0].id)
    session.commit()

    recurring_service.generate_planned(session, horizon_months=2,
                                       today=date(2026, 8, 1))
    session.commit()
    assert txs.get_transaction(session, planned[0].id).deleted_at is not None


def test_backfill_creates_past_occurrences(session, accounts, categories, rules):
    forward_only = recurring_service.generate_planned(
        session, horizon_months=1, today=date(2026, 8, 1))
    session.commit()
    assert forward_only.created > 0
    earliest = min(row.txn_date for row in txs.list_transactions(
        session, txs.TxnFilter(order_desc=False)))
    assert earliest >= date(2026, 8, 1)

    with_backfill = recurring_service.generate_planned(
        session, horizon_months=1, today=date(2026, 8, 1), backfill=True)
    session.commit()
    assert with_backfill.created > 0
    earliest = min(row.txn_date for row in txs.list_transactions(
        session, txs.TxnFilter(order_desc=False)))
    assert earliest < date(2026, 8, 1)


def test_deleting_a_rule_removes_only_its_planned_transactions(session, accounts,
                                                              categories, rules):
    recurring_service.generate_planned(session, horizon_months=3,
                                       today=date(2026, 8, 1))
    session.commit()
    rule = next(item for item in recurring_service.list_rules(session)
                if item.name == "Rent")
    planned = txs.list_transactions(session, txs.TxnFilter(
        rule_id=rule.id, statuses=[TxnStatus.PLANNED.value], order_desc=False))
    txs.complete_transaction(session, planned[0].id)
    session.commit()

    result = recurring_service.delete_rule(session, rule.id)
    session.commit()
    assert result["planned_removed"] == len(planned) - 1
    assert txs.get_transaction(session, planned[0].id).deleted_at is None


# ==========================================================================
# Date edge cases through the whole stack
# ==========================================================================
def test_leap_day_transaction_lands_in_february(session, accounts, categories):
    txs.create_transaction(session, {
        "txn_date": date(2024, 2, 29), "description": "Leap day shop",
        "amount": "99", "kind": TxnKind.EXPENSE.value,
        "account_id": accounts["Checking"].id,
        "category_id": categories["groceries"].id,
    })
    session.commit()
    actuals = txs.actuals_for_period(session, make_period(2024, 2))
    assert actuals.expense_total == Decimal("99.00")


def test_transaction_paid_after_the_period_ends_counts_in_the_next_one(
        session, accounts, categories):
    txs.create_transaction(session, {
        "txn_date": date(2026, 7, 31), "description": "Late settlement",
        "amount": "500", "kind": TxnKind.EXPENSE.value,
        "actual_date": date(2026, 8, 2),
        "account_id": accounts["Checking"].id,
        "category_id": categories["groceries"].id,
    })
    session.commit()
    assert txs.actuals_for_period(session, make_period(2026, 7)).expense_total == \
        Decimal("0.00")
    assert txs.actuals_for_period(session, make_period(2026, 8)).expense_total == \
        Decimal("500.00")


def test_custom_first_day_moves_period_boundaries(session, accounts, categories):
    settings_service.update_settings(session, {"first_day_of_month": 5})
    session.commit()
    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 3), "description": "Before the boundary",
        "amount": "120", "kind": TxnKind.EXPENSE.value,
        "account_id": accounts["Checking"].id,
        "category_id": categories["groceries"].id,
    })
    session.commit()

    settings = settings_service.get_settings(session)
    period = settings.period_for(date(2026, 8, 3))
    assert period.key == "2026-07"
    assert txs.actuals_for_period(session, period).expense_total == Decimal("120.00")
    assert txs.actuals_for_period(
        session, settings.period(2026, 8)).expense_total == Decimal("0.00")


def test_partial_current_period_elapsed_fraction(session):
    period = make_period(2026, 8)
    tracking = budget_service.track_period(session, period, today=date(2026, 8, 8))
    assert 0.2 < tracking.elapsed_fraction < 0.3


# ==========================================================================
# CSV import / export
# ==========================================================================
def test_csv_import_preview_then_commit(session, accounts):
    from import_export import csv_handler

    csv_text = (
        "date,description,amount,kind,category,account\n"
        "2026-08-01,Bakery,-45.90,expense,Food › Groceries,Checking\n"
        "2026-08-02,Salary,5000.00,income,Salary › Net salary,Checking\n"
        "2026-08-03,Broken row,,expense,,Checking\n"
    )
    preview = csv_handler.build_preview(
        session, csv_text, source_name="bank.csv",
        default_account_id=accounts["Checking"].id)
    assert len(preview.rows) == 3
    assert len(preview.valid_rows) == 2
    assert len(preview.error_rows) == 1
    assert preview.total_in == Decimal("5000.00")
    assert preview.total_out == Decimal("45.90")

    result = csv_handler.commit(session, preview)
    session.commit()
    assert result.imported == 2
    assert result.failed == 1
    assert txs.count_transactions(session, txs.TxnFilter()) == 2


def test_csv_import_detects_duplicates_against_existing_data(session, accounts):
    from import_export import csv_handler

    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 1), "description": "Bakery",
        "amount": "45.90", "kind": TxnKind.EXPENSE.value,
        "account_id": accounts["Checking"].id,
    })
    session.commit()

    csv_text = ("date,description,amount\n"
                "2026-08-01,Bakery,-45.90\n")
    preview = csv_handler.build_preview(session, csv_text,
                                        default_account_id=accounts["Checking"].id)
    assert len(preview.duplicate_rows) == 1
    assert preview.importable == []

    result = csv_handler.commit(session, preview)
    session.commit()
    assert result.imported == 0
    assert result.skipped == 1


def test_csv_import_spots_duplicates_inside_the_same_file(session, accounts):
    from import_export import csv_handler

    csv_text = ("date,description,amount\n"
                "2026-08-01,Bakery,-45.90\n"
                "2026-08-01,Bakery,-45.90\n")
    preview = csv_handler.build_preview(session, csv_text,
                                        default_account_id=accounts["Checking"].id)
    assert len(preview.importable) == 1
    assert len(preview.duplicate_rows) == 1


def test_csv_import_handles_semicolons_and_brazilian_numbers(session, accounts):
    from import_export import csv_handler

    csv_text = ("data;descrição;valor;tipo\n"
                "05/08/2026;Supermercado;-1.234,56;despesa\n")
    preview = csv_handler.build_preview(
        session, csv_text, default_account_id=accounts["Checking"].id,
        date_pattern="%d/%m/%Y")
    assert len(preview.valid_rows) == 1
    row = preview.valid_rows[0]
    assert row.payload["amount"] == Decimal("1234.56")
    assert row.payload["kind"] == TxnKind.EXPENSE.value
    assert row.payload["txn_date"] == date(2026, 8, 5)


def test_csv_import_rollback_restores_the_previous_state(session, accounts):
    from import_export import csv_handler

    csv_text = ("date,description,amount\n"
                "2026-08-01,One,-10.00\n"
                "2026-08-02,Two,-20.00\n")
    preview = csv_handler.build_preview(session, csv_text,
                                        default_account_id=accounts["Checking"].id)
    result = csv_handler.commit(session, preview)
    session.commit()
    assert txs.count_transactions(session, txs.TxnFilter()) == 2

    removed = csv_handler.rollback(session, result.batch_id)
    session.commit()
    assert removed == 2
    assert txs.count_transactions(session, txs.TxnFilter()) == 0
    assert txs.count_transactions(session, txs.TxnFilter(only_deleted=True)) == 2


def test_mapping_detection_handles_portuguese_headers():
    from import_export import csv_handler

    mapping = csv_handler.detect_mapping(
        ["Data", "Histórico", "Valor", "Tipo", "Conta"])
    assert mapping["date"] == "Data"
    assert mapping["description"] == "Histórico"
    assert mapping["amount"] == "Valor"
    assert mapping["account"] == "Conta"


def test_csv_export_round_trips(session, accounts, categories):
    from import_export import csv_handler

    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 1), "description": "Bakery",
        "amount": "45.90", "kind": TxnKind.EXPENSE.value,
        "account_id": accounts["Checking"].id,
        "category_id": categories["groceries"].id,
    })
    session.commit()

    exported = csv_handler.transactions_to_csv(
        session, txs.list_transactions(session, txs.TxnFilter()))
    assert "Bakery" in exported and "45.90" in exported

    txs.purge_deleted(session)
    for row in txs.list_transactions(session, txs.TxnFilter()):
        session.delete(row)
    session.commit()

    preview = csv_handler.build_preview(session, exported,
                                        default_account_id=accounts["Checking"].id)
    assert len(preview.valid_rows) == 1
    assert preview.valid_rows[0].payload["amount"] == Decimal("45.90")


def test_template_csv_is_importable(session, accounts):
    from import_export import csv_handler

    preview = csv_handler.build_preview(
        session, csv_handler.template_csv(),
        default_account_id=accounts["Checking"].id)
    assert len(preview.error_rows) == 0
    assert len(preview.valid_rows) == 2


# ==========================================================================
# Excel export
# ==========================================================================
def test_excel_workbook_has_every_sheet(session, accounts, categories, rules):
    from import_export import excel_handler

    budget_service.generate_from_rules(session, make_period(2026, 8))
    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 1), "description": "Bakery", "amount": "45.90",
        "kind": TxnKind.EXPENSE.value, "account_id": accounts["Checking"].id,
        "category_id": categories["groceries"].id,
    })
    session.commit()

    workbook = excel_handler.build_workbook(session, months=3, today=date(2026, 8, 17))
    expected = {"About", "Transactions", "Budget", "History", "Accounts", "Goals",
                "Debts", "Net worth", "Forecast"}
    assert expected <= set(workbook.sheetnames)

    sheet = workbook["Transactions"]
    assert sheet["A1"].value == "Date"
    # Locate the money column by its header rather than by letter: the columns
    # shift whenever a new one (Currency, say) is inserted before it.
    headers = {cell.value: cell.column_letter for cell in sheet[1]}
    amount_col = headers["Amount"]
    formulas = [cell.value for cell in sheet[amount_col] if isinstance(cell.value, str)
                and str(cell.value).startswith("=")]
    assert formulas, "the total row should be a live SUM formula"


def test_excel_bytes_are_a_valid_zip(session):
    from import_export import excel_handler

    payload = excel_handler.workbook_bytes(session, months=1, today=date(2026, 8, 17))
    assert payload[:2] == b"PK"
    assert len(payload) > 5000


# ==========================================================================
# Backup and restore
# ==========================================================================
def test_json_backup_and_restore_round_trip(session, accounts, categories, db_path):
    from database.database import get_session_factory
    from import_export import backup

    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 1), "description": "Before backup",
        "amount": "123.45", "kind": TxnKind.EXPENSE.value,
        "account_id": accounts["Checking"].id,
        "category_id": categories["groceries"].id,
    })
    session.commit()
    payload = backup.json_bytes(session)
    assert b"Before backup" in payload

    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 2), "description": "After backup",
        "amount": "99.00", "kind": TxnKind.EXPENSE.value,
        "account_id": accounts["Checking"].id,
    })
    session.commit()
    assert txs.count_transactions(session, txs.TxnFilter()) == 2
    session.close()

    report = backup.restore_json(payload, db_path=db_path, backup_first=False)
    assert report.tables_restored["transactions"] == 1

    fresh = get_session_factory(db_path)()
    try:
        rows = txs.list_transactions(fresh, txs.TxnFilter())
        assert [row.description for row in rows] == ["Before backup"]
        assert rows[0].amount == Decimal("123.45")
    finally:
        fresh.close()


def test_sqlite_backup_and_restore(session, accounts, db_path, tmp_path):
    from database.database import get_session_factory
    from import_export import backup

    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 1), "description": "Snapshot me",
        "amount": "50.00", "kind": TxnKind.EXPENSE.value,
        "account_id": accounts["Checking"].id,
    })
    session.commit()
    snapshot = backup.create_sqlite_backup(target_dir=tmp_path / "bk", db_path=db_path)
    assert snapshot.exists()

    txs.create_transaction(session, {
        "txn_date": date(2026, 8, 5), "description": "Added later",
        "amount": "10.00", "kind": TxnKind.EXPENSE.value,
        "account_id": accounts["Checking"].id,
    })
    session.commit()
    session.close()

    report = backup.restore_sqlite(snapshot, db_path=db_path)
    assert report.previous_saved_to is not None

    fresh = get_session_factory(db_path)()
    try:
        rows = txs.list_transactions(fresh, txs.TxnFilter())
        assert [row.description for row in rows] == ["Snapshot me"]
    finally:
        fresh.close()


def test_restore_rejects_a_file_that_is_not_a_backup(tmp_path, db_path):
    from import_export import backup
    from services.common import ServiceError

    junk = tmp_path / "not-a-db.db"
    junk.write_bytes(b"definitely not sqlite")
    with pytest.raises(ServiceError):
        backup.restore_sqlite(junk, db_path=db_path)


def test_zip_backup_contains_both_formats(session, tmp_path):
    import zipfile

    from import_export import backup

    archive = backup.create_zip_backup(session, target_dir=tmp_path / "bk")
    with zipfile.ZipFile(archive) as handle:
        names = set(handle.namelist())
    assert {"finance.db", "finance.json", "README.txt"} <= names


def test_listing_and_pruning_backups(session, tmp_path):
    from import_export import backup

    directory = tmp_path / "bk"
    for _ in range(3):
        backup.create_json_backup(session, target_dir=directory)
    listed = backup.list_backups(target_dir=directory)
    assert len(listed) == 3
    assert all(item.size_bytes > 0 for item in listed)
    assert backup.prune_backups(1, target_dir=directory) == 2


# ==========================================================================
# Migrations and demo data
# ==========================================================================
def test_migrations_add_a_missing_column(db_path):
    from sqlalchemy import inspect, text

    from database.database import get_engine, init_db
    from database.migrations import ensure_columns

    init_db(db_path)
    engine = get_engine(db_path)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE goals DROP COLUMN icon")
    assert "icon" not in {
        column["name"] for column in inspect(engine).get_columns("goals")}

    added = ensure_columns(engine)
    assert "goals.icon" in added
    assert "icon" in {
        column["name"] for column in inspect(engine).get_columns("goals")}


def test_init_db_is_idempotent(db_path):
    from database.database import database_stats, init_db

    init_db(db_path)
    first = database_stats(db_path)["tables"]["categories"]
    init_db(db_path)
    assert database_stats(db_path)["tables"]["categories"] == first


def test_demo_data_is_coherent(session):
    from demo.demo_data import clear_all_data, has_demo_data, load_demo_data

    report = load_demo_data(session, months_back=6, months_forward=2,
                            today=date(2026, 8, 17))
    session.commit()
    assert report.transactions_completed > 0
    assert report.budget_periods == 8
    assert has_demo_data(session)

    dashboard = reporting_service.dashboard(session, today=date(2026, 8, 17))
    assert dashboard.has_data
    assert dashboard.total_assets > 0

    counts = clear_all_data(session, keep_accounts=True, keep_categories=True)
    session.commit()
    assert counts["transactions"] > 0
    assert txs.count_transactions(session, txs.TxnFilter()) == 0
    assert len(category_service.list_categories(session)) > 0


def test_clearing_data_leaves_no_money_behind(session):
    """Clearing must zero the kept accounts, not just empty their history.

    The demo accounts open with 3200 + 6500 + 250 in cash. Deleting only the
    transactions left those opening balances standing, so a freshly cleared
    book still reported 9950 in cash with nothing on screen to explain it.
    """
    from demo.demo_data import clear_all_data, load_demo_data

    load_demo_data(session, months_back=6, months_forward=2,
                   today=date(2026, 8, 17))
    session.commit()
    before = account_service.totals(account_service.balance_views(session))
    assert before.cash > 0

    clear_all_data(session, keep_accounts=True, keep_categories=True)
    session.commit()

    after = account_service.totals(account_service.balance_views(session))
    assert after.cash == Decimal("0")
    assert after.net_worth == Decimal("0")
    assert after.liabilities == Decimal("0")
    # the accounts themselves survive, so the user keeps their setup
    accounts = session.execute(select(Account)).scalars().all()
    assert accounts
    assert all(a.opening_balance == Decimal("0") for a in accounts)


def test_demo_data_can_be_loaded_twice_without_duplicating(session):
    from demo.demo_data import load_demo_data

    load_demo_data(session, months_back=3, months_forward=1,
                   today=date(2026, 8, 17))
    session.commit()
    first = txs.count_transactions(session, txs.TxnFilter())
    rules_first = len(recurring_service.list_rules(session))

    load_demo_data(session, months_back=3, months_forward=1,
                   today=date(2026, 8, 17))
    session.commit()
    assert txs.count_transactions(session, txs.TxnFilter()) == first
    assert len(recurring_service.list_rules(session)) == rules_first


# ==========================================================================
# Reporting sanity
# ==========================================================================
def test_history_and_averages_agree(session, accounts, categories):
    for month, amount in ((6, "1000"), (7, "2000"), (8, "3000")):
        txs.create_transaction(session, {
            "txn_date": date(2026, month, 10), "description": f"Spend {month}",
            "amount": amount, "kind": TxnKind.EXPENSE.value,
            "account_id": accounts["Checking"].id,
            "category_id": categories["groceries"].id,
        })
    session.commit()

    history = reporting_service.trailing_history(session, 3, date(2026, 8, 17))
    assert [row["expenses"] for row in history] == [
        Decimal("1000.00"), Decimal("2000.00"), Decimal("3000.00")]

    averages = reporting_service.averages(session, 2, date(2026, 8, 17))
    assert averages["expenses"] == Decimal("1500.00")  # June and July only


def test_dashboard_survives_an_empty_database(session):
    snapshot = reporting_service.dashboard(session, today=date(2026, 8, 17))
    assert snapshot.cash == Decimal("0.00")
    assert snapshot.net_worth == Decimal("0.00")
    assert not snapshot.has_data
    assert isinstance(snapshot.alerts, list)


# --------------------------------------------------------------------------
# Language
# --------------------------------------------------------------------------
#: Characters that only appear in the Portuguese the dataset used to ship with.
PORTUGUESE_CHARS = set("áàâãéêíóôõúüçÁÀÂÃÉÊÍÓÔÕÚÜÇº")

#: Whole words that would betray Portuguese without needing an accent.
PORTUGUESE_WORDS = (
    "conta", "cartao", "credito", "poupanca", "carteira", "aluguel",
    "supermercado", "salario", "viagem", "reserva", "pagamento", "parcela",
    "seguro", "imposto", "financiamento", "apartamento", "geladeira",
)


def _demo_strings(session):
    """Every string the demo dataset puts in front of a person."""
    from database.models import Account, Debt, Goal, RecurringRule, Transaction

    fields = [
        (Account, ("name", "institution")),
        (RecurringRule, ("name", "description_template")),
        (Goal, ("name", "notes")),
        (Debt, ("name",)),
        (Transaction, ("description", "tags")),
    ]
    seen = set()
    for model, names in fields:
        for row in session.execute(select(model)).scalars():
            for field in names:
                value = getattr(row, field, None)
                if value:
                    seen.add(f"{model.__name__}.{field}: {value}")
    return seen


def test_demo_data_is_written_in_english(session):
    """No accented characters, and no give-away Portuguese words either."""
    from demo.demo_data import load_demo_data

    load_demo_data(session, months_back=6, months_forward=2,
                   today=date(2026, 8, 17))
    session.commit()

    values = _demo_strings(session)
    assert values, "the demo dataset produced no text at all"

    accented = [v for v in values if PORTUGUESE_CHARS & set(v)]
    assert not accented, "accented Portuguese left in the demo data:\n" + "\n".join(accented)

    worded = [v for v in values
              if any(word in v.split(": ", 1)[1].lower() for word in PORTUGUESE_WORDS)]
    assert not worded, "Portuguese words left in the demo data:\n" + "\n".join(worded)


def test_a_portuguese_book_translates_itself(session):
    """The path a person upgrading actually takes.

    Their database was seeded when the dataset was still Portuguese, so editing
    the definitions does nothing for them — the rows have to be rewritten in
    place, without disturbing a single balance or link.
    """
    from demo.demo_data import (
        LEGACY_NAMES,
        _TRANSLATABLE_FIELDS,
        has_demo_data,
        load_demo_data,
        needs_translation,
        translate_legacy_data,
    )
    from services import account_service

    load_demo_data(session, months_back=6, months_forward=2,
                   today=date(2026, 8, 17))
    session.commit()
    before = account_service.totals(account_service.balance_views(session))

    # Put the book back into Portuguese, exactly as an older install holds it.
    reverse = [(new, old) for old, new in LEGACY_NAMES]
    for model, fields in _TRANSLATABLE_FIELDS:
        for row in session.execute(select(model)).scalars():
            for field in fields:
                value = getattr(row, field, None)
                if not value:
                    continue
                for new, old in reverse:
                    value = value.replace(new, old)
                setattr(row, field, value)
    session.commit()
    assert needs_translation(session)
    assert has_demo_data(session), "the old name must still count as demo data"

    changed = translate_legacy_data(session)
    session.commit()
    assert changed["Account"] and changed["Transaction"]

    values = _demo_strings(session)
    accented = [v for v in values if PORTUGUESE_CHARS & set(v)]
    assert not accented, "translation missed:\n" + "\n".join(accented)

    # Idempotent, and nothing financial moved.
    assert not needs_translation(session)
    assert translate_legacy_data(session) == {}
    after = account_service.totals(account_service.balance_views(session))
    assert after.net_worth == before.net_worth
    assert after.cash == before.cash
    assert after.liabilities == before.liabilities


def test_translation_leaves_a_persons_own_entries_alone(session):
    """Only the known demo phrases are rewritten — nothing else is touched."""
    from demo.demo_data import translate_legacy_data, translate_text

    assert translate_text("Conta Corrente") == "Main checking"
    assert translate_text("Interest · Cartão de Crédito · 08/2026") == \
        "Interest · Credit card · 08/2026"
    # A person's own words, in either language, pass through unchanged.
    assert translate_text("Lunch with Ana") == "Lunch with Ana"
    assert translate_text("Consulta veterinária") == "Consulta veterinária"
    assert translate_text(None) is None
    assert translate_text("") == ""

    # And on an empty book it does nothing at all.
    assert translate_legacy_data(session) == {}


def test_renamed_transactions_keep_a_valid_fingerprint(session):
    """The duplicate-import guard hashes the description, so it must be redone."""
    from demo.demo_data import load_demo_data, translate_legacy_data
    from database.models import Transaction
    from services.transaction_service import fingerprint

    load_demo_data(session, months_back=3, months_forward=1,
                   today=date(2026, 8, 17))
    session.commit()

    row = session.execute(
        select(Transaction).where(Transaction.description == "Groceries")
    ).scalars().first()
    assert row is not None
    row.description = "Supermercado"
    session.commit()

    translate_legacy_data(session)
    session.commit()
    session.refresh(row)
    assert row.description == "Groceries"
    assert row.fingerprint == fingerprint(
        row.txn_date, row.amount, row.description, row.account_id, row.kind)
