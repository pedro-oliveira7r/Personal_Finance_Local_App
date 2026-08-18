"""Cash-flow, account balances and income-availability rules.

Two questions this module answers, and they are genuinely different:

*How much money do I have?*
    Account balances derived from opening balances plus completed movements.

*How much money can I budget with this period?*
    Income only becomes budgetable when it is *available*, and that depends on
    the user's chosen rule. A salary earned in January but paid on 31 January
    can legitimately fund January (accrual thinking), February (the common
    "this paycheck pays next month's bills" pattern), or whichever period
    contains the actual deposit date.

Sign convention for balances: **assets positive, liabilities negative.** A
credit card you owe 1.200 on has a balance of ``-1200``. ``owed`` properties
flip it back for display, so users always type and read positive numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Mapping, Optional, Sequence

from calculations.money import ZERO, D, money, money_sum, pct_of
from calculations.periods import Period, next_period, period_for_date
from constants import (
    CASH_ACCOUNT_TYPES,
    LIABILITY_ACCOUNT_TYPES,
    AvailabilityRule,
    BalanceMode,
    CategoryKind,
    TxnKind,
    TxnStatus,
)


# ==========================================================================
# Input shapes (ORM-free so the maths is testable in isolation)
# ==========================================================================
@dataclass
class AccountInfo:
    id: int
    type: str
    name: str = ""
    opening_balance: Decimal = ZERO
    opening_date: Optional[date] = None
    include_in_cash: bool = True
    include_in_net_worth: bool = True
    balance_mode: str = BalanceMode.TRANSACTIONS.value
    #: ``(as_of_date, value)`` pairs, ascending. Used when ``balance_mode`` is
    #: manual — property and market-priced holdings are re-valued, not summed.
    valuations: list[tuple[date, Decimal]] = field(default_factory=list)
    credit_limit: Optional[Decimal] = None

    def __post_init__(self) -> None:
        self.opening_balance = money(self.opening_balance)
        self.valuations = sorted(
            ((when, money(value)) for when, value in (self.valuations or [])),
            key=lambda item: item[0],
        )

    @property
    def manual_value(self) -> Optional[Decimal]:
        return self.valuations[-1][1] if self.valuations else None

    def manual_value_at(self, as_of: Optional[date] = None) -> Optional[Decimal]:
        """Latest valuation on or before ``as_of``; ``None`` before the first one."""
        if not self.valuations:
            return None
        if as_of is None:
            return self.valuations[-1][1]
        chosen: Optional[Decimal] = None
        for when, value in self.valuations:
            if when <= as_of:
                chosen = value
            else:
                break
        return chosen

    @property
    def is_liability(self) -> bool:
        return self.type in LIABILITY_ACCOUNT_TYPES

    @property
    def sign(self) -> int:
        return -1 if self.is_liability else 1

    @property
    def is_cash_like(self) -> bool:
        return self.type in CASH_ACCOUNT_TYPES and self.include_in_cash


@dataclass
class CashTxn:
    """The subset of a transaction the cash-flow maths needs."""

    txn_date: date
    amount: Decimal
    kind: str
    status: str = TxnStatus.COMPLETED.value
    actual_date: Optional[date] = None
    availability_date: Optional[date] = None
    account_id: Optional[int] = None
    to_account_id: Optional[int] = None
    category_id: Optional[int] = None
    category_kind: Optional[str] = None
    exclude_from_budget: bool = False
    id: Optional[int] = None

    def __post_init__(self) -> None:
        self.amount = money(self.amount)

    @property
    def effective_date(self) -> date:
        return self.actual_date or self.txn_date

    @property
    def is_completed(self) -> bool:
        return self.status == TxnStatus.COMPLETED.value


def account_info_from_orm(account,
                          valuations: Optional[list[tuple[date, Decimal]]] = None) -> AccountInfo:
    return AccountInfo(
        id=account.id,
        type=account.type,
        name=account.name,
        opening_balance=account.opening_balance,
        opening_date=account.opening_date,
        include_in_cash=account.include_in_cash,
        include_in_net_worth=account.include_in_net_worth,
        balance_mode=account.balance_mode,
        valuations=list(valuations or []),
        credit_limit=account.credit_limit,
    )


def cash_txn_from_orm(txn, category_kinds: Optional[Mapping[int, str]] = None) -> CashTxn:
    kind = None
    if txn.category_id and category_kinds:
        kind = category_kinds.get(txn.category_id)
    return CashTxn(
        id=txn.id,
        txn_date=txn.txn_date,
        amount=txn.amount,
        kind=txn.kind,
        status=txn.status,
        actual_date=txn.actual_date,
        availability_date=txn.availability_date,
        account_id=txn.account_id,
        to_account_id=txn.to_account_id,
        category_id=txn.category_id,
        category_kind=kind,
        exclude_from_budget=txn.exclude_from_budget,
    )


# ==========================================================================
# Income availability
# ==========================================================================
def resolve_availability_date(
    txn: CashTxn,
    rule: str = AvailabilityRule.EARNED_PERIOD.value,
    *,
    cutoff_day: int = 25,
    first_day_of_month: int = 1,
) -> date:
    """The date on which this income becomes budgetable money.

    A per-transaction ``availability_date`` always wins — that is the manual
    escape hatch for the one payment that behaved differently.
    """
    if txn.availability_date:
        return txn.availability_date

    earned = txn.txn_date
    settled = txn.actual_date or txn.txn_date

    if rule == AvailabilityRule.ACTUAL_DATE.value:
        return settled

    if rule == AvailabilityRule.NEXT_PERIOD.value:
        current = period_for_date(earned, first_day_of_month)
        return next_period(current, first_day_of_month).start

    if rule == AvailabilityRule.CUTOFF_DAY.value:
        if settled.day > int(cutoff_day):
            current = period_for_date(settled, first_day_of_month)
            return next_period(current, first_day_of_month).start
        return settled

    # AvailabilityRule.EARNED_PERIOD (default)
    return earned


def availability_period(
    txn: CashTxn,
    rule: str = AvailabilityRule.EARNED_PERIOD.value,
    *,
    cutoff_day: int = 25,
    first_day_of_month: int = 1,
) -> Period:
    resolved = resolve_availability_date(
        txn, rule, cutoff_day=cutoff_day, first_day_of_month=first_day_of_month
    )
    return period_for_date(resolved, first_day_of_month)


def available_income_for_period(
    txns: Iterable[CashTxn],
    period: Period,
    rule: str = AvailabilityRule.EARNED_PERIOD.value,
    *,
    cutoff_day: int = 25,
    first_day_of_month: int = 1,
    include_planned: bool = True,
) -> Decimal:
    """Income that this period is allowed to spend."""
    total = ZERO
    for txn in txns:
        if txn.kind != TxnKind.INCOME.value or txn.exclude_from_budget:
            continue
        if txn.status == TxnStatus.VOID.value:
            continue
        if not include_planned and txn.status == TxnStatus.PLANNED.value:
            continue
        target = availability_period(
            txn, rule, cutoff_day=cutoff_day, first_day_of_month=first_day_of_month
        )
        if target.key == period.key:
            total += txn.amount
    return money(total)


@dataclass
class IncomeTiming:
    """The four ways income can be counted, for the same period."""

    earned: Decimal = ZERO       # belongs to the period (accrual)
    expected: Decimal = ZERO     # still planned, cash has not arrived
    received: Decimal = ZERO     # cash actually landed inside the period
    available: Decimal = ZERO    # budgetable in this period per the rule
    late: Decimal = ZERO         # earned here, cash arrived after period end


def income_timing(
    txns: Iterable[CashTxn],
    period: Period,
    rule: str = AvailabilityRule.EARNED_PERIOD.value,
    *,
    cutoff_day: int = 25,
    first_day_of_month: int = 1,
) -> IncomeTiming:
    timing = IncomeTiming()
    for txn in txns:
        if txn.kind != TxnKind.INCOME.value or txn.status == TxnStatus.VOID.value:
            continue
        earned_here = period.contains(txn.txn_date)
        if earned_here:
            timing.earned = money(timing.earned + txn.amount)
            if txn.status == TxnStatus.PLANNED.value:
                timing.expected = money(timing.expected + txn.amount)
            elif txn.actual_date and txn.actual_date > period.end:
                timing.late = money(timing.late + txn.amount)
        if txn.is_completed and period.contains(txn.effective_date):
            timing.received = money(timing.received + txn.amount)
    timing.available = available_income_for_period(
        txns, period, rule, cutoff_day=cutoff_day,
        first_day_of_month=first_day_of_month,
    )
    return timing


# ==========================================================================
# Balances
# ==========================================================================
def account_movement(
    txns: Iterable[CashTxn],
    account_id: int,
    *,
    as_of: Optional[date] = None,
    include_planned: bool = False,
) -> Decimal:
    """Asset-style delta for one account: ``+in −out``.

    Applies to liabilities too — charging a credit card produces a negative
    movement, which combined with :func:`account_balance` makes the debt grow.
    """
    total = ZERO
    for txn in txns:
        if txn.status == TxnStatus.VOID.value:
            continue
        if not include_planned and not txn.is_completed:
            continue
        when = txn.effective_date
        if as_of is not None and when > as_of:
            continue
        if txn.kind == TxnKind.INCOME.value:
            if txn.account_id == account_id:
                total += txn.amount
        elif txn.kind == TxnKind.EXPENSE.value:
            if txn.account_id == account_id:
                total -= txn.amount
        else:  # transfer
            if txn.account_id == account_id:
                total -= txn.amount
            if txn.to_account_id == account_id:
                total += txn.amount
    return money(total)


def account_balance(
    info: AccountInfo,
    txns: Iterable[CashTxn],
    *,
    as_of: Optional[date] = None,
    include_planned: bool = False,
) -> Decimal:
    """Signed balance: assets positive, liabilities negative."""
    if info.balance_mode == BalanceMode.MANUAL.value:
        valued = info.manual_value_at(as_of)
        if valued is not None:
            return money(info.sign * valued)

    opening = info.opening_balance
    if as_of is not None and info.opening_date and info.opening_date > as_of:
        opening = ZERO
    movement = account_movement(
        txns, info.id, as_of=as_of, include_planned=include_planned
    )
    return money(info.sign * opening + movement)


def balances_as_of(
    accounts: Sequence[AccountInfo],
    txns: Sequence[CashTxn],
    on_date: Optional[date] = None,
    *,
    include_planned: bool = False,
) -> dict[int, Decimal]:
    return {
        info.id: account_balance(
            info, txns, as_of=on_date, include_planned=include_planned
        )
        for info in accounts
    }


def cash_available(
    accounts: Sequence[AccountInfo],
    txns: Sequence[CashTxn],
    on_date: Optional[date] = None,
    *,
    include_planned: bool = False,
) -> Decimal:
    """Spendable cash: checking + savings + wallet, at a point in time."""
    total = ZERO
    for info in accounts:
        if not info.is_cash_like:
            continue
        total += account_balance(
            info, txns, as_of=on_date, include_planned=include_planned
        )
    return money(total)


def credit_utilisation(info: AccountInfo, balance: Decimal) -> Optional[Decimal]:
    """Percentage of a card's limit in use, or ``None`` when no limit is set."""
    if not info.credit_limit or info.credit_limit <= 0:
        return None
    owed = -balance if balance < 0 else ZERO
    return pct_of(owed, info.credit_limit)


# ==========================================================================
# Per-period cash flow
# ==========================================================================
@dataclass
class PeriodCashflow:
    period: Period
    opening_cash: Decimal = ZERO
    income_received: Decimal = ZERO
    income_available: Decimal = ZERO
    income_earned: Decimal = ZERO
    expenses_paid: Decimal = ZERO
    transfers_in: Decimal = ZERO
    transfers_out: Decimal = ZERO
    savings_contributed: Decimal = ZERO
    investments_contributed: Decimal = ZERO
    debt_paid: Decimal = ZERO
    net_flow: Decimal = ZERO
    closing_cash: Decimal = ZERO
    is_actual: bool = True
    txn_count: int = 0
    by_category: dict[int, Decimal] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.period.short_label

    @property
    def savings_rate(self) -> Decimal:
        """Share of received income that was saved or invested."""
        saved = money(self.savings_contributed + self.investments_contributed)
        return pct_of(saved, self.income_received)

    @property
    def core_expenses(self) -> Decimal:
        """Expenses excluding savings/investment/debt-flagged categories."""
        return money(
            self.expenses_paid - self.savings_contributed
            - self.investments_contributed - self.debt_paid
        )


def period_cashflow(
    period: Period,
    accounts: Sequence[AccountInfo],
    txns: Sequence[CashTxn],
    *,
    availability_rule: str = AvailabilityRule.EARNED_PERIOD.value,
    cutoff_day: int = 25,
    first_day_of_month: int = 1,
    include_planned: bool = False,
    opening_override: Optional[Decimal] = None,
) -> PeriodCashflow:
    """Everything that happened (or is planned) inside one period."""
    cash_ids = {info.id for info in accounts if info.is_cash_like}

    opening = (
        money(opening_override)
        if opening_override is not None
        else cash_available(
            accounts, txns, period.start - timedelta(days=1), include_planned=False
        )
    )
    flow = PeriodCashflow(period=period, opening_cash=opening,
                          is_actual=not include_planned)

    for txn in txns:
        if txn.status == TxnStatus.VOID.value:
            continue
        counts = txn.is_completed or (include_planned and txn.status == TxnStatus.PLANNED.value)
        if txn.kind == TxnKind.INCOME.value and period.contains(txn.txn_date):
            flow.income_earned = money(flow.income_earned + txn.amount)
        if not counts or not period.contains(txn.effective_date):
            continue

        flow.txn_count += 1
        if txn.kind == TxnKind.INCOME.value:
            if txn.account_id in cash_ids:
                flow.income_received = money(flow.income_received + txn.amount)
        elif txn.kind == TxnKind.EXPENSE.value:
            if txn.account_id in cash_ids:
                flow.expenses_paid = money(flow.expenses_paid + txn.amount)
            if txn.category_id is not None:
                flow.by_category[txn.category_id] = money(
                    flow.by_category.get(txn.category_id, ZERO) + txn.amount
                )
            if txn.category_kind == CategoryKind.SAVINGS.value:
                flow.savings_contributed = money(flow.savings_contributed + txn.amount)
            elif txn.category_kind == CategoryKind.INVESTMENT.value:
                flow.investments_contributed = money(flow.investments_contributed + txn.amount)
            elif txn.category_kind == CategoryKind.DEBT.value:
                flow.debt_paid = money(flow.debt_paid + txn.amount)
        else:  # transfer
            from_cash = txn.account_id in cash_ids
            to_cash = txn.to_account_id in cash_ids
            if from_cash and not to_cash:
                flow.transfers_out = money(flow.transfers_out + txn.amount)
                target = next((a for a in accounts if a.id == txn.to_account_id), None)
                if target is not None:
                    if target.type == "investment":
                        flow.investments_contributed = money(
                            flow.investments_contributed + txn.amount)
                    elif target.is_liability:
                        flow.debt_paid = money(flow.debt_paid + txn.amount)
            elif to_cash and not from_cash:
                flow.transfers_in = money(flow.transfers_in + txn.amount)

    flow.income_available = available_income_for_period(
        txns, period, availability_rule, cutoff_day=cutoff_day,
        first_day_of_month=first_day_of_month, include_planned=include_planned,
    )
    flow.net_flow = money(
        flow.income_received - flow.expenses_paid + flow.transfers_in - flow.transfers_out
    )
    flow.closing_cash = money(flow.opening_cash + flow.net_flow)
    return flow


def cashflow_series(
    periods: Sequence[Period],
    accounts: Sequence[AccountInfo],
    txns: Sequence[CashTxn],
    *,
    availability_rule: str = AvailabilityRule.EARNED_PERIOD.value,
    cutoff_day: int = 25,
    first_day_of_month: int = 1,
    today: Optional[date] = None,
    chain_opening: bool = True,
) -> list[PeriodCashflow]:
    """Cash flow for a run of periods, chaining closing → opening balances.

    Periods that end before ``today`` are marked actual; later ones include
    planned transactions and are marked forecast.
    """
    today = today or date.today()
    results: list[PeriodCashflow] = []
    running: Optional[Decimal] = None
    for period in periods:
        include_planned = period.end >= today
        flow = period_cashflow(
            period, accounts, txns,
            availability_rule=availability_rule,
            cutoff_day=cutoff_day,
            first_day_of_month=first_day_of_month,
            include_planned=include_planned,
            opening_override=running if (chain_opening and running is not None) else None,
        )
        flow.is_actual = period.end < today
        results.append(flow)
        running = flow.closing_cash
    return results


def carry_in_for_period(
    period: Period,
    accounts: Sequence[AccountInfo],
    txns: Sequence[CashTxn],
) -> Decimal:
    """Cash on hand the moment the period opens (completed movements only)."""
    return cash_available(accounts, txns, period.start - timedelta(days=1))


def upcoming_outflows(
    txns: Iterable[CashTxn],
    start: date,
    days: int = 30,
    *,
    minimum: Decimal = ZERO,
) -> list[CashTxn]:
    """Planned expenses in the next ``days`` days, largest first."""
    end = start + timedelta(days=days)
    upcoming = [
        txn for txn in txns
        if txn.kind == TxnKind.EXPENSE.value
        and txn.status == TxnStatus.PLANNED.value
        and start <= txn.effective_date <= end
        and txn.amount >= D(minimum)
    ]
    return sorted(upcoming, key=lambda t: (-t.amount, t.effective_date))
