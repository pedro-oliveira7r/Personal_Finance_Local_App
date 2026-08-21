"""SQLAlchemy ORM model definitions.

Money is stored as an **integer number of cents** through :class:`Money`,
not as SQLite REAL. SQLite has no true decimal type, so persisting
``Numeric`` there silently round-trips through a C double and eventually
produces balances like ``1234.9999999999998``. Integer cents are exact.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from calculations.money import D, money
from constants import (
    AccountType,
    AllocationTarget,
    AvailabilityRule,
    BalanceMode,
    BudgetMethod,
    BusinessDayRule,
    CategoryKind,
    DebtType,
    Frequency,
    GoalStatus,
    GoalType,
    PeriodStatus,
    TxnKind,
    TxnStatus,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Money(TypeDecorator):
    """Exact monetary column backed by INTEGER cents."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Optional[int]:
        if value is None:
            return None
        return int(money(value).scaleb(2).to_integral_value())

    def process_result_value(self, value: Any, dialect: Any) -> Optional[Decimal]:
        if value is None:
            return None
        return (Decimal(int(value)) / Decimal(100)).quantize(Decimal("0.01"))


class Rate(TypeDecorator):
    """Percentage/rate column stored as INTEGER basis-points-of-a-percent.

    ``12.3456 %`` -> ``1234560``; six decimal places of headroom, exact.
    """

    impl = Integer
    cache_ok = True
    SCALE = Decimal(10) ** 6

    def process_bind_param(self, value: Any, dialect: Any) -> Optional[int]:
        if value is None:
            return None
        return int((D(value) * self.SCALE).to_integral_value())

    def process_result_value(self, value: Any, dialect: Any) -> Optional[Decimal]:
        if value is None:
            return None
        return Decimal(int(value)) / self.SCALE


class JSONText(TypeDecorator):
    """Small JSON blobs (seasonal factors, dashboard preferences...)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Optional[str]:
        if value is None:
            return None
        return json.dumps(value, default=str, ensure_ascii=False)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if not value:
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


# ==========================================================================
# Settings
# ==========================================================================
class AppSettings(Base, TimestampMixin):
    """Single-row table (``id == 1``) holding user preferences."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    base_currency: Mapped[str] = mapped_column(String(3), default="BRL", nullable=False)
    date_format: Mapped[str] = mapped_column(String(16), default="DD/MM/YYYY", nullable=False)
    show_cents: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    first_day_of_month: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    budget_method: Mapped[str] = mapped_column(
        String(24), default=BudgetMethod.ZERO_BASED.value, nullable=False
    )
    carry_over_surplus: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    income_availability_rule: Mapped[str] = mapped_column(
        String(24), default=AvailabilityRule.EARNED_PERIOD.value, nullable=False
    )
    income_cutoff_day: Mapped[int] = mapped_column(Integer, default=25, nullable=False)

    warning_threshold_pct: Mapped[Decimal] = mapped_column(Rate, default=Decimal("80"), nullable=False)
    critical_threshold_pct: Mapped[Decimal] = mapped_column(Rate, default=Decimal("100"), nullable=False)
    variance_tolerance_pct: Mapped[Decimal] = mapped_column(Rate, default=Decimal("5"), nullable=False)

    forecast_months: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    theme: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    backup_dir: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dashboard_prefs: Mapped[Optional[dict]] = mapped_column(JSONText, nullable=True)
    #: Currency codes this book uses, primary first. At most three; validated
    #: in :class:`schemas.validation.SettingsIn`. ``None`` on an old book means
    #: "just the primary" — the migration backfills it.
    active_currencies: Mapped[Optional[list]] = mapped_column(JSONText, nullable=True)

    onboarded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __table_args__ = (
        CheckConstraint("first_day_of_month BETWEEN 1 AND 28", name="ck_settings_first_day"),
        CheckConstraint("fiscal_year_start_month BETWEEN 1 AND 12", name="ck_settings_fiscal"),
        CheckConstraint("income_cutoff_day BETWEEN 1 AND 31", name="ck_settings_cutoff"),
    )


# ==========================================================================
# Categories
# ==========================================================================
class Category(Base, TimestampMixin):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    color: Mapped[Optional[str]] = mapped_column(String(9), nullable=True)
    icon: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    #: Eager-loaded (with a depth cap, since the tree is only two levels) so
    #: ``full_name`` still works on an instance whose session has closed — the
    #: UI reads rows outside the session that produced them.
    parent: Mapped[Optional["Category"]] = relationship(
        "Category", remote_side="Category.id", back_populates="children",
        lazy="joined", join_depth=2,
    )
    children: Mapped[list["Category"]] = relationship(
        "Category", back_populates="parent", cascade="save-update"
    )

    __table_args__ = (
        UniqueConstraint("name", "parent_id", "kind", name="uq_category_name_parent_kind"),
        CheckConstraint(
            "kind IN ('income','expense','savings','investment','debt')",
            name="ck_category_kind",
        ),
        Index("ix_category_kind", "kind"),
    )

    @property
    def full_name(self) -> str:
        """``"Food › Groceries"``. Degrades to the bare name if detached."""
        from sqlalchemy.orm.exc import DetachedInstanceError

        try:
            parent = self.parent
        except DetachedInstanceError:  # pragma: no cover - defensive
            return self.name
        return f"{parent.name} › {self.name}" if parent else self.name

    @property
    def is_subcategory(self) -> bool:
        return self.parent_id is not None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Category {self.id} {self.full_name} ({self.kind})>"


# ==========================================================================
# Accounts
# ==========================================================================
class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="BRL", nullable=False)

    opening_balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    opening_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)

    balance_mode: Mapped[str] = mapped_column(
        String(16), default=BalanceMode.TRANSACTIONS.value, nullable=False
    )

    credit_limit: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    interest_rate: Mapped[Optional[Decimal]] = mapped_column(Rate, nullable=True)
    statement_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    due_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    institution: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    color: Mapped[Optional[str]] = mapped_column(String(9), nullable=True)
    icon: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    include_in_net_worth: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    include_in_cash: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    valuations: Mapped[list["AccountValuation"]] = relationship(
        "AccountValuation", back_populates="account",
        cascade="all, delete-orphan", order_by="AccountValuation.as_of_date",
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('checking','savings','cash','investment','credit_card',"
            "'loan','other_asset','other_liability')",
            name="ck_account_type",
        ),
        Index("ix_account_type", "type"),
    )

    @property
    def is_liability(self) -> bool:
        from constants import LIABILITY_ACCOUNT_TYPES
        return self.type in LIABILITY_ACCOUNT_TYPES

    @property
    def is_cash_like(self) -> bool:
        from constants import CASH_ACCOUNT_TYPES
        return self.type in CASH_ACCOUNT_TYPES and self.include_in_cash

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Account {self.id} {self.name} ({self.type})>"


class AccountValuation(Base, TimestampMixin):
    """Point-in-time value for accounts whose balance is not transaction-driven
    (property, market-priced investments)."""

    __tablename__ = "account_valuations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal] = mapped_column(Money, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    account: Mapped["Account"] = relationship("Account", back_populates="valuations")

    __table_args__ = (
        UniqueConstraint("account_id", "as_of_date", name="uq_valuation_account_date"),
        Index("ix_valuation_date", "as_of_date"),
    )


# ==========================================================================
# Recurring rules
# ==========================================================================
class RecurringRule(Base, TimestampMixin):
    """A template that materialises planned transactions on a schedule.

    Handles the awkward real-world cases the spreadsheet could not: a salary
    that grows 5% every January, rent indexed once a year, an electricity bill
    that doubles in winter, insurance paid annually, a subscription whose price
    steps up every six months.
    """

    __tablename__ = "recurring_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: Denomination of ``amount``. Defaults to the book's primary currency.
    currency: Mapped[str] = mapped_column(String(3), default="BRL", nullable=False)

    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    to_account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    goal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("goals.id", ondelete="SET NULL"), nullable=True
    )
    debt_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("debts.id", ondelete="SET NULL"), nullable=True
    )

    frequency: Mapped[str] = mapped_column(String(20), nullable=False)
    interval: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    day_of_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    weekday: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 0=Mon
    month_of_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    max_occurrences: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    #: Percentage applied every ``growth_every_months`` months from
    #: ``growth_anchor_month`` (e.g. +5% each January).
    growth_pct: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0"), nullable=False)
    growth_every_months: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    growth_anchor_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    #: ``{"1": 1.4, "7": 0.8}`` -> January 40% higher, July 20% lower.
    seasonal_factors: Mapped[Optional[dict]] = mapped_column(JSONText, nullable=True)

    business_day_rule: Mapped[str] = mapped_column(
        String(24), default=BusinessDayRule.NONE.value, nullable=False
    )
    #: Days between the nominal due date and when cash really moves.
    settlement_offset_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    availability_rule: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)

    payment_method: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    description_template: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_generate: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    include_in_budget: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    generated_through: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    category: Mapped[Optional["Category"]] = relationship("Category")
    account: Mapped[Optional["Account"]] = relationship("Account", foreign_keys=[account_id])
    to_account: Mapped[Optional["Account"]] = relationship("Account", foreign_keys=[to_account_id])
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction", back_populates="rule", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint("kind IN ('income','expense','transfer')", name="ck_rule_kind"),
        CheckConstraint("interval >= 1", name="ck_rule_interval"),
        Index("ix_rule_active", "is_active"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RecurringRule {self.id} {self.name} {self.frequency}>"


# ==========================================================================
# Transactions
# ==========================================================================
class Transaction(Base, TimestampMixin):
    """One movement of money — planned or actually completed.

    ``amount`` is always a positive magnitude; direction comes from ``kind``.
    That removes an entire class of sign bugs from the reporting layer.
    """

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: The date the movement belongs to (accrual / expected date).
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: When the cash actually moved. ``None`` while still planned.
    actual_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    #: Explicit override of the period this money becomes budgetable in.
    availability_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    description: Mapped[str] = mapped_column(String(240), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: Magnitude ARRIVING at ``to_account_id``, in that account's currency.
    #: ``None`` means the two sides share a currency — the overwhelming case,
    #: and what keeps every pre-multi-currency row correct without a backfill.
    to_amount: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    #: ``amount / to_amount`` — source units per 1 destination unit. Derived and
    #: stored for the record; never used to reconstruct either amount.
    fx_rate: Mapped[Optional[Decimal]] = mapped_column(Rate, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=TxnStatus.COMPLETED.value, nullable=False
    )

    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    to_account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    goal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("goals.id", ondelete="SET NULL"), nullable=True
    )
    debt_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("debts.id", ondelete="SET NULL"), nullable=True
    )
    rule_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("recurring_rules.id", ondelete="SET NULL"), nullable=True
    )
    #: Stable per-rule occurrence identifier — the duplicate guard for the
    #: recurrence engine.
    occurrence_key: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    payment_method: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    #: Was this movement part of the plan, or a surprise?
    is_planned: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Excluded from budget maths (e.g. reimbursed expense) but kept on record.
    exclude_from_budget: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    import_batch_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("import_batches.id", ondelete="SET NULL"), nullable=True
    )
    fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    #: Soft delete — enables the undo/recycle-bin behaviour.
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    category: Mapped[Optional["Category"]] = relationship("Category", lazy="joined")
    account: Mapped[Optional["Account"]] = relationship(
        "Account", foreign_keys=[account_id], lazy="joined"
    )
    to_account: Mapped[Optional["Account"]] = relationship(
        "Account", foreign_keys=[to_account_id], lazy="joined"
    )
    rule: Mapped[Optional["RecurringRule"]] = relationship(
        "RecurringRule", back_populates="transactions"
    )

    __table_args__ = (
        CheckConstraint("kind IN ('income','expense','transfer')", name="ck_txn_kind"),
        CheckConstraint("status IN ('planned','completed','void')", name="ck_txn_status"),
        CheckConstraint("amount >= 0", name="ck_txn_amount_positive"),
        UniqueConstraint("rule_id", "occurrence_key", name="uq_txn_rule_occurrence"),
        Index("ix_txn_date", "txn_date"),
        Index("ix_txn_kind_status", "kind", "status"),
        Index("ix_txn_category", "category_id"),
        Index("ix_txn_account", "account_id"),
        Index("ix_txn_deleted", "deleted_at"),
        Index("ix_txn_fingerprint", "fingerprint"),
    )

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None and self.status != TxnStatus.VOID.value

    @property
    def is_completed(self) -> bool:
        return self.status == TxnStatus.COMPLETED.value and self.deleted_at is None

    @property
    def effective_date(self) -> date:
        """The date to use for cash-flow purposes."""
        return self.actual_date or self.txn_date

    @property
    def is_fx(self) -> bool:
        """True when the two sides of this transfer hold different currencies."""
        return self.kind == TxnKind.TRANSFER.value and self.to_amount is not None

    @property
    def signed_amount(self) -> Decimal:
        """Positive for income, negative for expense, zero-sum for transfers.

        Transfers stay zero even when :attr:`is_fx` — deliberately. A
        cross-currency transfer moves one magnitude out and a *different*
        magnitude in, denominated differently, and this property has no way to
        say which currency it would be returning. Render such a transfer as
        both legs (``R$ 1.000,00 → € 160,00``) rather than signing it here.
        """
        if self.kind == TxnKind.INCOME.value:
            return money(self.amount)
        if self.kind == TxnKind.EXPENSE.value:
            return -money(self.amount)
        return Decimal("0.00")

    @property
    def tag_list(self) -> list[str]:
        return [t.strip() for t in (self.tags or "").split(",") if t.strip()]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Transaction {self.id} {self.txn_date} {self.kind} {self.amount}>"


# ==========================================================================
# Budget periods & lines
# ==========================================================================
class BudgetPeriod(Base, TimestampMixin):
    __tablename__ = "budget_periods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=PeriodStatus.DRAFT.value, nullable=False
    )
    method: Mapped[str] = mapped_column(
        String(24), default=BudgetMethod.ZERO_BASED.value, nullable=False
    )
    #: Manual override of the cash carried into this period.
    opening_cash_override: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generated_from_rules_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    lines: Mapped[list["BudgetLine"]] = relationship(
        "BudgetLine", back_populates="period", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("year", "month", name="uq_period_year_month"),
        CheckConstraint("month BETWEEN 1 AND 12", name="ck_period_month"),
        CheckConstraint("status IN ('draft','active','closed')", name="ck_period_status"),
        Index("ix_period_ym", "year", "month"),
    )

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BudgetPeriod {self.key} {self.status}>"


class BudgetLine(Base, TimestampMixin):
    """One planned figure inside a period.

    ``kind == 'income'`` lines are *expected money in*; every other kind is an
    *allocation of that money out*. Zero-based budgeting is simply
    ``sum(income) + carry-in - sum(allocations) == 0``.
    """

    __tablename__ = "budget_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_id: Mapped[int] = mapped_column(
        ForeignKey("budget_periods.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target: Mapped[str] = mapped_column(
        String(16), default=AllocationTarget.EXPENSE.value, nullable=False
    )

    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), nullable=True
    )
    goal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("goals.id", ondelete="CASCADE"), nullable=True
    )
    debt_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("debts.id", ondelete="CASCADE"), nullable=True
    )
    account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    rule_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("recurring_rules.id", ondelete="SET NULL"), nullable=True
    )

    label: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    planned_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    #: Denomination of the money on this row.
    currency: Mapped[str] = mapped_column(String(3), default="BRL", nullable=False)
    expected_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    #: True once the user edits a value that was generated from a rule, so
    #: regeneration never stomps a deliberate override.
    is_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    period: Mapped["BudgetPeriod"] = relationship("BudgetPeriod", back_populates="lines")
    category: Mapped[Optional["Category"]] = relationship("Category", lazy="joined")

    __table_args__ = (
        CheckConstraint(
            "kind IN ('income','expense','savings','investment','debt')",
            name="ck_line_kind",
        ),
        UniqueConstraint(
            "period_id", "kind", "category_id", "goal_id", "debt_id", "label",
            name="uq_line_unique_target",
        ),
        Index("ix_line_period", "period_id"),
    )

    @property
    def display_label(self) -> str:
        from sqlalchemy.orm.exc import DetachedInstanceError

        if self.label:
            return self.label
        try:
            category = self.category
        except DetachedInstanceError:  # pragma: no cover - defensive
            return "(unnamed line)"
        return category.full_name if category is not None else "(unnamed line)"


# ==========================================================================
# Goals
# ==========================================================================
class Goal(Base, TimestampMixin):
    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    goal_type: Mapped[str] = mapped_column(
        String(32), default=GoalType.CUSTOM.value, nullable=False
    )
    target_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: Money already set aside before the app started tracking.
    starting_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    planned_monthly: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    #: Denomination of the money on this row.
    currency: Mapped[str] = mapped_column(String(3), default="BRL", nullable=False)
    target_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    start_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)

    account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )

    priority: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=GoalStatus.ACTIVE.value, nullable=False
    )
    color: Mapped[Optional[str]] = mapped_column(String(9), nullable=True)
    icon: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    account: Mapped[Optional["Account"]] = relationship("Account")

    __table_args__ = (
        CheckConstraint("target_amount > 0", name="ck_goal_target_positive"),
        CheckConstraint("priority BETWEEN 1 AND 5", name="ck_goal_priority"),
        Index("ix_goal_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Goal {self.id} {self.name}>"


# ==========================================================================
# Debts
# ==========================================================================
class Debt(Base, TimestampMixin):
    __tablename__ = "debts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    debt_type: Mapped[str] = mapped_column(
        String(32), default=DebtType.OTHER.value, nullable=False
    )

    original_principal: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    principal_balance: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: Nominal annual interest rate in percent (e.g. ``12.5``).
    interest_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0"), nullable=False)
    minimum_payment: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    planned_payment: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    extra_payment: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    #: Denomination of the money on this row.
    currency: Mapped[str] = mapped_column(String(3), default="BRL", nullable=False)
    due_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )

    opened_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    balance_as_of: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Whether the payment gets its own budget allocation. Turn this OFF for a
    #: revolving credit card whose *spending* is already budgeted by category —
    #: otherwise the same money is allocated twice (once as groceries, once as
    #: "card payment").
    include_in_budget: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    account: Mapped[Optional["Account"]] = relationship("Account")

    __table_args__ = (
        CheckConstraint("principal_balance >= 0", name="ck_debt_balance_non_negative"),
        Index("ix_debt_active", "is_active"),
    )

    @property
    def monthly_rate(self) -> Decimal:
        return D(self.interest_rate) / Decimal(1200)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Debt {self.id} {self.name} {self.principal_balance}>"


# ==========================================================================
# Net worth
# ==========================================================================
class NetWorthSnapshot(Base, TimestampMixin):
    __tablename__ = "net_worth_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Not unique on its own any more — one row per currency per date. A
    #: snapshot is a record of what each currency was worth, not a single
    #: converted figure that would silently re-value whenever a rate moved.
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="BRL", nullable=False)
    total_assets: Mapped[Decimal] = mapped_column(Money, nullable=False)
    total_liabilities: Mapped[Decimal] = mapped_column(Money, nullable=False)
    net_worth: Mapped[Decimal] = mapped_column(Money, nullable=False)
    detail: Mapped[Optional[dict]] = mapped_column(JSONText, nullable=True)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        UniqueConstraint("as_of_date", "currency", name="uq_nw_date_currency"),
        Index("ix_nw_date", "as_of_date"),
    )


# ==========================================================================
# Imports & recycle bin
# ==========================================================================
class ImportBatch(Base, TimestampMixin):
    """One CSV import, so it can be reviewed and rolled back wholesale."""

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    imported_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mapping: Mapped[Optional[dict]] = mapped_column(JSONText, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rolled_back_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class RecycleBin(Base):
    """JSON snapshots of deleted records so a mis-click is never fatal."""

    __tablename__ = "recycle_bin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(48), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSONText, nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    restored_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_recycle_type", "entity_type", "restored_at"),)


class ExchangeRate(Base, TimestampMixin):
    """One dated quote: how many units of ``base_currency`` buy 1 ``currency``.

    ``base_currency`` is stored rather than assumed. Without it a row silently
    changes meaning the moment the book's primary currency is switched, and the
    ambiguity would be undetectable after the fact.

    The book keeps a daily log, but conversion always reads the most recent row
    per currency — see :func:`services.currency_service.book`.
    """

    __tablename__ = "exchange_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Rate, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("as_of_date", "currency", "base_currency",
                         name="uq_fx_date_pair"),
        Index("ix_fx_pair_date", "currency", "base_currency", "as_of_date"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExchangeRate {self.as_of_date} 1 {self.currency}={self.rate} {self.base_currency}>"


class SchemaMeta(Base):
    """Tracks the applied migration version."""

    __tablename__ = "schema_meta"

    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


ALL_MODELS = [
    AppSettings, Category, Account, AccountValuation, RecurringRule,
    Transaction, BudgetPeriod, BudgetLine, Goal, Debt, NetWorthSnapshot,
    ImportBatch, RecycleBin, ExchangeRate, SchemaMeta,
]
