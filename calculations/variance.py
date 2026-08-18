"""Planned-vs-actual variance.

Sign convention — fixed once, used everywhere:

``variance = actual − planned``

That is the raw difference, so it always reads the same way regardless of what
is being measured. Whether the difference is *good* depends on the line type
and is carried separately in :attr:`VarianceRow.favorable`:

* expense-like line — spending less than planned is favourable
* income-like line — earning more than planned is favourable

``remaining`` is what is still expected to happen (``planned − actual``,
floored at zero for display purposes via :attr:`VarianceRow.remaining_positive`)
and ``consumed_pct`` is ``actual / planned``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from calculations.money import ZERO, D, money, money_sum, pct_of
from constants import CategoryKind, Severity

# Status codes
STATUS_NONE = "none"                # nothing planned, nothing spent
STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_OVER = "over"                # expense past its limit
STATUS_SHORT = "short"              # income below plan
STATUS_UNBUDGETED = "unbudgeted"    # activity with no plan at all
STATUS_UNUSED = "unused"            # planned but nothing happened

STATUS_LABELS = {
    STATUS_NONE: "—",
    STATUS_OK: "On track",
    STATUS_WARNING: "Approaching limit",
    STATUS_OVER: "Over budget",
    STATUS_SHORT: "Below plan",
    STATUS_UNBUDGETED: "Not budgeted",
    STATUS_UNUSED: "Untouched",
}

STATUS_SEVERITY = {
    STATUS_NONE: Severity.INFO.value,
    STATUS_OK: Severity.SUCCESS.value,
    STATUS_WARNING: Severity.WARNING.value,
    STATUS_OVER: Severity.CRITICAL.value,
    STATUS_SHORT: Severity.WARNING.value,
    STATUS_UNBUDGETED: Severity.WARNING.value,
    STATUS_UNUSED: Severity.INFO.value,
}

#: Icons so status is never communicated by colour alone.
STATUS_ICONS = {
    STATUS_NONE: "·",
    STATUS_OK: "✓",
    STATUS_WARNING: "!",
    STATUS_OVER: "▲",
    STATUS_SHORT: "▼",
    STATUS_UNBUDGETED: "?",
    STATUS_UNUSED: "○",
}

INCOME_LIKE = {CategoryKind.INCOME.value, "income"}


@dataclass
class VarianceRow:
    label: str
    kind: str
    planned: Decimal
    actual: Decimal
    key: Optional[str] = None
    category_id: Optional[int] = None
    parent_label: Optional[str] = None
    period_key: Optional[str] = None

    variance: Decimal = ZERO
    variance_pct: Decimal = ZERO
    remaining: Decimal = ZERO
    consumed_pct: Decimal = ZERO
    favorable: Optional[bool] = None
    status: str = STATUS_NONE

    def __post_init__(self) -> None:
        self.planned = money(self.planned)
        self.actual = money(self.actual)

    # -- derived helpers ---------------------------------------------------
    @property
    def is_income(self) -> bool:
        return self.kind in INCOME_LIKE

    @property
    def remaining_positive(self) -> Decimal:
        return self.remaining if self.remaining > 0 else ZERO

    @property
    def overshoot(self) -> Decimal:
        """Amount past the plan (expense) or missing from it (income)."""
        if self.is_income:
            return -self.variance if self.variance < 0 else ZERO
        return self.variance if self.variance > 0 else ZERO

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def status_icon(self) -> str:
        return STATUS_ICONS.get(self.status, "·")

    @property
    def severity(self) -> str:
        return STATUS_SEVERITY.get(self.status, Severity.INFO.value)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "parent": self.parent_label,
            "kind": self.kind,
            "period": self.period_key,
            "planned": self.planned,
            "actual": self.actual,
            "variance": self.variance,
            "variance_pct": self.variance_pct,
            "remaining": self.remaining,
            "consumed_pct": self.consumed_pct,
            "favorable": self.favorable,
            "status": self.status,
            "status_label": self.status_label,
            "status_icon": self.status_icon,
        }


def compute_variance(
    planned: Decimal | float | int | str,
    actual: Decimal | float | int | str,
    kind: str = CategoryKind.EXPENSE.value,
    *,
    label: str = "",
    warning_pct: Decimal = Decimal("80"),
    critical_pct: Decimal = Decimal("100"),
    tolerance_pct: Decimal = Decimal("5"),
    **extra,
) -> VarianceRow:
    """Build a fully-populated :class:`VarianceRow`."""
    row = VarianceRow(label=label, kind=kind, planned=planned, actual=actual, **extra)

    row.variance = money(row.actual - row.planned)
    row.remaining = money(row.planned - row.actual)
    row.variance_pct = pct_of(row.variance, row.planned) if row.planned else ZERO
    row.consumed_pct = pct_of(row.actual, row.planned) if row.planned else ZERO

    if row.planned == 0 and row.actual == 0:
        row.status, row.favorable = STATUS_NONE, None
        return row

    if row.planned == 0:
        # Money moved in a category that was never budgeted.
        row.status = STATUS_UNBUDGETED
        row.favorable = row.is_income  # unplanned income is a good surprise
        return row

    if row.actual == 0:
        row.status = STATUS_UNUSED
        row.favorable = None if row.is_income else True
        return row

    if row.is_income:
        row.favorable = row.variance >= 0
        shortfall = -row.variance_pct  # positive when income is below plan
        if shortfall <= D(tolerance_pct):
            row.status = STATUS_OK
        elif row.consumed_pct >= D(warning_pct):
            row.status = STATUS_WARNING
        else:
            row.status = STATUS_SHORT
        return row

    row.favorable = row.variance <= 0
    if row.consumed_pct > D(critical_pct):
        row.status = STATUS_OVER
    elif row.consumed_pct >= D(warning_pct):
        row.status = STATUS_WARNING
    else:
        row.status = STATUS_OK
    return row


def variance_table(
    entries: Iterable[dict],
    *,
    warning_pct: Decimal = Decimal("80"),
    critical_pct: Decimal = Decimal("100"),
    tolerance_pct: Decimal = Decimal("5"),
) -> list[VarianceRow]:
    """Vectorised helper: ``[{"label":..,"kind":..,"planned":..,"actual":..}, ...]``."""
    rows: list[VarianceRow] = []
    for entry in entries:
        payload = dict(entry)
        rows.append(compute_variance(
            payload.pop("planned", ZERO),
            payload.pop("actual", ZERO),
            payload.pop("kind", CategoryKind.EXPENSE.value),
            warning_pct=warning_pct,
            critical_pct=critical_pct,
            tolerance_pct=tolerance_pct,
            **payload,
        ))
    return rows


# --------------------------------------------------------------------------
# Aggregations
# --------------------------------------------------------------------------
@dataclass
class VarianceSummary:
    planned: Decimal = ZERO
    actual: Decimal = ZERO
    variance: Decimal = ZERO
    variance_pct: Decimal = ZERO
    favorable_count: int = 0
    unfavorable_count: int = 0
    over_count: int = 0
    unbudgeted_count: int = 0
    rows: list[VarianceRow] = field(default_factory=list)

    @property
    def accuracy_pct(self) -> Decimal:
        """How close the plan was, as ``100 − |variance| / planned``.

        100% means the plan matched reality exactly; it never goes below 0.
        """
        if self.planned == 0:
            return ZERO
        raw = Decimal("100") - pct_of(abs(self.variance), self.planned)
        return raw if raw > 0 else ZERO


def summarise(rows: Sequence[VarianceRow]) -> VarianceSummary:
    summary = VarianceSummary(rows=list(rows))
    summary.planned = money_sum(row.planned for row in rows)
    summary.actual = money_sum(row.actual for row in rows)
    summary.variance = money(summary.actual - summary.planned)
    summary.variance_pct = pct_of(summary.variance, summary.planned)
    for row in rows:
        if row.favorable is True:
            summary.favorable_count += 1
        elif row.favorable is False:
            summary.unfavorable_count += 1
        if row.status == STATUS_OVER:
            summary.over_count += 1
        if row.status == STATUS_UNBUDGETED:
            summary.unbudgeted_count += 1
    return summary


def top_overspending(rows: Sequence[VarianceRow], limit: int = 5) -> list[VarianceRow]:
    """Expense rows furthest above plan, worst first."""
    candidates = [r for r in rows if not r.is_income and r.variance > 0]
    return sorted(candidates, key=lambda r: r.variance, reverse=True)[:limit]


def top_underspending(rows: Sequence[VarianceRow], limit: int = 5) -> list[VarianceRow]:
    """Expense rows furthest below plan, biggest saving first."""
    candidates = [r for r in rows if not r.is_income and r.variance < 0]
    return sorted(candidates, key=lambda r: r.variance)[:limit]


def income_shortfalls(rows: Sequence[VarianceRow], limit: int = 5) -> list[VarianceRow]:
    candidates = [r for r in rows if r.is_income and r.variance < 0]
    return sorted(candidates, key=lambda r: r.variance)[:limit]


def approaching_limit(
    rows: Sequence[VarianceRow],
    warning_pct: Decimal = Decimal("80"),
    critical_pct: Decimal = Decimal("100"),
) -> list[VarianceRow]:
    """Expense categories in the warning band but not yet over."""
    return sorted(
        [r for r in rows
         if not r.is_income and D(warning_pct) <= r.consumed_pct <= D(critical_pct)],
        key=lambda r: r.consumed_pct,
        reverse=True,
    )


def pace_projection(row: VarianceRow, elapsed_fraction: float) -> Decimal:
    """Extrapolate a category's spend to the end of the period.

    ``elapsed_fraction`` is 0-1 from :meth:`Period.elapsed_fraction`. Used for
    "at this rate you will finish the month at X" messages.
    """
    if elapsed_fraction <= 0:
        return row.actual
    return money(row.actual / D(str(elapsed_fraction)))
