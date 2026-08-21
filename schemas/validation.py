"""Pydantic input schemas.

Every write that originates from a human (a form, a CSV row) passes through
one of these before it reaches the database, so the service layer can assume
amounts are non-negative decimals, dates are dates, and enum-ish strings are
actually in the enum. Error messages are written to be shown to the user
verbatim.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from calculations.money import money, parse_money
from constants import (
    AccountType,
    AllocationTarget,
    AvailabilityRule,
    BalanceMode,
    BusinessDayRule,
    CategoryKind,
    DebtType,
    Frequency,
    GoalStatus,
    GoalType,
    TxnKind,
    TxnStatus,
)

MAX_AMOUNT = Decimal("999999999.99")


class BaseIn(BaseModel):
    #: ``validate_assignment`` is deliberately OFF: the ``mode="after"`` model
    #: validators below normalise fields by assigning to ``self`` (clearing
    #: ``to_account_id`` on a non-transfer, filling ``actual_date``…), and
    #: re-validating on assignment would recurse into the same validator.
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="ignore",
    )


def _coerce_money(value: object) -> Decimal:
    if value is None or value == "":
        return Decimal("0.00")
    return money(parse_money(value) if isinstance(value, str) else value)


class MoneyMixin:
    @staticmethod
    def check_amount(value: Decimal, field_name: str = "amount",
                     *, allow_zero: bool = True) -> Decimal:
        amount = _coerce_money(value)
        if amount < 0:
            raise ValueError(f"{field_name} cannot be negative — use the type to set direction.")
        if not allow_zero and amount == 0:
            raise ValueError(f"{field_name} must be greater than zero.")
        if amount > MAX_AMOUNT:
            raise ValueError(f"{field_name} looks wrong — the maximum accepted is {MAX_AMOUNT}.")
        return amount


# ==========================================================================
class CategoryIn(BaseIn, MoneyMixin):
    name: str = Field(min_length=1, max_length=120)
    kind: str = CategoryKind.EXPENSE.value
    parent_id: Optional[int] = None
    color: Optional[str] = None
    icon: Optional[str] = None
    notes: Optional[str] = None
    sort_order: int = 100

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if not CategoryKind.has(value):
            raise ValueError(f"Unknown category type “{value}”.")
        return value

    @field_validator("color")
    @classmethod
    def _color(cls, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        text = value.strip()
        if not text.startswith("#") or len(text) not in (4, 7, 9):
            raise ValueError("Colour must be a hex value such as #2a78d6.")
        return text


class CurrencyMixin:
    """A ``currency`` field plus the one rule every code must satisfy.

    Membership in ``SUPPORTED_CURRENCIES`` is *not* checked here: an unknown
    code still formats (``format_money`` falls back to the code as its own
    symbol), and rejecting one at this layer would make a restored backup from
    a differently-configured book unloadable.
    """

    @classmethod
    def check_currency(cls, value: object) -> str:
        text = str(value or "BRL").upper()
        if len(text) != 3 or not text.isalpha():
            raise ValueError("Currency must be a 3-letter code such as BRL or EUR.")
        return text


class AccountIn(BaseIn, MoneyMixin):
    name: str = Field(min_length=1, max_length=120)
    type: str = AccountType.CHECKING.value
    currency: str = "BRL"
    opening_balance: Decimal = Decimal("0")
    opening_date: date = Field(default_factory=date.today)
    balance_mode: str = BalanceMode.TRANSACTIONS.value
    credit_limit: Optional[Decimal] = None
    interest_rate: Optional[Decimal] = None
    statement_day: Optional[int] = Field(default=None, ge=1, le=31)
    due_day: Optional[int] = Field(default=None, ge=1, le=31)
    institution: Optional[str] = None
    color: Optional[str] = None
    icon: Optional[str] = None
    notes: Optional[str] = None
    include_in_net_worth: bool = True
    include_in_cash: bool = True
    sort_order: int = 100

    @field_validator("type")
    @classmethod
    def _type(cls, value: str) -> str:
        if not AccountType.has(value):
            raise ValueError(f"Unknown account type “{value}”.")
        return value

    @field_validator("opening_balance", mode="before")
    @classmethod
    def _opening(cls, value: object) -> Decimal:
        return _coerce_money(value)

    @field_validator("credit_limit", mode="before")
    @classmethod
    def _limit(cls, value: object) -> Optional[Decimal]:
        if value in (None, ""):
            return None
        amount = _coerce_money(value)
        if amount < 0:
            raise ValueError("Credit limit cannot be negative.")
        return amount

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        text = (value or "BRL").upper()
        if len(text) != 3 or not text.isalpha():
            raise ValueError("Currency must be a 3-letter code such as BRL or USD.")
        return text


class TransactionIn(BaseIn, MoneyMixin):
    txn_date: date
    description: str = Field(min_length=1, max_length=240)
    amount: Decimal
    kind: str = TxnKind.EXPENSE.value
    status: str = TxnStatus.COMPLETED.value
    actual_date: Optional[date] = None
    availability_date: Optional[date] = None
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    to_account_id: Optional[int] = None
    #: Magnitude arriving at ``to_account_id`` when the two sides hold
    #: different currencies. ``None`` otherwise.
    to_amount: Optional[Decimal] = None
    #: Always recomputed from the two amounts — never trusted from the payload.
    fx_rate: Optional[Decimal] = None
    goal_id: Optional[int] = None
    debt_id: Optional[int] = None
    payment_method: Optional[str] = None
    tags: Optional[str] = None
    notes: Optional[str] = None
    is_planned: bool = True
    exclude_from_budget: bool = False

    @field_validator("amount", mode="before")
    @classmethod
    def _amount(cls, value: object) -> Decimal:
        return MoneyMixin.check_amount(value, "Amount", allow_zero=False)

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if not TxnKind.has(value):
            raise ValueError(f"Unknown transaction type “{value}”.")
        return value

    @field_validator("status")
    @classmethod
    def _status(cls, value: str) -> str:
        if not TxnStatus.has(value):
            raise ValueError(f"Unknown status “{value}”.")
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, value: object) -> Optional[str]:
        if not value:
            return None
        if isinstance(value, (list, tuple, set)):
            parts = [str(item).strip() for item in value]
        else:
            parts = [part.strip() for part in str(value).split(",")]
        cleaned = [part for part in parts if part]
        return ", ".join(sorted(set(cleaned), key=cleaned.index)) or None

    @model_validator(mode="after")
    def _coherent(self) -> "TransactionIn":
        if self.kind == TxnKind.TRANSFER.value:
            if not self.account_id or not self.to_account_id:
                raise ValueError("A transfer needs both a source and a destination account.")
            if self.account_id == self.to_account_id:
                raise ValueError("A transfer cannot have the same account on both sides.")
            self.category_id = None
            if self.to_amount is not None:
                self.to_amount = MoneyMixin.check_amount(
                    self.to_amount, "Amount received", allow_zero=False)
                # Derived here so a payload can never disagree with its own
                # amounts. Whether a cross-currency transfer *requires* a
                # ``to_amount`` needs both accounts, so that check lives in
                # ``transaction_service._validate_relations``.
                from services.currency_service import derive_fx_rate

                self.fx_rate = derive_fx_rate(self.amount, self.to_amount)
            else:
                self.fx_rate = None
        else:
            self.to_account_id = None
            self.to_amount = None
            self.fx_rate = None
            if not self.account_id:
                raise ValueError("Choose the account this transaction belongs to.")
        if self.status == TxnStatus.COMPLETED.value and self.actual_date is None:
            self.actual_date = self.txn_date
        if self.status == TxnStatus.PLANNED.value:
            self.actual_date = None
        if self.actual_date and self.actual_date < self.txn_date:
            # Cash arriving before it was earned is legal (advance) but worth a
            # sanity bound: reject differences beyond a year.
            if (self.txn_date - self.actual_date).days > 366:
                raise ValueError("The payment date is more than a year before the entry date.")
        return self


class TransferIn(BaseIn, MoneyMixin):
    txn_date: date
    amount: Decimal
    from_account_id: int
    to_account_id: int
    #: Set only when the two accounts hold different currencies.
    to_amount: Optional[Decimal] = None
    description: str = "Transfer"
    notes: Optional[str] = None
    payment_method: Optional[str] = None
    status: str = TxnStatus.COMPLETED.value
    # Present so every transfer in the app can route through one constructor.
    # Their absence is precisely why six call sites hand-rolled the payload.
    actual_date: Optional[date] = None
    goal_id: Optional[int] = None
    debt_id: Optional[int] = None

    @field_validator("amount", mode="before")
    @classmethod
    def _amount(cls, value: object) -> Decimal:
        return MoneyMixin.check_amount(value, "Amount", allow_zero=False)

    @model_validator(mode="after")
    def _distinct(self) -> "TransferIn":
        if self.from_account_id == self.to_account_id:
            raise ValueError("Pick two different accounts for a transfer.")
        return self


class RecurringRuleIn(BaseIn, MoneyMixin):
    currency: str = "BRL"
    name: str = Field(min_length=1, max_length=160)
    kind: str = TxnKind.EXPENSE.value
    amount: Decimal
    frequency: str = Frequency.MONTHLY.value
    interval: int = Field(default=1, ge=1, le=365)
    start_date: date
    end_date: Optional[date] = None
    max_occurrences: Optional[int] = Field(default=None, ge=1, le=1000)
    day_of_month: Optional[int] = Field(default=None, ge=1, le=31)
    weekday: Optional[int] = Field(default=None, ge=0, le=6)
    month_of_year: Optional[int] = Field(default=None, ge=1, le=12)
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    to_account_id: Optional[int] = None
    goal_id: Optional[int] = None
    debt_id: Optional[int] = None
    growth_pct: Decimal = Decimal("0")
    growth_every_months: int = Field(default=12, ge=1, le=120)
    growth_anchor_month: Optional[int] = Field(default=None, ge=1, le=12)
    seasonal_factors: Optional[dict] = None
    business_day_rule: str = BusinessDayRule.NONE.value
    settlement_offset_days: int = Field(default=0, ge=-60, le=180)
    availability_rule: Optional[str] = None
    payment_method: Optional[str] = None
    description_template: Optional[str] = None
    tags: Optional[str] = None
    notes: Optional[str] = None
    is_active: bool = True
    auto_generate: bool = True
    include_in_budget: bool = True

    @field_validator("amount", mode="before")
    @classmethod
    def _amount(cls, value: object) -> Decimal:
        return MoneyMixin.check_amount(value, "Amount", allow_zero=False)

    @field_validator("growth_pct", mode="before")
    @classmethod
    def _growth(cls, value: object) -> Decimal:
        amount = _coerce_money(value)
        if amount < Decimal("-100") or amount > Decimal("1000"):
            raise ValueError("Growth must be between -100% and 1000%.")
        return amount

    @field_validator("frequency")
    @classmethod
    def _frequency(cls, value: str) -> str:
        if not Frequency.has(value):
            raise ValueError(f"Unknown frequency “{value}”.")
        return value

    @field_validator("seasonal_factors", mode="before")
    @classmethod
    def _seasonal(cls, value: object) -> Optional[dict]:
        if not value:
            return None
        if not isinstance(value, dict):
            raise ValueError("Seasonal factors must be a month → multiplier mapping.")
        cleaned: dict[str, float] = {}
        for key, factor in value.items():
            try:
                month = int(key)
                multiplier = float(factor)
            except (TypeError, ValueError):
                raise ValueError("Seasonal factors must map month numbers to numbers.")
            if not 1 <= month <= 12:
                raise ValueError("Seasonal factor months must be between 1 and 12.")
            if multiplier < 0 or multiplier > 20:
                raise ValueError("Seasonal multipliers must be between 0 and 20.")
            if multiplier != 1.0:
                cleaned[str(month)] = multiplier
        return cleaned or None

    @model_validator(mode="after")
    def _coherent(self) -> "RecurringRuleIn":
        if self.end_date and self.end_date < self.start_date:
            raise ValueError("The end date cannot be before the start date.")
        if self.kind == TxnKind.TRANSFER.value:
            if not self.account_id or not self.to_account_id:
                raise ValueError("A recurring transfer needs both accounts.")
            if self.account_id == self.to_account_id:
                raise ValueError("A transfer cannot have the same account on both sides.")
            self.category_id = None
        else:
            self.to_account_id = None
        if self.availability_rule and not AvailabilityRule.has(self.availability_rule):
            raise ValueError(f"Unknown availability rule “{self.availability_rule}”.")
        if self.frequency in (Frequency.MONTHLY.value, Frequency.QUARTERLY.value,
                              Frequency.SEMIANNUAL.value, Frequency.ANNUAL.value,
                              Frequency.CUSTOM_MONTHS.value) and self.day_of_month is None:
            self.day_of_month = self.start_date.day
        return self

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: object) -> str:
        return CurrencyMixin.check_currency(value)


class BudgetLineIn(BaseIn, MoneyMixin):
    currency: str = "BRL"
    kind: str = CategoryKind.EXPENSE.value
    target: str = AllocationTarget.EXPENSE.value
    planned_amount: Decimal = Decimal("0")
    category_id: Optional[int] = None
    goal_id: Optional[int] = None
    debt_id: Optional[int] = None
    account_id: Optional[int] = None
    rule_id: Optional[int] = None
    label: Optional[str] = None
    expected_day: Optional[int] = Field(default=None, ge=1, le=31)
    notes: Optional[str] = None
    is_override: bool = False
    is_locked: bool = False

    @field_validator("planned_amount", mode="before")
    @classmethod
    def _planned(cls, value: object) -> Decimal:
        return MoneyMixin.check_amount(value, "Planned amount", allow_zero=True)

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if not CategoryKind.has(value):
            raise ValueError(f"Unknown budget line type “{value}”.")
        return value

    @field_validator("target")
    @classmethod
    def _target(cls, value: str) -> str:
        if not AllocationTarget.has(value):
            raise ValueError(f"Unknown allocation target “{value}”.")
        return value

    @model_validator(mode="after")
    def _identified(self) -> "BudgetLineIn":
        if not any([self.category_id, self.goal_id, self.debt_id, self.label]):
            raise ValueError("A budget line needs a category, a goal, a debt, or a label.")
        return self

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: object) -> str:
        return CurrencyMixin.check_currency(value)


class GoalIn(BaseIn, MoneyMixin):
    currency: str = "BRL"
    name: str = Field(min_length=1, max_length=160)
    goal_type: str = GoalType.CUSTOM.value
    target_amount: Decimal
    starting_amount: Decimal = Decimal("0")
    planned_monthly: Decimal = Decimal("0")
    target_date: Optional[date] = None
    start_date: date = Field(default_factory=date.today)
    account_id: Optional[int] = None
    category_id: Optional[int] = None
    priority: int = Field(default=3, ge=1, le=5)
    status: str = GoalStatus.ACTIVE.value
    color: Optional[str] = None
    icon: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("target_amount", mode="before")
    @classmethod
    def _target(cls, value: object) -> Decimal:
        return MoneyMixin.check_amount(value, "Target amount", allow_zero=False)

    @field_validator("starting_amount", "planned_monthly", mode="before")
    @classmethod
    def _amounts(cls, value: object) -> Decimal:
        return MoneyMixin.check_amount(value, "Amount", allow_zero=True)

    @model_validator(mode="after")
    def _dates(self) -> "GoalIn":
        if self.target_date and self.target_date < self.start_date:
            raise ValueError("The target date cannot be before the start date.")
        if self.starting_amount > self.target_amount:
            raise ValueError("The starting amount is already above the target.")
        return self

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: object) -> str:
        return CurrencyMixin.check_currency(value)


class DebtIn(BaseIn, MoneyMixin):
    currency: str = "BRL"
    name: str = Field(min_length=1, max_length=160)
    debt_type: str = DebtType.OTHER.value
    principal_balance: Decimal
    original_principal: Optional[Decimal] = None
    interest_rate: Decimal = Decimal("0")
    minimum_payment: Decimal = Decimal("0")
    planned_payment: Decimal = Decimal("0")
    extra_payment: Decimal = Decimal("0")
    due_day: Optional[int] = Field(default=None, ge=1, le=31)
    account_id: Optional[int] = None
    category_id: Optional[int] = None
    opened_date: Optional[date] = None
    balance_as_of: date = Field(default_factory=date.today)
    is_active: bool = True
    include_in_budget: bool = True
    notes: Optional[str] = None

    @field_validator("principal_balance", "minimum_payment", "planned_payment",
                     "extra_payment", mode="before")
    @classmethod
    def _amounts(cls, value: object) -> Decimal:
        return MoneyMixin.check_amount(value, "Amount", allow_zero=True)

    @field_validator("original_principal", mode="before")
    @classmethod
    def _original(cls, value: object) -> Optional[Decimal]:
        if value in (None, ""):
            return None
        return MoneyMixin.check_amount(value, "Original principal", allow_zero=True)

    @field_validator("interest_rate", mode="before")
    @classmethod
    def _rate(cls, value: object) -> Decimal:
        amount = _coerce_money(value)
        if amount < 0:
            raise ValueError("Interest rate cannot be negative.")
        if amount > Decimal("1000"):
            raise ValueError("Interest rate above 1000% a year is almost certainly a typo.")
        return amount

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: object) -> str:
        return CurrencyMixin.check_currency(value)


class SettingsIn(BaseIn):
    base_currency: str = "BRL"
    #: Every currency the book uses. Normalised below; the primary is forced to
    #: the front whether or not the caller included it.
    active_currencies: list[str] = Field(default_factory=lambda: ["BRL"])
    date_format: str = "DD/MM/YYYY"
    show_cents: bool = True
    first_day_of_month: int = Field(default=1, ge=1, le=28)
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)
    budget_method: str = "zero_based"
    carry_over_surplus: bool = True
    income_availability_rule: str = AvailabilityRule.EARNED_PERIOD.value
    income_cutoff_day: int = Field(default=25, ge=1, le=31)
    warning_threshold_pct: Decimal = Decimal("80")
    critical_threshold_pct: Decimal = Decimal("100")
    variance_tolerance_pct: Decimal = Decimal("5")
    forecast_months: int = Field(default=12, ge=1, le=120)
    theme: str = "auto"
    backup_dir: Optional[str] = None

    @field_validator("income_availability_rule")
    @classmethod
    def _rule(cls, value: str) -> str:
        if not AvailabilityRule.has(value):
            raise ValueError(f"Unknown availability rule “{value}”.")
        return value

    @field_validator("warning_threshold_pct", "critical_threshold_pct",
                     "variance_tolerance_pct", mode="before")
    @classmethod
    def _pct(cls, value: object) -> Decimal:
        amount = _coerce_money(value)
        if amount < 0 or amount > Decimal("1000"):
            raise ValueError("Thresholds must be between 0% and 1000%.")
        return amount

    @field_validator("base_currency")
    @classmethod
    def _base_currency(cls, value: str) -> str:
        from constants import SUPPORTED_CURRENCIES

        text = (value or "BRL").upper()
        if text not in SUPPORTED_CURRENCIES:
            raise ValueError(f"{text} is not a currency this app can format.")
        return text

    @model_validator(mode="after")
    def _ordered(self) -> "SettingsIn":
        if self.critical_threshold_pct < self.warning_threshold_pct:
            raise ValueError("The critical threshold must be at or above the warning threshold.")
        return self

    @model_validator(mode="after")
    def _currencies(self) -> "SettingsIn":
        """Normalise the currency list. The only place the ≤3 cap is enforced."""
        from constants import SUPPORTED_CURRENCIES
        from services.currency_service import MAX_CURRENCIES

        primary = self.base_currency
        cleaned: list[str] = [primary]
        for code in self.active_currencies or []:
            text = str(code or "").strip().upper()
            if not text or text in cleaned:
                continue
            if text not in SUPPORTED_CURRENCIES:
                raise ValueError(f"{text} is not a currency this app can format.")
            cleaned.append(text)
        if len(cleaned) > MAX_CURRENCIES:
            raise ValueError(
                f"This app handles at most {MAX_CURRENCIES} currencies at once."
            )
        self.active_currencies = cleaned
        return self


class ImportRowIn(BaseIn, MoneyMixin):
    """One row of a CSV import, before it becomes a transaction."""

    date: date
    description: str = Field(min_length=1, max_length=240)
    amount: Decimal
    kind: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    account: Optional[str] = None
    to_account: Optional[str] = None
    payment_method: Optional[str] = None
    tags: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None

    @field_validator("amount", mode="before")
    @classmethod
    def _amount(cls, value: object) -> Decimal:
        amount = _coerce_money(value)
        if amount == 0:
            raise ValueError("Amount is zero.")
        if abs(amount) > MAX_AMOUNT:
            raise ValueError(f"Amount above the accepted maximum of {MAX_AMOUNT}.")
        return amount

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, value: object) -> Optional[str]:
        if not value:
            return None
        text = str(value).strip().lower()
        aliases = {
            "in": "income", "income": "income", "credit": "income",
            "receita": "income", "entrada": "income", "deposit": "income",
            "out": "expense", "expense": "expense", "debit": "expense",
            "despesa": "expense", "saida": "expense", "saída": "expense",
            "transfer": "transfer", "transferencia": "transfer",
            "transferência": "transfer",
        }
        if text not in aliases:
            raise ValueError(f"Unrecognised type “{value}”.")
        return aliases[text]
