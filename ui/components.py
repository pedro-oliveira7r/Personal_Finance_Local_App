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


def currency_book():
    """The book's currencies and latest rates, fetched once per rerun.

    Cached like the settings snapshot. Rates are global and unversioned, so a
    stale cache would show yesterday's number immediately after a save.
    """
    from services.currency_service import book as _book

    cached = st.session_state.get("_currency_book")
    if cached is not None and st.session_state.get("_currency_rerun") == st.session_state.get(
            "_rerun_token"):
        return cached
    with db_read() as session:
        resolved = _book(session)
    st.session_state["_currency_book"] = resolved
    st.session_state["_currency_rerun"] = st.session_state.get("_rerun_token")
    return resolved


def invalidate_settings() -> None:
    st.session_state.pop("_settings_snapshot", None)
    st.session_state.pop("_currency_book", None)


def savings_rate_of(row: dict) -> Decimal:
    """Savings rate for a history row, recomputed from its own totals.

    Combining currencies means the component figures change; averaging the
    per-currency percentages instead would weight a small balance equally with
    a large one.
    """
    from calculations.budgeting import savings_rate

    saved = money(D(row.get("savings", ZERO)) + D(row.get("investments", ZERO)))
    return savings_rate(D(row.get("income", ZERO)), saved)


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
    #: Page-level default. Set once at the top of a filtered page so every
    #: ``fmt.money(x)`` below is right without threading a keyword through
    #: forty call sites.
    currency: Optional[str] = None

    def money(self, value: Any, *, currency: Optional[str] = None,
              compact: bool = False, signed: bool = False,
              show_symbol: bool = True) -> str:
        places = 2 if self.settings.show_cents else 0
        code = currency or self.currency or self.settings.base_currency
        return format_money(value, code, places=places,
                            compact=compact, signed=signed, show_symbol=show_symbol)

    def for_currency(self, code: Optional[str]) -> "Formatter":
        """A sibling formatter pinned to another currency."""
        return Formatter(self.settings, code)

    def symbol(self, code: Optional[str] = None) -> str:
        from services.currency_service import symbol_for

        return symbol_for(code or self.currency or self.settings.base_currency)

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


def formatter(currency: Optional[str] = None) -> Formatter:
    return Formatter(current_settings(), currency)


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


def theme(currency: Optional[str] = None) -> ChartTheme:
    """Chart theme for the active palette, so figures sit on the right plane.

    ``currency`` sets the axis prefix and separators for the whole figure. That
    one slot is enough because every chart in the app is single-currency: a
    filtered page draws one currency, and the combined view draws values
    already converted into the primary.
    """
    settings = current_settings()
    return get_theme(active_palette(settings), currency or settings.base_currency)


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


DISMISSED_ALERTS_KEY = "dismissed_alerts"


def _alert_fingerprint(alert) -> str:
    """Stable id for one alert, from what the user actually reads.

    The ``code`` alone is not unique — "category_over" fires once per category —
    so the message is folded in too. Hashing keeps the stored preference small
    and free of the user's own category names.
    """
    import hashlib

    raw = f"{getattr(alert, 'code', '')}|{getattr(alert, 'message', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _read_dismissed_alerts() -> set[str]:
    from services import settings_service

    try:
        with db_read() as session:
            prefs = settings_service.get_dashboard_prefs(session)
    except Exception:
        return set()
    return set(prefs.get(DISMISSED_ALERTS_KEY) or [])


def _write_dismissed_alerts(fingerprints: Iterable[str]) -> None:
    from services import settings_service

    payload = sorted(set(fingerprints))
    with db_write() as session:
        settings_service.set_dashboard_prefs(session, {DISMISSED_ALERTS_KEY: payload})
    invalidate_settings()
    for key in [k for k in st.session_state if k.startswith("alert_")]:
        if key[len("alert_"):] not in payload:
            del st.session_state[key]


def alert_panel(alerts: Sequence, *, limit: int = 8, title: str = "Needs your attention",
                empty_message: str = "Nothing needs attention right now.",
                dismissible: bool = True) -> None:
    """Alerts the user can tick off.

    Ticking one stores its fingerprint, so it stays gone across reruns and
    restarts. Fingerprints for alerts that are no longer raised are pruned on
    every pass: once the underlying problem is actually fixed the record of
    having dismissed it is dropped, so if the same problem returns later it is
    raised again rather than staying silently hidden.
    """
    if not dismissible:
        visible, dismissed_count = list(alerts), 0
    else:
        live = {_alert_fingerprint(alert): alert for alert in alerts}
        stored = _read_dismissed_alerts()
        pruned = stored & set(live)
        if pruned != stored:  # some dismissed alerts no longer apply
            _write_dismissed_alerts(pruned)
        visible = [alert for key, alert in live.items() if key not in pruned]
        dismissed_count = len(pruned)

    if not visible:
        note = (f" {dismissed_count} dismissed." if dismissed_count else "")
        st.success(f"✅ {empty_message}{note}")
        if dismissed_count and st.button("Bring dismissed ones back", key="alerts_restore"):
            _write_dismissed_alerts([])
            st.rerun()
        return

    counts = {"critical": 0, "warning": 0, "info": 0, "success": 0}
    for alert in visible:
        severity = getattr(alert, "severity", "info")
        counts[severity] = counts.get(severity, 0) + 1
    header = f"{title} — {counts['critical']} urgent · {counts['warning']} warnings"
    with st.expander(header, expanded=counts["critical"] > 0):
        if dismissible:
            st.caption("Tick a warning to clear it. It comes back if the problem does.")
        for alert in visible[:limit]:
            severity = getattr(alert, "severity", "info")
            icon = SEVERITY_ICONS.get(severity, "•")
            detail = getattr(alert, "detail", None)
            body = (f"{icon} **{alert.message}**"
                    + (f"  \n<span class='pf-muted'>{detail}</span>" if detail else ""))
            if not dismissible:
                st.markdown(body, unsafe_allow_html=True)
                continue
            tick, text = st.columns([0.04, 0.96])
            with tick:
                checked = st.checkbox(
                    "Dismiss this warning", key=f"alert_{_alert_fingerprint(alert)}",
                    value=False, label_visibility="collapsed",
                )
            with text:
                st.markdown(body, unsafe_allow_html=True)
            if checked:
                _write_dismissed_alerts(
                    _read_dismissed_alerts() | {_alert_fingerprint(alert)})
                st.rerun()
        if len(visible) > limit:
            st.caption(f"…and {len(visible) - limit} more.")
        if dismissible and dismissed_count:
            st.caption(f"{dismissed_count} dismissed.")
            if st.button("Bring dismissed ones back", key="alerts_restore"):
                _write_dismissed_alerts([])
                st.rerun()


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
                disabled: bool = False, currency: Optional[str] = None) -> Decimal:
    settings = current_settings()
    symbol = ""
    from constants import CURRENCY_FORMATS

    code = (currency or settings.base_currency).upper()
    fmt = CURRENCY_FORMATS.get(code)
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
# --------------------------------------------------------------------------
# Currency selection
# --------------------------------------------------------------------------
CURRENCY_KEY = "_selected_currency"

#: What the picker stores when the user asks for everything at once.
ALL_CURRENCIES = "__all__"


def selected_currency(book=None) -> Optional[str]:
    """The active filter: a currency code, or ``None`` meaning "All".

    Persists across pages like the period does — filter to euros on the
    Dashboard and the Transactions list follows you there.
    """
    book = book or currency_book()
    if not book.is_multi:
        return book.primary
    stored = st.session_state.get(CURRENCY_KEY)
    if stored == ALL_CURRENCIES:
        return None
    if stored in book.active:
        return stored
    return book.primary


def set_selected_currency(code: Optional[str]) -> None:
    st.session_state[CURRENCY_KEY] = code or ALL_CURRENCIES


def currency_picker(*, key: str = "currency_picker", label: str = "Currency",
                    include_all: bool = True,
                    default: Optional[str] = None, book=None) -> Optional[str]:
    """Pick one currency, or "All" for a converted view.

    Renders **nothing** on a single-currency book and simply returns the
    primary — that is what keeps a book that never leaves reais looking exactly
    as it did before any of this existed.

    Returns ``None`` for "All", which every aggregator reads as "do not filter".
    """
    book = book or currency_book()
    if not book.is_multi:
        return book.primary

    options: list[str] = list(book.active)
    if include_all:
        options.append(ALL_CURRENCIES)

    if CURRENCY_KEY not in st.session_state and default is not None:
        set_selected_currency(default)
    active = st.session_state.get(CURRENCY_KEY)
    if active not in options:
        active = book.primary

    def render(code: str) -> str:
        if code == ALL_CURRENCIES:
            return f"All · {book.primary}"
        return f"{book.symbol(code)} {code}"

    chosen = st.radio(label, options, index=options.index(active),
                      format_func=render, horizontal=True, key=f"{key}_radio")
    set_selected_currency(None if chosen == ALL_CURRENCIES else chosen)
    return None if chosen == ALL_CURRENCIES else chosen


def converted_notice(book=None, *, today: Optional[date] = None) -> None:
    """Say plainly that a combined figure was converted, and at what."""
    book = book or currency_book()
    others = [c for c in book.active if c != book.primary]
    if not others:
        return
    parts = []
    for code in others:
        if not book.has_rate(code):
            continue
        # md() because two currency symbols in one markdown block render as LaTeX.
        parts.append(md(f"1 {book.symbol(code)} = "
                        f"{format_money(book.rate_to_primary(code), book.primary)}"))
    detail = " · ".join(parts)
    st.caption(
        f"Combined view — every currency converted to {md(book.symbol())} "
        f"{book.primary} at today's rate. {detail}\n\n"
        "Past periods use today's rate too, so they move when you update it."
    )


def fx_rate_slot(*, key: str = "fx_rates", today: Optional[date] = None) -> None:
    """Today's rate for each non-primary currency.

    Hidden entirely on a single-currency book.
    """
    book = currency_book()
    others = [c for c in book.active if c != book.primary]
    if not others:
        return
    today = today or date.today()

    with st.expander(f"💱 Exchange rates · {book.primary}", expanded=not all(
            book.has_rate(c) for c in others)):
        entered: dict[str, Decimal] = {}
        columns = st.columns(len(others) + 1)
        for index, code in enumerate(others):
            with columns[index]:
                current = book.rates.get(code)
                entered[code] = D(st.number_input(
                    f"1 {book.symbol(code)} {code} = ? {book.primary}",
                    min_value=0.0, max_value=1_000_000.0,
                    value=to_float(current) if current is not None else 0.0,
                    step=0.01, format="%.6f", key=f"{key}_{code}",
                ))
                stale = book.stale_days(code, today)
                if current is None:
                    st.caption("⚠️ no rate on file yet")
                elif stale:
                    st.caption(f"set {stale} day(s) ago")
                else:
                    st.caption("set today")
        with columns[-1]:
            st.write("")
            if st.button("Save today's rates", key=f"{key}_save", **wide()):
                def action(session):
                    from services import currency_service

                    saved = 0
                    for code, value in entered.items():
                        if value > 0 and value != book.rates.get(code):
                            currency_service.set_rate(session, code, value, as_of=today)
                            saved += 1
                    return saved

                saved = run_action(action, rerun=False)
                if saved is not None:
                    invalidate_settings()
                    flash(f"{saved} rate(s) recorded." if saved
                          else "Nothing changed.", "success" if saved else "info")
                    st.rerun()

        for code in others:
            stale = book.stale_days(code, today)
            if stale is not None and stale >= 7:
                st.warning(
                    f"The {code} rate was last set {stale} days ago. Converted "
                    "totals are only as current as this number.", icon="⏳")


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
                key: Optional[str] = None, currency: Optional[str] = None,
                currency_key: Optional[str] = None) -> None:
    """``columns`` are ``(source_key, header, kind)`` with kind in
    ``text|money|pct|date|int``.

    ``currency`` pins the whole table; ``currency_key`` names a key on each row
    holding that row's own code, for the genuinely mixed tables (accounts,
    transactions). Cells stay strings either way, so the column dtype the
    dataframe needs is unaffected.
    """
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
                code = (row.get(currency_key) if currency_key else None) or currency
                record[header] = (fmt.money(value, currency=code)
                                  if value is not None else "—")
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
