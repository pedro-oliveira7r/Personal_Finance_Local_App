"""Accounts, balances and manual valuations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from calculations.cashflow import (
    AccountInfo,
    CashTxn,
    account_balance,
    credit_utilisation,
)
from calculations.money import ZERO, D, money, money_sum
from calculations.periods import Period
from constants import (
    ACCOUNT_TYPE_LABELS,
    ASSET_ACCOUNT_TYPES,
    CASH_ACCOUNT_TYPES,
    LIABILITY_ACCOUNT_TYPES,
    AccountType,
    BalanceMode,
)
from database.models import Account, AccountValuation, Debt, Goal, Transaction
from schemas.validation import AccountIn
from services.common import (
    ConflictError,
    NotFoundError,
    ServiceError,
    apply_fields,
    load_account_infos,
    load_cash_txns,
    send_to_recycle_bin,
)


@dataclass
class AccountBalanceView:
    """One row of the Accounts screen."""

    account: Account
    info: AccountInfo
    balance: Decimal              # signed: assets +, liabilities −
    utilisation_pct: Optional[Decimal] = None
    movement_this_period: Decimal = ZERO

    @property
    def id(self) -> int:
        return self.account.id

    @property
    def name(self) -> str:
        return self.account.name

    @property
    def type_label(self) -> str:
        return ACCOUNT_TYPE_LABELS.get(self.account.type, self.account.type)

    @property
    def is_liability(self) -> bool:
        return self.info.is_liability

    @property
    def display_balance(self) -> Decimal:
        """What the user should read: positive amount owed for liabilities."""
        return money(-self.balance) if self.is_liability else money(self.balance)

    @property
    def is_negative(self) -> bool:
        """Overdrawn asset, or a liability with a balance owed."""
        return self.balance < 0 if not self.is_liability else self.display_balance > 0

    @property
    def available_credit(self) -> Optional[Decimal]:
        limit = self.account.credit_limit
        if not limit:
            return None
        return money(limit - self.display_balance)


def list_accounts(session: Session, *, include_archived: bool = False,
                  types: Optional[Sequence[str]] = None) -> list[Account]:
    stmt = select(Account).order_by(Account.sort_order, Account.name)
    if not include_archived:
        stmt = stmt.where(Account.is_archived.is_(False))
    if types:
        stmt = stmt.where(Account.type.in_(list(types)))
    return list(session.execute(stmt).scalars())


def get_account(session: Session, account_id: int) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise NotFoundError(f"Account #{account_id} was not found.")
    return account


def find_by_name(session: Session, name: str) -> Optional[Account]:
    return session.execute(
        select(Account).where(func.lower(Account.name) == (name or "").strip().lower())
    ).scalars().first()


def create_account(session: Session, payload: dict[str, Any]) -> Account:
    data = AccountIn(**payload)
    if find_by_name(session, data.name):
        raise ConflictError(f"An account called “{data.name}” already exists.")
    account = Account(**data.model_dump())
    session.add(account)
    session.flush()
    return account


def update_account(session: Session, account_id: int, payload: dict[str, Any]) -> Account:
    account = get_account(session, account_id)
    merged = {
        "name": account.name,
        "type": account.type,
        "currency": account.currency,
        "opening_balance": account.opening_balance,
        "opening_date": account.opening_date,
        "balance_mode": account.balance_mode,
        "credit_limit": account.credit_limit,
        "interest_rate": account.interest_rate,
        "statement_day": account.statement_day,
        "due_day": account.due_day,
        "institution": account.institution,
        "color": account.color,
        "icon": account.icon,
        "notes": account.notes,
        "include_in_net_worth": account.include_in_net_worth,
        "include_in_cash": account.include_in_cash,
        "sort_order": account.sort_order,
    }
    merged.update(payload)
    data = AccountIn(**merged)
    clash = find_by_name(session, data.name)
    if clash is not None and clash.id != account_id:
        raise ConflictError(f"Another account is already called “{data.name}”.")
    apply_fields(account, data.model_dump())
    session.flush()
    return account


def usage_count(session: Session, account_id: int) -> dict[str, int]:
    return {
        "transactions": session.execute(
            select(func.count(Transaction.id)).where(
                (Transaction.account_id == account_id)
                | (Transaction.to_account_id == account_id)
            )
        ).scalar() or 0,
        "goals": session.execute(
            select(func.count(Goal.id)).where(Goal.account_id == account_id)
        ).scalar() or 0,
        "debts": session.execute(
            select(func.count(Debt.id)).where(Debt.account_id == account_id)
        ).scalar() or 0,
    }


def archive_account(session: Session, account_id: int, archived: bool = True) -> Account:
    account = get_account(session, account_id)
    account.is_archived = archived
    session.flush()
    return account


def delete_account(session: Session, account_id: int, *, force: bool = False) -> dict[str, Any]:
    account = get_account(session, account_id)
    usage = usage_count(session, account_id)
    if usage["transactions"] and not force:
        raise ConflictError(
            f"“{account.name}” has {usage['transactions']} transaction(s). "
            "Archive it to keep the history, or confirm deletion to detach them."
        )
    send_to_recycle_bin(session, "account", account, label=account.name)
    session.delete(account)
    session.flush()
    return {"deleted": account_id, "usage": usage}


# --------------------------------------------------------------------------
# Balances
# --------------------------------------------------------------------------
def balance_views(
    session: Session,
    *,
    as_of: Optional[date] = None,
    include_archived: bool = False,
    include_planned: bool = False,
    period: Optional[Period] = None,
) -> list[AccountBalanceView]:
    accounts = list_accounts(session, include_archived=include_archived)
    infos = {info.id: info for info in load_account_infos(session)}
    txns = load_cash_txns(session)
    views: list[AccountBalanceView] = []
    for account in accounts:
        info = infos.get(account.id)
        if info is None:
            continue
        balance = account_balance(info, txns, as_of=as_of, include_planned=include_planned)
        view = AccountBalanceView(
            account=account,
            info=info,
            balance=balance,
            utilisation_pct=credit_utilisation(info, balance),
        )
        if period is not None:
            opening = account_balance(info, txns, as_of=period.start - timedelta(days=1))
            closing = account_balance(info, txns, as_of=period.end)
            view.movement_this_period = money(closing - opening)
        views.append(view)
    return views


@dataclass
class AccountTotals:
    cash: Decimal = ZERO
    assets: Decimal = ZERO
    liabilities: Decimal = ZERO
    net_worth: Decimal = ZERO
    investments: Decimal = ZERO
    savings: Decimal = ZERO
    credit_used: Decimal = ZERO
    credit_limit: Decimal = ZERO


def totals(views: Sequence[AccountBalanceView]) -> AccountTotals:
    result = AccountTotals()
    for view in views:
        if not view.account.include_in_net_worth:
            continue
        if view.is_liability:
            result.liabilities = money(result.liabilities + view.display_balance)
            if view.account.type == AccountType.CREDIT_CARD.value:
                result.credit_used = money(result.credit_used + view.display_balance)
                if view.account.credit_limit:
                    result.credit_limit = money(result.credit_limit + view.account.credit_limit)
        else:
            result.assets = money(result.assets + view.balance)
            if view.info.is_cash_like:
                result.cash = money(result.cash + view.balance)
            if view.account.type == AccountType.INVESTMENT.value:
                result.investments = money(result.investments + view.balance)
            if view.account.type == AccountType.SAVINGS.value:
                result.savings = money(result.savings + view.balance)
    result.net_worth = money(result.assets - result.liabilities)
    return result


def cash_accounts(session: Session) -> list[Account]:
    return list_accounts(session, types=CASH_ACCOUNT_TYPES)


def liability_accounts(session: Session) -> list[Account]:
    return list_accounts(session, types=LIABILITY_ACCOUNT_TYPES)


# --------------------------------------------------------------------------
# Manual valuations
# --------------------------------------------------------------------------
def add_valuation(session: Session, account_id: int, value: Decimal,
                  as_of: Optional[date] = None, notes: Optional[str] = None) -> AccountValuation:
    account = get_account(session, account_id)
    as_of = as_of or date.today()
    amount = money(value)
    if amount < 0:
        raise ServiceError("A valuation cannot be negative — enter the amount owed as a positive "
                           "number for liabilities.")
    existing = session.execute(
        select(AccountValuation).where(
            AccountValuation.account_id == account_id,
            AccountValuation.as_of_date == as_of,
        )
    ).scalars().first()
    if existing is not None:
        existing.value = amount
        existing.notes = notes
        session.flush()
        return existing
    valuation = AccountValuation(
        account_id=account_id, as_of_date=as_of, value=amount, notes=notes
    )
    session.add(valuation)
    if account.balance_mode != BalanceMode.MANUAL.value:
        account.balance_mode = BalanceMode.MANUAL.value
    session.flush()
    return valuation


def list_valuations(session: Session, account_id: int) -> list[AccountValuation]:
    return list(session.execute(
        select(AccountValuation)
        .where(AccountValuation.account_id == account_id)
        .order_by(AccountValuation.as_of_date.desc())
    ).scalars())


def delete_valuation(session: Session, valuation_id: int) -> None:
    valuation = session.get(AccountValuation, valuation_id)
    if valuation is None:
        raise NotFoundError("That valuation no longer exists.")
    session.delete(valuation)
    session.flush()


# --------------------------------------------------------------------------
# Interest accrual
# --------------------------------------------------------------------------
INTEREST_TAG = "interest-accrual"


def accrue_interest(
    session: Session,
    account_id: int,
    *,
    through: Optional[date] = None,
    since: Optional[date] = None,
    day_of_month: int = 1,
    category_id: Optional[int] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Post the monthly interest a loan or card has been charging you.

    Balances derived purely from payments drift optimistically — a financed car
    does not shrink by the payment amount, it shrinks by the payment *minus*
    interest. This walks month by month from the account's opening date (or the
    last accrual) to ``through``, computing interest on the balance as it stood
    at each posting date. Idempotent: an accrual already posted for a month is
    never posted twice.

    Accrued interest is flagged ``exclude_from_budget`` — it is a balance-sheet
    mechanic, and the money is already inside the payment you budgeted. Leaving
    it in the budget would count the same cost twice.
    """
    from calculations.cashflow import account_balance
    from calculations.periods import safe_date, shift_date_months
    from constants import CategoryKind, TxnKind, TxnStatus
    from database.models import Category, Transaction
    from services.transaction_service import fingerprint

    account = get_account(session, account_id)
    if not account.interest_rate or account.interest_rate <= 0:
        raise ServiceError(
            f"“{account.name}” has no interest rate set — add one before accruing interest."
        )
    through = through or date.today()
    monthly_rate = D(account.interest_rate) / Decimal(1200)

    if category_id is None:
        kind = CategoryKind.DEBT.value if account.type in LIABILITY_ACCOUNT_TYPES \
            else CategoryKind.INCOME.value
        category = session.execute(
            select(Category).where(Category.kind == kind, Category.parent_id.is_(None))
            .order_by(Category.sort_order)
        ).scalars().first()
        category_id = category.id if category else None

    existing = {
        txn.occurrence_key
        for txn in session.execute(
            select(Transaction).where(
                Transaction.account_id == account_id,
                Transaction.occurrence_key.is_not(None),
                Transaction.tags.like(f"%{INTEREST_TAG}%"),
            )
        ).scalars().unique()
    }

    infos = {info.id: info for info in load_account_infos(session)}
    info = infos[account_id]
    is_liability = account.type in LIABILITY_ACCOUNT_TYPES

    posted: list[dict[str, Any]] = []
    start_from = since or account.opening_date
    cursor = safe_date(start_from.year, start_from.month, day_of_month)
    if cursor < start_from:
        cursor = shift_date_months(cursor, 1, day=day_of_month)

    while cursor <= through:
        key = f"interest-{cursor.isoformat()}"
        if key in existing:
            cursor = shift_date_months(cursor, 1, day=day_of_month)
            continue
        txns = load_cash_txns(session)
        balance = account_balance(info, txns, as_of=cursor - timedelta(days=1))
        base = abs(balance)
        interest = money(base * monthly_rate)
        if interest <= 0:
            cursor = shift_date_months(cursor, 1, day=day_of_month)
            continue
        description = f"Interest · {account.name} · {cursor.strftime('%m/%Y')}"
        if not dry_run:
            session.add(Transaction(
                txn_date=cursor,
                actual_date=cursor,
                description=description[:240],
                amount=interest,
                kind=TxnKind.EXPENSE.value if is_liability else TxnKind.INCOME.value,
                status=TxnStatus.COMPLETED.value,
                category_id=category_id,
                account_id=account_id,
                occurrence_key=key,
                tags=INTEREST_TAG,
                is_planned=True,
                exclude_from_budget=True,
                fingerprint=fingerprint(cursor, interest, description,
                                        account_id, TxnKind.EXPENSE.value),
            ))
            session.flush()
        posted.append({"date": cursor, "amount": interest, "balance_before": base})
        cursor = shift_date_months(cursor, 1, day=day_of_month)

    return {
        "account": account.name,
        "posted": len(posted),
        "total": money_sum(item["amount"] for item in posted),
        "entries": posted,
        "dry_run": dry_run,
    }


def interest_bearing_accounts(session: Session) -> list[Account]:
    return [
        account for account in list_accounts(session)
        if account.interest_rate and account.interest_rate > 0
    ]


def options_for_select(session: Session, *, include_archived: bool = False,
                       types: Optional[Sequence[str]] = None) -> list[tuple[int, str]]:
    return [
        (account.id, f"{account.icon + ' ' if account.icon else ''}{account.name}")
        for account in list_accounts(session, include_archived=include_archived, types=types)
    ]
