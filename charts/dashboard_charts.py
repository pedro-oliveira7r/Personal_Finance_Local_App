"""Plotly figures for the Dashboard and Budget screens."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

import plotly.graph_objects as go

from calculations.money import D, format_money, to_float
from charts.theme import (
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
# Planned vs actual
# --------------------------------------------------------------------------
def planned_vs_actual_bars(rows: Sequence, theme: ChartTheme, *,
                           height: int = 380, limit: int = 12,
                           title: Optional[str] = None) -> go.Figure:
    """Horizontal grouped bars: what you planned against what happened.

    Two series, so a legend is always shown; values are direct-labelled so the
    figure stays readable without relying on colour.
    """
    data = [row for row in rows if row.planned or row.actual]
    data = sorted(data, key=lambda row: max(D(row.planned), D(row.actual)), reverse=True)[:limit]
    if not data:
        return empty_figure(theme, "Nothing planned or spent in this period yet.", height)

    data.reverse()  # largest at the top of a horizontal chart
    labels = [truncate(f"{row.status_icon} {row.label}", 30) for row in data]
    planned = [to_float(row.planned) for row in data]
    actual = [to_float(row.actual) for row in data]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=labels, x=planned, name="Planned", orientation="h",
        marker=bar_marker(theme, theme.series(0)),
        text=money_text([row.planned for row in data], theme.currency),
        textposition="outside", textfont={"size": 10, "color": theme.text_secondary},
        hovertemplate="Planned: " + theme.tick_prefix + "%{x:,.2f}<extra>%{y}</extra>",
    ))
    fig.add_trace(go.Bar(
        y=labels, x=actual, name="Actual", orientation="h",
        marker=bar_marker(theme, theme.series(1)),
        text=money_text([row.actual for row in data], theme.currency),
        textposition="outside", textfont={"size": 10, "color": theme.text_secondary},
        hovertemplate="Actual: " + theme.tick_prefix + "%{x:,.2f}<extra>%{y}</extra>",
    ))
    layout = base_layout(theme, height=height, title=title, money_axis="x")
    layout["barmode"] = "group"
    layout["bargap"] = 0.3
    layout["bargroupgap"] = 0.12
    layout["margin"]["l"] = 4
    layout["margin"]["r"] = 70
    fig.update_layout(**layout)
    return fig


def variance_diverging_bars(rows: Sequence, theme: ChartTheme, *,
                            height: int = 320, limit: int = 10,
                            title: Optional[str] = None) -> go.Figure:
    """One bar per category showing how far from plan it landed.

    Diverging encoding: warm for over budget, cool for under. A single series,
    so no legend — the axis and labels carry the meaning.
    """
    data = [row for row in rows if row.variance]
    data = sorted(data, key=lambda row: abs(D(row.variance)), reverse=True)[:limit]
    if not data:
        return empty_figure(theme, "No variances to show — plan and reality agree.", height)

    data = sorted(data, key=lambda row: D(row.variance))
    labels = [truncate(row.label, 28) for row in data]
    values = [to_float(row.variance) for row in data]
    colors = [
        theme.status("critical") if row.favorable is False else theme.status("good")
        for row in data
    ]
    signs = ["▲ over" if D(row.variance) > 0 else "▼ under" for row in data]

    fig = go.Figure(go.Bar(
        y=labels, x=values, orientation="h",
        marker={"color": colors, "cornerradius": 4,
                "line": {"color": theme.surface, "width": 2}},
        text=[f"{sign} {format_money(abs(D(row.variance)), theme.currency, compact=True)}"
              for sign, row in zip(signs, data)],
        textposition="outside", textfont={"size": 10, "color": theme.text_secondary},
        customdata=[[row.planned, row.actual] for row in data],
        hovertemplate=("%{y}<br>Planned: " + theme.tick_prefix + "%{customdata[0]:,.2f}"
                       "<br>Actual: " + theme.tick_prefix + "%{customdata[1]:,.2f}"
                       "<extra></extra>"),
    ))
    layout = base_layout(theme, height=height, title=title,
                         show_legend=False, money_axis="x")
    layout["margin"]["r"] = 90
    fig.update_layout(**layout)
    return fig


# --------------------------------------------------------------------------
# Allocation
# --------------------------------------------------------------------------
def allocation_donut(slices: Sequence[tuple[str, Decimal]], theme: ChartTheme, *,
                     height: int = 300, title: Optional[str] = None,
                     center_label: str = "", center_value: str = "") -> go.Figure:
    """Where the money is going. Folded to the all-pairs-safe slot count."""
    rows = [{"label": label, "amount": D(amount)}
            for label, amount in slices if D(amount) != 0]
    if not rows:
        return empty_figure(theme, "No allocations yet.", height)
    rows = fold_to_other(rows, limit=8)

    fig = go.Figure(go.Pie(
        labels=[row["label"] for row in rows],
        values=[to_float(row["amount"]) for row in rows],
        hole=0.62,
        sort=False,
        marker={"colors": theme.colors(len(rows)),
                "line": {"color": theme.surface, "width": 2}},
        textinfo="percent",
        textposition="inside",
        insidetextfont={"size": 11, "color": "#ffffff"},
        hovertemplate=("%{label}<br>" + theme.tick_prefix +
                       "%{value:,.2f} · %{percent}<extra></extra>"),
    ))
    layout = base_layout(theme, height=height, title=title,
                         legend_horizontal=False, money_axis=None)
    layout["margin"] = {"l": 8, "r": 8, "t": 44 if title else 12, "b": 8}
    fig.update_layout(**layout)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    if center_value:
        fig.add_annotation(
            text=f"<b>{center_value}</b><br><span style='font-size:10px'>{center_label}</span>",
            x=0.5, y=0.5, showarrow=False,
            font={"size": 16, "color": theme.text_primary},
        )
    return fig


def category_treemap(rows: Sequence[dict], theme: ChartTheme, *,
                     height: int = 340, title: Optional[str] = None) -> go.Figure:
    """Proportional spend by category. Labels carry the identity, not colour."""
    data = [row for row in rows if D(row.get("amount", 0)) > 0]
    if not data:
        return empty_figure(theme, "No spending recorded in this period.", height)
    data = fold_to_other(data, limit=12)

    total = sum(D(row["amount"]) for row in data)
    fig = go.Figure(go.Treemap(
        labels=[truncate(row["label"], 26) for row in data],
        parents=[""] * len(data),
        values=[to_float(row["amount"]) for row in data],
        marker={"colors": [row.get("color") or theme.series(index % 8)
                           for index, row in enumerate(data)],
                "line": {"color": theme.surface, "width": 2}},
        textinfo="label+value+percent root",
        textfont={"size": 11, "color": "#ffffff", "family": theme.surface and None},
        hovertemplate=("%{label}<br>" + theme.tick_prefix +
                       "%{value:,.2f}<extra></extra>"),
        tiling={"packing": "squarify"},
        root={"color": theme.surface},
    ))
    layout = base_layout(theme, height=height, title=title, show_legend=False,
                         money_axis=None)
    layout["margin"] = {"l": 4, "r": 4, "t": 40 if title else 6, "b": 4}
    fig.update_layout(**layout)
    return fig


def stacked_allocation_bars(history: Sequence[dict], theme: ChartTheme, *,
                            height: int = 340, title: Optional[str] = None) -> go.Figure:
    """Monthly outflow split into its components, stacked."""
    if not history:
        return empty_figure(theme, "No history to show yet.", height)
    labels = [row["label"] for row in history]
    series = [
        ("Expenses", "expenses", 0),
        ("Savings", "savings", 2),
        ("Investments", "investments", 6),
        ("Debt payments", "debt_payments", 1),
    ]
    fig = go.Figure()
    for name, key, slot in series:
        values = [to_float(row.get(key, 0)) for row in history]
        if not any(values):
            continue
        fig.add_trace(go.Bar(
            x=labels, y=values, name=name,
            marker=bar_marker(theme, theme.series(slot)),
            hovertemplate=(name + ": " + theme.tick_prefix +
                           "%{y:,.2f}<extra>%{x}</extra>"),
        ))
    layout = base_layout(theme, height=height, title=title)
    layout["barmode"] = "stack"
    layout["bargap"] = 0.28
    fig.update_layout(**layout)
    return fig


# --------------------------------------------------------------------------
# Cash flow
# --------------------------------------------------------------------------
def income_expense_bars(history: Sequence[dict], theme: ChartTheme, *,
                        height: int = 340, title: Optional[str] = None,
                        show_net_line: bool = False) -> go.Figure:
    """Income against total outflow, month by month."""
    if not history:
        return empty_figure(theme, "Record some transactions to see your cash flow.", height)
    labels = [row["label"] for row in history]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=labels, y=[to_float(row.get("income", 0)) for row in history],
        name="Income", marker=bar_marker(theme, theme.series(2)),
        hovertemplate="Income: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
    ))
    fig.add_trace(go.Bar(
        x=labels, y=[to_float(row.get("total_outflow", row.get("expenses", 0)))
                     for row in history],
        name="Outflow", marker=bar_marker(theme, theme.series(1)),
        hovertemplate="Outflow: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
    ))
    if show_net_line:
        fig.add_trace(go.Scatter(
            x=labels, y=[to_float(row.get("net", 0)) for row in history],
            name="Net", mode="lines+markers",
            line=line_marker(theme, theme.series(0)),
            marker={"size": 8, "line": {"color": theme.surface, "width": 2}},
            hovertemplate="Net: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
        ))
    layout = base_layout(theme, height=height, title=title)
    layout["barmode"] = "group"
    layout["bargap"] = 0.3
    layout["hovermode"] = "x unified"
    fig.update_layout(**layout)
    return fig


def cash_balance_line(rows: Sequence, theme: ChartTheme, *,
                      height: int = 340, title: Optional[str] = None,
                      show_free_cash: bool = True) -> go.Figure:
    """Cash over time: recorded history solid, projection dashed.

    Actual and forecast are the *same* entity in two states, so they share a
    hue and are distinguished by line style plus a labelled boundary — not by a
    second colour that would imply a second series.
    """
    if not rows:
        return empty_figure(theme, "No cash history to project from yet.", height)

    labels = [row.label for row in rows]
    actual = [to_float(row.closing_cash) if row.is_actual else None for row in rows]
    forecast = [to_float(row.closing_cash) if not row.is_actual else None for row in rows]

    # Join the two lines so there is no visual gap at the boundary.
    last_actual = max((i for i, row in enumerate(rows) if row.is_actual), default=None)
    if last_actual is not None and last_actual + 1 < len(rows):
        forecast[last_actual] = actual[last_actual]

    fig = go.Figure()
    if any(value is not None for value in actual):
        fig.add_trace(go.Scatter(
            x=labels, y=actual, name="Cash (recorded)", mode="lines+markers",
            line=line_marker(theme, theme.series(0)),
            marker={"size": 8, "line": {"color": theme.surface, "width": 2}},
            connectgaps=False,
            hovertemplate="Recorded: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
        ))
    fig.add_trace(go.Scatter(
        x=labels, y=forecast, name="Cash (forecast)", mode="lines+markers",
        line=line_marker(theme, theme.series(0), dash="dash"),
        marker={"size": 7, "symbol": "circle-open",
                "line": {"color": theme.series(0), "width": 2}},
        connectgaps=False,
        hovertemplate="Forecast: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
    ))
    if show_free_cash and any(row.reserved for row in rows):
        fig.add_trace(go.Scatter(
            x=labels, y=[to_float(row.free_cash) for row in rows],
            name="Free of goal earmarks", mode="lines",
            line=line_marker(theme, theme.series(6), dash="dot"),
            hovertemplate="Free cash: " + theme.tick_prefix + "%{y:,.2f}<extra>%{x}</extra>",
        ))

    layout = base_layout(theme, height=height, title=title)
    layout["hovermode"] = "x unified"
    fig.update_layout(**layout)

    if last_actual is not None and last_actual + 1 < len(labels):
        fig.add_vline(
            x=last_actual + 0.5, line_width=1, line_dash="dot",
            line_color=theme.axis,
        )
        fig.add_annotation(
            x=last_actual + 0.5, y=1.0, yref="paper", yanchor="bottom",
            text="forecast →", showarrow=False,
            font={"size": 10, "color": theme.muted},
        )
    fig.add_hline(y=0, line_width=1, line_color=theme.axis)
    return fig


def cashflow_waterfall(flow, theme: ChartTheme, *, height: int = 320,
                       title: Optional[str] = None) -> go.Figure:
    """Opening cash → income → outflows → closing cash."""
    if flow is None:
        return empty_figure(theme, "No cash movements in this period.", height)

    steps = [
        ("Opening cash", flow.opening_cash, "absolute"),
        ("Income received", flow.income_received, "relative"),
        ("Expenses paid", -D(flow.expenses_paid), "relative"),
    ]
    if flow.transfers_in:
        steps.append(("Transfers in", flow.transfers_in, "relative"))
    if flow.transfers_out:
        steps.append(("Transfers out", -D(flow.transfers_out), "relative"))
    steps.append(("Closing cash", flow.closing_cash, "total"))

    fig = go.Figure(go.Waterfall(
        orientation="v",
        measure=[step[2] for step in steps],
        x=[step[0] for step in steps],
        y=[to_float(step[1]) for step in steps],
        text=money_text([step[1] for step in steps], theme.currency),
        textposition="outside",
        textfont={"size": 10, "color": theme.text_secondary},
        connector={"line": {"color": theme.grid, "width": 1}},
        increasing={"marker": {"color": theme.status("good")}},
        decreasing={"marker": {"color": theme.status("critical")}},
        totals={"marker": {"color": theme.series(0)}},
        hovertemplate="%{x}<br>" + theme.tick_prefix + "%{y:,.2f}<extra></extra>",
    ))
    layout = base_layout(theme, height=height, title=title, show_legend=False)
    fig.update_layout(**layout)
    return fig


def savings_rate_line(history: Sequence[dict], theme: ChartTheme, *,
                      height: int = 260, title: Optional[str] = None,
                      target_pct: Optional[Decimal] = None) -> go.Figure:
    if not history:
        return empty_figure(theme, "No history yet.", height)
    labels = [row["label"] for row in history]
    values = [to_float(row.get("savings_rate", 0)) for row in history]
    fig = go.Figure(go.Scatter(
        x=labels, y=values, mode="lines+markers", name="Savings rate",
        line=line_marker(theme, theme.series(2)),
        marker={"size": 8, "line": {"color": theme.surface, "width": 2}},
        hovertemplate="%{y:.1f}%<extra>%{x}</extra>",
    ))
    layout = base_layout(theme, height=height, title=title, show_legend=False)
    layout["yaxis"]["ticksuffix"] = "%"
    layout["yaxis"]["tickprefix"] = ""
    layout["yaxis"]["showgrid"] = True
    fig.update_layout(**layout)
    if target_pct is not None:
        fig.add_hline(
            y=to_float(target_pct), line_dash="dash", line_width=1,
            line_color=theme.status("good"),
            annotation_text=f"target {to_float(target_pct):.0f}%",
            annotation_font_size=10,
            annotation_font_color=theme.muted,
        )
    return fig


# --------------------------------------------------------------------------
# Budget utilisation
# --------------------------------------------------------------------------
def utilisation_bullets(rows: Sequence, theme: ChartTheme, *,
                        height: Optional[int] = None, limit: int = 10,
                        title: Optional[str] = None) -> go.Figure:
    """A bullet bar per category: consumed against its limit.

    The bar is the spend, the tick is the plan; the % label is the relief
    channel so the reading never depends on colour alone.
    """
    data = [row for row in rows if row.planned > 0]
    data = sorted(data, key=lambda row: D(row.consumed_pct), reverse=True)[:limit]
    if not data:
        return empty_figure(theme, "Set some budget amounts to track utilisation.", 200)
    data.reverse()

    labels = [truncate(f"{row.status_icon} {row.label}", 30) for row in data]
    consumed = [min(to_float(row.consumed_pct), 140.0) for row in data]
    colors = []
    for row in data:
        if row.status == "over":
            colors.append(theme.status("critical"))
        elif row.status == "warning":
            colors.append(theme.status("warning"))
        else:
            colors.append(theme.series(0))

    fig = go.Figure(go.Bar(
        y=labels, x=consumed, orientation="h",
        marker={"color": colors, "cornerradius": 4,
                "line": {"color": theme.surface, "width": 2}},
        text=[f"{to_float(row.consumed_pct):.0f}% · "
              f"{format_money(row.actual, theme.currency, compact=True)} of "
              f"{format_money(row.planned, theme.currency, compact=True)}"
              for row in data],
        textposition="outside",
        textfont={"size": 10, "color": theme.text_secondary},
        hovertemplate="%{y}<br>%{x:.1f}% of plan<extra></extra>",
    ))
    layout = base_layout(theme, height=height or max(200, 34 * len(data) + 60),
                         title=title, show_legend=False, money_axis=None)
    layout["xaxis"]["ticksuffix"] = "%"
    layout["xaxis"]["showgrid"] = True
    layout["xaxis"]["range"] = [0, 150]
    layout["margin"]["r"] = 190
    layout["margin"]["l"] = 4
    fig.update_layout(**layout)
    fig.add_vline(x=100, line_width=1, line_dash="dash", line_color=theme.axis)
    return fig


def goal_progress_bars(progresses: Sequence, theme: ChartTheme, *,
                       height: Optional[int] = None,
                       title: Optional[str] = None) -> go.Figure:
    """Progress toward each goal, with the shortfall shown behind it."""
    data = [item for item in progresses if item.target_amount > 0]
    if not data:
        return empty_figure(theme, "Create a goal to start tracking progress.", 200)
    data = sorted(data, key=lambda item: D(item.progress_pct))

    labels = [truncate(f"{item.status_icon} {item.name}", 28) for item in data]
    saved = [to_float(item.current_amount) for item in data]
    remaining = [to_float(item.remaining) for item in data]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=labels, x=saved, name="Saved", orientation="h",
        marker=bar_marker(theme, theme.series(2)),
        hovertemplate="Saved: " + theme.tick_prefix + "%{x:,.2f}<extra>%{y}</extra>",
    ))
    fig.add_trace(go.Bar(
        y=labels, x=remaining, name="Still needed", orientation="h",
        marker=bar_marker(theme, theme.grid if not theme.dark else theme.axis),
        text=[f"{to_float(item.progress_pct):.0f}% · "
              f"{format_money(item.target_amount, theme.currency, compact=True)} target"
              for item in data],
        textposition="outside",
        textfont={"size": 10, "color": theme.text_secondary},
        hovertemplate="Remaining: " + theme.tick_prefix + "%{x:,.2f}<extra>%{y}</extra>",
    ))
    layout = base_layout(theme, height=height or max(200, 40 * len(data) + 70),
                         title=title, money_axis="x")
    layout["barmode"] = "stack"
    layout["bargap"] = 0.35
    layout["margin"]["r"] = 150
    layout["margin"]["l"] = 4
    fig.update_layout(**layout)
    return fig
