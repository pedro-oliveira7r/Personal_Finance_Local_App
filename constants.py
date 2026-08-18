"""Domain enumerations and shared constant tables.

Kept free of third-party imports so it can be used by every layer
(database, calculations, services, UI) without creating import cycles.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """Minimal ``str`` enum (stdlib ``StrEnum`` requires Python 3.11+)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def has(cls, value: str) -> bool:
        return value in cls.values()


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------
class TxnKind(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"


class TxnStatus(StrEnum):
    PLANNED = "planned"      # scheduled / expected, money has not moved yet
    COMPLETED = "completed"  # money actually moved
    VOID = "void"            # cancelled, kept for audit but ignored everywhere


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------
class CategoryKind(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"
    SAVINGS = "savings"
    INVESTMENT = "investment"
    DEBT = "debt"


#: Category kinds that consume money when a budget is built.
ALLOCATION_KINDS = (
    CategoryKind.EXPENSE.value,
    CategoryKind.SAVINGS.value,
    CategoryKind.INVESTMENT.value,
    CategoryKind.DEBT.value,
)


class AllocationTarget(StrEnum):
    """What a budget allocation line is funding."""

    EXPENSE = "expense"
    SAVINGS = "savings"
    INVESTMENT = "investment"
    DEBT = "debt"
    GOAL = "goal"
    OTHER = "other"


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------
class AccountType(StrEnum):
    CHECKING = "checking"
    SAVINGS = "savings"
    CASH = "cash"
    INVESTMENT = "investment"
    CREDIT_CARD = "credit_card"
    LOAN = "loan"
    OTHER_ASSET = "other_asset"
    OTHER_LIABILITY = "other_liability"


#: Accounts whose balance counts as spendable cash for zero-based budgeting.
CASH_ACCOUNT_TYPES = (
    AccountType.CHECKING.value,
    AccountType.CASH.value,
    AccountType.SAVINGS.value,
)

#: Accounts that represent money you owe. Their balance is stored as a
#: positive number meaning "amount owed".
LIABILITY_ACCOUNT_TYPES = (
    AccountType.CREDIT_CARD.value,
    AccountType.LOAN.value,
    AccountType.OTHER_LIABILITY.value,
)

ASSET_ACCOUNT_TYPES = (
    AccountType.CHECKING.value,
    AccountType.SAVINGS.value,
    AccountType.CASH.value,
    AccountType.INVESTMENT.value,
    AccountType.OTHER_ASSET.value,
)

ACCOUNT_TYPE_LABELS = {
    AccountType.CHECKING.value: "Checking account",
    AccountType.SAVINGS.value: "Savings account",
    AccountType.CASH.value: "Cash / wallet",
    AccountType.INVESTMENT.value: "Investment account",
    AccountType.CREDIT_CARD.value: "Credit card",
    AccountType.LOAN.value: "Loan",
    AccountType.OTHER_ASSET.value: "Other asset (property, vehicle...)",
    AccountType.OTHER_LIABILITY.value: "Other liability",
}


class BalanceMode(StrEnum):
    """How an account's balance is determined."""

    TRANSACTIONS = "transactions"  # opening balance + transaction history
    MANUAL = "manual"              # latest manual valuation (property, funds...)


# --------------------------------------------------------------------------
# Recurrence
# --------------------------------------------------------------------------
class Frequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    ANNUAL = "annual"
    CUSTOM_DAYS = "custom_days"
    CUSTOM_MONTHS = "custom_months"
    ONE_TIME = "one_time"


FREQUENCY_LABELS = {
    Frequency.DAILY.value: "Daily",
    Frequency.WEEKLY.value: "Weekly",
    Frequency.BIWEEKLY.value: "Every 2 weeks",
    Frequency.MONTHLY.value: "Monthly",
    Frequency.QUARTERLY.value: "Quarterly",
    Frequency.SEMIANNUAL.value: "Every 6 months",
    Frequency.ANNUAL.value: "Annual",
    Frequency.CUSTOM_DAYS.value: "Custom (every N days)",
    Frequency.CUSTOM_MONTHS.value: "Custom (every N months)",
    Frequency.ONE_TIME.value: "One time only",
}

#: Number of months advanced per step, for month-based frequencies.
MONTH_STEP = {
    Frequency.MONTHLY.value: 1,
    Frequency.QUARTERLY.value: 3,
    Frequency.SEMIANNUAL.value: 6,
    Frequency.ANNUAL.value: 12,
}


class BusinessDayRule(StrEnum):
    NONE = "none"
    NEXT = "next_business_day"
    PREVIOUS = "previous_business_day"
    NEAREST = "nearest_business_day"


# --------------------------------------------------------------------------
# Income availability (cash-flow timing)
# --------------------------------------------------------------------------
class AvailabilityRule(StrEnum):
    """When income becomes usable money for zero-based budgeting."""

    #: Available in the period the income belongs to (earned date).
    EARNED_PERIOD = "earned_period"
    #: Always pushed to the period after the one it was earned in.
    NEXT_PERIOD = "next_period"
    #: Available in the period containing the date the cash actually arrived.
    ACTUAL_DATE = "actual_date"
    #: Available in the current period if it arrives on/before a cut-off day,
    #: otherwise it funds the following period.
    CUTOFF_DAY = "cutoff_day"


AVAILABILITY_RULE_LABELS = {
    AvailabilityRule.EARNED_PERIOD.value:
        "Same period it was earned in",
    AvailabilityRule.ACTUAL_DATE.value:
        "Period containing the date the money actually arrived",
    AvailabilityRule.NEXT_PERIOD.value:
        "Always the following period (paycheck funds next month)",
    AvailabilityRule.CUTOFF_DAY.value:
        "Following period if it arrives after a cut-off day",
}


# --------------------------------------------------------------------------
# Budget periods
# --------------------------------------------------------------------------
class PeriodStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    CLOSED = "closed"


class BudgetMethod(StrEnum):
    ZERO_BASED = "zero_based"
    CATEGORY_LIMITS = "category_limits"


# --------------------------------------------------------------------------
# Goals & debts
# --------------------------------------------------------------------------
class GoalType(StrEnum):
    EMERGENCY_FUND = "emergency_fund"
    VACATION = "vacation"
    VEHICLE = "vehicle"
    HOME_DOWN_PAYMENT = "home_down_payment"
    EDUCATION = "education"
    DEBT_PAYOFF = "debt_payoff"
    INVESTMENT = "investment"
    CUSTOM = "custom"


GOAL_TYPE_LABELS = {
    GoalType.EMERGENCY_FUND.value: "Emergency fund",
    GoalType.VACATION.value: "Vacation / travel",
    GoalType.VEHICLE.value: "New car / vehicle",
    GoalType.HOME_DOWN_PAYMENT.value: "House down payment",
    GoalType.EDUCATION.value: "Education",
    GoalType.DEBT_PAYOFF.value: "Debt repayment",
    GoalType.INVESTMENT.value: "Investment target",
    GoalType.CUSTOM.value: "Custom goal",
}


class GoalStatus(StrEnum):
    ACTIVE = "active"
    ACHIEVED = "achieved"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class DebtType(StrEnum):
    CREDIT_CARD = "credit_card"
    PERSONAL_LOAN = "personal_loan"
    CAR_LOAN = "car_loan"
    MORTGAGE = "mortgage"
    STUDENT_LOAN = "student_loan"
    OVERDRAFT = "overdraft"
    OTHER = "other"


DEBT_TYPE_LABELS = {
    DebtType.CREDIT_CARD.value: "Credit card",
    DebtType.PERSONAL_LOAN.value: "Personal loan",
    DebtType.CAR_LOAN.value: "Car loan",
    DebtType.MORTGAGE.value: "Mortgage",
    DebtType.STUDENT_LOAN.value: "Student loan",
    DebtType.OVERDRAFT.value: "Overdraft",
    DebtType.OTHER.value: "Other debt",
}


class PayoffStrategy(StrEnum):
    SNOWBALL = "snowball"          # smallest balance first
    AVALANCHE = "avalanche"        # highest interest rate first
    MINIMUM_ONLY = "minimum_only"


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    SUCCESS = "success"


SEVERITY_ICONS = {
    Severity.CRITICAL.value: "🔴",
    Severity.WARNING.value: "🟠",
    Severity.INFO.value: "🔵",
    Severity.SUCCESS.value: "🟢",
}


# --------------------------------------------------------------------------
# Payment methods
# --------------------------------------------------------------------------
PAYMENT_METHODS = [
    "Debit card",
    "Credit card",
    "Bank transfer",
    "Pix",
    "Boleto",
    "Cash",
    "Direct debit",
    "Automatic payment",
    "Cheque",
    "Other",
]


# --------------------------------------------------------------------------
# Currency formatting
# --------------------------------------------------------------------------
#: symbol, thousands separator, decimal separator, symbol placement, spacing
CURRENCY_FORMATS: dict[str, dict] = {
    "BRL": {"symbol": "R$", "thousands": ".", "decimal": ",", "prefix": True, "space": True},
    "USD": {"symbol": "$", "thousands": ",", "decimal": ".", "prefix": True, "space": False},
    "EUR": {"symbol": "€", "thousands": ".", "decimal": ",", "prefix": True, "space": True},
    "GBP": {"symbol": "£", "thousands": ",", "decimal": ".", "prefix": True, "space": False},
    "ARS": {"symbol": "$", "thousands": ".", "decimal": ",", "prefix": True, "space": True},
    "CLP": {"symbol": "$", "thousands": ".", "decimal": ",", "prefix": True, "space": True},
    "MXN": {"symbol": "$", "thousands": ",", "decimal": ".", "prefix": True, "space": True},
    "CAD": {"symbol": "C$", "thousands": ",", "decimal": ".", "prefix": True, "space": False},
    "JPY": {"symbol": "¥", "thousands": ",", "decimal": ".", "prefix": True, "space": False},
    "CHF": {"symbol": "CHF", "thousands": "'", "decimal": ".", "prefix": True, "space": True},
}

SUPPORTED_CURRENCIES = sorted(CURRENCY_FORMATS)

DATE_FORMATS = {
    "DD/MM/YYYY": "%d/%m/%Y",
    "MM/DD/YYYY": "%m/%d/%Y",
    "YYYY-MM-DD": "%Y-%m-%d",
    "DD.MM.YYYY": "%d.%m.%Y",
}

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

MONTH_ABBR = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]
