"""Theme tests — the colour rules that keep both modes readable.

Dark mode was previously broken in a way no unit test could have caught, because
the palette only reached the app's own stylesheet and never Streamlit's theme:
dark cards on a white page, with light-theme dark text inside them. These tests
lock in the three things that failure depended on.

1. **Contrast is measured, not eyeballed.** Every foreground/background pair the
   interface actually puts together has to clear its WCAG threshold, in both
   palettes. Change a colour and this fails before a person has to squint at it.
2. **The palette reaches Streamlit.** Widgets Streamlit ships — above all the
   dataframe grid, which draws to a canvas and cannot be reached by CSS — are
   only themed if the palette is pushed into config.
3. **config.toml keeps runtime switching alive.** Declaring ``[theme.light]``
   and ``[theme.dark]`` hands theme selection to the browser and silently
   disables the app's own Theme setting. The generated file must not do that.
"""

from __future__ import annotations

import re

import pytest

from charts.theme import CATEGORICAL_DARK, CATEGORICAL_LIGHT, STATUS, STATUS_DARK, get_theme
from ui.palette import (
    DARK,
    LIGHT,
    PALETTES,
    Palette,
    contrast_checks,
    contrast_ratio,
    failing_checks,
    palette_for,
    render_config,
    streamlit_options,
)

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


# --------------------------------------------------------------------------
# Contrast
# --------------------------------------------------------------------------
@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_every_pair_clears_its_threshold(palette: Palette):
    """No combination the UI puts on screen may fall below its minimum."""
    failures = failing_checks(palette)
    assert not failures, "\n".join(
        f"{label}: {ratio:.2f}:1 (needs {minimum}:1)"
        for label, ratio, minimum in failures
    )


@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_body_text_has_real_headroom(palette: Palette):
    """Body copy should be comfortably past AA, not scraping it."""
    assert contrast_ratio(palette.text, palette.background) >= 7.0
    assert contrast_ratio(palette.text, palette.surface) >= 7.0


def test_contrast_ratio_matches_known_values():
    """Sanity-check the formula itself against the two fixed points."""
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio("#ffffff", "#ffffff") == pytest.approx(1.0, abs=0.01)
    # Order must not matter.
    assert contrast_ratio("#767676", "#ffffff") == pytest.approx(
        contrast_ratio("#ffffff", "#767676"))


def test_dark_is_actually_dark_and_light_actually_light():
    """The obvious invariant, stated once so an inverted edit cannot pass."""
    from ui.palette import relative_luminance

    assert relative_luminance(DARK.background) < 0.1
    assert relative_luminance(DARK.text) > 0.5
    assert relative_luminance(LIGHT.background) > 0.8
    assert relative_luminance(LIGHT.text) < 0.1


def test_raised_surfaces_are_distinguishable_from_the_page():
    """Cards have to read as raised without a border doing all the work."""
    for palette in (LIGHT, DARK):
        assert palette.surface != palette.background
        assert contrast_ratio(palette.surface, palette.background) >= 1.03
        assert palette.surface_hover != palette.surface


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    text = hex_colour.lstrip("#")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _separation(first: str, second: str) -> float:
    """Straight-line distance in sRGB — a rough stand-in for "these look different".

    Deliberately *not* contrast ratio: that measures lightness only, and a green
    and a red can share a luminance while being obviously different colours.
    """
    a, b = _rgb(first), _rgb(second)
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_status_colours_stay_distinct(palette: Palette):
    """Good / warning / critical must never collapse into each other.

    Hue never carries meaning alone in this app — every status also travels with
    an icon or a word — but two statuses rendering as near-identical swatches
    would still be a defect.
    """
    trio = [palette.good, palette.warning, palette.critical]
    assert len(set(trio)) == 3
    for first, second in ((0, 1), (1, 2), (0, 2)):
        assert _separation(trio[first], trio[second]) >= 60, (
            f"{trio[first]} and {trio[second]} are too close to tell apart")


@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_every_field_is_a_hex_colour(palette: Palette):
    from dataclasses import fields

    for spec in fields(palette):
        if spec.name == "name":
            continue
        value = getattr(palette, spec.name)
        assert HEX.match(value), f"{spec.name} is not a hex colour: {value!r}"


def test_contrast_checks_cover_the_visible_combinations():
    labels = {label for label, _, _ in contrast_checks(LIGHT)}
    for expected in ("body text on page", "body text on surface", "sidebar text",
                     "text on accent", "critical on its tint"):
        assert expected in labels


# --------------------------------------------------------------------------
# Reaching Streamlit
# --------------------------------------------------------------------------
@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_streamlit_options_carry_the_palette(palette: Palette):
    options = streamlit_options(palette)
    assert options["theme.base"] == palette.name
    assert options["theme.backgroundColor"] == palette.background
    assert options["theme.textColor"] == palette.text
    assert options["theme.sidebar.backgroundColor"] == palette.sidebar_background
    # The grid draws to a canvas: CSS cannot reach it, so the theme must.
    assert options["theme.dataframeHeaderBackgroundColor"] == palette.surface
    assert options["theme.dataframeBorderColor"] == palette.border


@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_streamlit_option_values_are_well_formed(palette: Palette):
    for key, value in streamlit_options(palette).items():
        assert key.startswith("theme.")
        if isinstance(value, bool):
            continue
        assert key == "theme.base" or HEX.match(value), f"{key} = {value!r}"


def test_the_two_palettes_produce_different_options():
    assert streamlit_options(LIGHT) != streamlit_options(DARK)


def test_apply_to_streamlit_sets_config_and_is_idempotent():
    """Applying reports change once, then reports none — that drives the rerun."""
    from streamlit import config as st_config

    from ui.palette import apply_to_streamlit

    before = st_config.get_option("theme.backgroundColor")
    try:
        apply_to_streamlit(LIGHT)
        assert apply_to_streamlit(DARK) is True
        assert st_config.get_option("theme.backgroundColor") == DARK.background
        assert st_config.get_option("theme.textColor") == DARK.text
        # Nothing left to change, so the caller must not rerun forever.
        assert apply_to_streamlit(DARK) is False
    finally:
        apply_to_streamlit(LIGHT)
        if before:
            st_config.set_option("theme.backgroundColor", before)


def test_apply_survives_an_option_streamlit_does_not_know():
    """Older Streamlit builds lack some keys; the app must still start."""
    from ui import palette as palette_module

    real = palette_module.streamlit_options
    try:
        palette_module.streamlit_options = lambda pal: {
            "theme.backgroundColor": pal.background,
            "theme.notARealOptionAtAll": pal.text,
        }
        palette_module.apply_to_streamlit(DARK)  # must not raise
    finally:
        palette_module.streamlit_options = real
        palette_module.apply_to_streamlit(LIGHT)


def test_palette_lookup():
    assert palette_for("dark") is DARK
    assert palette_for("light") is LIGHT
    assert palette_for("auto") is LIGHT       # unresolved falls back to light
    assert palette_for("nonsense") is LIGHT
    assert set(PALETTES) == {"light", "dark"}
    assert DARK.is_dark and not LIGHT.is_dark


# --------------------------------------------------------------------------
# config.toml
# --------------------------------------------------------------------------
def test_generated_config_never_splits_light_and_dark():
    """The regression that silently disabled the in-app Theme setting.

    Matched at the start of a line so the header comment, which explains *why*
    those sections are absent, does not trip the check.
    """
    rendered = render_config(LIGHT)
    sections = set(re.findall(r"^\[([^\]]+)\]", rendered, flags=re.MULTILINE))
    assert "theme.light" not in sections
    assert "theme.dark" not in sections
    assert {"theme", "theme.sidebar"} <= sections


def test_generated_config_carries_the_palette_and_stays_local():
    rendered = render_config(LIGHT)
    assert f'backgroundColor = "{LIGHT.background}"' in rendered
    assert f'textColor = "{LIGHT.text}"' in rendered
    assert "gatherUsageStats = false" in rendered


def test_config_on_disk_matches_the_palette():
    """The checked-in file must not drift away from ui/palette.py."""
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / ".streamlit" / "config.toml"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == render_config(LIGHT)


def test_generated_config_parses_as_toml():
    import tomllib

    parsed = tomllib.loads(render_config(DARK))
    assert parsed["theme"]["backgroundColor"] == DARK.background
    assert parsed["theme"]["sidebar"]["backgroundColor"] == DARK.sidebar_background
    assert parsed["browser"]["gatherUsageStats"] is False


# --------------------------------------------------------------------------
# Charts read the same palette
# --------------------------------------------------------------------------
@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_chart_theme_derives_from_the_palette(palette: Palette):
    theme = get_theme(palette, "BRL")
    assert theme.dark is palette.is_dark
    assert theme.surface == palette.chart_surface
    assert theme.page == palette.background
    assert theme.text_primary == palette.text
    assert theme.grid == palette.chart_grid


def test_chart_theme_picks_the_right_series_and_status_tables():
    assert list(get_theme(DARK).categorical) == CATEGORICAL_DARK
    assert list(get_theme(LIGHT).categorical) == CATEGORICAL_LIGHT
    assert get_theme(DARK).status("critical") == STATUS_DARK["critical"]
    assert get_theme(LIGHT).status("critical") == STATUS["critical"]


def test_chart_series_never_cycles_past_the_validated_set():
    theme = get_theme(LIGHT)
    assert theme.series(0) != theme.series(1)
    assert theme.series(99) == theme.muted   # folds to muted, never reuses a hue


def test_chart_text_is_readable_on_the_chart_surface():
    for palette in (LIGHT, DARK):
        theme = get_theme(palette)
        assert contrast_ratio(theme.text_primary, theme.surface) >= 4.5
        assert contrast_ratio(theme.muted, theme.surface) >= 3.0


def test_series_colours_are_visible_on_their_own_plane():
    """Every dark series colour must be a mark you can actually see."""
    for colour in CATEGORICAL_DARK:
        assert contrast_ratio(colour, DARK.chart_surface) >= 2.0


def test_currency_separators_follow_the_setting():
    assert get_theme(LIGHT, "BRL").separators == ",."
    assert get_theme(LIGHT, "USD").separators == ".,"
    assert get_theme(LIGHT, "BRL").tick_prefix.strip() == "R$"


# --------------------------------------------------------------------------
# The stylesheet
# --------------------------------------------------------------------------
def _css_for(palette: Palette) -> str:
    """Render inject_css without a Streamlit runtime by capturing the markdown."""
    import streamlit as st

    from ui import components

    captured: list[str] = []
    real = st.markdown
    try:
        st.markdown = lambda body, **kwargs: captured.append(str(body))
        components.inject_css(palette)
    finally:
        st.markdown = real
    assert captured, "inject_css emitted nothing"
    return captured[0]


@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_stylesheet_hardcodes_no_colours(palette: Palette):
    """Every colour in the CSS must come from the palette.

    A single hardcoded hex is all it takes to strand a light-theme card on a
    dark page, so the rule is absolute rather than a matter of taste.
    """
    css = _css_for(palette)
    allowed = {value.lower() for value in vars(palette).values()
               if isinstance(value, str) and HEX.match(value)}
    found = {match.group(0).lower()[:7] for match in re.finditer(r"#[0-9a-fA-F]{6}", css)}
    assert found <= allowed, f"hardcoded colours in the stylesheet: {sorted(found - allowed)}"


def test_stylesheet_swaps_with_the_palette():
    assert DARK.background in _css_for(DARK) or DARK.surface in _css_for(DARK)
    assert _css_for(LIGHT) != _css_for(DARK)
