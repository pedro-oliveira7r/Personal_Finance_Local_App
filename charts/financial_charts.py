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
    money_text,
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


def comparison_bars(metrics: dict, theme: ChartTheme, *, height: int = 300,
                    title: Optional[str] = None,
                    previous_label: str = "Previous",
                    current_label: str = "Current") -> go.Figure:
    """Two-period comparison across the headline metrics."""
    if not metrics:
        return empty_figure(theme, "Not enough history to compare yet.", height)
    keys = [key for key in ("income", "expenses", "savings", "investments", "debt_payments")
            if key in metrics]
    if not keys:
        return empty_figure(theme, "Not enough history to compare yet.", height)

    names = {"income": "Income", "expenses": "Expenses", "savings": "Savings",
             "investments": "Investments", "debt_payments": "Debt payments"}
    labels = [names[key] for key in keys]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=labels, y=[to_float(metrics[key]["previous"]) for key in keys],
        name=previous_label, marker=bar_marker(theme, theme.series(3)),
        text=money_text([metrics[key]["previous"] for key in keys], theme.currency),
        textposition="outside", textfont={"size": 10, "color": theme.text_secondary},
        hovertemplate=previous_label + ": " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
    ))
    fig.add_trace(go.Bar(
        x=labels, y=[to_float(metrics[key]["current"]) for key in keys],
        name=current_label, marker=bar_marker(theme, theme.series(0)),
        text=money_text([metrics[key]["current"] for key in keys], theme.currency),
        textposition="outside", textfont={"size": 10, "color": theme.text_secondary},
        hovertemplate=current_label + ": " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
    ))
    layout = base_layout(theme, height=height, title=title)
    layout["barmode"] = "group"
    layout["bargap"] = 0.3
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


def strategy_comparison_bars(results: dict, theme: ChartTheme, *, height: int = 280,
                             title: Optional[str] = None) -> go.Figure:
    """Total interest paid under each payoff strategy. One measure, one axis."""
    usable = {name: result for name, result in results.items()
              if not result.never_pays_off and result.months}
    if not usable:
        return empty_figure(
            theme,
            "At least one debt never gets paid off at the current payment — "
            "raise it above the monthly interest first.",
            height,
        )
    names = {"avalanche": "Avalanche (highest rate first)",
             "snowball": "Snowball (smallest balance first)",
             "minimum_only": "Minimums only"}
    labels = [names.get(key, key) for key in usable]
    fig = go.Figure(go.Bar(
        x=labels, y=[to_float(result.total_interest) for result in usable.values()],
        marker={"color": theme.colors(len(usable)), "cornerradius": 4,
                "line": {"color": theme.surface, "width": 2}},
        text=[f"{format_money(result.total_interest, theme.currency, compact=True)} · "
              f"{result.months} mo" for result in usable.values()],
        textposition="outside", textfont={"size": 10, "color": theme.text_secondary},
        hovertemplate="%{x}<br>Interest: " + theme.tick_prefix + "%{y:,.2f}<extra></extra>",
    ))
    layout = base_layout(theme, height=height, title=title, show_legend=False)
    fig.update_layout(**layout)
    return fig


# --------------------------------------------------------------------------
# Forecast
# --------------------------------------------------------------------------
def forecast_components_bars(rows: Sequence, theme: ChartTheme, *, height: int = 340,
                             title: Optional[str] = None) -> go.Figure:
    """Projected income against the components of projected outflow."""
    if not rows:
        return empty_figure(theme, "Nothing to forecast yet.", height)
    labels = [row.label for row in rows]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=labels, y=[to_float(row.assumption.income) for row in rows],
        name="Income", marker=bar_marker(theme, theme.series(2)),
        hovertemplate="Income: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
    ))
    for name, attr, slot in (("Expenses", "expenses", 1),
                             ("Investments", "investments", 6),
                             ("Debt payments", "debt_payments", 7),
                             ("Savings (moved out)", "savings_outflow", 4)):
        values = [to_float(getattr(row.assumption, attr)) for row in rows]
        if not any(values):
            continue
        fig.add_trace(go.Bar(
            x=labels, y=values, name=name,
            marker=bar_marker(theme, theme.series(slot)),
            offsetgroup="out", base=None,
            hovertemplate=name + ": " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
        ))
    layout = base_layout(theme, height=height, title=title)
    layout["barmode"] = "stack"
    layout["bargap"] = 0.3
    fig.update_layout(**layout)
    return fig


def scenario_comparison_line(base_rows: Sequence, scenario_rows: Sequence,
                             theme: ChartTheme, *, height: int = 340,
                             scenario_name: str = "Scenario",
                             title: Optional[str] = None) -> go.Figure:
    """Baseline projection against a what-if, on the same axis."""
    if not base_rows:
        return empty_figure(theme, "Build a forecast first.", height)
    labels = [row.label for row in base_rows]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=labels, y=[to_float(row.closing_cash) for row in base_rows],
        name="Baseline", mode="lines+markers",
        line=line_marker(theme, theme.series(0)),
        marker={"size": 7, "line": {"color": theme.surface, "width": 2}},
        hovertemplate="Baseline: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
    ))
    if scenario_rows:
        fig.add_trace(go.Scatter(
            x=[row.label for row in scenario_rows],
            y=[to_float(row.closing_cash) for row in scenario_rows],
            name=scenario_name, mode="lines+markers",
            line=line_marker(theme, theme.series(1), dash="dash"),
            marker={"size": 7, "symbol": "diamond",
                    "line": {"color": theme.surface, "width": 2}},
            hovertemplate=(scenario_name + ": " + theme.tick_prefix +
                           "%{y:,.2f}<extra>%{x}</extra>"),
        ))
    layout = base_layout(theme, height=height, title=title)
    layout["hovermode"] = "x unified"
    fig.update_layout(**layout)
    fig.add_hline(y=0, line_width=1, line_color=theme.axis)
    return fig


def budget_accuracy_bars(rows: Sequence[dict], theme: ChartTheme, *, height: int = 300,
                         title: Optional[str] = None) -> go.Figure:
    """How realistic past budgets were — 100% means the plan matched reality."""
    if not rows:
        return empty_figure(theme, "Close a few periods to measure budget accuracy.", height)
    labels = [row["label"] for row in rows]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=labels, y=[to_float(row["income_accuracy"]) for row in rows],
        name="Income plan accuracy", marker=bar_marker(theme, theme.series(2)),
        hovertemplate="Income accuracy: %{y:.1f}%<extra>%{x}</extra>",
    ))
    fig.add_trace(go.Bar(
        x=labels, y=[to_float(row["expense_accuracy"]) for row in rows],
        name="Spending plan accuracy", marker=bar_marker(theme, theme.series(0)),
        hovertemplate="Spending accuracy: %{y:.1f}%<extra>%{x}</extra>",
    ))
    layout = base_layout(theme, height=height, title=title)
    layout["barmode"] = "group"
    layout["bargap"] = 0.3
    layout["yaxis"]["ticksuffix"] = "%"
    layout["yaxis"]["tickprefix"] = ""
    layout["yaxis"]["range"] = [0, 105]
    layout["yaxis"]["showgrid"] = True
    fig.update_layout(**layout)
    return fig
