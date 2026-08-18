"""Shared Streamlit widgets, formatting helpers and page chrome.

Everything visual that more than one screen needs lives here, so the pages stay
about *what* they show rather than how it is drawn.

Two conventions worth knowing:

* **Reads and writes use separate sessions.** A render pass opens a read-only
  session; an action opens a transactional one, commits, and reruns. That keeps
  a half-finished form from ever leaving a partial write behind.
* **Colour is never the only signal.** Status always ships with an icon and a
  number, and every chart has a "Show the numbers" table beneath it.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Optional, Sequence

import streamlit as st

import config
from calculations.money import D, ZERO, format_money, format_pct, money, to_float
from calculations.periods import Period, make_period, shift_period
from charts.theme import ChartTheme, get_theme
from ui.palette import Palette, palette_for
from constants import MONTH_NAMES, SEVERITY_ICONS, Severity
from database.database import read_session, session_scope
from services.common import ServiceError, SettingsSnapshot, settings_snapshot

# --------------------------------------------------------------------------
# Streamlit version compatibility
# --------------------------------------------------------------------------
def _accepts(func, name: str) -> bool:
    import inspect

    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtin/partial
        return False


#: Streamlit renamed ``use_container_width`` to ``width`` in 2025. Detect which
#: spelling this installation understands so the app is quiet on both.
_USES_WIDTH = _accepts(st.dataframe, "width")


def wide(stretch: bool = True) -> dict:
    """Kwargs that make a widget fill its column, on old and new Streamlit."""
    if _USES_WIDTH:
        return {"width": "stretch" if stretch else "content"}
    return {"use_container_width": stretch}


def sized(height: Optional[int] = None) -> dict:
    """``height`` kwargs, omitted entirely when unset.

    Newer Streamlit rejects ``height=None`` outright, so it has to be absent
    rather than explicitly empty.
    """
    return {"height": int(height)} if height else {}


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
@contextmanager
def db_read():
    with read_session() as session:
        yield session


@contextmanager
def db_write():
    with session_scope() as session:
        yield session


def run_action(action: Callable[[Any], Any], *, success: Optional[str] = None,
               rerun: bool = True, spinner: Optional[str] = None) -> Any:
    """Execute a write inside a transaction, reporting failures kindly."""
    try:
        if spinner:
            with st.spinner(spinner):
                with db_write() as session:
                    result = action(session)
        else:
            with db_write() as session:
                result = action(session)
    except ServiceError as exc:
        st.error(str(exc))
        return None
    except Exception as exc:  # pragma: no cover - surfaced to the user
        st.error(f"Something went wrong: {exc}")
        return None
    if success:
        flash(success, "success")
    if rerun:
        st.rerun()
    return result


# --------------------------------------------------------------------------
# Flash messages that survive a rerun
# --------------------------------------------------------------------------
FLASH_KEY = "_flash"


def flash(message: str, kind: str = "success") -> None:
    st.session_state.setdefault(FLASH_KEY, []).append((kind, message))


def render_flashes() -> None:
    messages = st.session_state.pop(FLASH_KEY, [])
    for kind, message in messages:
        if kind == "success":
            st.success(message, icon="✅")
        elif kind == "warning":
            st.warning(message, icon="⚠️")
        elif kind == "error":
            st.error(message, icon="🚫")
        else:
            st.info(message, icon="ℹ️")


# --------------------------------------------------------------------------
# Settings & formatting
# --------------------------------------------------------------------------
def current_settings() -> SettingsSnapshot:
    """Settings snapshot for this rerun, fetched once."""
    cached = st.session_state.get("_settings_snapshot")
    if cached is not None and st.session_state.get("_settings_rerun") == st.session_state.get(
            "_rerun_token"):
        return cached
    with db_read() as session:
        snapshot = settings_snapshot(session)
    st.session_state["_settings_snapshot"] = snapshot
    st.session_state["_settings_rerun"] = st.session_state.get("_rerun_token")
    return snapshot


def invalidate_settings() -> None:
    st.session_state.pop("_settings_snapshot", None)


def md(text: Any) -> str:
    """Make a string safe to hand to Streamlit's markdown.

    Streamlit reads ``$…$`` as LaTeX, so two currency symbols in one block turn
    the money between them into a maths expression — ``R$ 1.234,50 · -R$ 98,76``
    renders the first amount in a serif italic and swallows the symbols. Every
    formatted amount that lands in ``st.markdown``, ``st.caption``, ``st.write``,
    an alert or a button label goes through here first.

    Only ``$`` is escaped: money is the one thing the app interpolates into
    markdown often enough to matter, and escaping more would mangle the
    deliberate ``**bold**`` in those same strings.
    """
    return str(text).replace("$", r"\$")


@dataclass
class Formatter:
    settings: SettingsSnapshot

    def money(self, value: Any, *, compact: bool = False, signed: bool = False,
              show_symbol: bool = True) -> str:
        places = 2 if self.settings.show_cents else 0
        return format_money(value, self.settings.base_currency, places=places,
                            compact=compact, signed=signed, show_symbol=show_symbol)

    def md_money(self, value: Any, **kwargs: Any) -> str:
        """``money`` for a markdown context — see :func:`md`."""
        return md(self.money(value, **kwargs))

    def pct(self, value: Any, places: int = 1, *, signed: bool = False) -> str:
        return format_pct(value, places, signed=signed)

    def date(self, value: Optional[date]) -> str:
        if not value:
            return "—"
        return value.strftime(self.settings.date_pattern)

    def signed_money(self, value: Any) -> str:
        return self.money(value, signed=True)


def formatter() -> Formatter:
    return Formatter(current_settings())


def active_mode(settings: Optional[SettingsSnapshot] = None) -> str:
    """Which palette is in force right now: ``"light"`` or ``"dark"``.

    The stored preference wins. ``"auto"`` asks the browser, falling back to
    light when it cannot be determined — guessing dark and being wrong is the
    more jarring failure.
    """
    settings = settings or current_settings()
    choice = (settings.theme or "auto").lower()
    if choice in ("light", "dark"):
        return choice

    resolved = getattr(getattr(st, "context", None), "theme", None)
    kind = getattr(resolved, "type", None)
    if isinstance(kind, str) and kind.lower() in ("light", "dark"):
        return kind.lower()
    return "light"


def active_palette(settings: Optional[SettingsSnapshot] = None) -> Palette:
    return palette_for(active_mode(settings))


def theme() -> ChartTheme:
    """Chart theme for the active palette, so figures sit on the right plane."""
    settings = current_settings()
    return get_theme(active_palette(settings), settings.base_currency)


def _dark_mode(settings: Optional[SettingsSnapshot] = None) -> bool:
    return active_mode(settings) == "dark"


# --------------------------------------------------------------------------
# Page chrome
# --------------------------------------------------------------------------
def page_header(title: str, subtitle: str = "", *, icon: str = "",
                actions: Optional[Callable[[], None]] = None) -> None:
    left, right = st.columns([0.72, 0.28])
    with left:
        st.markdown(f"### {icon + ' ' if icon else ''}{title}")
        if subtitle:
            st.caption(subtitle)
    if actions is not None:
        with right:
            actions()


def section(title: str, help_text: str = "") -> None:
    st.markdown(f"#### {title}")
    if help_text:
        st.caption(help_text)


def divider() -> None:
    st.markdown("<hr class='pf-rule'>", unsafe_allow_html=True)


def empty_state(title: str, body: str, *, icon: str = "🌱",
                action_label: str = "", action: Optional[Callable[[], None]] = None,
                secondary: str = "") -> None:
    st.markdown(
        f"<div class='pf-empty'><div class='pf-empty-icon'>{icon}</div>"
        f"<div class='pf-empty-title'>{title}</div>"
        f"<div class='pf-empty-body'>{body}</div></div>",
        unsafe_allow_html=True,
    )
    if action_label and action is not None:
        columns = st.columns([1, 2, 1])
        with columns[1]:
            if st.button(action_label, type="primary", **wide()):
                action()
    if secondary:
        st.caption(secondary)


# --------------------------------------------------------------------------
# KPI cards
# --------------------------------------------------------------------------
@dataclass
class Kpi:
    label: str
    value: str
    delta: Optional[str] = None
    delta_good: Optional[bool] = None
    help_text: str = ""
    icon: str = ""


def kpi_row(items: Sequence[Kpi], columns: Optional[int] = None) -> None:
    if not items:
        return
    count = columns or min(len(items), 5)
    for start in range(0, len(items), count):
        chunk = items[start:start + count]
        cols = st.columns(len(chunk))
        for col, item in zip(cols, chunk):
            with col:
                delta_color = "off"
                if item.delta is not None and item.delta_good is not None:
                    delta_color = "normal" if item.delta_good else "inverse"
                st.metric(
                    label=f"{item.icon + ' ' if item.icon else ''}{item.label}",
                    value=item.value,
                    delta=item.delta,
                    delta_color=delta_color if item.delta else "off",
                    help=item.help_text or None,
                )


# --------------------------------------------------------------------------
# Status pills & alerts
# --------------------------------------------------------------------------
SEVERITY_CLASS = {
    Severity.CRITICAL.value: "pf-pill-critical",
    Severity.WARNING.value: "pf-pill-warning",
    Severity.INFO.value: "pf-pill-info",
    Severity.SUCCESS.value: "pf-pill-success",
}


def pill(text: str, severity: str = Severity.INFO.value, icon: str = "") -> str:
    css = SEVERITY_CLASS.get(severity, "pf-pill-info")
    glyph = icon or SEVERITY_ICONS.get(severity, "")
    return f"<span class='pf-pill {css}'>{glyph} {text}</span>"


def alert_panel(alerts: Sequence, *, limit: int = 8, title: str = "Needs your attention",
                empty_message: str = "Nothing needs attention right now.") -> None:
    if not alerts:
        st.success(f"✅ {empty_message}")
        return
    counts = {"critical": 0, "warning": 0, "info": 0, "success": 0}
    for alert in alerts:
        counts[getattr(alert, "severity", "info")] = counts.get(
            getattr(alert, "severity", "info"), 0) + 1
    header = f"{title} — {counts['critical']} urgent · {counts['warning']} warnings"
    with st.expander(header, expanded=counts["critical"] > 0):
        for alert in alerts[:limit]:
            severity = getattr(alert, "severity", "info")
            icon = SEVERITY_ICONS.get(severity, "•")
            detail = getattr(alert, "detail", None)
            st.markdown(
                f"{icon} **{alert.message}**" + (f"  \n<span class='pf-muted'>{detail}</span>"
                                                 if detail else ""),
                unsafe_allow_html=True,
            )
        if len(alerts) > limit:
            st.caption(f"…and {len(alerts) - limit} more.")


# --------------------------------------------------------------------------
# Charts with a table fallback
# --------------------------------------------------------------------------
def chart(fig, *, table: Optional[Sequence[dict]] = None,
          table_label: str = "Show the numbers", key: Optional[str] = None,
          caption: str = "") -> None:
    """Render a figure plus an accessible table view of the same data.

    The table is not decoration: several palette slots sit below 3:1 contrast on
    a light surface, and a text alternative is the required relief.
    """
    st.plotly_chart(fig, key=key, **wide(),
                    config={"displaylogo": False,
                            "modeBarButtonsToRemove": ["lasso2d", "select2d"]})
    if caption:
        st.caption(caption)
    if table:
        with st.expander(table_label):
            st.dataframe(table, hide_index=True, **wide())


# --------------------------------------------------------------------------
# Confirmation for destructive actions
# --------------------------------------------------------------------------
def confirm_action(
    label: str,
    key: str,
    *,
    prompt: str,
    confirm_label: str = "Yes, do it",
    cancel_label: str = "Cancel",
    require_text: Optional[str] = None,
    button_type: str = "secondary",
    **button_kwargs,
) -> bool:
    """Two-step confirmation. Returns ``True`` only on the confirming click.

    Extra keyword arguments (``width``/``use_container_width``) are forwarded
    to the underlying buttons, so callers can size it like any other widget.
    """
    state_key = f"_confirm_{key}"
    if not st.session_state.get(state_key):
        if st.button(label, key=f"{key}_ask", type=button_type, **button_kwargs):
            st.session_state[state_key] = True
            st.rerun()
        return False

    st.warning(prompt, icon="⚠️")
    typed_ok = True
    if require_text:
        typed = st.text_input(
            f"Type **{require_text}** to confirm", key=f"{key}_text",
            placeholder=require_text,
        )
        typed_ok = typed.strip() == require_text
        if typed and not typed_ok:
            st.caption("That does not match yet.")
    left, right = st.columns(2)
    with left:
        confirmed = st.button(confirm_label, key=f"{key}_yes", type="primary",
                              disabled=not typed_ok, **wide())
    with right:
        if st.button(cancel_label, key=f"{key}_no", **wide()):
            st.session_state.pop(state_key, None)
            st.rerun()
    if confirmed:
        st.session_state.pop(state_key, None)
        return True
    return False


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------
def money_input(label: str, value: Any = ZERO, *, key: Optional[str] = None,
                help_text: str = "", min_value: float = 0.0,
                max_value: float = 999_999_999.99, step: float = 50.0,
                disabled: bool = False) -> Decimal:
    settings = current_settings()
    symbol = ""
    from constants import CURRENCY_FORMATS

    fmt = CURRENCY_FORMATS.get(settings.base_currency.upper())
    if fmt:
        symbol = fmt["symbol"]
    raw = st.number_input(
        f"{label} ({symbol})" if symbol else label,
        min_value=min_value, max_value=max_value,
        value=max(min_value, to_float(value)), step=step, format="%.2f",
        key=key, help=help_text or None, disabled=disabled,
    )
    return money(raw)


def pct_input(label: str, value: Any = ZERO, *, key: Optional[str] = None,
              help_text: str = "", min_value: float = -100.0,
              max_value: float = 1000.0, step: float = 0.5) -> Decimal:
    raw = st.number_input(
        f"{label} (%)", min_value=min_value, max_value=max_value,
        value=to_float(value), step=step, format="%.2f",
        key=key, help=help_text or None,
    )
    return D(str(raw))


def select_with_none(label: str, options: Sequence[tuple[int, str]], *,
                     value: Optional[int] = None, none_label: str = "— none —",
                     key: Optional[str] = None, help_text: str = "",
                     disabled: bool = False) -> Optional[int]:
    ids = [None] + [option[0] for option in options]
    labels = {None: none_label}
    labels.update({option[0]: option[1] for option in options})
    index = ids.index(value) if value in ids else 0
    chosen = st.selectbox(
        label, ids, index=index, format_func=lambda item: labels.get(item, str(item)),
        key=key, help=help_text or None, disabled=disabled,
    )
    return chosen


# --------------------------------------------------------------------------
# Period picker
# --------------------------------------------------------------------------
PERIOD_KEY = "_selected_period"


def selected_period(settings: Optional[SettingsSnapshot] = None,
                    today: Optional[date] = None) -> Period:
    settings = settings or current_settings()
    today = today or date.today()
    stored = st.session_state.get(PERIOD_KEY)
    if stored:
        try:
            year, month = int(stored[:4]), int(stored[5:7])
            return settings.period(year, month)
        except (ValueError, IndexError):
            pass
    return settings.current_period(today)


def set_selected_period(period: Period) -> None:
    st.session_state[PERIOD_KEY] = period.key


def period_picker(*, key: str = "period_picker", label: str = "Period",
                  back: int = 36, forward: int = 24,
                  settings: Optional[SettingsSnapshot] = None) -> Period:
    """Month selector with quick previous/next stepping."""
    settings = settings or current_settings()
    today = date.today()
    current = settings.current_period(today)
    options = [shift_period(current, offset, settings.first_day_of_month)
               for offset in range(-back, forward + 1)]
    keys = [period.key for period in options]
    active = selected_period(settings, today)
    if active.key not in keys:
        active = current
    index = keys.index(active.key)

    prev_col, select_col, next_col, today_col = st.columns([0.09, 0.58, 0.09, 0.24])
    with prev_col:
        if st.button("‹", key=f"{key}_prev", help="Previous period",
                     **wide()):
            set_selected_period(options[max(0, index - 1)])
            st.rerun()
    with select_col:
        chosen_key = st.selectbox(
            label, keys, index=index,
            format_func=lambda item: _period_label(item, current.key),
            key=f"{key}_select", label_visibility="collapsed",
        )
    with next_col:
        if st.button("›", key=f"{key}_next", help="Next period",
                     **wide()):
            set_selected_period(options[min(len(options) - 1, index + 1)])
            st.rerun()
    with today_col:
        if st.button("Today", key=f"{key}_today", **wide()):
            set_selected_period(current)
            st.rerun()

    if chosen_key != active.key:
        set_selected_period(options[keys.index(chosen_key)])
        st.rerun()
    return options[keys.index(chosen_key)]


def _period_label(key: str, current_key: str) -> str:
    year, month = int(key[:4]), int(key[5:7])
    label = f"{MONTH_NAMES[month - 1]} {year}"
    if key == current_key:
        return f"{label}  ·  current"
    return label


def range_picker(*, key: str = "range_picker",
                 default_months: int = 12) -> tuple[int, str]:
    """Number of months to look back, plus a grain for grouping."""
    left, right = st.columns([0.6, 0.4])
    with left:
        months = st.select_slider(
            "Months of history", options=[3, 6, 12, 18, 24, 36, 60],
            value=default_months, key=f"{key}_months",
        )
    with right:
        grain = st.radio("Group by", ["month", "quarter", "year"], horizontal=True,
                         key=f"{key}_grain", label_visibility="visible")
    return months, grain


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------
def variance_table(rows: Sequence, fmt: Formatter, *, height: Optional[int] = None,
                   key: Optional[str] = None) -> None:
    if not rows:
        st.caption("No lines to compare yet.")
        return
    data = [
        {
            "": row.status_icon,
            "Line": row.label,
            "Type": row.kind.title(),
            "Planned": fmt.money(row.planned),
            "Actual": fmt.money(row.actual),
            "Variance": fmt.signed_money(row.variance),
            "Used": fmt.pct(row.consumed_pct, 0) if row.planned else "—",
            "Left": fmt.money(row.remaining_positive) if row.planned else "—",
            "Status": row.status_label,
        }
        for row in rows
    ]
    st.dataframe(data, hide_index=True, key=key, **wide(), **sized(height))


def money_table(rows: Sequence[dict], columns: Sequence[tuple[str, str, str]],
                fmt: Formatter, *, height: Optional[int] = None,
                key: Optional[str] = None) -> None:
    """``columns`` are ``(source_key, header, kind)`` with kind in
    ``text|money|pct|date|int``."""
    if not rows:
        st.caption("Nothing to show.")
        return
    shaped: list[dict[str, Any]] = []
    for row in rows:
        record: dict[str, Any] = {}
        for source, header, kind in columns:
            value = row.get(source)
            if kind == "money":
                # Missing is not the same as zero — say so, and keep the whole
                # column textual so its dtype stays consistent.
                record[header] = fmt.money(value) if value is not None else "—"
            elif kind == "pct":
                record[header] = fmt.pct(value) if value is not None else "—"
            elif kind == "date":
                record[header] = fmt.date(value)
            elif kind == "int":
                # Must stay None, never a dash: mixing a string into a numeric
                # column makes the dtype `object`, and Arrow refuses to convert
                # it — the table then fails to render at all.
                record[header] = int(value) if value is not None else None
            else:
                record[header] = value if value is not None else ""
        shaped.append(record)
    st.dataframe(shaped, hide_index=True, key=key, **wide(), **sized(height))


# --------------------------------------------------------------------------
# CSS
# --------------------------------------------------------------------------
def inject_css(palette: Optional[Palette] = None) -> None:
    """Style the few things Streamlit does not, using the active palette.

    Streamlit itself is themed from the same palette (see
    :mod:`ui.palette`), so this stylesheet only has to cover the app's own
    components — metric cards, status pills, empty states — plus a handful of
    spacing tweaks. Nothing here hardcodes a colour, which is what previously
    left dark cards stranded on a light page.
    """
    pal = palette or active_palette()
    st.markdown(
        f"""
        <style>
        /* ---- layout ------------------------------------------------- */
        .block-container {{ padding-top: 2.1rem; padding-bottom: 3rem; max-width: 1400px; }}
        h1, h2, h3, h4 {{ letter-spacing: -0.01em; color: {pal.text}; }}
        .pf-rule {{ border: none; border-top: 1px solid {pal.border};
                    margin: 1.1rem 0 0.9rem; }}
        .pf-muted {{ color: {pal.text_secondary}; font-size: 0.86rem; }}

        /* ---- status pills ------------------------------------------- */
        .pf-pill {{
            display: inline-block; padding: 2px 9px; border-radius: 999px;
            font-size: 0.76rem; font-weight: 600; white-space: nowrap;
            border: 1px solid transparent;
        }}
        .pf-pill-critical {{ background: {pal.critical_bg}; color: {pal.critical};
                             border-color: {pal.critical}55; }}
        .pf-pill-warning  {{ background: {pal.warning_bg};  color: {pal.warning};
                             border-color: {pal.warning}55; }}
        .pf-pill-info     {{ background: {pal.info_bg};     color: {pal.info};
                             border-color: {pal.info}55; }}
        .pf-pill-success  {{ background: {pal.good_bg};     color: {pal.good};
                             border-color: {pal.good}55; }}
        .pf-badge-row {{ display: flex; gap: 0.4rem; flex-wrap: wrap;
                         margin: 0.2rem 0 0.6rem; }}

        /* ---- empty states ------------------------------------------- */
        .pf-empty {{
            border: 1px dashed {pal.border_strong}; border-radius: 14px;
            padding: 2.2rem 1.5rem; text-align: center;
            background: {pal.surface}; color: {pal.text};
            margin: 0.6rem 0 1rem;
        }}
        .pf-empty-icon {{ font-size: 2.1rem; line-height: 1; margin-bottom: 0.6rem; }}
        .pf-empty-title {{ font-weight: 650; font-size: 1.03rem; margin-bottom: 0.3rem;
                           color: {pal.text}; }}
        .pf-empty-body {{ color: {pal.text_secondary}; font-size: 0.9rem;
                          max-width: 46rem; margin: 0 auto; line-height: 1.5; }}

        /* ---- metric cards ------------------------------------------- */
        div[data-testid="stMetric"] {{
            background: {pal.surface}; border: 1px solid {pal.border};
            border-radius: 12px; padding: 0.75rem 0.9rem;
        }}
        div[data-testid="stMetric"] * {{ color: {pal.text}; }}
        div[data-testid="stMetricLabel"] p {{
            font-size: 0.79rem; color: {pal.text_secondary} !important;
        }}
        div[data-testid="stMetricValue"] {{
            font-size: 1.42rem; color: {pal.text} !important;
        }}
        div[data-testid="stMetricDelta"] {{ font-size: 0.78rem; }}

        /* ---- tabs & sidebar ----------------------------------------- */
        .stTabs [data-baseweb="tab-list"] {{ gap: 0.35rem; }}
        .stTabs [data-baseweb="tab"] {{ padding: 0.4rem 0.85rem; }}
        section[data-testid="stSidebar"] {{ border-right: 1px solid {pal.border}; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------
def status_pills(items: Sequence[tuple[str, str]]) -> None:
    """``[(text, severity), ...]`` rendered as a row of pills."""
    if not items:
        return
    html = "".join(pill(text, severity) for text, severity in items)
    st.markdown(f"<div class='pf-badge-row'>{html}</div>", unsafe_allow_html=True)


def caption_help(text: str) -> None:
    st.caption(text)


def download_row(items: Sequence[tuple[str, str, bytes | str, str]]) -> None:
    """``[(label, filename, data, mime), ...]`` as a row of download buttons."""
    if not items:
        return
    cols = st.columns(len(items))
    for col, (label, filename, data, mime) in zip(cols, items):
        with col:
            st.download_button(label, data=data, file_name=filename, mime=mime,
                               **wide())


def value_with_icon(value: str, good: Optional[bool], *, neutral_icon: str = "·") -> str:
    if good is None:
        return f"{neutral_icon} {value}"
    return f"{'✓' if good else '▲'} {value}"
