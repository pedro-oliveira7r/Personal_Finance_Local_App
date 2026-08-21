"""First-run defaults: the settings row, a usable category tree, basic accounts.

Seeding is idempotent and additive — it never touches or overwrites a row the
user already has.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from constants import AccountType, CategoryKind
from database.models import Account, AppSettings, Category

# --------------------------------------------------------------------------
# Default category tree: (name, icon, [subcategories])
# --------------------------------------------------------------------------
INCOME_CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("Salary", "💼", ["Net salary", "13th salary", "Overtime", "Vacation pay"]),
    ("Freelance", "🧑‍💻", ["Projects", "Consulting"]),
    ("Business", "🏪", ["Sales", "Services", "Owner draw"]),
    ("Bonus", "🎉", ["Annual bonus", "Performance bonus", "Profit sharing"]),
    ("Commission", "📈", []),
    ("Investment income", "💹", ["Dividends", "Capital gains", "Rental income"]),
    ("Interest", "🏦", ["Savings interest", "Fixed income interest"]),
    ("Government benefits", "🏛️", ["Pension", "Family allowance", "Tax refund"]),
    ("Gifts received", "🎁", []),
    ("Reimbursements", "↩️", ["Work expenses", "Insurance claim"]),
    ("Other income", "➕", []),
]

EXPENSE_CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("Housing", "🏠", ["Rent", "Mortgage", "Condo fee", "Property tax",
                       "Repairs & maintenance", "Furniture & decor"]),
    ("Utilities", "💡", ["Electricity", "Water & sewage", "Gas", "Internet",
                         "Mobile phone", "Waste collection"]),
    ("Transportation", "🚗", ["Fuel", "Public transport", "Ride-hailing", "Parking",
                              "Tolls", "Vehicle maintenance", "Vehicle insurance",
                              "Licensing & taxes"]),
    ("Food", "🍽️", ["Groceries", "Restaurants", "Delivery", "Coffee & snacks",
                     "Work lunch"]),
    ("Healthcare", "🩺", ["Health insurance", "Doctor & dentist", "Medication",
                          "Therapy", "Exams", "Gym & fitness"]),
    ("Insurance", "🛡️", ["Life insurance", "Home insurance", "Other insurance"]),
    ("Debt payments", "💳", ["Credit card payment", "Loan payment", "Financing",
                             "Interest & penalties"]),
    ("Subscriptions", "🔁", ["Streaming", "Software", "Memberships",
                             "Cloud storage", "News & magazines"]),
    ("Entertainment", "🎬", ["Events & shows", "Hobbies", "Games", "Books",
                             "Bars & nightlife"]),
    ("Education", "🎓", ["Tuition", "Courses & certifications", "School supplies",
                         "Childcare", "Languages"]),
    ("Shopping", "🛍️", ["Clothing & shoes", "Electronics", "Household goods",
                         "Personal care", "Gifts given"]),
    ("Travel", "✈️", ["Flights", "Accommodation", "Travel food",
                      "Activities & tours", "Travel insurance"]),
    ("Taxes", "🧾", ["Income tax", "Municipal taxes", "Other taxes"]),
    ("Fees & banking", "🏧", ["Bank fees", "Card annual fee", "Late fees",
                              "Transfer fees"]),
    ("Pets", "🐾", ["Pet food", "Veterinarian", "Grooming"]),
    ("Family & gifts", "👨‍👩‍👧", ["Children", "Support to family", "Donations"]),
    ("Miscellaneous", "❓", ["Uncategorised", "Cash withdrawal", "Unexpected"]),
]

SAVINGS_CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("Emergency fund", "🚨", []),
    ("Short-term savings", "🐖", ["Reserve", "Planned purchases"]),
    ("Goal contributions", "🎯", []),
]

INVESTMENT_CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("Investments", "📊", ["Stocks & ETFs", "Fixed income", "Retirement",
                           "Real estate funds", "Crypto", "Other investments"]),
]

DEBT_CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("Debt repayment", "⛓️", ["Principal", "Interest"]),
]

#: Validated categorical palette (see charts/theme.py) reused so a category's
#: colour is stable everywhere it appears.
_PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
            "#e87ba4", "#008300", "#4a3aa7", "#e34948"]

DEFAULT_ACCOUNTS = [
    ("Checking account", AccountType.CHECKING.value, "🏦", True),
    ("Cash / wallet", AccountType.CASH.value, "👛", True),
    ("Savings account", AccountType.SAVINGS.value, "🐖", True),
]


# --------------------------------------------------------------------------
def get_or_create_settings(session: Session) -> AppSettings:
    settings = session.get(AppSettings, 1)
    if settings is None:
        defaults = config.DEFAULT_SETTINGS
        settings = AppSettings(
            id=1,
            base_currency=defaults["base_currency"],
            active_currencies=list(defaults["active_currencies"]),
            date_format=defaults["date_format"],
            show_cents=defaults["show_cents"],
            first_day_of_month=defaults["first_day_of_month"],
            fiscal_year_start_month=defaults["fiscal_year_start_month"],
            budget_method=defaults["budget_method"],
            carry_over_surplus=defaults["carry_over_surplus"],
            income_availability_rule=defaults["income_availability_rule"],
            income_cutoff_day=defaults["income_cutoff_day"],
            warning_threshold_pct=Decimal(defaults["warning_threshold_pct"]),
            critical_threshold_pct=Decimal(defaults["critical_threshold_pct"]),
            variance_tolerance_pct=Decimal(defaults["variance_tolerance_pct"]),
            forecast_months=defaults["forecast_months"],
            theme=defaults["theme"],
        )
        session.add(settings)
        session.flush()
    return settings


def _seed_group(session: Session, kind: str, groups: list[tuple[str, str, list[str]]],
                start_order: int = 0) -> int:
    """Insert missing parents/children for one category kind."""
    created = 0
    existing = {
        (row.name, row.parent_id, row.kind)
        for row in session.execute(select(Category)).scalars()
    }
    for index, (name, icon, children) in enumerate(groups):
        parent = session.execute(
            select(Category).where(
                Category.name == name, Category.parent_id.is_(None), Category.kind == kind
            )
        ).scalar_one_or_none()
        if parent is None:
            parent = Category(
                name=name, kind=kind, icon=icon, is_system=True,
                color=_PALETTE[index % len(_PALETTE)],
                sort_order=start_order + index * 10,
            )
            session.add(parent)
            session.flush()
            created += 1
        for child_index, child_name in enumerate(children):
            if (child_name, parent.id, kind) in existing:
                continue
            exists = session.execute(
                select(Category.id).where(
                    Category.name == child_name,
                    Category.parent_id == parent.id,
                    Category.kind == kind,
                )
            ).first()
            if exists:
                continue
            session.add(Category(
                name=child_name, kind=kind, parent_id=parent.id, is_system=True,
                color=parent.color, sort_order=parent.sort_order + child_index + 1,
            ))
            created += 1
    return created


def seed_categories(session: Session) -> int:
    total = 0
    total += _seed_group(session, CategoryKind.INCOME.value, INCOME_CATEGORIES, 0)
    total += _seed_group(session, CategoryKind.EXPENSE.value, EXPENSE_CATEGORIES, 1000)
    total += _seed_group(session, CategoryKind.SAVINGS.value, SAVINGS_CATEGORIES, 5000)
    total += _seed_group(session, CategoryKind.INVESTMENT.value, INVESTMENT_CATEGORIES, 6000)
    total += _seed_group(session, CategoryKind.DEBT.value, DEBT_CATEGORIES, 7000)
    session.flush()
    return total


def seed_accounts(session: Session, currency: str = "BRL") -> int:
    created = 0
    for index, (name, acct_type, icon, cash) in enumerate(DEFAULT_ACCOUNTS):
        exists = session.execute(
            select(Account.id).where(Account.name == name)
        ).first()
        if exists:
            continue
        session.add(Account(
            name=name, type=acct_type, icon=icon, currency=currency,
            opening_balance=Decimal("0"), opening_date=date.today(),
            include_in_cash=cash, sort_order=index * 10,
            color=_PALETTE[index % len(_PALETTE)],
        ))
        created += 1
    session.flush()
    return created


def seed_defaults(session: Session) -> dict[str, int]:
    """Called on every ``init_db``. Additive and safe to repeat."""
    settings = get_or_create_settings(session)
    categories = seed_categories(session)
    accounts = seed_accounts(session, settings.base_currency)
    return {"categories": categories, "accounts": accounts}


def is_database_empty(session: Session) -> bool:
    """True when the user has not entered any real data yet."""
    from database.models import BudgetPeriod, Transaction

    has_txn = session.execute(select(Transaction.id).limit(1)).first() is not None
    has_period = session.execute(select(BudgetPeriod.id).limit(1)).first() is not None
    return not (has_txn or has_period)
