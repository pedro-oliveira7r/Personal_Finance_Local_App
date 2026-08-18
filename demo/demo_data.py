"""Deterministic demo dataset — 18 months of a realistic household.

Everything is generated from the same recurring-rule engine the app uses, then
past occurrences are "actualised" with plausible jitter, so planned-vs-actual,
variance, trends and forecasting all have something honest to chew on.

The generator is seeded, so the demo looks identical every time and screenshots
stay reproducible. Nothing here is required by the application: it can be
cleared at any time from Settings without touching categories or accounts.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from calculations.money import ZERO, D, money
from calculations.periods import Period, make_period, shift_period
from calculations.recurrence import generate_occurrences, spec_from_rule
from constants import (
    AccountType,
    AvailabilityRule,
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
from database.models import (
    Account,
    AccountValuation,
    BudgetLine,
    BudgetPeriod,
    Debt,
    Goal,
    ImportBatch,
    NetWorthSnapshot,
    RecurringRule,
    RecycleBin,
    Transaction,
)
from database.seed import seed_defaults
from services import budget_service, category_service, networth_service
from services.transaction_service import fingerprint

DEMO_SEED = 20260817
DEMO_TAG = "demo"


# --------------------------------------------------------------------------
# Account and rule definitions
# --------------------------------------------------------------------------
DEMO_ACCOUNTS = [
    dict(name="Conta Corrente", type=AccountType.CHECKING.value, icon="🏦",
         opening_balance="3200", institution="Banco do Brasil", color="#2a78d6"),
    dict(name="Poupança", type=AccountType.SAVINGS.value, icon="🐖",
         opening_balance="6500", institution="Banco do Brasil", color="#1baf7a"),
    dict(name="Carteira", type=AccountType.CASH.value, icon="👛",
         opening_balance="250", color="#eda100"),
    dict(name="Cartão de Crédito", type=AccountType.CREDIT_CARD.value, icon="💳",
         opening_balance="1850", credit_limit="9000",
         statement_day=28, due_day=10, include_in_cash=False, color="#e34948"),
    dict(name="Investimentos", type=AccountType.INVESTMENT.value, icon="📊",
         opening_balance="18400", institution="XP", include_in_cash=False,
         interest_rate="10.5", color="#4a3aa7"),
    dict(name="Financiamento do Carro", type=AccountType.LOAN.value, icon="🚗",
         opening_balance="31500", interest_rate="21.9", due_day=15,
         include_in_cash=False, color="#eb6834"),
    dict(name="Apartamento", type=AccountType.OTHER_ASSET.value, icon="🏠",
         opening_balance="0", balance_mode="manual", include_in_cash=False,
         color="#008300"),
]

#: ``jitter`` is the +/- fraction applied when a past occurrence is actualised.
#: ``skip`` is the chance the payment simply did not happen that month.
DEMO_RULES: list[dict[str, Any]] = [
    # ---- income -------------------------------------------------------
    dict(name="Salário", kind="income", amount="11400", category="Salary › Net salary",
         account="Conta Corrente", frequency=Frequency.MONTHLY.value, day_of_month=5,
         growth_pct="6", growth_anchor_month=1, jitter=0.0,
         business_day_rule=BusinessDayRule.PREVIOUS.value,
         description_template="Salário {month}/{year}"),
    dict(name="13º salário", kind="income", amount="11400", category="Salary › 13th salary",
         account="Conta Corrente", frequency=Frequency.ANNUAL.value,
         month_of_year=12, day_of_month=20, growth_pct="6", growth_anchor_month=1,
         jitter=0.0),
    dict(name="Freelance (design)", kind="income", amount="1800",
         category="Freelance › Projects", account="Conta Corrente",
         frequency=Frequency.CUSTOM_MONTHS.value, interval=2, day_of_month=18,
         jitter=0.45, skip=0.25, settlement_offset_days=12,
         description_template="Freelance {month}/{year}"),
    dict(name="Rendimento de investimentos", kind="income", amount="145",
         category="Investment income › Dividends", account="Investimentos",
         frequency=Frequency.MONTHLY.value, day_of_month=15, growth_pct="1.5",
         growth_every_months=6, jitter=0.30),

    # ---- housing ------------------------------------------------------
    dict(name="Aluguel", kind="expense", amount="2200", category="Housing › Rent",
         account="Conta Corrente", frequency=Frequency.MONTHLY.value, day_of_month=10,
         growth_pct="8", growth_anchor_month=3, jitter=0.0),
    dict(name="Condomínio", kind="expense", amount="480",
         category="Housing › Condo fee", account="Conta Corrente",
         frequency=Frequency.MONTHLY.value, day_of_month=10, growth_pct="5",
         growth_anchor_month=1, jitter=0.02),
    dict(name="IPTU", kind="expense", amount="420",
         category="Housing › Property tax", account="Conta Corrente",
         frequency=Frequency.QUARTERLY.value, month_of_year=1, day_of_month=20,
         jitter=0.0),

    # ---- utilities ----------------------------------------------------
    dict(name="Energia elétrica", kind="expense", amount="185",
         category="Utilities › Electricity", account="Conta Corrente",
         frequency=Frequency.MONTHLY.value, day_of_month=14,
         seasonal_factors={"1": 1.5, "2": 1.5, "3": 1.3, "6": 0.8, "7": 0.75, "12": 1.35},
         jitter=0.12),
    dict(name="Água", kind="expense", amount="92", category="Utilities › Water & sewage",
         account="Conta Corrente", frequency=Frequency.MONTHLY.value, day_of_month=16,
         jitter=0.10),
    dict(name="Internet fibra", kind="expense", amount="119",
         category="Utilities › Internet", account="Conta Corrente",
         frequency=Frequency.MONTHLY.value, day_of_month=8, growth_pct="7",
         growth_anchor_month=9, jitter=0.0),
    dict(name="Celular", kind="expense", amount="59", category="Utilities › Mobile phone",
         account="Cartão de Crédito", frequency=Frequency.MONTHLY.value, day_of_month=22,
         jitter=0.05),

    # ---- food ---------------------------------------------------------
    dict(name="Supermercado", kind="expense", amount="1280", category="Food › Groceries",
         account="Conta Corrente", frequency=Frequency.MONTHLY.value, day_of_month=6,
         growth_pct="4", growth_anchor_month=1, jitter=0.18),
    dict(name="Restaurantes", kind="expense", amount="430", category="Food › Restaurants",
         account="Cartão de Crédito", frequency=Frequency.MONTHLY.value, day_of_month=20,
         jitter=0.35),
    dict(name="Delivery", kind="expense", amount="215", category="Food › Delivery",
         account="Cartão de Crédito", frequency=Frequency.MONTHLY.value, day_of_month=25,
         jitter=0.40),

    # ---- transport ----------------------------------------------------
    dict(name="Combustível", kind="expense", amount="390",
         category="Transportation › Fuel", account="Conta Corrente",
         frequency=Frequency.MONTHLY.value, day_of_month=4, growth_pct="5",
         growth_anchor_month=1, jitter=0.22),
    dict(name="Transporte público", kind="expense", amount="110",
         category="Transportation › Public transport", account="Carteira",
         frequency=Frequency.MONTHLY.value, day_of_month=2, jitter=0.20),
    dict(name="Seguro do carro", kind="expense", amount="2380",
         category="Transportation › Vehicle insurance", account="Conta Corrente",
         frequency=Frequency.ANNUAL.value, month_of_year=3, day_of_month=15,
         growth_pct="9", growth_anchor_month=3, jitter=0.0),
    dict(name="Manutenção do carro", kind="expense", amount="480",
         category="Transportation › Vehicle maintenance", account="Conta Corrente",
         frequency=Frequency.SEMIANNUAL.value, month_of_year=4, day_of_month=12,
         jitter=0.5),

    # ---- health & insurance -------------------------------------------
    dict(name="Plano de saúde", kind="expense", amount="612",
         category="Healthcare › Health insurance", account="Conta Corrente",
         frequency=Frequency.MONTHLY.value, day_of_month=12, growth_pct="11",
         growth_anchor_month=6, jitter=0.0),
    dict(name="Academia", kind="expense", amount="129",
         category="Healthcare › Gym & fitness", account="Cartão de Crédito",
         frequency=Frequency.MONTHLY.value, day_of_month=3, jitter=0.0, skip=0.08),
    dict(name="Farmácia", kind="expense", amount="140",
         category="Healthcare › Medication", account="Cartão de Crédito",
         frequency=Frequency.MONTHLY.value, day_of_month=17, jitter=0.45),

    # ---- subscriptions ------------------------------------------------
    dict(name="Streaming (vídeo)", kind="expense", amount="44.90",
         category="Subscriptions › Streaming", account="Cartão de Crédito",
         frequency=Frequency.MONTHLY.value, day_of_month=7, growth_pct="12",
         growth_every_months=6, jitter=0.0),
    dict(name="Streaming (música)", kind="expense", amount="21.90",
         category="Subscriptions › Streaming", account="Cartão de Crédito",
         frequency=Frequency.MONTHLY.value, day_of_month=9, jitter=0.0),
    dict(name="Software / nuvem", kind="expense", amount="63",
         category="Subscriptions › Software", account="Cartão de Crédito",
         frequency=Frequency.MONTHLY.value, day_of_month=11, jitter=0.0),

    # ---- lifestyle ----------------------------------------------------
    dict(name="Lazer e cultura", kind="expense", amount="260",
         category="Entertainment › Events & shows", account="Cartão de Crédito",
         frequency=Frequency.MONTHLY.value, day_of_month=21, jitter=0.55, skip=0.15),
    dict(name="Vestuário", kind="expense", amount="320",
         category="Shopping › Clothing & shoes", account="Cartão de Crédito",
         frequency=Frequency.CUSTOM_MONTHS.value, interval=2, day_of_month=19,
         jitter=0.6, skip=0.2),
    dict(name="Pet (ração e vet)", kind="expense", amount="185",
         category="Pets › Pet food", account="Cartão de Crédito",
         frequency=Frequency.MONTHLY.value, day_of_month=13, jitter=0.25),
    dict(name="Curso de inglês", kind="expense", amount="349",
         category="Education › Languages", account="Conta Corrente",
         frequency=Frequency.MONTHLY.value, day_of_month=15, jitter=0.0,
         months_active=10),
    dict(name="Viagem de julho", kind="expense", amount="4600",
         category="Travel › Accommodation", account="Cartão de Crédito",
         frequency=Frequency.ANNUAL.value, month_of_year=7, day_of_month=8,
         growth_pct="10", growth_anchor_month=7, jitter=0.18),

    # ---- taxes & fees -------------------------------------------------
    dict(name="Imposto trimestral (autônomo)", kind="expense", amount="640",
         category="Taxes › Income tax", account="Conta Corrente",
         frequency=Frequency.QUARTERLY.value, month_of_year=2, day_of_month=20,
         jitter=0.15),
    dict(name="Tarifas bancárias", kind="expense", amount="34.90",
         category="Fees & banking › Bank fees", account="Conta Corrente",
         frequency=Frequency.MONTHLY.value, day_of_month=28, jitter=0.0),

    # ---- savings, investments, debt ----------------------------------
    dict(name="Reserva de emergência", kind="transfer", amount="700",
         account="Conta Corrente", to_account="Poupança",
         frequency=Frequency.MONTHLY.value, day_of_month=6, jitter=0.0,
         goal="Reserva de emergência"),
    dict(name="Aporte em investimentos", kind="transfer", amount="1400",
         account="Conta Corrente", to_account="Investimentos",
         frequency=Frequency.MONTHLY.value, day_of_month=6, jitter=0.12,
         goal="Entrada do apartamento"),
    dict(name="Reserva da viagem", kind="transfer", amount="300",
         account="Conta Corrente", to_account="Poupança",
         frequency=Frequency.MONTHLY.value, day_of_month=6, jitter=0.0,
         goal="Viagem de férias"),
    dict(name="Saque para carteira", kind="transfer", amount="200",
         account="Conta Corrente", to_account="Carteira",
         frequency=Frequency.MONTHLY.value, day_of_month=2, jitter=0.15),
    dict(name="Parcela do financiamento", kind="transfer", amount="948",
         account="Conta Corrente", to_account="Financiamento do Carro",
         frequency=Frequency.MONTHLY.value, day_of_month=15, jitter=0.0,
         debt="Financiamento do Carro"),
    dict(name="Pagamento do cartão", kind="transfer", amount="2350",
         account="Conta Corrente", to_account="Cartão de Crédito",
         frequency=Frequency.MONTHLY.value, day_of_month=10, jitter=0.15,
         debt="Cartão de Crédito", include_in_budget=False),
]

DEMO_GOALS = [
    dict(name="Reserva de emergência", goal_type=GoalType.EMERGENCY_FUND.value,
         target_amount="36000", starting_amount="6500", planned_monthly="700",
         account="Poupança", priority=1, icon="🚨",
         target_date_offset_months=30,
         notes="Six months of core expenses."),
    dict(name="Viagem de férias", goal_type=GoalType.VACATION.value,
         target_amount="9000", starting_amount="0", planned_monthly="300",
         account="Poupança", priority=3, icon="✈️",
         target_date_offset_months=11),
    dict(name="Entrada do apartamento", goal_type=GoalType.HOME_DOWN_PAYMENT.value,
         target_amount="80000", starting_amount="18400", planned_monthly="1400",
         account="Investimentos", priority=2, icon="🏠",
         target_date_offset_months=60),
    dict(name="Troca do carro", goal_type=GoalType.VEHICLE.value,
         target_amount="25000", starting_amount="0", planned_monthly="0",
         priority=5, icon="🚙", target_date_offset_months=42,
         notes="No contributions yet — deliberately behind, to show the alert."),
]

DEMO_DEBTS = [
    # ``include_in_budget=False``: the spending charged to this card is already
    # budgeted category by category, so giving the payment its own allocation
    # would claim the same money twice.
    dict(name="Cartão de Crédito", debt_type=DebtType.CREDIT_CARD.value,
         principal_balance="1850", interest_rate="180", minimum_payment="280",
         planned_payment="3200", due_day=10, account="Cartão de Crédito",
         include_in_budget=False),
    dict(name="Financiamento do Carro", debt_type=DebtType.CAR_LOAN.value,
         principal_balance="31500", original_principal="42000",
         interest_rate="21.9", minimum_payment="948", planned_payment="948",
         extra_payment="100", due_day=15, account="Financiamento do Carro"),
]

#: One-off surprises, as (month offset from the first demo month, payload).
DEMO_ONE_OFFS = [
    (2, dict(description="Conserto da geladeira", amount="820",
             category="Housing › Repairs & maintenance", account="Cartão de Crédito")),
    (5, dict(description="Presente de aniversário", amount="360",
             category="Family & gifts › Donations", account="Cartão de Crédito")),
    (7, dict(description="Multa de trânsito", amount="195",
             category="Transportation › Tolls", account="Conta Corrente")),
    (9, dict(description="Dentista (canal)", amount="1450",
             category="Healthcare › Doctor & dentist", account="Conta Corrente")),
    (11, dict(description="Notebook novo", amount="4200",
              category="Shopping › Electronics", account="Cartão de Crédito")),
    (13, dict(description="Restituição do IR", amount="1780", kind="income",
              category="Government benefits › Tax refund", account="Conta Corrente")),
    (15, dict(description="Venda de móveis usados", amount="650", kind="income",
              category="Other income", account="Carteira")),
    (16, dict(description="Pneus novos", amount="1980",
              category="Transportation › Vehicle maintenance", account="Conta Corrente")),
]

PROPERTY_VALUATIONS = [(0, "365000"), (6, "372000"), (12, "384000"), (17, "391500")]


@dataclass
class DemoReport:
    accounts: int = 0
    rules: int = 0
    transactions_planned: int = 0
    transactions_completed: int = 0
    one_offs: int = 0
    goals: int = 0
    debts: int = 0
    budget_periods: int = 0
    budget_lines: int = 0
    snapshots: int = 0
    first_period: str = ""
    last_period: str = ""

    def summary(self) -> str:
        return (
            f"{self.transactions_completed} completed and {self.transactions_planned} planned "
            f"transactions, {self.rules} recurring rules, {self.budget_periods} budgets "
            f"({self.first_period} → {self.last_period}), {self.goals} goals, {self.debts} debts."
        )


# --------------------------------------------------------------------------
def has_demo_data(session: Session) -> bool:
    return session.execute(
        select(RecurringRule.id).where(RecurringRule.name == "Salário").limit(1)
    ).first() is not None


def is_empty(session: Session) -> bool:
    from database.seed import is_database_empty

    return is_database_empty(session)


def _account_map(session: Session) -> dict[str, Account]:
    return {row.name: row for row in session.execute(select(Account)).scalars()}


def _create_accounts(session: Session, opening: date,
                     report: DemoReport) -> dict[str, Account]:
    existing = _account_map(session)
    for spec in DEMO_ACCOUNTS:
        if spec["name"] in existing:
            continue
        payload = dict(spec)
        payload["opening_balance"] = money(payload.get("opening_balance", "0"))
        for key in ("credit_limit", "interest_rate"):
            if key in payload:
                payload[key] = D(payload[key])
        session.add(Account(currency="BRL", opening_date=opening, **payload))
        report.accounts += 1
    session.flush()
    _retire_unused_defaults(session)
    return _account_map(session)


def _retire_unused_defaults(session: Session) -> None:
    """Drop the generic starter accounts if the user never touched them.

    The demo brings its own named accounts; leaving "Checking account" and
    "Savings account" beside them just adds clutter with zero balances.
    """
    from sqlalchemy import func

    for name in ("Checking account", "Cash / wallet", "Savings account"):
        account = session.execute(
            select(Account).where(Account.name == name)
        ).scalars().first()
        if account is None:
            continue
        used = session.execute(
            select(func.count(Transaction.id)).where(
                (Transaction.account_id == account.id)
                | (Transaction.to_account_id == account.id)
            )
        ).scalar() or 0
        if not used and not account.opening_balance:
            session.delete(account)
    session.flush()


def _resolve_category(session: Session, path: Optional[str], kind: str) -> Optional[int]:
    if not path:
        return None
    category = category_service.resolve_path(session, path, kind=kind)
    if category is None:
        category = category_service.resolve_path(session, path)
    return category.id if category is not None else None


def _create_rules(session: Session, accounts: dict[str, Account],
                  goals: dict[str, Goal], debts: dict[str, Debt],
                  start: date, end: date, report: DemoReport) -> list[RecurringRule]:
    created: list[RecurringRule] = []
    for spec in DEMO_RULES:
        if session.execute(
            select(RecurringRule.id).where(RecurringRule.name == spec["name"])
        ).first():
            continue
        kind = spec["kind"]
        category_kind = CategoryKind.INCOME.value if kind == TxnKind.INCOME.value \
            else CategoryKind.EXPENSE.value
        months_active = spec.get("months_active")
        rule_end = end
        if months_active:
            rule_end = min(end, start + timedelta(days=int(months_active) * 31))
        rule = RecurringRule(
            name=spec["name"],
            kind=kind,
            amount=money(spec["amount"]),
            category_id=_resolve_category(session, spec.get("category"), category_kind),
            account_id=accounts[spec["account"]].id if spec.get("account") else None,
            to_account_id=accounts[spec["to_account"]].id if spec.get("to_account") else None,
            goal_id=goals[spec["goal"]].id if spec.get("goal") in goals else None,
            debt_id=debts[spec["debt"]].id if spec.get("debt") in debts else None,
            frequency=spec["frequency"],
            interval=int(spec.get("interval", 1)),
            day_of_month=spec.get("day_of_month"),
            month_of_year=spec.get("month_of_year"),
            start_date=start,
            end_date=rule_end if months_active else None,
            growth_pct=D(spec.get("growth_pct", 0)),
            growth_every_months=int(spec.get("growth_every_months", 12)),
            growth_anchor_month=spec.get("growth_anchor_month"),
            seasonal_factors=spec.get("seasonal_factors"),
            business_day_rule=spec.get("business_day_rule", BusinessDayRule.NONE.value),
            settlement_offset_days=int(spec.get("settlement_offset_days", 0)),
            description_template=spec.get("description_template"),
            tags=DEMO_TAG,
            is_active=True,
            auto_generate=True,
            include_in_budget=True,
        )
        session.add(rule)
        created.append(rule)
        report.rules += 1
    session.flush()
    return created


def _jitter_for(name: str) -> tuple[float, float]:
    for spec in DEMO_RULES:
        if spec["name"] == name:
            return float(spec.get("jitter", 0.0)), float(spec.get("skip", 0.0))
    return 0.0, 0.0


def _materialise(session: Session, rules: list[RecurringRule], start: date, end: date,
                 today: date, rng: random.Random, report: DemoReport) -> None:
    """Create planned transactions, then actualise the ones in the past."""
    for rule in rules:
        jitter, skip = _jitter_for(rule.name)
        spec = spec_from_rule(rule)
        for occurrence in generate_occurrences(spec, start, end):
            in_past = occurrence.due_date <= today
            if in_past and skip and rng.random() < skip:
                continue

            amount = occurrence.amount
            status = TxnStatus.PLANNED.value
            actual_date: Optional[date] = None
            if in_past:
                status = TxnStatus.COMPLETED.value
                if jitter:
                    factor = Decimal(str(1 + rng.uniform(-jitter, jitter)))
                    amount = money(amount * factor)
                    if amount <= 0:
                        amount = money(occurrence.amount)
                drift = rng.choice([0, 0, 0, 1, -1, 2]) if jitter else 0
                actual_date = occurrence.cash_date + timedelta(days=drift)
                if actual_date > today:
                    actual_date = today

            description = rule.description_template or rule.name
            try:
                description = description.format(
                    name=rule.name, date=occurrence.due_date.isoformat(),
                    amount=amount, month=occurrence.due_date.strftime("%m"),
                    year=occurrence.due_date.year,
                )
            except (KeyError, IndexError, ValueError):
                description = rule.name

            session.add(Transaction(
                txn_date=occurrence.due_date,
                actual_date=actual_date,
                availability_date=(occurrence.cash_date
                                   if rule.kind == TxnKind.INCOME.value
                                   and occurrence.cash_date != occurrence.due_date else None),
                description=description[:240],
                amount=amount,
                kind=rule.kind,
                status=status,
                category_id=rule.category_id,
                account_id=rule.account_id,
                to_account_id=rule.to_account_id,
                goal_id=rule.goal_id,
                debt_id=rule.debt_id,
                rule_id=rule.id,
                occurrence_key=occurrence.key,
                tags=DEMO_TAG,
                is_planned=True,
                fingerprint=fingerprint(occurrence.due_date, amount, description,
                                        rule.account_id, rule.kind),
            ))
            if status == TxnStatus.COMPLETED.value:
                report.transactions_completed += 1
            else:
                report.transactions_planned += 1
        rule.generated_through = end
    session.flush()


def _add_one_offs(session: Session, accounts: dict[str, Account], first: Period,
                  today: date, seed: int, report: DemoReport) -> None:
    """The surprises — a broken fridge, a dentist bill, a tax refund.

    Each uses its own seeded day so a repeated load lands on the same date, and
    an existing entry with the same description is left alone rather than
    duplicated.
    """
    for offset, spec in DEMO_ONE_OFFS:
        period = shift_period(first, offset)
        if period.start > today:
            continue
        description = spec["description"]
        already = session.execute(
            select(Transaction.id).where(Transaction.description == description).limit(1)
        ).first()
        if already:
            continue

        local_rng = random.Random(seed + offset)
        day = min(local_rng.randint(3, 26), 28)
        when = date(period.year, period.month, day)
        if when > today:
            when = today
        kind = spec.get("kind", TxnKind.EXPENSE.value)
        category_kind = (CategoryKind.INCOME.value if kind == TxnKind.INCOME.value
                         else CategoryKind.EXPENSE.value)
        account = accounts.get(spec["account"])
        amount = money(spec["amount"])
        session.add(Transaction(
            txn_date=when,
            actual_date=when,
            description=description,
            amount=amount,
            kind=kind,
            status=TxnStatus.COMPLETED.value,
            category_id=_resolve_category(session, spec.get("category"), category_kind),
            account_id=account.id if account else None,
            tags=f"{DEMO_TAG}, unplanned",
            is_planned=False,
            fingerprint=fingerprint(when, amount, description,
                                    account.id if account else None, kind),
        ))
        report.one_offs += 1
    session.flush()


def _create_goals(session: Session, accounts: dict[str, Account], today: date,
                  report: DemoReport) -> dict[str, Goal]:
    for spec in DEMO_GOALS:
        if session.execute(select(Goal.id).where(Goal.name == spec["name"])).first():
            continue
        offset = int(spec.get("target_date_offset_months", 12))
        year, month = divmod(today.month - 1 + offset, 12)
        target_date = date(today.year + year, month + 1, min(today.day, 28))
        account = accounts.get(spec.get("account", ""))
        session.add(Goal(
            name=spec["name"],
            goal_type=spec["goal_type"],
            target_amount=money(spec["target_amount"]),
            starting_amount=money(spec.get("starting_amount", "0")),
            planned_monthly=money(spec.get("planned_monthly", "0")),
            target_date=target_date,
            start_date=today.replace(day=1),
            account_id=account.id if account else None,
            priority=int(spec.get("priority", 3)),
            status=GoalStatus.ACTIVE.value,
            icon=spec.get("icon"),
            notes=spec.get("notes"),
        ))
        report.goals += 1
    session.flush()
    return {row.name: row for row in session.execute(select(Goal)).scalars()}


def _create_debts(session: Session, accounts: dict[str, Account], today: date,
                  report: DemoReport) -> dict[str, Debt]:
    for spec in DEMO_DEBTS:
        if session.execute(select(Debt.id).where(Debt.name == spec["name"])).first():
            continue
        account = accounts.get(spec.get("account", ""))
        session.add(Debt(
            name=spec["name"],
            debt_type=spec["debt_type"],
            principal_balance=money(spec["principal_balance"]),
            original_principal=money(spec.get("original_principal",
                                              spec["principal_balance"])),
            interest_rate=D(spec.get("interest_rate", 0)),
            minimum_payment=money(spec.get("minimum_payment", 0)),
            planned_payment=money(spec.get("planned_payment", 0)),
            extra_payment=money(spec.get("extra_payment", 0)),
            due_day=spec.get("due_day"),
            account_id=account.id if account else None,
            balance_as_of=today,
            is_active=True,
            include_in_budget=bool(spec.get("include_in_budget", True)),
        ))
        report.debts += 1
    session.flush()
    return {row.name: row for row in session.execute(select(Debt)).scalars()}


def _create_budgets(session: Session, first: Period, count: int,
                    today: date, report: DemoReport) -> None:
    """Plan every period in the window, then mark past ones closed.

    A closed period refuses regeneration, so re-running the loader leaves the
    already-closed history alone instead of failing.
    """
    for offset in range(count):
        period = shift_period(first, offset)
        row = budget_service.get_or_create_period(session, period.year, period.month)
        report.budget_periods += 1
        if row.status == PeriodStatus.CLOSED.value:
            continue
        plan = budget_service.generate_from_rules(session, period)
        report.budget_lines += plan.created
        if period.end < today:
            row.status = PeriodStatus.CLOSED.value
        elif period.contains(today):
            row.status = PeriodStatus.ACTIVE.value
    session.flush()


def _accrue_loan_interest(session: Session, accounts: dict[str, Account],
                          today: date, report: DemoReport) -> None:
    """A financed car shrinks by payment minus interest, not by the payment."""
    from services import account_service

    loan = accounts.get("Financiamento do Carro")
    if loan is None:
        return
    result = account_service.accrue_interest(
        session, loan.id, through=today, since=loan.opening_date, day_of_month=16
    )
    report.transactions_completed += int(result.get("posted", 0))


def _add_valuations(session: Session, accounts: dict[str, Account],
                    first: Period, today: date) -> None:
    """Property valuations, clamped to today and de-duplicated by date.

    With a short demo window several offsets can clamp onto the same day; the
    latest value for a given date wins rather than causing a collision.
    """
    apartment = accounts.get("Apartamento")
    if apartment is None:
        return
    planned: dict[date, str] = {}
    for offset, value in PROPERTY_VALUATIONS:
        period = shift_period(first, offset)
        planned[min(period.end, today)] = value

    for as_of, value in sorted(planned.items()):
        existing = session.execute(
            select(AccountValuation).where(
                AccountValuation.account_id == apartment.id,
                AccountValuation.as_of_date == as_of,
            )
        ).scalars().first()
        if existing is not None:
            existing.value = money(value)
            continue
        session.add(AccountValuation(
            account_id=apartment.id, as_of_date=as_of,
            value=money(value), notes="Estimated market value",
        ))
    session.flush()


def _add_snapshots(session: Session, first: Period, count: int,
                   today: date, report: DemoReport) -> None:
    for offset in range(0, count, 3):
        period = shift_period(first, offset)
        if period.end > today:
            break
        networth_service.save_snapshot(session, as_of=period.end)
        report.snapshots += 1
    session.flush()


# --------------------------------------------------------------------------
def load_demo_data(
    session: Session,
    *,
    months_back: int = 18,
    months_forward: int = 6,
    today: Optional[date] = None,
    seed: int = DEMO_SEED,
) -> DemoReport:
    """Populate the current database with the demo dataset.

    Safe to call twice — existing rules, goals, debts and accounts with the
    same names are left alone rather than duplicated.
    """
    today = today or date.today()
    rng = random.Random(seed)
    report = DemoReport()

    seed_defaults(session)
    current = make_period(today.year, today.month)
    first = shift_period(current, -months_back + 1)
    last = shift_period(current, months_forward)
    accounts = _create_accounts(session, first.start, report)

    goals = _create_goals(session, accounts, today, report)
    debts = _create_debts(session, accounts, today, report)
    rules = _create_rules(session, accounts, goals, debts, first.start, last.end, report)
    _materialise(session, rules, first.start, last.end, today, rng, report)
    _add_one_offs(session, accounts, first, today, seed, report)
    _accrue_loan_interest(session, accounts, today, report)
    _add_valuations(session, accounts, first, today)
    _create_budgets(session, first, months_back + months_forward, today, report)
    _add_snapshots(session, first, months_back, today, report)

    report.first_period = first.key
    report.last_period = last.key
    session.flush()
    return report


def clear_all_data(session: Session, *, keep_accounts: bool = True,
                   keep_categories: bool = True) -> dict[str, int]:
    """Wipe transactional data, leaving a clean book. Irreversible.

    Categories and accounts are kept by default so the user does not have to
    rebuild their setup after dismissing the demo.
    """
    counts: dict[str, int] = {}
    order = [
        ("transactions", Transaction),
        ("budget_lines", BudgetLine),
        ("budget_periods", BudgetPeriod),
        ("recurring_rules", RecurringRule),
        ("goals", Goal),
        ("debts", Debt),
        ("net_worth_snapshots", NetWorthSnapshot),
        ("account_valuations", AccountValuation),
        ("import_batches", ImportBatch),
        ("recycle_bin", RecycleBin),
    ]
    from sqlalchemy import func

    def _count(model) -> int:
        return int(session.execute(select(func.count()).select_from(model)).scalar() or 0)

    for name, model in order:
        counts[name] = _count(model)
        session.execute(delete(model))
    if not keep_accounts:
        counts["accounts"] = _count(Account)
        session.execute(delete(Account))
    if not keep_categories:
        from database.models import Category

        counts["categories"] = _count(Category)
        session.execute(delete(Category))
    session.flush()
    seed_defaults(session)
    return counts
