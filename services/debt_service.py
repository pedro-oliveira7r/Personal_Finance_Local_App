"""Debt tracking, payments and payoff scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from calculations.debt import (
    DebtInput,
    PayoffResult,
    StrategyResult,
    compare_extra_payment,
    debt_alerts,
    debt_input_from_orm,
    interest_for_month,
    minimum_viable_payment,
    project_debt,
    strategy_comparison,
)
from calculations.money import ZERO, D, money, money_sum
from calculations.periods import Period, month_diff
from constants import CategoryKind, DebtType, PayoffStrategy, TxnKind, TxnStatus
from database.models import Account, Category, Debt, Transaction
from schemas.validation import DebtIn
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


def list_debts(session: Session, *, active_only: bool = True) -> list[Debt]:
    stmt = select(Debt).order_by(Debt.name)
    if active_only:
        stmt = stmt.where(Debt.is_active.is_(True))
    return list(session.execute(stmt).scalars())


def get_debt(session: Session, debt_id: int) -> Debt:
    debt = session.get(Debt, debt_id)
    if debt is None:
        raise NotFoundError(f"Debt #{debt_id} was not found.")
    return debt


def create_debt(session: Session, payload: dict[str, Any]) -> Debt:
    data = DebtIn(**payload)
    ensure_exists(session, Account, data.account_id, "Account")
    ensure_exists(session, Category, data.category_id, "Category")
    clash = session.execute(
        select(Debt).where(func.lower(Debt.name) == data.name.lower())
    ).scalars().first()
    if clash is not None:
        raise ConflictError(f"A debt called “{data.name}” already exists.")
    if data.original_principal is None:
        data.original_principal = data.principal_balance
    debt = Debt(**data.model_dump())
    session.add(debt)
    session.flush()
    return debt


def update_debt(session: Session, debt_id: int, payload: dict[str, Any]) -> Debt:
    debt = get_debt(session, debt_id)
    merged = {name: getattr(debt, name) for name in DebtIn.model_fields if hasattr(debt, name)}
    merged.update(payload)
    data = DebtIn(**merged)
    clash = session.execute(
        select(Debt).where(func.lower(Debt.name) == data.name.lower(), Debt.id != debt_id)
    ).scalars().first()
    if clash is not None:
        raise ConflictError(f"Another debt is already called “{data.name}”.")
    apply_fields(debt, data.model_dump())
    session.flush()
    return debt


def delete_debt(session: Session, debt_id: int, *, force: bool = False) -> dict[str, Any]:
    debt = get_debt(session, debt_id)
    payments = session.execute(
        select(func.count(Transaction.id)).where(
            Transaction.debt_id == debt_id, Transaction.deleted_at.is_(None)
        )
    ).scalar() or 0
    if payments and not force:
        raise ConflictError(
            f"“{debt.name}” has {payments} recorded payment(s). Confirm to delete it "
            "— the transactions stay in your history."
        )
    send_to_recycle_bin(session, "debt", debt, label=debt.name)
    session.delete(debt)
    session.flush()
    return {"deleted": debt_id, "payments": payments}


# --------------------------------------------------------------------------
# Payments
# --------------------------------------------------------------------------
def accrued_interest_since(debt: Debt, until: date) -> Decimal:
    """Interest added between ``balance_as_of`` and ``until``, monthly compounded."""
    months = month_diff(debt.balance_as_of, until)
    if months <= 0 or D(debt.interest_rate) <= 0:
        return ZERO
    balance = D(debt.principal_balance)
    total = ZERO
    for _ in range(months):
        interest = interest_for_month(balance, debt.interest_rate)
        total = money(total + interest)
        balance = money(balance + interest)
    return total


def record_payment(
    session: Session,
    debt_id: int,
    amount: Decimal,
    *,
    on_date: Optional[date] = None,
    from_account_id: Optional[int] = None,
    principal_portion: Optional[Decimal] = None,
    description: Optional[str] = None,
    allow_duplicate: bool = False,
) -> dict[str, Any]:
    """Book a payment and move the balance.

    The balance moves by ``accrued interest − payment``. Supply
    ``principal_portion`` to use the exact figure from a statement instead of
    the app's estimate.
    """
    debt = get_debt(session, debt_id)
    amount = money(amount)
    if amount <= 0:
        raise ServiceError("A payment must be greater than zero.")
    on_date = on_date or date.today()

    interest = ZERO if principal_portion is not None else accrued_interest_since(debt, on_date)
    principal = money(principal_portion) if principal_portion is not None else money(amount - interest)
    new_balance = money(debt.principal_balance + interest - amount)
    if new_balance < 0:
        new_balance = ZERO

    label = description or f"Payment · {debt.name}"
    account_id = from_account_id
    if account_id is None:
        raise ServiceError("Choose the account the payment comes from.")

    if debt.account_id and debt.account_id != account_id:
        txn = create_transaction(session, {
            "txn_date": on_date,
            "description": label,
            "amount": amount,
            "kind": TxnKind.TRANSFER.value,
            "status": TxnStatus.COMPLETED.value,
            "account_id": account_id,
            "to_account_id": debt.account_id,
            "debt_id": debt_id,
        }, allow_duplicate=allow_duplicate)
    else:
        category_id = debt.category_id
        if category_id is None:
            category = session.execute(
                select(Category).where(
                    Category.kind == CategoryKind.DEBT.value,
                    Category.parent_id.is_(None),
                ).order_by(Category.sort_order)
            ).scalars().first()
            category_id = category.id if category else None
        txn = create_transaction(session, {
            "txn_date": on_date,
            "description": label,
            "amount": amount,
            "kind": TxnKind.EXPENSE.value,
            "status": TxnStatus.COMPLETED.value,
            "account_id": account_id,
            "category_id": category_id,
            "debt_id": debt_id,
        }, allow_duplicate=allow_duplicate)

    debt.principal_balance = new_balance
    debt.balance_as_of = on_date
    if new_balance == 0:
        debt.is_active = False
    session.flush()
    return {
        "transaction": txn,
        "interest_applied": interest,
        "principal_applied": principal,
        "new_balance": new_balance,
        "cleared": new_balance == 0,
    }


def payments_total(session: Session, debt_id: int,
                   period: Optional[Period] = None) -> Decimal:
    flt = TxnFilter(debt_id=debt_id, statuses=[TxnStatus.COMPLETED.value],
                    use_effective_date=True)
    if period is not None:
        flt.start, flt.end = period.start, period.end
    return money_sum(txn.amount for txn in list_transactions(session, flt))


def set_balance(session: Session, debt_id: int, balance: Decimal,
                as_of: Optional[date] = None) -> Debt:
    """Manually reconcile the balance against a statement."""
    debt = get_debt(session, debt_id)
    amount = money(balance)
    if amount < 0:
        raise ServiceError("A debt balance cannot be negative.")
    debt.principal_balance = amount
    debt.balance_as_of = as_of or date.today()
    if amount == 0:
        debt.is_active = False
    session.flush()
    return debt


# --------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------
def effective_balance(session: Session, debt: Debt,
                      *, as_of: Optional[date] = None) -> Decimal:
    """The balance to trust for this debt.

    When a debt is linked to a liability account, that account's balance is
    authoritative — it updates itself as charges and payments are recorded, so
    it cannot drift out of sync with the transaction history. Only debts
    without an account fall back to the manually-maintained figure.
    """
    if debt.account_id:
        from calculations.cashflow import account_balance

        from services.common import load_account_infos, load_cash_txns

        infos = {info.id: info for info in load_account_infos(session)}
        info = infos.get(debt.account_id)
        if info is not None:
            balance = account_balance(info, load_cash_txns(session), as_of=as_of)
            owed = -balance if balance < 0 else ZERO
            return money(owed)
    return money(debt.principal_balance)


def balances(session: Session, *, active_only: bool = True,
             as_of: Optional[date] = None) -> dict[int, Decimal]:
    return {
        debt.id: effective_balance(session, debt, as_of=as_of)
        for debt in list_debts(session, active_only=active_only)
    }


def inputs(session: Session, *, active_only: bool = True) -> list[DebtInput]:
    result: list[DebtInput] = []
    for debt in list_debts(session, active_only=active_only):
        item = debt_input_from_orm(debt)
        item.balance = effective_balance(session, debt)
        result.append(item)
    return result


def unlinked_total(session: Session) -> Decimal:
    """Debt not represented by an account — added to net worth separately."""
    return money_sum(
        debt.principal_balance
        for debt in list_debts(session)
        if not debt.account_id
    )


@dataclass
class DebtView:
    debt: Debt
    projection: PayoffResult
    balance: Decimal = ZERO
    paid_this_period: Decimal = ZERO
    monthly_interest: Decimal = ZERO
    linked_to_account: bool = False

    @property
    def progress_pct(self) -> Decimal:
        from calculations.money import pct_of

        original = self.debt.original_principal or self.balance
        if not original:
            return ZERO
        paid = money(original - self.balance)
        return pct_of(paid, original)


def views(session: Session, *, period: Optional[Period] = None,
          today: Optional[date] = None) -> list[DebtView]:
    today = today or date.today()
    result: list[DebtView] = []
    for debt in list_debts(session):
        balance = effective_balance(session, debt)
        item = debt_input_from_orm(debt)
        item.balance = balance
        result.append(DebtView(
            debt=debt,
            balance=balance,
            projection=project_debt(item, start_date=today),
            paid_this_period=payments_total(session, debt.id, period) if period else ZERO,
            monthly_interest=minimum_viable_payment(balance, debt.interest_rate),
            linked_to_account=bool(debt.account_id),
        ))
    return result


def totals(session: Session) -> dict[str, Decimal]:
    debts = list_debts(session)
    current = balances(session)
    return {
        "balance": money_sum(current.get(d.id, d.principal_balance) for d in debts),
        "unlinked_balance": unlinked_total(session),
        "minimum_payments": money_sum(d.minimum_payment for d in debts),
        "planned_payments": money_sum(
            (d.planned_payment or d.minimum_payment) + (d.extra_payment or ZERO) for d in debts),
        "monthly_interest": money_sum(
            minimum_viable_payment(current.get(d.id, d.principal_balance), d.interest_rate)
            for d in debts),
        "count": Decimal(len(debts)),
    }


def compare_strategies(session: Session, extra_pool: Decimal = ZERO,
                       today: Optional[date] = None) -> dict[str, StrategyResult]:
    return strategy_comparison(inputs(session), extra_pool=extra_pool,
                               start_date=today or date.today())


def extra_payment_scenario(session: Session, debt_id: int, extra: Decimal,
                           today: Optional[date] = None) -> dict[str, object]:
    debt = get_debt(session, debt_id)
    return compare_extra_payment(
        debt_input_from_orm(debt), extra, start_date=today or date.today()
    )


def alerts(session: Session) -> list[tuple[str, str]]:
    return debt_alerts(inputs(session))


def payoff_series(session: Session, months: int = 60,
                  today: Optional[date] = None) -> list[dict[str, Any]]:
    """Total debt balance month by month, for the trend chart."""
    today = today or date.today()
    projections = [project_debt(item, start_date=today) for item in inputs(session)]
    horizon = min(max(1, months), 600)
    series: list[dict[str, Any]] = []
    for index in range(horizon):
        total = ZERO
        for projection in projections:
            if projection.never_pays_off and not projection.schedule:
                total = money(total + projection.original_balance)
            elif index < len(projection.schedule):
                total = money(total + projection.schedule[index].closing_balance)
        series.append({"month_index": index + 1, "balance": total})
        if total == 0:
            break
    return series
