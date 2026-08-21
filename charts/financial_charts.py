"""Plotly figures for Reports, Forecast, Accounts and Net worth."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

import plotly.graph_objects as go

from calculations.money import D, format_money, to_float
from charts.theme import (
    SEQUENTIAL_BLUE,
    ChartTheme,
    bar_marker,
    base_layout,
    empty_figure,
    fold_to_other,
    line_marker,
    truncate,
)


# --------------------------------------------------------------------------
# Trends
# --------------------------------------------------------------------------
def category_trend_lines(rows: Sequence[dict], theme: ChartTheme, *,
                         height: int = 340, title: Optional[str] = None,
                         max_series: int = 8) -> go.Figure:
    """One line per category over time. Capped at the palette size."""
    if not rows:
        return empty_figure(theme, "Pick a category to see its trend.", height)

    periods: list[str] = []
    for row in rows:
        if row["label"] not in periods:
            periods.append(row["label"])

    by_category: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        by_category.setdefault(row["category"], {})[row["label"]] = D(row["amount"])

    ranked = sorted(by_category.items(),
                    key=lambda item: sum(item[1].values()), reverse=True)[:max_series]
    if not ranked:
        return empty_figure(theme, "No spending found for those categories.", height)

    fig = go.Figure()
    for index, (name, series) in enumerate(ranked):
        values = [to_float(series.get(period, 0)) for period in periods]
        fig.add_trace(go.Scatter(
            x=periods, y=values, name=truncate(name, 24), mode="lines+markers",
            line=line_marker(theme, theme.series(index)),
            marker={"size": 7, "line": {"color": theme.surface, "width": 2}},
            hovertemplate=(truncate(name, 24) + ": " + theme.tick_prefix +
                           "%{y:,.2f}<extra>%{x}</extra>"),
        ))
    layout = base_layout(theme, height=height, title=title,
                         show_legend=len(ranked) >= 2)
    layout["hovermode"] = "x unified"
    fig.update_layout(**layout)
    return fig


def stacked_category_bars(rows: Sequence[dict], theme: ChartTheme, *,
                          height: int = 360, title: Optional[str] = None,
                          max_series: int = 8) -> go.Figure:
    """Composition of spending per period, stacked."""
    if not rows:
        return empty_figure(theme, "No data for this range.", height)

    periods: list[str] = []
    for row in rows:
        if row["label"] not in periods:
            periods.append(row["label"])
    by_category: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        by_category.setdefault(row["category"], {})[row["label"]] = D(row["amount"])

    ranked = sorted(by_category.items(), key=lambda item: sum(item[1].values()), reverse=True)
    head, tail = ranked[:max_series - 1], ranked[max_series - 1:]
    if tail:
        merged: dict[str, Decimal] = {}
        for _, series in tail:
            for period, amount in series.items():
                merged[period] = D(merged.get(period, 0)) + amount
        head.append((f"Other ({len(tail)})", merged))

    fig = go.Figure()
    for index, (name, series) in enumerate(head):
        fig.add_trace(go.Bar(
            x=periods, y=[to_float(series.get(period, 0)) for period in periods],
            name=truncate(name, 22),
            marker=bar_marker(theme, theme.series(index)),
            hovertemplate=(truncate(name, 22) + ": " + theme.tick_prefix +
                           "%{y:,.2f}<extra>%{x}</extra>"),
        ))
    layout = base_layout(theme, height=height, title=title)
    layout["barmode"] = "stack"
    layout["bargap"] = 0.28
    fig.update_layout(**layout)
    return fig


def spending_heatmap(rows: Sequence[dict], theme: ChartTheme, *,
                     height: int = 380, title: Optional[str] = None,
                     max_categories: int = 14) -> go.Figure:
    """Category × period magnitude. Sequential single-hue ramp, light → dark."""
    if not rows:
        return empty_figure(theme, "No data for a heatmap yet.", height)

    periods: list[str] = []
    for row in rows:
        if row["label"] not in periods:
            periods.append(row["label"])
    by_category: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        by_category.setdefault(row["category"], {})[row["label"]] = D(row["amount"])
    ranked = sorted(by_category.items(),
                    key=lambda item: sum(item[1].values()), reverse=True)[:max_categories]
    ranked.reverse()

    matrix = [[to_float(series.get(period, 0)) for period in periods]
              for _, series in ranked]
    labels = [truncate(name, 26) for name, _ in ranked]

    fig = go.Figure(go.Heatmap(
        z=matrix, x=periods, y=labels,
        colorscale=[[index / (len(SEQUENTIAL_BLUE) - 1), color]
                    for index, color in enumerate(SEQUENTIAL_BLUE)],
        xgap=2, ygap=2,
        colorbar={"tickprefix": theme.tick_prefix, "separatethousands": True,
                  "outlinewidth": 0, "thickness": 10,
                  "tickfont": {"size": 10, "color": theme.muted}},
        hovertemplate=("%{y} · %{x}<br>" + theme.tick_prefix +
                       "%{z:,.2f}<extra></extra>"),
    ))
    layout = base_layout(theme, height=height, title=title, show_legend=False,
                         money_axis=None)
    layout["margin"]["l"] = 4
    fig.update_layout(**layout)
    return fig


# --------------------------------------------------------------------------
# Net worth
# --------------------------------------------------------------------------
def net_worth_chart(points: Sequence, theme: ChartTheme, *, height: int = 360,
                    title: Optional[str] = None) -> go.Figure:
    """Assets, liabilities and the resulting net worth on one scale."""
    if not points:
        return empty_figure(theme, "Add accounts and transactions to track net worth.", height)

    labels = [point.label or point.as_of.isoformat() for point in points]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=labels, y=[to_float(point.total_assets) for point in points],
        name="Assets", marker=bar_marker(theme, theme.series(2)),
        hovertemplate="Assets: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
    ))
    fig.add_trace(go.Bar(
        x=labels, y=[-to_float(point.total_liabilities) for point in points],
        name="Liabilities", marker=bar_marker(theme, theme.series(7)),
        hovertemplate="Liabilities: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
    ))
    fig.add_trace(go.Scatter(
        x=labels, y=[to_float(point.net_worth) for point in points],
        name="Net worth", mode="lines+markers",
        line=line_marker(theme, theme.series(0)),
        marker={"size": 8, "line": {"color": theme.surface, "width": 2}},
        hovertemplate="Net worth: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
    ))
    layout = base_layout(theme, height=height, title=title)
    layout["barmode"] = "relative"
    layout["bargap"] = 0.3
    layout["hovermode"] = "x unified"
    fig.update_layout(**layout)
    fig.add_hline(y=0, line_width=1, line_color=theme.axis)
    return fig


def account_balance_bars(views: Sequence, theme: ChartTheme, *, height: int = 320,
                         title: Optional[str] = None) -> go.Figure:
    """Every account on one axis: assets to the right, debts to the left."""
    data = [view for view in views if view.balance != 0 or view.display_balance != 0]
    if not data:
        return empty_figure(theme, "Add an account to see balances.", height)
    data = sorted(data, key=lambda view: to_float(view.balance))

    labels = [truncate(view.name, 26) for view in data]
    values = [to_float(view.balance) for view in data]
    colors = [theme.status("critical") if view.balance < 0 else theme.series(2)
              for view in data]
    tags = ["owes" if view.is_liability else "holds" for view in data]

    fig = go.Figure(go.Bar(
        y=labels, x=values, orientation="h",
        marker={"color": colors, "cornerradius": 4,
                "line": {"color": theme.surface, "width": 2}},
        text=[f"{tag} {format_money(abs(D(value)), theme.currency, compact=True)}"
              for tag, value in zip(tags, values)],
        textposition="outside",
        textfont={"size": 10, "color": theme.text_secondary},
        hovertemplate="%{y}<br>" + theme.tick_prefix + "%{x:,.2f}<extra></extra>",
    ))
    layout = base_layout(theme, height=height, title=title, show_legend=False,
                         money_axis="x")
    layout["margin"]["r"] = 120
    layout["margin"]["l"] = 4
    fig.update_layout(**layout)
    fig.add_vline(x=0, line_width=1, line_color=theme.axis)
    return fig


def balance_history_lines(series: dict[str, list[tuple[str, Decimal]]],
                          theme: ChartTheme, *, height: int = 320,
                          title: Optional[str] = None) -> go.Figure:
    """Balance over time, one line per account."""
    if not series:
        return empty_figure(theme, "No balance history yet.", height)
    fig = go.Figure()
    for index, (name, points) in enumerate(list(series.items())[:8]):
        fig.add_trace(go.Scatter(
            x=[label for label, _ in points],
            y=[to_float(value) for _, value in points],
            name=truncate(name, 22), mode="lines+markers",
            line=line_marker(theme, theme.series(index)),
            marker={"size": 6, "line": {"color": theme.surface, "width": 2}},
            hovertemplate=(truncate(name, 22) + ": " + theme.tick_prefix +
                           "%{y:,.2f}<extra>%{x}</extra>"),
        ))
    layout = base_layout(theme, height=height, title=title,
                         show_legend=len(series) >= 2)
    layout["hovermode"] = "x unified"
    fig.update_layout(**layout)
    fig.add_hline(y=0, line_width=1, line_color=theme.axis)
    return fig


# --------------------------------------------------------------------------
# Debt
# --------------------------------------------------------------------------
def debt_payoff_line(series: Sequence[dict], theme: ChartTheme, *, height: int = 320,
                     title: Optional[str] = None,
                     comparison: Optional[Sequence[dict]] = None,
                     comparison_name: str = "With extra payment") -> go.Figure:
    """Projected total debt balance month by month."""
    if not series:
        return empty_figure(theme, "No active debts — nothing to project.", height)
    labels = [f"M{row['month_index']}" for row in series]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=labels, y=[to_float(row["balance"]) for row in series],
        name="Current plan", mode="lines",
        line=line_marker(theme, theme.series(1)),
        fill="tozeroy", fillcolor="rgba(235,104,52,0.12)",
        hovertemplate="Current plan: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
    ))
    if comparison:
        comp_labels = [f"M{row['month_index']}" for row in comparison]
        fig.add_trace(go.Scatter(
            x=comp_labels, y=[to_float(row["balance"]) for row in comparison],
            name=comparison_name, mode="lines",
            line=line_marker(theme, theme.series(2), dash="dash"),
            hovertemplate=(comparison_name + ": " + theme.tick_prefix +
                           "%{y:,.2f}<extra>%{x}</extra>"),
        ))
    layout = base_layout(theme, height=height, title=title,
                         show_legend=bool(comparison), x_title="Months from now")
    layout["hovermode"] = "x unified"
    fig.update_layout(**layout)
    return fig


def amortisation_split_bars(schedule: Sequence, theme: ChartTheme, *,
                            height: int = 300, limit: int = 36,
                            title: Optional[str] = None) -> go.Figure:
    """How much of each payment is principal and how much is interest."""
    rows = list(schedule)[:limit]
    if not rows:
        return empty_figure(theme, "Nothing to amortise — check the payment amount.", height)
    labels = [f"M{row.month_index}" for row in rows]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=labels, y=[to_float(row.principal) for row in rows], name="Principal",
        marker=bar_marker(theme, theme.series(2)),
        hovertemplate="Principal: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
    ))
    fig.add_trace(go.Bar(
        x=labels, y=[to_float(row.interest) for row in rows], name="Interest",
        marker=bar_marker(theme, theme.series(7)),
        hovertemplate="Interest: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
    ))
    layout = base_layout(theme, height=height, title=title)
    layout["barmode"] = "stack"
    layout["bargap"] = 0.2
    fig.update_layout(**layout)
    return fig
