"""Net worth: assets minus liabilities, and how it moves over time.

Balances arrive here already signed (assets positive, liabilities negative —
see :mod:`calculations.cashflow`), so the split is simply "which side of zero".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from calculations.money import ZERO, D, money, money_sum, pct_of
from calculations.periods import Period
from constants import ASSET_ACCOUNT_TYPES, LIABILITY_ACCOUNT_TYPES


@dataclass
class NetWorthLine:
    account_id: Optional[int]
    name: str
    type: str
    balance: Decimal          # signed
    include: bool = True

    @property
    def is_liability(self) -> bool:
        return self.balance < 0 or self.type in LIABILITY_ACCOUNT_TYPES

    @property
    def magnitude(self) -> Decimal:
        return abs(money(self.balance))


@dataclass
class NetWorthSummary:
    as_of: date
    total_assets: Decimal = ZERO
    total_liabilities: Decimal = ZERO
    net_worth: Decimal = ZERO
    assets: list[NetWorthLine] = field(default_factory=list)
    liabilities: list[NetWorthLine] = field(default_factory=list)
    by_type: dict[str, Decimal] = field(default_factory=dict)

    @property
    def debt_to_asset_pct(self) -> Decimal:
        return pct_of(self.total_liabilities, self.total_assets)

    @property
    def is_solvent(self) -> bool:
        return self.net_worth >= 0

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "total_assets": self.total_assets,
            "total_liabilities": self.total_liabilities,
            "net_worth": self.net_worth,
            "assets": [(line.name, line.magnitude) for line in self.assets],
            "liabilities": [(line.name, line.magnitude) for line in self.liabilities],
            "by_type": dict(self.by_type),
        }


def summarise_net_worth(lines: Sequence[NetWorthLine], as_of: Optional[date] = None) -> NetWorthSummary:
    """Split signed balances into assets and liabilities and total them up."""
    summary = NetWorthSummary(as_of=as_of or date.today())
    for line in lines:
        if not line.include:
            continue
        if line.balance >= 0 and line.type in ASSET_ACCOUNT_TYPES:
            summary.assets.append(line)
            summary.total_assets = money(summary.total_assets + line.balance)
        elif line.balance < 0 and line.type in ASSET_ACCOUNT_TYPES:
            # An overdrawn checking account is a liability in substance.
            summary.liabilities.append(line)
            summary.total_liabilities = money(summary.total_liabilities + line.magnitude)
        elif line.type in LIABILITY_ACCOUNT_TYPES:
            if line.balance <= 0:
                summary.liabilities.append(line)
                summary.total_liabilities = money(summary.total_liabilities + line.magnitude)
            else:
                # Overpaid card — a small asset.
                summary.assets.append(line)
                summary.total_assets = money(summary.total_assets + line.balance)
        else:
            summary.assets.append(line)
            summary.total_assets = money(summary.total_assets + line.balance)

        summary.by_type[line.type] = money(summary.by_type.get(line.type, ZERO) + line.balance)

    summary.net_worth = money(summary.total_assets - summary.total_liabilities)
    summary.assets.sort(key=lambda line: line.magnitude, reverse=True)
    summary.liabilities.sort(key=lambda line: line.magnitude, reverse=True)
    return summary


@dataclass
class NetWorthPoint:
    as_of: date
    total_assets: Decimal
    total_liabilities: Decimal
    net_worth: Decimal
    label: str = ""

    @property
    def is_solvent(self) -> bool:
        return self.net_worth >= 0


def series_from_summaries(summaries: Sequence[NetWorthSummary]) -> list[NetWorthPoint]:
    return [
        NetWorthPoint(
            as_of=item.as_of,
            total_assets=item.total_assets,
            total_liabilities=item.total_liabilities,
            net_worth=item.net_worth,
        )
        for item in sorted(summaries, key=lambda s: s.as_of)
    ]


def change_between(points: Sequence[NetWorthPoint]) -> dict[str, Decimal]:
    """First-to-last delta of a net-worth series."""
    if len(points) < 2:
        return {"absolute": ZERO, "percent": ZERO, "monthly_average": ZERO}
    first, last = points[0], points[-1]
    absolute = money(last.net_worth - first.net_worth)
    months = max(1, (last.as_of.year - first.as_of.year) * 12
                 + (last.as_of.month - first.as_of.month))
    return {
        "absolute": absolute,
        "percent": pct_of(absolute, abs(first.net_worth)) if first.net_worth else ZERO,
        "monthly_average": money(absolute / Decimal(months)),
    }


def project_net_worth(
    start: NetWorthPoint,
    monthly_savings: Decimal,
    monthly_debt_reduction: Decimal,
    months: int,
    *,
    annual_return_pct: Decimal = ZERO,
) -> list[NetWorthPoint]:
    """Straight-line projection with optional compounding on assets."""
    points: list[NetWorthPoint] = [start]
    assets = D(start.total_assets)
    liabilities = D(start.total_liabilities)
    rate = D(annual_return_pct) / Decimal(1200)
    current = start.as_of
    for index in range(1, max(0, int(months)) + 1):
        assets = assets * (Decimal(1) + rate) + D(monthly_savings)
        liabilities = max(ZERO, liabilities - D(monthly_debt_reduction))
        year, month = divmod((current.month - 1) + index, 12)
        as_of = date(current.year + year, month + 1,
                     min(current.day, 28))
        points.append(NetWorthPoint(
            as_of=as_of,
            total_assets=money(assets),
            total_liabilities=money(liabilities),
            net_worth=money(assets - liabilities),
        ))
    return points


def liquidity_ratio(cash: Decimal, monthly_expenses: Decimal) -> Optional[Decimal]:
    """Months of expenses covered by cash on hand — the emergency-fund metric."""
    if D(monthly_expenses) <= 0:
        return None
    return (D(cash) / D(monthly_expenses)).quantize(Decimal("0.1"))
