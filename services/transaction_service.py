"""Transactions: create, edit, search, complete, delete, undo.

Two safety features are built in rather than bolted on:

**Duplicate protection** — every transaction carries a ``fingerprint`` of its
date, amount, normalised description and account. Before inserting, near-matches
are surfaced so the user confirms rather than discovering the double entry three
weeks later.

**Soft delete** — deleting sets ``deleted_at`` and files a recycle-bin entry.
Nothing is physically removed until the user purges it, so a mis-click is always
reversible.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from calculations.money import ZERO, D, money, money_sum
from calculations.periods import Period
from constants import CategoryKind, TxnKind, TxnStatus
from database.models import Account, Category, Transaction, utcnow
from schemas.validation import TransactionIn, TransferIn
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

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def normalise_description(text: str) -> str:
    """Lower-case, strip accents and punctuation — for fingerprinting only."""
    raw = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(ch for ch in raw if not unicodedata.combining(ch))
    cleaned = _PUNCT.sub(" ", stripped.lower())
    return _WHITESPACE.sub(" ", cleaned).strip()


def fingerprint(txn_date: date, amount: Decimal, description: str,
                account_id: Optional[int], kind: str,
                to_amount: Optional[Decimal] = None) -> str:
    """Duplicate-detection key.

    ``to_amount`` is appended **only when it exists**. Appending it
    unconditionally — even as an empty string — adds a separator and changes
    the hash of every row already on file, which would silently switch off
    duplicate detection across the user's whole history.
    """
    parts = [
        txn_date.isoformat(),
        f"{money(amount):.2f}",
        normalise_description(description),
        str(account_id or ""),
        kind,
    ]
    if to_amount is not None:
        parts.append(f"{money(to_amount):.2f}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


# ==========================================================================
# Filtering
# ==========================================================================
@dataclass
class TxnFilter:
    start: Optional[date] = None
    end: Optional[date] = None
    kinds: Optional[Sequence[str]] = None
    statuses: Optional[Sequence[str]] = None
    category_ids: Optional[Sequence[int]] = None
    account_ids: Optional[Sequence[int]] = None
    #: Narrow to transactions whose **source** account holds this currency.
    #: That is the denomination of ``amount``, which is what every caller sums.
    currency: Optional[str] = None
    goal_id: Optional[int] = None
    debt_id: Optional[int] = None
    rule_id: Optional[int] = None
    search: Optional[str] = None
    tags: Optional[Sequence[str]] = None
    min_amount: Optional[Decimal] = None
    max_amount: Optional[Decimal] = None
    planned_flag: Optional[bool] = None      # is_planned column
    include_deleted: bool = False
    only_deleted: bool = False
    use_effective_date: bool = False
    limit: Optional[int] = None
    offset: int = 0
    order_desc: bool = True

    @classmethod
    def for_period(cls, period: Period, **kwargs) -> "TxnFilter":
        return cls(start=period.start, end=period.end, **kwargs)


def _build_stmt(flt: TxnFilter, *, count_only: bool = False):
    date_col = (
        func.coalesce(Transaction.actual_date, Transaction.txn_date)
        if flt.use_effective_date else Transaction.txn_date
    )
    stmt = select(func.count(Transaction.id)) if count_only else select(Transaction)

    if flt.only_deleted:
        stmt = stmt.where(Transaction.deleted_at.is_not(None))
    elif not flt.include_deleted:
        stmt = stmt.where(Transaction.deleted_at.is_(None))

    if flt.start is not None:
        stmt = stmt.where(date_col >= flt.start)
    if flt.end is not None:
        stmt = stmt.where(date_col <= flt.end)
    if flt.kinds:
        stmt = stmt.where(Transaction.kind.in_(list(flt.kinds)))
    if flt.statuses:
        stmt = stmt.where(Transaction.status.in_(list(flt.statuses)))
    if flt.category_ids:
        stmt = stmt.where(Transaction.category_id.in_(list(flt.category_ids)))
    if flt.account_ids:
        ids = list(flt.account_ids)
        stmt = stmt.where(or_(
            Transaction.account_id.in_(ids),
            Transaction.to_account_id.in_(ids),
        ))
    if flt.currency:
        stmt = stmt.where(
            Transaction.account_id.in_(
                select(Account.id).where(
                    func.upper(Account.currency) == flt.currency.upper())
            )
        )
    if flt.goal_id is not None:
        stmt = stmt.where(Transaction.goal_id == flt.goal_id)
    if flt.debt_id is not None:
        stmt = stmt.where(Transaction.debt_id == flt.debt_id)
    if flt.rule_id is not None:
        stmt = stmt.where(Transaction.rule_id == flt.rule_id)
    if flt.planned_flag is not None:
        stmt = stmt.where(Transaction.is_planned.is_(flt.planned_flag))
    # Comparisons pass Decimals so the Money type decorator converts to cents.
    if flt.min_amount is not None:
        stmt = stmt.where(Transaction.amount >= money(flt.min_amount))
    if flt.max_amount is not None:
        stmt = stmt.where(Transaction.amount <= money(flt.max_amount))
    if flt.search:
        needle = f"%{flt.search.strip().lower()}%"
        stmt = stmt.where(or_(
            func.lower(Transaction.description).like(needle),
            func.lower(func.coalesce(Transaction.notes, "")).like(needle),
            func.lower(func.coalesce(Transaction.tags, "")).like(needle),
        ))
    if flt.tags:
        conditions = [
            func.lower(func.coalesce(Transaction.tags, "")).like(f"%{tag.strip().lower()}%")
            for tag in flt.tags if tag and tag.strip()
        ]
        if conditions:
            stmt = stmt.where(or_(*conditions))

    if not count_only:
        order = date_col.desc() if flt.order_desc else date_col.asc()
        secondary = Transaction.id.desc() if flt.order_desc else Transaction.id.asc()
        stmt = stmt.order_by(order, secondary)
        if flt.limit:
            stmt = stmt.limit(flt.limit).offset(flt.offset or 0)
    return stmt


def list_transactions(session: Session, flt: Optional[TxnFilter] = None) -> list[Transaction]:
    return list(session.execute(_build_stmt(flt or TxnFilter())).scalars().unique())


def count_transactions(session: Session, flt: Optional[TxnFilter] = None) -> int:
    return session.execute(_build_stmt(flt or TxnFilter(), count_only=True)).scalar() or 0


def get_transaction(session: Session, txn_id: int) -> Transaction:
    txn = session.get(Transaction, txn_id)
    if txn is None:
        raise NotFoundError(f"Transaction #{txn_id} was not found.")
    return txn


# ==========================================================================
# Duplicate detection
# ==========================================================================
def find_duplicates(
    session: Session,
    txn_date: date,
    amount: Decimal,
    description: str,
    account_id: Optional[int],
    kind: str,
    *,
    day_window: int = 3,
    exclude_id: Optional[int] = None,
) -> list[Transaction]:
    """Transactions that look like the one about to be created.

    Exact fingerprint matches first, then same-amount/same-account entries
    within ``day_window`` days.
    """
    target = fingerprint(txn_date, amount, description, account_id, kind)
    exact_amount = money(amount)
    stmt = (
        select(Transaction)
        .where(Transaction.deleted_at.is_(None))
        .where(Transaction.kind == kind)
        .where(or_(
            Transaction.fingerprint == target,
            and_(
                Transaction.amount == exact_amount,
                Transaction.txn_date >= txn_date - timedelta(days=day_window),
                Transaction.txn_date <= txn_date + timedelta(days=day_window),
                Transaction.account_id == account_id,
            ),
        ))
        .order_by(Transaction.txn_date.desc())
        .limit(10)
    )
    if exclude_id:
        stmt = stmt.where(Transaction.id != exclude_id)
    matches = list(session.execute(stmt).scalars().unique())
    exact = [t for t in matches if t.fingerprint == target]
    near = [t for t in matches if t.fingerprint != target]
    return exact + near


# ==========================================================================
# Writes
# ==========================================================================
def _validate_relations(session: Session, data: TransactionIn) -> None:
    source = ensure_exists(session, Account, data.account_id, "Account")
    target = ensure_exists(session, Account, data.to_account_id, "Destination account")
    # Whether a transfer needs a second amount depends on the two accounts, so
    # it cannot live in the schema — that has no session to look them up with.
    if data.kind == TxnKind.TRANSFER.value and source is not None and target is not None:
        if source.currency != target.currency:
            if data.to_amount is None:
                raise ServiceError(
                    f"“{source.name}” holds {source.currency} and “{target.name}” holds "
                    f"{target.currency}. Enter the amount that arrived as well, so the "
                    f"rate can be recorded."
                )
        elif data.to_amount is not None and money(data.to_amount) != money(data.amount):
            raise ServiceError(
                f"Both accounts hold {source.currency}, so the amount that arrives "
                f"must match the amount sent."
            )
    if data.category_id:
        category = ensure_exists(session, Category, data.category_id, "Category")
        if category is not None:
            expected_income = category.kind == CategoryKind.INCOME.value
            if data.kind == TxnKind.INCOME.value and not expected_income:
                raise ServiceError(
                    f"“{category.full_name}” is not an income category. "
                    "Pick an income category or change the transaction type."
                )
            if data.kind == TxnKind.EXPENSE.value and expected_income:
                raise ServiceError(
                    f"“{category.full_name}” is an income category and cannot be used "
                    "for an expense."
                )


def create_transaction(
    session: Session,
    payload: dict[str, Any],
    *,
    allow_duplicate: bool = False,
    rule_id: Optional[int] = None,
    occurrence_key: Optional[str] = None,
    import_batch_id: Optional[int] = None,
) -> Transaction:
    data = TransactionIn(**payload)
    _validate_relations(session, data)

    if not allow_duplicate:
        duplicates = find_duplicates(
            session, data.txn_date, data.amount, data.description,
            data.account_id, data.kind,
        )
        exact = [t for t in duplicates
                 if t.fingerprint == fingerprint(
                     data.txn_date, data.amount, data.description,
                     data.account_id, data.kind, data.to_amount)]
        if exact:
            raise ConflictError(
                f"An identical transaction already exists on "
                f"{exact[0].txn_date.isoformat()} ({exact[0].description}). "
                "Confirm to save it anyway."
            )

    values = data.model_dump()
    txn = Transaction(
        **values,
        rule_id=rule_id,
        occurrence_key=occurrence_key,
        import_batch_id=import_batch_id,
        fingerprint=fingerprint(
            data.txn_date, data.amount, data.description, data.account_id,
            data.kind, data.to_amount,
        ),
    )
    session.add(txn)
    session.flush()
    return txn


def update_transaction(session: Session, txn_id: int, payload: dict[str, Any]) -> Transaction:
    txn = get_transaction(session, txn_id)
    merged = {
        "txn_date": txn.txn_date,
        "description": txn.description,
        "amount": txn.amount,
        "kind": txn.kind,
        "status": txn.status,
        "actual_date": txn.actual_date,
        "availability_date": txn.availability_date,
        "category_id": txn.category_id,
        "account_id": txn.account_id,
        "to_account_id": txn.to_account_id,
        # Omitting these would silently blank an FX transfer on any edit:
        # BaseIn ignores unknown keys, so nothing would complain.
        "to_amount": txn.to_amount,
        "fx_rate": txn.fx_rate,
        "goal_id": txn.goal_id,
        "debt_id": txn.debt_id,
        "payment_method": txn.payment_method,
        "tags": txn.tags,
        "notes": txn.notes,
        "is_planned": txn.is_planned,
        "exclude_from_budget": txn.exclude_from_budget,
    }
    merged.update(payload)
    data = TransactionIn(**merged)
    _validate_relations(session, data)
    apply_fields(txn, data.model_dump())
    txn.fingerprint = fingerprint(
        data.txn_date, data.amount, data.description, data.account_id,
        data.kind, data.to_amount,
    )
    session.flush()
    return txn


def create_transfer(session: Session, payload: dict[str, Any], *,
                    allow_duplicate: bool = False,
                    rule_id: Optional[int] = None,
                    occurrence_key: Optional[str] = None,
                    import_batch_id: Optional[int] = None) -> Transaction:
    """The one way to move money between accounts.

    It carries ``goal_id``/``debt_id``/``actual_date`` and the import and rule
    identifiers because their absence is exactly why every caller used to build
    the payload by hand — and hand-built payloads are how a cross-currency
    transfer ends up without the second amount that makes it correct.
    """
    data = TransferIn(**payload)
    return create_transaction(session, {
        "txn_date": data.txn_date,
        "description": data.description or "Transfer",
        "amount": data.amount,
        "to_amount": data.to_amount,
        "kind": TxnKind.TRANSFER.value,
        "status": data.status,
        "actual_date": data.actual_date,
        "account_id": data.from_account_id,
        "to_account_id": data.to_account_id,
        "goal_id": data.goal_id,
        "debt_id": data.debt_id,
        "payment_method": data.payment_method,
        "notes": data.notes,
    }, allow_duplicate=allow_duplicate, rule_id=rule_id,
       occurrence_key=occurrence_key, import_batch_id=import_batch_id)


def complete_transaction(session: Session, txn_id: int, *,
                         actual_date: Optional[date] = None,
                         actual_amount: Optional[Decimal] = None,
                         actual_to_amount: Optional[Decimal] = None) -> Transaction:
    """Mark a planned transaction as done, optionally correcting the amounts.

    Correcting one leg of an FX transfer without the other would leave
    ``fx_rate`` describing a trade that no longer matches its own amounts, so
    the rate is re-derived whenever either side moves.
    """
    txn = get_transaction(session, txn_id)
    if (txn.status == TxnStatus.COMPLETED.value
            and actual_amount is None and actual_to_amount is None):
        return txn
    txn.status = TxnStatus.COMPLETED.value
    txn.actual_date = actual_date or txn.actual_date or txn.txn_date
    if actual_amount is not None:
        amount = money(actual_amount)
        if amount <= 0:
            raise ServiceError("The actual amount must be greater than zero.")
        txn.amount = amount
    if actual_to_amount is not None:
        if txn.to_amount is None:
            raise ServiceError("This transfer does not cross currencies.")
        received = money(actual_to_amount)
        if received <= 0:
            raise ServiceError("The amount that arrived must be greater than zero.")
        txn.to_amount = received
    if txn.to_amount is not None and (actual_amount is not None
                                      or actual_to_amount is not None):
        from services.currency_service import derive_fx_rate

        txn.fx_rate = derive_fx_rate(txn.amount, txn.to_amount)
    txn.fingerprint = fingerprint(
        txn.txn_date, txn.amount, txn.description, txn.account_id, txn.kind,
        txn.to_amount,
    )
    session.flush()
    return txn


def revert_to_planned(session: Session, txn_id: int) -> Transaction:
    txn = get_transaction(session, txn_id)
    txn.status = TxnStatus.PLANNED.value
    txn.actual_date = None
    session.flush()
    return txn


def void_transaction(session: Session, txn_id: int) -> Transaction:
    """Keep the record but remove it from every calculation."""
    txn = get_transaction(session, txn_id)
    txn.status = TxnStatus.VOID.value
    session.flush()
    return txn


def delete_transaction(session: Session, txn_id: int) -> Transaction:
    """Soft delete — recoverable from the recycle bin."""
    txn = get_transaction(session, txn_id)
    if txn.deleted_at is None:
        txn.deleted_at = utcnow()
        send_to_recycle_bin(
            session, "transaction", txn,
            label=f"{txn.txn_date.isoformat()} · {txn.description} · {txn.amount}",
        )
    session.flush()
    return txn


def restore_transaction(session: Session, txn_id: int) -> Transaction:
    txn = get_transaction(session, txn_id)
    txn.deleted_at = None
    session.flush()
    return txn


def purge_deleted(session: Session, *, older_than_days: int = 0) -> int:
    """Physically remove soft-deleted transactions. Irreversible."""
    cutoff = utcnow() - timedelta(days=max(0, older_than_days))
    stmt = select(Transaction).where(
        Transaction.deleted_at.is_not(None),
        Transaction.deleted_at <= cutoff,
    )
    rows = list(session.execute(stmt).scalars().unique())
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)


def bulk_delete(session: Session, txn_ids: Sequence[int]) -> int:
    count = 0
    for txn_id in txn_ids:
        try:
            delete_transaction(session, txn_id)
            count += 1
        except NotFoundError:
            continue
    return count


def bulk_complete(session: Session, txn_ids: Sequence[int],
                  actual_date: Optional[date] = None) -> int:
    count = 0
    for txn_id in txn_ids:
        try:
            complete_transaction(session, txn_id, actual_date=actual_date)
            count += 1
        except (NotFoundError, ServiceError):
            continue
    return count


def bulk_recategorise(session: Session, txn_ids: Sequence[int], category_id: int) -> int:
    category = ensure_exists(session, Category, category_id, "Category")
    count = 0
    for txn_id in txn_ids:
        txn = session.get(Transaction, txn_id)
        if txn is None:
            continue
        if txn.kind == TxnKind.TRANSFER.value:
            continue
        if (txn.kind == TxnKind.INCOME.value) != (category.kind == CategoryKind.INCOME.value):
            continue
        txn.category_id = category_id
        count += 1
    session.flush()
    return count


# ==========================================================================
# Aggregations used by tracking / reporting
# ==========================================================================
@dataclass
class ActualsByCategory:
    period: Period
    by_category: dict[int, Decimal] = field(default_factory=dict)
    by_parent: dict[int, Decimal] = field(default_factory=dict)
    income_total: Decimal = ZERO
    expense_total: Decimal = ZERO
    savings_total: Decimal = ZERO
    investment_total: Decimal = ZERO
    debt_total: Decimal = ZERO
    uncategorised: Decimal = ZERO
    goal_contributions: dict[int, Decimal] = field(default_factory=dict)
    debt_payments: dict[int, Decimal] = field(default_factory=dict)


def actuals_for_period(
    session: Session,
    period: Period,
    *,
    include_planned: bool = False,
    use_effective_date: bool = True,
    currency: Optional[str] = None,
) -> ActualsByCategory:
    """Sum completed movements per category inside a period."""
    statuses = [TxnStatus.COMPLETED.value]
    if include_planned:
        statuses.append(TxnStatus.PLANNED.value)
    txns = list_transactions(session, TxnFilter(
        start=period.start, end=period.end, statuses=statuses,
        use_effective_date=use_effective_date, currency=currency,
    ))
    parents = {
        row[0]: row[1]
        for row in session.execute(select(Category.id, Category.parent_id)).all()
    }
    kinds = {
        row[0]: row[1]
        for row in session.execute(select(Category.id, Category.kind)).all()
    }

    result = ActualsByCategory(period=period)
    for txn in txns:
        if txn.exclude_from_budget:
            continue
        amount = money(txn.amount)

        # Goal and debt attribution is explicit user intent, so it counts even
        # when the money moved as a transfer (checking -> savings account).
        if txn.goal_id:
            result.goal_contributions[txn.goal_id] = money(
                result.goal_contributions.get(txn.goal_id, ZERO) + amount
            )
        if txn.debt_id:
            result.debt_payments[txn.debt_id] = money(
                result.debt_payments.get(txn.debt_id, ZERO) + amount
            )
        if txn.kind == TxnKind.TRANSFER.value:
            continue

        if txn.category_id is None:
            result.uncategorised = money(result.uncategorised + amount)
        else:
            result.by_category[txn.category_id] = money(
                result.by_category.get(txn.category_id, ZERO) + amount
            )
            root = parents.get(txn.category_id) or txn.category_id
            result.by_parent[root] = money(result.by_parent.get(root, ZERO) + amount)

        kind = kinds.get(txn.category_id) if txn.category_id else None
        if txn.kind == TxnKind.INCOME.value:
            result.income_total = money(result.income_total + amount)
        elif txn.category_id is None:
            # Kept only in ``uncategorised`` so the buckets stay disjoint and
            # callers can add them without double counting.
            pass
        else:
            if kind == CategoryKind.SAVINGS.value:
                result.savings_total = money(result.savings_total + amount)
            elif kind == CategoryKind.INVESTMENT.value:
                result.investment_total = money(result.investment_total + amount)
            elif kind == CategoryKind.DEBT.value:
                result.debt_total = money(result.debt_total + amount)
            else:
                result.expense_total = money(result.expense_total + amount)
    return result


def all_tags(session: Session) -> list[str]:
    rows = session.execute(
        select(Transaction.tags).where(
            Transaction.tags.is_not(None), Transaction.deleted_at.is_(None)
        )
    ).all()
    tags: set[str] = set()
    for (value,) in rows:
        for tag in (value or "").split(","):
            cleaned = tag.strip()
            if cleaned:
                tags.add(cleaned)
    return sorted(tags)


def recent_descriptions(session: Session, limit: int = 200) -> list[str]:
    rows = session.execute(
        select(Transaction.description)
        .where(Transaction.deleted_at.is_(None))
        .order_by(Transaction.id.desc())
        .limit(limit)
    ).all()
    seen: list[str] = []
    for (value,) in rows:
        if value and value not in seen:
            seen.append(value)
    return seen


def date_bounds(session: Session) -> tuple[Optional[date], Optional[date]]:
    row = session.execute(
        select(func.min(Transaction.txn_date), func.max(Transaction.txn_date))
        .where(Transaction.deleted_at.is_(None))
    ).first()
    return (row[0], row[1]) if row else (None, None)


def overdue_planned(session: Session, today: Optional[date] = None) -> list[Transaction]:
    """Planned transactions whose date has passed but were never completed."""
    today = today or date.today()
    return list_transactions(session, TxnFilter(
        end=today - timedelta(days=1),
        statuses=[TxnStatus.PLANNED.value],
        order_desc=False,
    ))


def upcoming_planned(session: Session, days: int = 30,
                     today: Optional[date] = None) -> list[Transaction]:
    today = today or date.today()
    return list_transactions(session, TxnFilter(
        start=today, end=today + timedelta(days=days),
        statuses=[TxnStatus.PLANNED.value],
        order_desc=False,
    ))
