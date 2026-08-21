"""CSV import with validation and preview, and CSV export.

Import is deliberately a three-step flow — **read, preview, commit** — because
silently merging a bank export into someone's finances is how people lose data.
Nothing is written until :func:`commit` is called, every row is validated
first, likely duplicates are flagged, and the whole import is recorded as a
batch that can be rolled back in one action.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from calculations.money import ZERO, money, money_sum, parse_money
from constants import CategoryKind, TxnKind, TxnStatus
from database.models import ImportBatch, Transaction, utcnow
from schemas.validation import ImportRowIn
from services import account_service, category_service
from services.common import ServiceError, category_name_map, settings_snapshot
from services.transaction_service import create_transaction, fingerprint, find_duplicates

#: Column aliases, English and Brazilian Portuguese, lower-cased.
COLUMN_ALIASES: dict[str, list[str]] = {
    "date": ["date", "data", "txn_date", "transaction date", "data da transação",
             "data lançamento", "data do lançamento", "posted date", "dt"],
    "description": ["description", "descrição", "descricao", "histórico", "historico",
                    "memo", "details", "detalhe", "lançamento", "lancamento", "payee"],
    "amount": ["amount", "valor", "value", "montante", "quantia", "total"],
    "kind": ["kind", "type", "tipo", "direction", "debit/credit", "d/c",
             "natureza", "income/expense"],
    "category": ["category", "categoria", "cat"],
    "subcategory": ["subcategory", "subcategoria", "sub category", "sub-categoria"],
    "account": ["account", "conta", "from account", "conta origem", "bank", "banco"],
    "to_account": ["to account", "conta destino", "destination", "destino"],
    # Cross-currency transfers need the far leg. Without it the destination is
    # credited with the source currency's magnitude, so the importer refuses
    # the row rather than inventing a rate for it.
    "to_amount": ["to amount", "amount received", "received", "valor recebido",
                  "valor destino", "credited"],
    "payment_method": ["payment method", "forma de pagamento", "método", "metodo",
                       "payment", "meio de pagamento"],
    "tags": ["tags", "etiquetas", "labels", "marcadores"],
    "notes": ["notes", "observações", "observacoes", "obs", "comment", "comentário"],
    "status": ["status", "situação", "situacao", "state"],
}

DATE_PATTERNS = [
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%Y/%m/%d", "%d/%m/%y", "%m/%d/%y", "%Y%m%d",
]

EXPORT_COLUMNS = [
    "date", "actual_date", "description", "amount", "currency", "kind", "status",
    "category", "account", "to_account", "to_amount", "fx_rate", "payment_method",
    "tags", "notes", "is_planned", "goal", "debt",
]


# ==========================================================================
# Reading
# ==========================================================================
def sniff_dialect(text: str) -> str:
    """Guess the delimiter — banks use commas, semicolons and tabs alike."""
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {sep: sample.count(sep) for sep in (";", ",", "\t", "|")}
        return max(counts, key=counts.get) if any(counts.values()) else ","


def decode(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_rows(raw: bytes | str) -> tuple[list[str], list[dict[str, str]]]:
    """Parse a CSV into headers and a list of dicts, nothing more."""
    text = decode(raw)
    if not text.strip():
        raise ServiceError("That file is empty.")
    delimiter = sniff_dialect(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [(name or "").strip() for name in (reader.fieldnames or [])]
    if not headers:
        raise ServiceError("No column headers were found in that file.")
    rows: list[dict[str, str]] = []
    for record in reader:
        cleaned = {
            (key or "").strip(): ("" if value is None else str(value).strip())
            for key, value in record.items()
        }
        if any(cleaned.values()):
            rows.append(cleaned)
    if not rows:
        raise ServiceError("The file has headers but no data rows.")
    return headers, rows


def detect_mapping(headers: Sequence[str]) -> dict[str, Optional[str]]:
    """Auto-match file columns to the fields the importer understands."""
    normalised = {header: header.strip().lower() for header in headers}
    mapping: dict[str, Optional[str]] = {}
    used: set[str] = set()
    for field_name, aliases in COLUMN_ALIASES.items():
        match = None
        for header, lowered in normalised.items():
            if header in used:
                continue
            if lowered in aliases:
                match = header
                break
        if match is None:
            for header, lowered in normalised.items():
                if header in used:
                    continue
                if any(alias in lowered for alias in aliases):
                    match = header
                    break
        if match is not None:
            mapping[field_name] = match
            used.add(match)
        else:
            mapping[field_name] = None
    return mapping


def parse_date(value: str, preferred: Optional[str] = None) -> Optional[date]:
    text = (value or "").strip()
    if not text:
        return None
    patterns = ([preferred] if preferred else []) + DATE_PATTERNS
    for pattern in patterns:
        if not pattern:
            continue
        try:
            return datetime.strptime(text[:10] if len(text) > 10 else text, pattern).date()
        except ValueError:
            continue
    # ISO timestamps
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


# ==========================================================================
# Preview
# ==========================================================================
@dataclass
class PreparedRow:
    index: int
    raw: dict[str, str]
    payload: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duplicate_of: Optional[int] = None
    duplicate_label: str = ""
    new_category: Optional[str] = None
    new_account: Optional[str] = None
    include: bool = True

    @property
    def is_valid(self) -> bool:
        return self.error is None

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None


@dataclass
class ImportPreview:
    source_name: str = ""
    mapping: dict[str, Optional[str]] = field(default_factory=dict)
    rows: list[PreparedRow] = field(default_factory=list)
    default_account_id: Optional[int] = None
    date_pattern: Optional[str] = None

    @property
    def valid_rows(self) -> list[PreparedRow]:
        return [row for row in self.rows if row.is_valid]

    @property
    def error_rows(self) -> list[PreparedRow]:
        return [row for row in self.rows if not row.is_valid]

    @property
    def duplicate_rows(self) -> list[PreparedRow]:
        return [row for row in self.rows if row.is_valid and row.is_duplicate]

    @property
    def importable(self) -> list[PreparedRow]:
        return [row for row in self.valid_rows if row.include]

    @property
    def new_categories(self) -> list[str]:
        return sorted({row.new_category for row in self.valid_rows if row.new_category})

    @property
    def new_accounts(self) -> list[str]:
        return sorted({row.new_account for row in self.valid_rows if row.new_account})

    @property
    def total_in(self) -> Decimal:
        return money_sum(
            row.payload["amount"] for row in self.importable
            if row.payload.get("kind") == TxnKind.INCOME.value
        )

    @property
    def total_out(self) -> Decimal:
        return money_sum(
            row.payload["amount"] for row in self.importable
            if row.payload.get("kind") == TxnKind.EXPENSE.value
        )

    def summary(self) -> str:
        return (
            f"{len(self.rows)} row(s) read · {len(self.valid_rows)} valid · "
            f"{len(self.error_rows)} with problems · "
            f"{len(self.duplicate_rows)} look like duplicates"
        )


def build_preview(
    session: Session,
    raw: bytes | str,
    *,
    source_name: str = "import.csv",
    mapping: Optional[dict[str, Optional[str]]] = None,
    default_account_id: Optional[int] = None,
    default_kind: Optional[str] = None,
    date_pattern: Optional[str] = None,
    negative_is_expense: bool = True,
    create_missing_categories: bool = False,
) -> ImportPreview:
    """Validate every row without writing anything.

    Amount sign handling: when the file has no explicit type column, a negative
    amount is read as an expense and a positive one as income (the convention
    almost every bank export follows). Flip ``negative_is_expense`` for files
    that do the opposite.
    """
    headers, records = read_rows(raw)
    mapping = mapping or detect_mapping(headers)
    settings = settings_snapshot(session)
    date_pattern = date_pattern or settings.date_pattern

    preview = ImportPreview(
        source_name=source_name, mapping=dict(mapping),
        default_account_id=default_account_id, date_pattern=date_pattern,
    )
    accounts = {a.name.strip().lower(): a for a in account_service.list_accounts(
        session, include_archived=True)}

    def column(record: dict[str, str], key: str) -> str:
        header = mapping.get(key)
        return record.get(header, "") if header else ""

    for index, record in enumerate(records, start=1):
        prepared = PreparedRow(index=index, raw=record)
        preview.rows.append(prepared)

        raw_date = column(record, "date")
        parsed_date = parse_date(raw_date, date_pattern)
        if parsed_date is None:
            prepared.error = f"Could not read the date “{raw_date or '(empty)'}”."
            continue

        raw_amount = column(record, "amount")
        amount = parse_money(raw_amount)
        if amount == 0:
            prepared.error = f"Amount “{raw_amount or '(empty)'}” is zero or unreadable."
            continue

        description = column(record, "description") or "(no description)"

        kind_text = column(record, "kind")
        kind: Optional[str] = None
        if kind_text:
            try:
                kind = ImportRowIn._kind(kind_text)
            except ValueError as exc:
                prepared.error = str(exc)
                continue
        if kind is None and default_kind:
            kind = default_kind
        if kind is None:
            if negative_is_expense:
                kind = TxnKind.EXPENSE.value if amount < 0 else TxnKind.INCOME.value
            else:
                kind = TxnKind.INCOME.value if amount < 0 else TxnKind.EXPENSE.value

        amount = abs(amount)

        account_name = column(record, "account")
        account_id = default_account_id
        if account_name:
            account = accounts.get(account_name.strip().lower())
            if account is not None:
                account_id = account.id
            else:
                prepared.new_account = account_name.strip()
        if account_id is None and prepared.new_account is None:
            prepared.error = "No account — choose a default account for this import."
            continue

        to_account_id = None
        to_amount = None
        to_name = column(record, "to_account")
        if kind == TxnKind.TRANSFER.value:
            target = accounts.get(to_name.strip().lower()) if to_name else None
            if target is None:
                prepared.error = (
                    f"Transfer needs a known destination account "
                    f"(“{to_name or '(empty)'}” was not found)."
                )
                continue
            to_account_id = target.id
            source = next((a for a in accounts.values() if a.id == account_id), None)
            if source is not None and source.currency != target.currency:
                raw_received = column(record, "to_amount")
                if not raw_received:
                    # Refuse rather than convert. A rate invented at import time
                    # is indistinguishable from a real one afterwards, and the
                    # error is permanent.
                    prepared.error = (
                        f"“{source.name}” holds {source.currency} and "
                        f"“{target.name}” holds {target.currency}. Add an "
                        f"“amount received” column so the rate can be recorded."
                    )
                    continue
                try:
                    to_amount = parse_money(raw_received)
                except Exception:
                    prepared.error = f"Could not read the amount received " \
                                     f"(“{raw_received}”)."
                    continue
                if to_amount <= 0:
                    prepared.error = "The amount received must be greater than zero."
                    continue

        category_id = None
        category_path = column(record, "category")
        subcategory = column(record, "subcategory")
        if subcategory:
            category_path = f"{category_path} › {subcategory}" if category_path else subcategory
        if category_path and kind != TxnKind.TRANSFER.value:
            wanted_kind = (CategoryKind.INCOME.value if kind == TxnKind.INCOME.value
                           else None)
            found = category_service.resolve_path(session, category_path, kind=wanted_kind)
            if found is not None and found.name.strip().lower() in {
                part.strip().lower() for part in category_path.replace("›", ">").split(">")
            }:
                category_id = found.id
            elif create_missing_categories:
                prepared.new_category = category_path.strip()
            else:
                prepared.new_category = category_path.strip()

        status_text = (column(record, "status") or "").strip().lower()
        status = TxnStatus.COMPLETED.value
        if status_text in {"planned", "planejado", "previsto", "pending", "scheduled"}:
            status = TxnStatus.PLANNED.value

        prepared.payload = {
            "txn_date": parsed_date,
            "description": description[:240],
            "amount": money(amount),
            "kind": kind,
            "status": status,
            "actual_date": parsed_date if status == TxnStatus.COMPLETED.value else None,
            "category_id": category_id,
            "account_id": account_id,
            "to_account_id": to_account_id,
            "to_amount": to_amount,
            "payment_method": column(record, "payment_method") or None,
            "tags": column(record, "tags") or None,
            "notes": column(record, "notes") or None,
            "is_planned": status == TxnStatus.PLANNED.value,
        }

        if account_id is not None:
            matches = find_duplicates(
                session, parsed_date, money(amount), description, account_id, kind
            )
            exact = fingerprint(parsed_date, money(amount), description, account_id, kind)
            hit = next((txn for txn in matches if txn.fingerprint == exact), None)
            if hit is not None:
                prepared.duplicate_of = hit.id
                prepared.duplicate_label = (
                    f"{hit.txn_date.isoformat()} · {hit.description} · {hit.amount}"
                )
                prepared.include = False

    _flag_internal_duplicates(preview)
    return preview


def _flag_internal_duplicates(preview: ImportPreview) -> None:
    """Two identical rows inside the same file are also duplicates."""
    seen: dict[tuple, PreparedRow] = {}
    for row in preview.valid_rows:
        payload = row.payload
        key = (payload["txn_date"], payload["amount"], payload["description"].lower(),
               payload["account_id"], payload["kind"])
        first = seen.get(key)
        if first is None:
            seen[key] = row
            continue
        if row.duplicate_of is None:
            row.duplicate_label = f"Same as row {first.index} in this file"
            row.duplicate_of = -first.index
            row.include = False


# ==========================================================================
# Commit
# ==========================================================================
@dataclass
class ImportResult:
    batch_id: Optional[int] = None
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    created_categories: list[str] = field(default_factory=list)
    created_accounts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.imported} transaction(s) imported"]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.created_categories:
            parts.append(f"{len(self.created_categories)} new category(ies)")
        if self.created_accounts:
            parts.append(f"{len(self.created_accounts)} new account(s)")
        return ", ".join(parts)


def commit(
    session: Session,
    preview: ImportPreview,
    *,
    create_missing_categories: bool = True,
    create_missing_accounts: bool = False,
    include_duplicates: bool = False,
) -> ImportResult:
    """Write the previewed rows. Only rows with ``include`` set are written."""
    result = ImportResult()
    batch = ImportBatch(
        source_name=preview.source_name,
        row_count=len(preview.rows),
        mapping={key: value for key, value in preview.mapping.items() if value},
    )
    session.add(batch)
    session.flush()
    result.batch_id = batch.id

    from constants import AccountType

    for row in preview.rows:
        if not row.is_valid:
            result.failed += 1
            result.errors.append(f"Row {row.index}: {row.error}")
            continue
        if row.is_duplicate and not include_duplicates and not row.include:
            result.skipped += 1
            continue
        if not row.include and not include_duplicates:
            result.skipped += 1
            continue

        payload = dict(row.payload)

        if row.new_account and payload.get("account_id") is None:
            if not create_missing_accounts:
                result.skipped += 1
                continue
            account = account_service.create_account(session, {
                "name": row.new_account,
                "type": AccountType.CHECKING.value,
            })
            result.created_accounts.append(account.name)
            payload["account_id"] = account.id

        if row.new_category and payload.get("category_id") is None and create_missing_categories:
            wanted_kind = (CategoryKind.INCOME.value
                           if payload["kind"] == TxnKind.INCOME.value
                           else CategoryKind.EXPENSE.value)
            category = category_service.resolve_path(
                session, row.new_category, kind=wanted_kind, create_missing=True
            )
            if category is not None:
                payload["category_id"] = category.id
                result.created_categories.append(category.full_name)

        try:
            create_transaction(session, payload, allow_duplicate=True,
                               import_batch_id=batch.id)
            result.imported += 1
        except Exception as exc:  # keep going; report at the end
            result.failed += 1
            result.errors.append(f"Row {row.index}: {exc}")

    batch.imported_count = result.imported
    batch.skipped_count = result.skipped
    session.flush()
    return result


def rollback(session: Session, batch_id: int) -> int:
    """Undo an entire import. Transactions are soft-deleted, not destroyed."""
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise ServiceError("That import batch no longer exists.")
    rows = session.execute(
        select(Transaction).where(
            Transaction.import_batch_id == batch_id,
            Transaction.deleted_at.is_(None),
        )
    ).scalars().unique()
    count = 0
    for txn in rows:
        txn.deleted_at = utcnow()
        count += 1
    batch.rolled_back_at = utcnow()
    session.flush()
    return count


def list_batches(session: Session, limit: int = 25) -> list[ImportBatch]:
    return list(session.execute(
        select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(limit)
    ).scalars())


# ==========================================================================
# Export
# ==========================================================================
def transactions_to_csv(session: Session, transactions: Iterable[Transaction],
                        *, delimiter: str = ",") -> str:
    categories = category_name_map(session)
    all_accounts = account_service.list_accounts(session, include_archived=True)
    accounts = {a.id: a.name for a in all_accounts}
    account_currency = {a.id: a.currency for a in all_accounts}
    from database.models import Debt, Goal

    goals = {row[0]: row[1] for row in session.execute(select(Goal.id, Goal.name)).all()}
    debts = {row[0]: row[1] for row in session.execute(select(Debt.id, Debt.name)).all()}

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for txn in transactions:
        writer.writerow([
            txn.txn_date.isoformat(),
            txn.actual_date.isoformat() if txn.actual_date else "",
            txn.description,
            f"{txn.amount:.2f}",
            account_currency.get(txn.account_id, ""),
            txn.kind,
            txn.status,
            categories.get(txn.category_id, "") if txn.category_id else "",
            accounts.get(txn.account_id, "") if txn.account_id else "",
            accounts.get(txn.to_account_id, "") if txn.to_account_id else "",
            f"{txn.to_amount:.2f}" if txn.to_amount is not None else "",
            f"{txn.fx_rate:.6f}" if txn.fx_rate is not None else "",
            txn.payment_method or "",
            txn.tags or "",
            (txn.notes or "").replace("\n", " "),
            "yes" if txn.is_planned else "no",
            goals.get(txn.goal_id, "") if txn.goal_id else "",
            debts.get(txn.debt_id, "") if txn.debt_id else "",
        ])
    return buffer.getvalue()


def rows_to_csv(rows: Sequence[dict], columns: Optional[Sequence[str]] = None,
                *, delimiter: str = ",") -> str:
    if not rows:
        return ""
    columns = columns or list(rows[0].keys())
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_stringify(row.get(column, "")) for column in columns])
    return buffer.getvalue()


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def template_csv() -> str:
    """A ready-to-fill template with one example row."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["date", "description", "amount", "kind", "category",
                     "account", "payment_method", "tags", "notes", "status"])
    writer.writerow(["2026-08-05", "Supermarket", "-284,90", "expense",
                     "Food › Groceries", "Checking account", "Debit card",
                     "essential", "weekly shop", "completed"])
    writer.writerow(["2026-08-05", "Salary August", "9800.00", "income",
                     "Salary › Net salary", "Checking account", "Bank transfer",
                     "", "", "completed"])
    return buffer.getvalue()
