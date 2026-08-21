"""Personal Finance — local-first budgeting, planning and forecasting.

Run it with::

    streamlit run app.py

Everything lives on this machine: a SQLite file under ``data/``, no accounts, no
network calls, no telemetry.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:  # so `streamlit run app.py` finds the packages
    sys.path.insert(0, str(BASE_DIR))

import config  # noqa: E402
from database.database import init_db  # noqa: E402
from ui import components as ui  # noqa: E402
from ui.palette import apply_to_streamlit as apply_palette_to_streamlit  # noqa: E402

PAGES = [
    ("Dashboard", "📊", "dashboard"),
    ("Budget planning", "🧮", "budget"),
    ("Budget tracking", "🎯", "tracking"),
    ("Transactions", "💳", "transactions"),
    ("Accounts", "🏦", "accounts"),
    ("Goals & debts", "🚩", "goals"),
    ("Forecast", "🔮", "forecast"),
    ("Settings", "⚙️", "settings"),
]

NAV_KEY = "_nav_page"


def bootstrap() -> None:
    """Create the database on first launch and seed the demo dataset."""
    if st.session_state.get("_bootstrapped"):
        return
    config.ensure_dirs()
    init_db()

    from database.seed import is_database_empty
    from services import settings_service

    with ui.db_write() as session:
        settings = settings_service.get_settings_row(session)
        if not settings.onboarded:
            if is_database_empty(session):
                from demo.demo_data import load_demo_data

                report = load_demo_data(session)
                st.session_state["_demo_report"] = report.summary()
            settings.onboarded = True
        else:
            _translate_legacy_names(session)

    st.session_state["_bootstrapped"] = True
    ui.invalidate_settings()


def _translate_legacy_names(session) -> None:
    """Rename the demo rows an older book was seeded with, once.

    The demo dataset used to ship in Portuguese. Changing its definitions only
    affects a fresh install, so a book created before that keeps the old names
    until they are rewritten here. The check is cheap and the rename only
    matches whole known phrases, so a book that has already been translated —
    or never held the demo data at all — pays a single indexed lookup and
    nothing is touched.
    """
    from demo.demo_data import needs_translation, translate_legacy_data

    try:
        if not needs_translation(session):
            return
        changed = translate_legacy_data(session)
    except Exception:
        # Never let a cosmetic rename stop the app from opening.
        return
    if changed:
        st.session_state["_translation_report"] = sum(changed.values())


def apply_theme(settings) -> None:
    """Push the chosen palette into Streamlit's own theme.

    This is what makes dark mode actually dark. Streamlit paints every widget
    it ships from its theme config — including the dataframe grid, which draws
    to a canvas and is unreachable by CSS — so the palette has to be applied
    here, not merely in a stylesheet. Applying it takes effect on the next
    render, hence the single rerun when the choice has changed.
    """
    palette = ui.active_palette(settings)
    if st.session_state.get("_applied_theme") == palette.name:
        return
    changed = apply_palette_to_streamlit(palette)
    st.session_state["_applied_theme"] = palette.name
    if changed:
        st.rerun()


def sidebar() -> str:
    settings = ui.current_settings()
    with st.sidebar:
        st.markdown(f"## 💰 {config.APP_NAME}")
        st.caption(config.APP_TAGLINE)

        labels = [f"{icon}  {name}" for name, icon, _ in PAGES]
        stored = st.session_state.get(NAV_KEY, PAGES[0][2])
        slugs = [slug for _, _, slug in PAGES]
        index = slugs.index(stored) if stored in slugs else 0
        choice = st.radio("Go to", labels, index=index, label_visibility="collapsed",
                          key="_nav_radio")
        slug = slugs[labels.index(choice)]
        st.session_state[NAV_KEY] = slug

        ui.divider()
        _sidebar_snapshot()
        ui.divider()
        st.caption(f"Data stored locally at\n`{config.db_path()}`")
        st.caption(f"v{config.APP_VERSION} · "
                   f"{' · '.join(settings.active_currencies)}")
    return slug


def _sidebar_snapshot() -> None:
    """A tiny always-visible position summary.

    The sidebar sits on every page and has no currency picker of its own, so on
    a multi-currency book it shows one block per currency and never a combined
    line — a single number here could only be a conversion, and the sidebar is
    the last place to quietly apply a rate.
    """
    from services import account_service

    fmt = ui.formatter()
    try:
        with ui.db_read() as session:
            # Loaded once and partitioned in Python: balance_views reads every
            # transaction in the book, so calling it per currency would pay
            # that cost again for each one.
            views = account_service.balance_views(session)
            by_currency = account_service.totals_by_currency(views)
    except Exception:
        return
    if not by_currency:
        return

    book = ui.currency_book()
    ordered = [c for c in book.active if c in by_currency]
    ordered += [c for c in by_currency if c not in ordered]

    st.markdown("**Right now**")
    for code in ordered:
        totals = by_currency[code]
        money = fmt.for_currency(code)
        if len(ordered) > 1:
            st.caption(f"{book.symbol(code)} {code}")
        st.metric("Cash available", money.money(totals.cash))
        st.metric("Net worth", money.money(totals.net_worth))
        if totals.liabilities:
            st.metric("Total owed", money.money(totals.liabilities))


def render(slug: str) -> None:
    if slug == "dashboard":
        from ui import dashboard as page
    elif slug == "budget":
        from ui import budget as page
    elif slug == "tracking":
        from ui import tracking as page
    elif slug == "transactions":
        from ui import transactions as page
    elif slug == "accounts":
        from ui import accounts as page
    elif slug == "goals":
        from ui import goals as page
    elif slug == "forecast":
        from ui import forecast as page
    else:
        from ui import settings as page
    page.render()


def main() -> None:
    st.set_page_config(
        page_title=config.APP_NAME,
        page_icon="💰",
        layout="wide",
        initial_sidebar_state="expanded",
        menu_items={
            "about": f"**{config.APP_NAME}** v{config.APP_VERSION}\n\n"
                     "Local-first personal budgeting. Your financial data never "
                     "leaves this computer.",
        },
    )
    st.session_state["_rerun_token"] = st.session_state.get("_rerun_token", 0) + 1

    bootstrap()
    settings = ui.current_settings()
    apply_theme(settings)
    ui.inject_css(ui.active_palette(settings))

    slug = sidebar()

    renamed = st.session_state.pop("_translation_report", None)
    if renamed:
        st.info(
            f"**Translated to English.** {renamed} demo record(s) that were still "
            "named in Portuguese — accounts, recurring entries, goals, debts and "
            "their transactions — now read in English. Nothing you typed yourself "
            "was changed.",
            icon="🌐",
        )

    demo_note = st.session_state.pop("_demo_report", None)
    if demo_note:
        st.info(
            "**Demo data loaded** so you can see the app working: " + demo_note +
            "\n\nClear it whenever you like — **Settings → Data → Clear all data** — "
            "and the app is yours with an empty book.",
            icon="🧪",
        )

    ui.render_flashes()
    render(slug)


if __name__ == "__main__":
    main()
