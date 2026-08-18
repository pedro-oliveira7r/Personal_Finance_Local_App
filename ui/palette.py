"""The application colour palette — one source of truth for light and dark.

Three layers have to agree on colour, and when they disagree you get the classic
half-dark screen: dark cards floating on a white page with unreadable dark text
inside them. Those layers are

1. **Streamlit's own theme**, which paints every widget it ships — inputs,
   tabs, expanders, alerts, and crucially the dataframe grid, which draws to a
   canvas and cannot be restyled with CSS at all;
2. **this app's custom CSS**, for the handful of things Streamlit does not style
   (metric cards, status pills, empty states);
3. **the Plotly figures**, which need to know the surface they sit on.

All three now read the palettes below, so they cannot drift apart.

The dark palette follows the neutral-grey, near-white-text convention used by
Google's own dark UIs: a very dark page, slightly lighter raised surfaces,
``#e3e3e3`` body text rather than pure white, and a lightened blue accent —
saturated blues that work on white are far too dark to read on black.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Palette:
    """Every colour the interface needs, for one mode."""

    name: str

    # -- surfaces ----------------------------------------------------------
    background: str          # the page itself
    surface: str             # raised things: cards, inputs, table headers
    surface_hover: str       # the same, one step lighter for hover/among rows
    sidebar_background: str
    sidebar_surface: str

    # -- ink ---------------------------------------------------------------
    text: str                # body copy
    text_secondary: str      # captions, help text
    text_muted: str          # axis labels, the quietest tier
    border: str              # hairlines, input outlines
    border_strong: str       # emphasised dividers

    # -- accent ------------------------------------------------------------
    primary: str             # buttons, focus rings, the selected nav item
    primary_contrast: str    # text that sits on top of `primary`
    link: str

    # -- status ------------------------------------------------------------
    good: str
    good_bg: str
    warning: str
    warning_bg: str
    critical: str
    critical_bg: str
    info: str
    info_bg: str

    # -- chart plane -------------------------------------------------------
    chart_surface: str
    chart_grid: str
    chart_axis: str

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"


#: Light mode. Deliberately close to the original — it reads well already, so
#: this is a formalisation of what was there rather than a redesign.
LIGHT = Palette(
    name="light",
    background="#ffffff",
    surface="#f5f5f2",
    surface_hover="#ececea",
    sidebar_background="#f8f8f6",
    sidebar_surface="#ffffff",
    text="#0b0b0b",
    text_secondary="#52514e",
    text_muted="#898781",
    border="#e1e0d9",
    border_strong="#c3c2b7",
    # Deepened just enough to clear 4.5:1 — same hues, same character, but
    # white-on-blue buttons and status text on their pale tints are now legible
    # rather than merely visible.
    primary="#1a63c4",
    primary_contrast="#ffffff",
    link="#1a63c4",
    good="#146c2e",
    good_bg="#e7f5e7",
    warning="#8a5d00",
    warning_bg="#fdf3dd",
    critical="#c5221f",
    critical_bg="#fbe9e9",
    info="#1a63c4",
    info_bg="#e8f0fc",
    chart_surface="#fcfcfb",
    chart_grid="#e1e0d9",
    chart_axis="#c3c2b7",
)

#: Dark mode. Neutral greys rather than blue-blacks, near-white body text, and
#: every accent lightened until it clears 4.5:1 against the page.
DARK = Palette(
    name="dark",
    background="#1b1b1b",
    surface="#282a2c",
    surface_hover="#333537",
    sidebar_background="#1b1b1b",
    sidebar_surface="#2d2f31",
    text="#e3e3e3",
    text_secondary="#c4c7c5",
    text_muted="#9aa0a6",
    border="#3c4043",
    border_strong="#5f6368",
    primary="#8ab4f8",
    primary_contrast="#0b1b33",
    link="#a8c7fa",
    good="#81c995",
    good_bg="#1e3226",
    warning="#fdd663",
    warning_bg="#37301b",
    critical="#f28b82",
    critical_bg="#3b2220",
    info="#8ab4f8",
    info_bg="#1c2c42",
    chart_surface="#1b1b1b",
    chart_grid="#3c4043",
    chart_axis="#5f6368",
)

PALETTES = {"light": LIGHT, "dark": DARK}


def palette_for(mode: str) -> Palette:
    return PALETTES.get(mode, LIGHT)


# ==========================================================================
# Streamlit theme options
# ==========================================================================
def streamlit_options(palette: Palette) -> dict[str, str]:
    """The ``theme.*`` config values that reproduce this palette.

    Streamlit paints its own widgets from these — including the dataframe
    grid, which renders to a canvas and therefore *cannot* be reached by CSS.
    That is why the palette has to be pushed here and not just into a
    stylesheet.
    """
    return {
        "theme.base": palette.name,
        "theme.backgroundColor": palette.background,
        "theme.secondaryBackgroundColor": palette.surface,
        "theme.textColor": palette.text,
        "theme.borderColor": palette.border,
        "theme.primaryColor": palette.primary,
        "theme.linkColor": palette.link,
        "theme.codeBackgroundColor": palette.surface,
        "theme.codeTextColor": palette.text_secondary,
        "theme.dataframeHeaderBackgroundColor": palette.surface,
        "theme.dataframeBorderColor": palette.border,
        "theme.showWidgetBorder": True,

        # The sidebar is its own surface in Streamlit's theming.
        "theme.sidebar.backgroundColor": palette.sidebar_background,
        "theme.sidebar.secondaryBackgroundColor": palette.sidebar_surface,
        "theme.sidebar.textColor": palette.text,
        "theme.sidebar.borderColor": palette.border,
        "theme.sidebar.primaryColor": palette.primary,
        "theme.sidebar.linkColor": palette.link,

        # Semantic colours drive st.success / st.warning / st.error / st.info
        # and the metric delta arrows.
        "theme.greenColor": palette.good,
        "theme.greenTextColor": palette.good,
        "theme.greenBackgroundColor": palette.good_bg,
        "theme.redColor": palette.critical,
        "theme.redTextColor": palette.critical,
        "theme.redBackgroundColor": palette.critical_bg,
        "theme.yellowColor": palette.warning,
        "theme.yellowTextColor": palette.warning,
        "theme.yellowBackgroundColor": palette.warning_bg,
        "theme.orangeColor": palette.warning,
        "theme.orangeTextColor": palette.warning,
        "theme.orangeBackgroundColor": palette.warning_bg,
        "theme.blueColor": palette.info,
        "theme.blueTextColor": palette.info,
        "theme.blueBackgroundColor": palette.info_bg,
        "theme.grayColor": palette.text_muted,
        "theme.grayTextColor": palette.text_secondary,
        "theme.grayBackgroundColor": palette.surface,
    }


def apply_to_streamlit(palette: Palette) -> bool:
    """Push the palette into Streamlit's live config.

    Returns ``True`` when something actually changed, so the caller can rerun
    once and let the frontend repaint. Unknown options are skipped rather than
    raising: Streamlit's theme keys have grown over time and the app should
    still run on an older build, just with fewer colours honoured.
    """
    from streamlit import config

    changed = False
    for key, value in streamlit_options(palette).items():
        try:
            if config.get_option(key) == value:
                continue
            config.set_option(key, value)
            changed = True
        except Exception:
            continue
    return changed


# ==========================================================================
# Contrast — checked, not eyeballed
# ==========================================================================
def _srgb_channel(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_colour: str) -> float:
    text = hex_colour.lstrip("#")
    r, g, b = (int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (0.2126 * _srgb_channel(r) + 0.7152 * _srgb_channel(g)
            + 0.0722 * _srgb_channel(b))


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio between two hex colours (1.0 – 21.0)."""
    a, b = relative_luminance(foreground), relative_luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


#: (foreground, background, minimum) triples every palette must satisfy.
#: 4.5:1 is the WCAG AA threshold for body text, 3:1 for large text and for
#: meaningful non-text marks.
def contrast_checks(palette: Palette) -> list[tuple[str, float, float]]:
    pairs = [
        ("body text on page", palette.text, palette.background, 4.5),
        ("body text on surface", palette.text, palette.surface, 4.5),
        ("secondary text on page", palette.text_secondary, palette.background, 4.5),
        ("secondary text on surface", palette.text_secondary, palette.surface, 4.5),
        ("muted text on page", palette.text_muted, palette.background, 3.0),
        ("sidebar text", palette.text, palette.sidebar_background, 4.5),
        ("sidebar text on its surface", palette.text, palette.sidebar_surface, 4.5),
        ("link on page", palette.link, palette.background, 4.5),
        ("accent on page", palette.primary, palette.background, 3.0),
        ("text on accent", palette.primary_contrast, palette.primary, 4.5),
        ("good on its tint", palette.good, palette.good_bg, 4.5),
        ("warning on its tint", palette.warning, palette.warning_bg, 4.5),
        ("critical on its tint", palette.critical, palette.critical_bg, 4.5),
        ("info on its tint", palette.info, palette.info_bg, 4.5),
        ("good on surface", palette.good, palette.surface, 3.0),
        ("critical on surface", palette.critical, palette.surface, 3.0),
        ("border against page", palette.border, palette.background, 1.2),
    ]
    return [(label, contrast_ratio(fg, bg), minimum)
            for label, fg, bg, minimum in pairs]


def failing_checks(palette: Palette) -> list[tuple[str, float, float]]:
    return [item for item in contrast_checks(palette) if item[1] < item[2]]


# ==========================================================================
# config.toml generation
# ==========================================================================
def _toml_section(palette: Palette, prefix: str) -> str:
    lines = [f"[{prefix}]", 'font = "sans serif"']
    for key, value in streamlit_options(palette).items():
        name = key.split(".", 1)[1]
        if name.startswith("sidebar."):
            continue
        rendered = str(value).lower() if isinstance(value, bool) else f'"{value}"'
        lines.append(f"{name} = {rendered}")
    lines += ["", f"[{prefix}.sidebar]"]
    for key, value in streamlit_options(palette).items():
        name = key.split(".", 1)[1]
        if not name.startswith("sidebar."):
            continue
        rendered = str(value).lower() if isinstance(value, bool) else f'"{value}"'
        lines.append(f"{name.split('.', 1)[1]} = {rendered}")
    return "\n".join(lines) + "\n"


CONFIG_HEADER = """# Local-only defaults. Streamlit's usage statistics are switched off.
#
# The colours below are generated from ui/palette.py, which is the single source
# of truth for colour across the whole app — Streamlit's widgets, the custom CSS
# and the Plotly figures all read it. What is written here is only the *starting*
# palette; the app re-applies whichever one matches your Theme setting on every
# run, so switching theme inside the app takes effect immediately.
#
# Deliberately NOT written as [theme.light] / [theme.dark] sections: declaring
# both hands the choice to the browser, and the app's own Theme setting can then
# no longer change it.
#
# Edit ui/palette.py rather than this file, then re-run:
#     python -c "from ui.palette import write_config; write_config()"

[browser]
gatherUsageStats = false

[server]
headless = false
address = "localhost"
fileWatcherType = "auto"
maxUploadSize = 50

[client]
showSidebarNavigation = false
toolbarMode = "minimal"

"""


def render_config(palette: Optional[Palette] = None) -> str:
    """The full ``.streamlit/config.toml`` contents for a starting palette."""
    return CONFIG_HEADER + _toml_section(palette or LIGHT, "theme")


def write_config(path: Optional[str] = None,
                 palette: Optional[Palette] = None) -> str:
    """Regenerate ``.streamlit/config.toml`` from the palette above."""
    from pathlib import Path

    target = Path(path) if path else Path(__file__).resolve().parent.parent / ".streamlit" / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_config(palette), encoding="utf-8")
    return str(target)
