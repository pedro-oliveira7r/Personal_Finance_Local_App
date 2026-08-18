"""Forward-looking projections.

The forecast chains periods: each period's closing cash is the next one's
opening cash. Where the numbers come from is explicit per period
(:class:`ForecastAssumption.source`) so the UI can tell the user *why* it
believes something — a budget you wrote, recurring rules, or an average of
recent history.

Cash vs. earmarked money
------------------------
Moving 500 from checking to a savings account does not change how much cash
exists — but it is no longer free to spend. The forecast therefore tracks

* ``closing_cash``    — every cash-like account added up
* ``reserved``        — cumulative money earmarked for goals/savings
* ``free_cash``       — ``closing_cash − reserved``, i.e. genuinely spendable

Only outflows that truly leave the cash pool (``savings_outflow``,
``investments``, ``debt_payments``, ``expenses``) reduce ``closing_cash``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Mapping, Optional, Sequence

from calculations.money import ZERO, D, money, money_sum, pct_of
from calculations.periods import Period
from constants import Severity

SOURCE_BUDGET = "budget"
SOURCE_RULES = "rules"
SOURCE_AVERAGE = "average"
SOURCE_ACTUAL = "actual"
SOURCE_MANUAL = "manual"

SOURCE_LABELS = {
    SOURCE_BUDGET: "From your budget",
    SOURCE_RULES: "From recurring rules",
    SOURCE_AVERAGE: "From recent averages",
    SOURCE_ACTUAL: "Actual (recorded)",
    SOURCE_MANUAL: "Manual assumption",
}


@dataclass
class ForecastAssumption:
    """What one future period is expected to do."""

    period_key: str
    income: Decimal = ZERO
    expenses: Decimal = ZERO           # core spending, leaves the cash pool
    savings_reserved: Decimal = ZERO   # set aside but still in a cash account
    savings_outflow: Decimal = ZERO    # left the cash pool entirely
    investments: Decimal = ZERO
    debt_payments: Decimal = ZERO
    source: str = SOURCE_RULES
    note: str = ""

    def __post_init__(self) -> None:
        for name in ("income", "expenses", "savings_reserved", "savings_outflow",
                     "investments", "debt_payments"):
            setattr(self, name, money(getattr(self, name)))

    @property
    def total_outflow(self) -> Decimal:
        """Everything that reduces cash."""
        return money(self.expenses + self.savings_outflow
                     + self.investments + self.debt_payments)

    @property
    def total_allocated(self) -> Decimal:
        """Everything the budget promised, cash-leaving or not."""
        return money(self.total_outflow + self.savings_reserved)

    def scaled(self, income_pct: Decimal = ZERO, expense_pct: Decimal = ZERO) -> "ForecastAssumption":
        """Copy with income/expenses shifted by a percentage (scenario tool)."""
        income_factor = Decimal(1) + D(income_pct) / Decimal(100)
        expense_factor = Decimal(1) + D(expense_pct) / Decimal(100)
        return ForecastAssumption(
            period_key=self.period_key,
            income=money(self.income * income_factor),
            expenses=money(self.expenses * expense_factor),
            savings_reserved=self.savings_reserved,
            savings_outflow=self.savings_outflow,
            investments=self.investments,
            debt_payments=self.debt_payments,
            source=self.source,
            note=self.note,
        )


@dataclass
class ForecastRow:
    period: Period
    assumption: ForecastAssumption
    opening_cash: Decimal = ZERO
    closing_cash: Decimal = ZERO
    net_flow: Decimal = ZERO
    reserved: Decimal = ZERO
    free_cash: Decimal = ZERO
    cumulative_saved: Decimal = ZERO
    cumulative_invested: Decimal = ZERO
    is_actual: bool = False

    @property
    def label(self) -> str:
        return self.period.short_label

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.assumption.source, self.assumption.source)

    @property
    def savings_rate(self) -> Decimal:
        saved = money(self.assumption.savings_reserved
                      + self.assumption.savings_outflow
                      + self.assumption.investments)
        return pct_of(saved, self.assumption.income)

    def as_dict(self) -> dict:
        return {
            "period": self.period.key,
            "label": self.label,
            "is_actual": self.is_actual,
            "source": self.assumption.source,
            "income": self.assumption.income,
            "expenses": self.assumption.expenses,
            "savings": money(self.assumption.savings_reserved + self.assumption.savings_outflow),
            "investments": self.assumption.investments,
            "debt_payments": self.assumption.debt_payments,
            "net_flow": self.net_flow,
            "opening_cash": self.opening_cash,
            "closing_cash": self.closing_cash,
            "reserved": self.reserved,
            "free_cash": self.free_cash,
        }


@dataclass
class ForecastAlert:
    severity: str
    code: str
    message: str
    period_key: Optional[str] = None


def build_forecast(
    periods: Sequence[Period],
    opening_cash: Decimal,
    assumptions: Mapping[str, ForecastAssumption],
    *,
    opening_reserved: Decimal = ZERO,
    default_assumption: Optional[ForecastAssumption] = None,
    today: Optional[date] = None,
) -> list[ForecastRow]:
    """Chain the periods into a projection.

    Periods missing from ``assumptions`` fall back to ``default_assumption``
    (typically the historical average), or to all-zeros.
    """
    today = today or date.today()
    rows: list[ForecastRow] = []
    cash = money(opening_cash)
    reserved = money(opening_reserved)
    saved_total = ZERO
    invested_total = ZERO

    for period in periods:
        assumption = assumptions.get(period.key)
        if assumption is None:
            if default_assumption is not None:
                assumption = ForecastAssumption(
                    period_key=period.key,
                    income=default_assumption.income,
                    expenses=default_assumption.expenses,
                    savings_reserved=default_assumption.savings_reserved,
                    savings_outflow=default_assumption.savings_outflow,
                    investments=default_assumption.investments,
                    debt_payments=default_assumption.debt_payments,
                    source=SOURCE_AVERAGE,
                    note=default_assumption.note or "Average of recent periods",
                )
            else:
                assumption = ForecastAssumption(period_key=period.key, source=SOURCE_MANUAL)

        net = money(assumption.income - assumption.total_outflow)
        cash = money(cash + net)
        reserved = money(reserved + assumption.savings_reserved)
        saved_total = money(saved_total + assumption.savings_reserved + assumption.savings_outflow)
        invested_total = money(invested_total + assumption.investments)

        rows.append(ForecastRow(
            period=period,
            assumption=assumption,
            opening_cash=money(cash - net),
            closing_cash=cash,
            net_flow=net,
            reserved=reserved,
            free_cash=money(cash - reserved),
            cumulative_saved=saved_total,
            cumulative_invested=invested_total,
            is_actual=period.end < today,
        ))
    return rows


# --------------------------------------------------------------------------
# Assumption builders
# --------------------------------------------------------------------------
def average_assumption(
    history: Sequence[Mapping[str, Decimal]],
    months: int = 6,
    *,
    period_key: str = "average",
) -> ForecastAssumption:
    """Average the last ``months`` entries of a history series.

    ``history`` items are dicts with ``income``/``expenses``/``savings``/
    ``investments``/``debt_payments`` keys — whatever the reporting layer
    already produces.
    """
    window = list(history)[-max(1, months):]
    if not window:
        return ForecastAssumption(period_key=period_key, source=SOURCE_AVERAGE)
    count = Decimal(len(window))

    def mean(key: str) -> Decimal:
        return money(money_sum(item.get(key, ZERO) for item in window) / count)

    return ForecastAssumption(
        period_key=period_key,
        income=mean("income"),
        expenses=mean("expenses"),
        savings_reserved=mean("savings"),
        investments=mean("investments"),
        debt_payments=mean("debt_payments"),
        source=SOURCE_AVERAGE,
        note=f"Average of the last {len(window)} period(s)",
    )


def scenario(
    rows: Sequence[ForecastRow],
    *,
    income_pct: Decimal = ZERO,
    expense_pct: Decimal = ZERO,
    one_off: Optional[Mapping[str, Decimal]] = None,
    opening_cash: Optional[Decimal] = None,
    opening_reserved: Decimal = ZERO,
    today: Optional[date] = None,
) -> list[ForecastRow]:
    """Re-run a forecast with income/expenses nudged and one-offs injected.

    ``one_off`` maps a period key to an extra expense in that period, for
    "what if I buy a car in March" style questions.
    """
    if not rows:
        return []
    periods = [row.period for row in rows]
    start_cash = rows[0].opening_cash if opening_cash is None else money(opening_cash)
    adjusted: dict[str, ForecastAssumption] = {}
    for row in rows:
        assumption = row.assumption.scaled(income_pct, expense_pct)
        if one_off and row.period.key in one_off:
            assumption.expenses = money(assumption.expenses + D(one_off[row.period.key]))
            assumption.note = (assumption.note + " · one-off included").strip(" ·")
        adjusted[row.period.key] = assumption
    return build_forecast(
        periods, start_cash, adjusted,
        opening_reserved=opening_reserved, today=today,
    )


# --------------------------------------------------------------------------
# Reading the result
# --------------------------------------------------------------------------
def negative_periods(rows: Sequence[ForecastRow], *, use_free_cash: bool = False) -> list[ForecastRow]:
    """Periods where the projected balance goes below zero."""
    attribute = "free_cash" if use_free_cash else "closing_cash"
    return [row for row in rows if getattr(row, attribute) < 0]


def first_negative(rows: Sequence[ForecastRow], *, use_free_cash: bool = False) -> Optional[ForecastRow]:
    hits = negative_periods(rows, use_free_cash=use_free_cash)
    return hits[0] if hits else None


def runway_periods(rows: Sequence[ForecastRow]) -> Optional[int]:
    """How many periods until cash runs out. ``None`` means it never does."""
    for index, row in enumerate(rows):
        if row.closing_cash < 0:
            return index
    return None


def lowest_point(rows: Sequence[ForecastRow]) -> Optional[ForecastRow]:
    return min(rows, key=lambda r: r.closing_cash) if rows else None


def forecast_totals(rows: Sequence[ForecastRow]) -> dict[str, Decimal]:
    return {
        "income": money_sum(r.assumption.income for r in rows),
        "expenses": money_sum(r.assumption.expenses for r in rows),
        "savings": money_sum(
            r.assumption.savings_reserved + r.assumption.savings_outflow for r in rows),
        "investments": money_sum(r.assumption.investments for r in rows),
        "debt_payments": money_sum(r.assumption.debt_payments for r in rows),
        "net_flow": money_sum(r.net_flow for r in rows),
        "closing_cash": rows[-1].closing_cash if rows else ZERO,
        "free_cash": rows[-1].free_cash if rows else ZERO,
    }


def forecast_alerts(
    rows: Sequence[ForecastRow],
    *,
    low_cash_threshold: Decimal = ZERO,
) -> list[ForecastAlert]:
    """Turn a projection into things worth acting on."""
    alerts: list[ForecastAlert] = []
    negative = first_negative(rows)
    if negative is not None:
        alerts.append(ForecastAlert(
            severity=Severity.CRITICAL.value,
            code="projected_negative_balance",
            period_key=negative.period.key,
            message=(f"Projected cash goes negative in {negative.period.label} "
                     f"({negative.closing_cash})."),
        ))

    threshold = D(low_cash_threshold)
    if threshold > 0:
        for row in rows:
            if 0 <= row.closing_cash < threshold:
                alerts.append(ForecastAlert(
                    severity=Severity.WARNING.value,
                    code="low_cash",
                    period_key=row.period.key,
                    message=(f"Cash dips to {row.closing_cash} in {row.period.label} — "
                             f"below your {threshold} comfort level."),
                ))
                break

    deficits = [row for row in rows if row.net_flow < 0 and not row.is_actual]
    if len(deficits) >= 3:
        alerts.append(ForecastAlert(
            severity=Severity.WARNING.value,
            code="repeated_deficit",
            message=f"{len(deficits)} upcoming periods spend more than they earn.",
        ))

    free_negative = [row for row in rows if row.free_cash < 0 and row.closing_cash >= 0]
    if free_negative:
        alerts.append(ForecastAlert(
            severity=Severity.WARNING.value,
            code="earmarked_overlap",
            period_key=free_negative[0].period.key,
            message=(f"In {free_negative[0].period.label} your plan spends money that is "
                     "already earmarked for goals."),
        ))
    return alerts
