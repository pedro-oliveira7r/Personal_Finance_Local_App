"""Recurring rules and the planned transactions they generate.

Generation is idempotent. Each occurrence has a stable key (its due date), and
the ``(rule_id, occurrence_key)`` pair is unique in the database, so running
generation twice never produces twins. Occurrences already marked *completed*
are left strictly alone — the engine only ever refreshes still-planned rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import config
from calculations.money import ZERO, money, money_sum
from calculations.periods import Period, shift_period
from calculations.recurrence import (
    Occurrence,
    RecurrenceSpec,
    describe,
    generate_occurrences,
    next_occurrence,
    occurrences_in_period,
    spec_from_rule,
)
from constants import CategoryKind, Frequency, TxnKind, TxnStatus
from database.models import Account, Category, RecurringRule, Transaction, utcnow
from schemas.validation import RecurringRuleIn
from services.common import (
    ConflictError,
    NotFoundError,
    ServiceError,
    SettingsSnapshot,
    apply_fields,
    ensure_exists,
    send_to_recycle_bin,
    settings_snapshot,
)
from services.transaction_service import fingerprint


# ==========================================================================
# CRUD
# ==========================================================================
def list_rules(session: Session, *, active_only: bool = False,
               kinds: Optional[Sequence[str]] = None) -> list[RecurringRule]:
    stmt = select(RecurringRule).order_by(RecurringRule.kind, RecurringRule.name)
    if active_only:
        stmt = stmt.where(RecurringRule.is_active.is_(True))
    if kinds:
        stmt = stmt.where(RecurringRule.kind.in_(list(kinds)))
    return list(session.execute(stmt).scalars())


def get_rule(session: Session, rule_id: int) -> RecurringRule:
    rule = session.get(RecurringRule, rule_id)
    if rule is None:
        raise NotFoundError(f"Recurring rule #{rule_id} was not found.")
    return rule


def create_rule(session: Session, payload: dict[str, Any]) -> RecurringRule:
    data = RecurringRuleIn(**payload)
    ensure_exists(session, Account, data.account_id, "Account")
    ensure_exists(session, Account, data.to_account_id, "Destination account")
    if data.category_id:
        category = ensure_exists(session, Category, data.category_id, "Category")
        _check_category_kind(data.kind, category)
    clash = session.execute(
        select(RecurringRule).where(func.lower(RecurringRule.name) == data.name.lower())
    ).scalars().first()
    if clash is not None:
        raise ConflictError(f"A rule called “{data.name}” already exists.")
    rule = RecurringRule(**data.model_dump())
    session.add(rule)
    session.flush()
    return rule


def update_rule(session: Session, rule_id: int, payload: dict[str, Any]) -> RecurringRule:
    rule = get_rule(session, rule_id)
    merged = {
        column: getattr(rule, column)
        for column in RecurringRuleIn.model_fields
        if hasattr(rule, column)
    }
    merged.update(payload)
    data = RecurringRuleIn(**merged)
    if data.category_id:
        category = ensure_exists(session, Category, data.category_id, "Category")
        _check_category_kind(data.kind, category)
    apply_fields(rule, data.model_dump())
    session.flush()
    return rule


def _check_category_kind(txn_kind: str, category: Optional[Category]) -> None:
    if category is None:
        return
    is_income_cat = category.kind == CategoryKind.INCOME.value
    if txn_kind == TxnKind.INCOME.value and not is_income_cat:
        raise ServiceError(f"“{category.full_name}” is not an income category.")
    if txn_kind == TxnKind.EXPENSE.value and is_income_cat:
        raise ServiceError(f"“{category.full_name}” is an income category.")


def set_active(session: Session, rule_id: int, active: bool) -> RecurringRule:
    rule = get_rule(session, rule_id)
    rule.is_active = active
    session.flush()
    return rule


def delete_rule(session: Session, rule_id: int, *,
                delete_planned: bool = True) -> dict[str, int]:
    """Remove a rule; optionally clear the planned transactions it created.

    Completed transactions always survive — they are history, not a plan.
    """
    rule = get_rule(session, rule_id)
    send_to_recycle_bin(session, "recurring_rule", rule, label=rule.name)
    removed = 0
    if delete_planned:
        planned = session.execute(
            select(Transaction).where(
                Transaction.rule_id == rule_id,
                Transaction.status == TxnStatus.PLANNED.value,
                Transaction.deleted_at.is_(None),
            )
        ).scalars().unique()
        for txn in planned:
            txn.deleted_at = utcnow()
            removed += 1
    session.delete(rule)
    session.flush()
    return {"deleted_rule": rule_id, "planned_removed": removed}


# ==========================================================================
# Generation
# ==========================================================================
@dataclass
class GenerationReport:
    created: int = 0
    updated: int = 0
    skipped_completed: int = 0
    unchanged: int = 0
    rules_processed: int = 0
    horizon_end: Optional[date] = None
    details: list[str] = field(default_factory=list)

    @property
    def total_touched(self) -> int:
        return self.created + self.updated

    def summary(self) -> str:
        parts = [f"{self.created} created", f"{self.updated} updated"]
        if self.skipped_completed:
            parts.append(f"{self.skipped_completed} already completed (left alone)")
        if self.unchanged:
            parts.append(f"{self.unchanged} unchanged")
        return ", ".join(parts)


def _description_for(rule: RecurringRule, occurrence: Occurrence) -> str:
    template = rule.description_template or rule.name
    try:
        return template.format(
            name=rule.name,
            date=occurrence.due_date.isoformat(),
            amount=occurrence.amount,
            month=occurrence.due_date.strftime("%m"),
            year=occurrence.due_date.year,
        )[:240]
    except (KeyError, IndexError, ValueError):
        return rule.name[:240]


def generate_for_rule(
    session: Session,
    rule: RecurringRule,
    window_start: date,
    window_end: date,
    *,
    report: Optional[GenerationReport] = None,
    refresh_existing: bool = True,
) -> GenerationReport:
    report = report or GenerationReport()
    report.rules_processed += 1
    spec = spec_from_rule(rule)
    occurrences = generate_occurrences(spec, window_start, window_end)
    if not occurrences:
        return report

    existing = {
        txn.occurrence_key: txn
        for txn in session.execute(
            select(Transaction).where(
                Transaction.rule_id == rule.id,
                Transaction.occurrence_key.is_not(None),
            )
        ).scalars().unique()
    }

    for occurrence in occurrences:
        key = occurrence.key
        current = existing.get(key)
        if current is not None:
            if current.status == TxnStatus.COMPLETED.value:
                report.skipped_completed += 1
                continue
            if current.deleted_at is not None:
                # The user deleted this occurrence on purpose: respect that.
                report.unchanged += 1
                continue
            if not refresh_existing:
                report.unchanged += 1
                continue
            changed = (
                current.amount != occurrence.amount
                or current.txn_date != occurrence.due_date
                or current.actual_date is not None
            )
            if changed:
                current.amount = occurrence.amount
                current.txn_date = occurrence.due_date
                current.availability_date = _availability_override(rule, occurrence)
                current.fingerprint = fingerprint(
                    occurrence.due_date, occurrence.amount, current.description,
                    current.account_id, current.kind,
                )
                report.updated += 1
            else:
                report.unchanged += 1
            continue

        description = _description_for(rule, occurrence)
        txn = Transaction(
            txn_date=occurrence.due_date,
            actual_date=None,
            availability_date=_availability_override(rule, occurrence),
            description=description,
            amount=occurrence.amount,
            kind=rule.kind,
            status=TxnStatus.PLANNED.value,
            category_id=rule.category_id,
            account_id=rule.account_id,
            to_account_id=rule.to_account_id,
            goal_id=rule.goal_id,
            debt_id=rule.debt_id,
            rule_id=rule.id,
            occurrence_key=key,
            payment_method=rule.payment_method,
            tags=rule.tags,
            notes=rule.notes,
            is_planned=True,
            fingerprint=fingerprint(
                occurrence.due_date, occurrence.amount, description,
                rule.account_id, rule.kind,
            ),
        )
        session.add(txn)
        report.created += 1

    rule.generated_through = max(rule.generated_through or window_end, window_end)
    session.flush()
    return report


def _availability_override(rule: RecurringRule, occurrence: Occurrence) -> Optional[date]:
    """Cash date differs from the due date when a settlement offset is set."""
    if rule.kind != TxnKind.INCOME.value:
        return None
    if occurrence.cash_date == occurrence.due_date:
        return None
    return occurrence.cash_date


def generate_planned(
    session: Session,
    *,
    horizon_months: int = config.DEFAULT_GENERATION_HORIZON_MONTHS,
    from_date: Optional[date] = None,
    rule_ids: Optional[Sequence[int]] = None,
    include_inactive: bool = False,
    backfill: bool = False,
    refresh_existing: bool = True,
    today: Optional[date] = None,
) -> GenerationReport:
    """Materialise planned transactions for every eligible rule.

    ``backfill=True`` also creates occurrences that fall before ``from_date``,
    starting at each rule's own start date — useful right after importing
    history. The default only looks forward from the current period.
    """
    today = today or date.today()
    horizon_months = max(1, min(int(horizon_months), config.MAX_GENERATION_HORIZON_MONTHS))
    settings = settings_snapshot(session)
    start_period = settings.current_period(today)
    window_start = from_date or start_period.start
    window_end = shift_period(start_period, horizon_months, settings.first_day_of_month).end

    report = GenerationReport(horizon_end=window_end)
    rules = list_rules(session, active_only=not include_inactive)
    for rule in rules:
        if rule_ids and rule.id not in set(rule_ids):
            continue
        if not include_inactive and not rule.is_active:
            continue
        if not rule.auto_generate and not rule_ids:
            continue
        rule_start = rule.start_date if backfill else max(window_start, rule.start_date)
        generate_for_rule(
            session, rule, rule_start, window_end,
            report=report, refresh_existing=refresh_existing,
        )
    return report


def preview_rule(rule_payload: dict[str, Any], months: int = 12,
                 today: Optional[date] = None) -> list[Occurrence]:
    """Dry-run a rule form so the user sees the schedule before saving."""
    data = RecurringRuleIn(**rule_payload)
    spec = RecurrenceSpec(
        frequency=data.frequency,
        start_date=data.start_date,
        amount=data.amount,
        interval=data.interval,
        end_date=data.end_date,
        max_occurrences=data.max_occurrences,
        day_of_month=data.day_of_month,
        weekday=data.weekday,
        month_of_year=data.month_of_year,
        growth_pct=data.growth_pct,
        growth_every_months=data.growth_every_months,
        growth_anchor_month=data.growth_anchor_month,
        seasonal_factors=data.seasonal_factors,
        business_day_rule=data.business_day_rule,
        settlement_offset_days=data.settlement_offset_days,
    )
    today = today or date.today()
    start = min(today, data.start_date)
    end = date(start.year + (start.month - 1 + months) // 12,
               (start.month - 1 + months) % 12 + 1,
               min(start.day, 28))
    return generate_occurrences(spec, start, end)


# ==========================================================================
# Projection helpers used by budget & forecast services
# ==========================================================================
@dataclass
class RuleProjection:
    income: Decimal = ZERO
    expense: Decimal = ZERO
    savings: Decimal = ZERO
    investment: Decimal = ZERO
    debt: Decimal = ZERO
    transfers: Decimal = ZERO
    per_category: dict[int, Decimal] = field(default_factory=dict)
    per_rule: dict[int, Decimal] = field(default_factory=dict)


def project_period(session: Session, period: Period, *,
                   only_budget_rules: bool = True) -> RuleProjection:
    """What the active rules imply for one period, by category."""
    kinds = {
        row[0]: row[1]
        for row in session.execute(select(Category.id, Category.kind)).all()
    }
    projection = RuleProjection()
    for rule in list_rules(session, active_only=True):
        if only_budget_rules and not rule.include_in_budget:
            continue
        total = money_sum(
            occ.amount for occ in occurrences_in_period(spec_from_rule(rule), period)
        )
        if total == 0:
            continue
        projection.per_rule[rule.id] = total
        if rule.category_id:
            projection.per_category[rule.category_id] = money(
                projection.per_category.get(rule.category_id, ZERO) + total
            )
        if rule.kind == TxnKind.INCOME.value:
            projection.income = money(projection.income + total)
        elif rule.kind == TxnKind.TRANSFER.value:
            projection.transfers = money(projection.transfers + total)
        else:
            kind = kinds.get(rule.category_id) if rule.category_id else None
            if kind == CategoryKind.SAVINGS.value:
                projection.savings = money(projection.savings + total)
            elif kind == CategoryKind.INVESTMENT.value:
                projection.investment = money(projection.investment + total)
            elif kind == CategoryKind.DEBT.value:
                projection.debt = money(projection.debt + total)
            else:
                projection.expense = money(projection.expense + total)
    return projection


@dataclass
class UpcomingRule:
    rule: RecurringRule
    occurrence: Occurrence
    description: str


def upcoming(session: Session, days: int = 30,
             today: Optional[date] = None) -> list[UpcomingRule]:
    """Next occurrence of every active rule inside the window."""
    today = today or date.today()
    horizon = today + timedelta(days=days)
    results: list[UpcomingRule] = []
    for rule in list_rules(session, active_only=True):
        spec = spec_from_rule(rule)
        for occurrence in generate_occurrences(spec, today, horizon):
            results.append(UpcomingRule(
                rule=rule, occurrence=occurrence,
                description=describe(spec),
            ))
    results.sort(key=lambda item: item.occurrence.due_date)
    return results


def rule_summary(session: Session) -> dict[str, Any]:
    rules = list_rules(session)
    active = [r for r in rules if r.is_active]
    return {
        "total": len(rules),
        "active": len(active),
        "income": len([r for r in active if r.kind == TxnKind.INCOME.value]),
        "expense": len([r for r in active if r.kind == TxnKind.EXPENSE.value]),
        "transfers": len([r for r in active if r.kind == TxnKind.TRANSFER.value]),
        "with_growth": len([r for r in active if r.growth_pct]),
        "seasonal": len([r for r in active if r.seasonal_factors]),
    }


def describe_rule(rule: RecurringRule) -> str:
    return describe(spec_from_rule(rule))
