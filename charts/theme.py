"""Chart design system: palette, layout defaults and shared helpers.

The categorical palette is a validated set — the eight hues below clear the
lightness band, chroma floor, colour-vision-deficiency separation and
normal-vision separation gates in both light and dark mode, on the surfaces
declared here. Slots are assigned **in fixed order and never cycled**; a ninth
series folds into "Other" instead of inventing a hue.

Three light-mode slots sit below 3:1 contrast against the light surface, so
every chart that uses them also ships a relief channel: direct value labels
and/or the "Show data" table the UI renders beneath each figure.

Status colours (good / warning / serious / critical) are reserved and never
reused as a series colour, and always travel with an icon or label so meaning
never rests on hue alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence

from constants import CURRENCY_FORMATS

# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------
CATEGORICAL_LIGHT = [
    "#2a78d6",  # 1 blue
    "#eb6834",  # 2 orange
    "#1baf7a",  # 3 aqua
    "#eda100",  # 4 yellow
    "#e87ba4",  # 5 magenta
    "#008300",  # 6 green
    "#4a3aa7",  # 7 violet
    "#e34948",  # 8 red
]

CATEGORICAL_DARK = [
    "#3987e5", "#d95926", "#199e70", "#c98500",
    "#d55181", "#008300", "#9085e9", "#e66767",
]

#: Forms that plot every pair against every other (scatter, treemap, pie) can
#: only safely use the first three slots. Past that, fold into "Other".
ALL_PAIRS_SAFE_SLOTS = 3

SEQUENTIAL_BLUE = [
    "#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec",
    "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab",
    "#184f95", "#104281", "#0d366b",
]

STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}

#: The same four roles re-stepped for a dark plane. Kept distinct from the dark
#: categorical slots so a status colour never impersonates a series.
STATUS_DARK = {
    "good": "#81c995",
    "warning": "#fdd663",
    "serious": "#fcad70",
    "critical": "#f28b82",
}

DIVERGING = ("#2a78d6", "#e34948")  # cool ↔ warm poles

FONT_FAMILY = 'system-ui, -apple-system, "Segoe UI", sans-serif'


@dataclass
class ChartTheme:
    dark: bool = False
    surface: str = "#fcfcfb"
    page: str = "#f9f9f7"
    text_primary: str = "#0b0b0b"
    text_secondary: str = "#52514e"
    muted: str = "#898781"
    grid: str = "#e1e0d9"
    axis: str = "#c3c2b7"
    positive_text: str = "#006300"
    categorical: Sequence[str] = field(default_factory=lambda: CATEGORICAL_LIGHT)
    currency: str = "BRL"

    @property
    def diverging_mid(self) -> str:
        return "#383835" if self.dark else "#f0efec"

    def series(self, index: int) -> str:
        """Colour for slot ``index`` — fixed order, never cycled past 8."""
        palette = self.categorical
        return palette[index] if index < len(palette) else self.muted

    def colors(self, count: int) -> list[str]:
        return [self.series(i) for i in range(count)]

    def status(self, key: str) -> str:
        """Reserved status colours, stepped for the surface in play.

        The light steps are unreadable on a dark plane and vice versa, so each
        mode gets its own — still distinct from every categorical slot, and
        still always paired with an icon or label so hue never carries meaning
        on its own.
        """
        table = STATUS_DARK if self.dark else STATUS
        return table.get(key, self.muted)

    @property
    def separators(self) -> str:
        """Plotly ``layout.separators``: decimal char + thousands char."""
        fmt = CURRENCY_FORMATS.get((self.currency or "BRL").upper())
        if not fmt:
            return ".,"
        return f"{fmt['decimal']}{fmt['thousands']}"

    @property
    def symbol(self) -> str:
        fmt = CURRENCY_FORMATS.get((self.currency or "BRL").upper())
        return fmt["symbol"] if fmt else ""

    @property
    def tick_prefix(self) -> str:
        symbol = self.symbol
        return f"{symbol} " if symbol else ""


def get_theme(palette, currency: str = "BRL") -> ChartTheme:
    """Build a chart theme from the application palette.

    Charts take their surface, grid and ink from the same place the rest of the
    interface does, so a figure can never end up on a plane that does not match
    the page behind it. Only the *series* colours are chart-specific — those are
    the validated sets above, which are stepped for their own surface.
    """
    dark = getattr(palette, "is_dark", False)
    return ChartTheme(
        dark=dark,
        surface=palette.chart_surface,
        page=palette.background,
        text_primary=palette.text,
        text_secondary=palette.text_secondary,
        muted=palette.text_muted,
        grid=palette.chart_grid,
        axis=palette.chart_axis,
        positive_text=palette.good,
        categorical=CATEGORICAL_DARK if dark else CATEGORICAL_LIGHT,
        currency=currency,
    )


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------
def base_layout(
    theme: ChartTheme,
    *,
    height: int = 320,
    title: Optional[str] = None,
    show_legend: bool = True,
    legend_horizontal: bool = True,
    margin_top: int = 44,
    x_title: Optional[str] = None,
    y_title: Optional[str] = None,
    money_axis: str = "y",
) -> dict:
    """Layout every chart in the app shares."""
    layout: dict = {
        "height": height,
        "paper_bgcolor": theme.surface,
        "plot_bgcolor": theme.surface,
        "separators": theme.separators,
        "font": {"family": FONT_FAMILY, "size": 12, "color": theme.text_secondary},
        "margin": {"l": 8, "r": 12, "t": margin_top if title else 16, "b": 8},
        "hoverlabel": {
            "bgcolor": theme.surface,
            "bordercolor": theme.axis,
            "font": {"family": FONT_FAMILY, "size": 12, "color": theme.text_primary},
        },
        "showlegend": show_legend,
        "colorway": list(theme.categorical),
        "xaxis": _axis(theme, title=x_title, money=money_axis == "x"),
        "yaxis": _axis(theme, title=y_title, money=money_axis == "y"),
    }
    if title:
        layout["title"] = {
            "text": title,
            "font": {"size": 14, "color": theme.text_primary},
            "x": 0, "xanchor": "left", "y": 0.97, "yanchor": "top",
        }
    if show_legend:
        layout["legend"] = (
            {"orientation": "h", "yanchor": "bottom", "y": 1.02,
             "xanchor": "left", "x": 0, "font": {"size": 11},
             "bgcolor": "rgba(0,0,0,0)"}
            if legend_horizontal else
            {"orientation": "v", "yanchor": "top", "y": 1,
             "xanchor": "left", "x": 1.02, "font": {"size": 11},
             "bgcolor": "rgba(0,0,0,0)"}
        )
    return layout


def _axis(theme: ChartTheme, *, title: Optional[str] = None, money: bool = False) -> dict:
    axis: dict = {
        "showgrid": money,
        "gridcolor": theme.grid,
        "gridwidth": 1,
        "zeroline": money,
        "zerolinecolor": theme.axis,
        "zerolinewidth": 1,
        "linecolor": theme.axis,
        "tickfont": {"size": 11, "color": theme.muted},
        "title": {"text": title, "font": {"size": 11, "color": theme.muted}} if title else None,
        "automargin": True,
    }
    if money:
        axis["tickprefix"] = theme.tick_prefix
        axis["separatethousands"] = True
        axis["showline"] = False
    else:
        axis["showline"] = True
    return axis


#: Applied to every bar so fills never touch: a 2px ring in the surface colour.
def bar_marker(theme: ChartTheme, color: str, *, gap: bool = True) -> dict:
    marker: dict = {"color": color, "cornerradius": 4}
    if gap:
        marker["line"] = {"color": theme.surface, "width": 2}
    return marker


def line_marker(theme: ChartTheme, color: str, *, dash: Optional[str] = None) -> dict:
    line: dict = {"color": color, "width": 2}
    if dash:
        line["dash"] = dash
    return line


EMPTY_LAYOUT_TEMPLATE = "No data yet — {hint}"


def empty_figure(theme: ChartTheme, message: str, height: int = 220):
    """A blank chart that explains itself instead of showing an empty grid."""
    import plotly.graph_objects as go

    fig = go.Figure()
    fig.update_layout(
        **base_layout(theme, height=height, show_legend=False),
        annotations=[{
            "text": message,
            "x": 0.5, "y": 0.5, "xref": "paper", "yref": "paper",
            "showarrow": False,
            "font": {"size": 13, "color": theme.muted},
        }],
    )
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


# --------------------------------------------------------------------------
# Hover / label formatting
# --------------------------------------------------------------------------
def money_hover(theme: ChartTheme, label: str = "%{y}") -> str:
    return f"{theme.tick_prefix}{label}<extra></extra>"


def money_text(values, currency: str = "BRL", *, compact: bool = True) -> list[str]:
    """Direct data labels — the relief channel for low-contrast slots."""
    from calculations.money import format_money

    return [format_money(value, currency, compact=compact) for value in values]


def truncate(label: str, limit: int = 22) -> str:
    text = str(label)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fold_to_other(rows: Sequence[dict], *, key: str = "amount",
                  limit: int = 8, other_label: str = "Other") -> list[dict]:
    """Keep the ``limit`` largest rows; everything else becomes one "Other".

    This is what keeps the palette honest — a ninth category never gets a
    generated colour.
    """
    ordered = sorted(rows, key=lambda row: abs(Decimal(str(row.get(key, 0)))), reverse=True)
    if len(ordered) <= limit:
        return list(ordered)
    head = list(ordered[:limit - 1])
    tail = ordered[limit - 1:]
    total = sum((Decimal(str(row.get(key, 0))) for row in tail), Decimal("0"))
    head.append({"label": other_label, key: total, "category_id": None,
                 "kind": tail[0].get("kind"), "color": None,
                 "folded_count": len(tail)})
    return head
