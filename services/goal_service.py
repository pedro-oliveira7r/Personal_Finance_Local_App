"""Financial goals: definition, contributions and progress."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from calculations.goals import GoalProgress, compute_progress, goal_alerts, prioritise
from calculations.money import ZERO, D, money, money_sum
from calculations.periods import Period, month_diff, shift_period
from constants import CategoryKind, GoalStatus, TxnKind, TxnStatus
from database.models import Account, Category, Goal, Transaction
from schemas.validation import GoalIn
from services.common import (
    ConflictError,
    NotFoundError,
    ServiceError,
    apply_fields,
    ensure_exists,
    send_to_recycle_bin,
    settings_snapshot,
)
from services.transaction_service import TxnFilter, create_transaction, list_transactions


def list_goals(session: Session, *, statuses: Optional[Sequence[str]] = None) -> list[Goal]:
    stmt = select(Goal).order_by(Goal.priority, Goal.target_date, Goal.name)
    if statuses:
        stmt = stmt.where(Goal.status.in_(list(statuses)))
    return list(session.execute(stmt).scalars())


def active_goals(session: Session) -> list[Goal]:
    return list_goals(session, statuses=[GoalStatus.ACTIVE.value])


def get_goal(session: Session, goal_id: int) -> Goal:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise NotFoundError(f"Goal #{goal_id} was not found.")
    return goal


def create_goal(session: Session, payload: dict[str, Any]) -> Goal:
    data = GoalIn(**payload)
    ensure_exists(session, Account, data.account_id, "Account")
    ensure_exists(session, Category, data.category_id, "Category")
    clash = session.execute(
        select(Goal).where(func.lower(Goal.name) == data.name.lower())
    ).scalars().first()
    if clash is not None:
        raise ConflictError(f"A goal called “{data.name}” already exists.")
    goal = Goal(**data.model_dump())
    session.add(goal)
    session.flush()
    return goal


def update_goal(session: Session, goal_id: int, payload: dict[str, Any]) -> Goal:
    goal = get_goal(session, goal_id)
    merged = {name: getattr(goal, name) for name in GoalIn.model_fields if hasattr(goal, name)}
    merged.update(payload)
    data = GoalIn(**merged)
    clash = session.execute(
        select(Goal).where(func.lower(Goal.name) == data.name.lower(), Goal.id != goal_id)
    ).scalars().first()
    if clash is not None:
        raise ConflictError(f"Another goal is already called “{data.name}”.")
    apply_fields(goal, data.model_dump())
    session.flush()
    return goal


def set_status(session: Session, goal_id: int, status: str) -> Goal:
    if status not in GoalStatus.values():
        raise ServiceError(f"Unknown goal status “{status}”.")
    goal = get_goal(session, goal_id)
    goal.status = status
    session.flush()
    return goal


def delete_goal(session: Session, goal_id: int, *, force: bool = False) -> dict[str, Any]:
    goal = get_goal(session, goal_id)
    contributions = session.execute(
        select(func.count(Transaction.id)).where(
            Transaction.goal_id == goal_id, Transaction.deleted_at.is_(None)
        )
    ).scalar() or 0
    if contributions and not force:
        raise ConflictError(
            f"“{goal.name}” has {contributions} contribution(s). Confirm to delete the goal "
            "— the transactions stay in your history."
        )
    send_to_recycle_bin(session, "goal", goal, label=goal.name)
    session.delete(goal)
    session.flush()
    return {"deleted": goal_id, "contributions": contributions}


# --------------------------------------------------------------------------
# Contributions
# --------------------------------------------------------------------------
def contributions_total(session: Session, goal_id: int,
                        *, up_to: Optional[date] = None) -> Decimal:
    """Everything actually paid into the goal (completed transactions only)."""
    txns = list_transactions(session, TxnFilter(
        goal_id=goal_id, statuses=[TxnStatus.COMPLETED.value],
        end=up_to, use_effective_date=True,
    ))
    return money_sum(txn.amount for txn in txns)


def contributions_in_period(session: Session, goal_id: int, period: Period) -> Decimal:
    txns = list_transactions(session, TxnFilter(
        goal_id=goal_id, statuses=[TxnStatus.COMPLETED.value],
        start=period.start, end=period.end, use_effective_date=True,
    ))
    return money_sum(txn.amount for txn in txns)


def average_monthly_contribution(session: Session, goal_id: int,
                                 months: int = 6, today: Optional[date] = None) -> Decimal:
    settings = settings_snapshot(session)
    today = today or date.today()
    current = settings.current_period(today)
    total = ZERO
    counted = 0
    for offset in range(1, max(1, months) + 1):
        period = shift_period(current, -offset, settings.first_day_of_month)
        total = money(total + contributions_in_period(session, goal_id, period))
        counted += 1
    return money(total / Decimal(counted)) if counted else ZERO


def current_amount(session: Session, goal: Goal, *, up_to: Optional[date] = None) -> Decimal:
    return money(goal.starting_amount + contributions_total(session, goal.id, up_to=up_to))


def record_contribution(
    session: Session,
    goal_id: int,
    amount: Decimal,
    *,
    on_date: Optional[date] = None,
    from_account_id: Optional[int] = None,
    description: Optional[str] = None,
    allow_duplicate: bool = False,
) -> Transaction:
    """Record money going into a goal.

    When the goal has a dedicated account the movement is a **transfer**, so
    total cash is unchanged and the money is simply earmarked. Without an
    account it is booked as a savings **expense** against the goal's category.
    """
    goal = get_goal(session, goal_id)
    amount = money(amount)
    if amount <= 0:
        raise ServiceError("A contribution must be greater than zero.")
    on_date = on_date or date.today()
    label = description or f"Contribution · {goal.name}"

    if goal.account_id and from_account_id and goal.account_id != from_account_id:
        return create_transaction(session, {
            "txn_date": on_date,
            "description": label,
            "amount": amount,
            "kind": TxnKind.TRANSFER.value,
            "status": TxnStatus.COMPLETED.value,
            "account_id": from_account_id,
            "to_account_id": goal.account_id,
            "goal_id": goal_id,
        }, allow_duplicate=allow_duplicate)

    category_id = goal.category_id
    if category_id is None:
        category = session.execute(
            select(Category).where(
                Category.kind == CategoryKind.SAVINGS.value,
                Category.parent_id.is_(None),
            ).order_by(Category.sort_order)
        ).scalars().first()
        category_id = category.id if category else None

    account_id = from_account_id or goal.account_id
    if account_id is None:
        raise ServiceError("Choose the account the money comes from.")

    return create_transaction(session, {
        "txn_date": on_date,
        "description": label,
        "amount": amount,
        "kind": TxnKind.EXPENSE.value,
        "status": TxnStatus.COMPLETED.value,
        "account_id": account_id,
        "category_id": category_id,
        "goal_id": goal_id,
    }, allow_duplicate=allow_duplicate)


# --------------------------------------------------------------------------
# Progress
# --------------------------------------------------------------------------
def progress_for(session: Session, goal: Goal, *, today: Optional[date] = None,
                 period: Optional[Period] = None) -> GoalProgress:
    settings = settings_snapshot(session)
    today = today or date.today()
    period = period or settings.current_period(today)
    return compute_progress(
        goal_id=goal.id,
        name=goal.name,
        target_amount=goal.target_amount,
        current_amount=current_amount(session, goal),
        planned_monthly=goal.planned_monthly,
        target_date=goal.target_date,
        average_monthly=average_monthly_contribution(session, goal.id, today=today),
        actual_last_period=contributions_in_period(session, goal.id, period),
        status=goal.status,
        today=today,
    )


def all_progress(session: Session, *, today: Optional[date] = None,
                 statuses: Optional[Sequence[str]] = None) -> list[GoalProgress]:
    goals = list_goals(session, statuses=statuses or [GoalStatus.ACTIVE.value])
    return [progress_for(session, goal, today=today) for goal in goals]


def earmarked_in_cash(session: Session, *, today: Optional[date] = None) -> Decimal:
    """Goal money that physically sits in a cash-like account.

    This is the amount that must be subtracted from raw cash before asking
    "how much do I have left to budget?" — otherwise the emergency fund gets
    re-allocated every single month.
    """
    from constants import CASH_ACCOUNT_TYPES
    from database.models import Account

    cash_ids = {
        row[0] for row in session.execute(
            select(Account.id, Account.type, Account.include_in_cash)
        ).all()
        if row[1] in CASH_ACCOUNT_TYPES and bool(row[2])
    }
    total = ZERO
    for goal in list_goals(session, statuses=[GoalStatus.ACTIVE.value]):
        if goal.account_id is not None and goal.account_id not in cash_ids:
            continue
        total = money(total + current_amount(session, goal))
    return total


def totals(session: Session, *, today: Optional[date] = None) -> dict[str, Decimal]:
    progresses = all_progress(session, today=today)
    return {
        "target": money_sum(p.target_amount for p in progresses),
        "saved": money_sum(p.current_amount for p in progresses),
        "remaining": money_sum(p.remaining for p in progresses),
        "planned_monthly": money_sum(p.planned_monthly for p in progresses),
        "required_monthly": money_sum(p.required_monthly for p in progresses),
        "count": Decimal(len(progresses)),
    }


def alerts(session: Session, *, today: Optional[date] = None) -> list[tuple[str, str]]:
    return goal_alerts(all_progress(session, today=today))


def suggest_distribution(session: Session, available: Decimal,
                         *, today: Optional[date] = None) -> list[tuple[GoalProgress, Decimal]]:
    """Split spare money across goals by urgency."""
    return prioritise(all_progress(session, today=today), available)


def auto_close_achieved(session: Session, *, today: Optional[date] = None) -> list[str]:
    """Flip fully-funded goals to 'achieved'. Returns the names changed."""
    changed: list[str] = []
    for goal in active_goals(session):
        if current_amount(session, goal) >= goal.target_amount:
            goal.status = GoalStatus.ACHIEVED.value
            changed.append(goal.name)
    if changed:
        session.flush()
    return changed
