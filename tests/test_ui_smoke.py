"""Smoke test: every screen renders, against a database full of demo data.

This is the test that would have caught a typo in a page module — it actually
executes ``app.py`` through Streamlit's own test harness and fails on any
uncaught exception, for every one of the nine sections.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

PAGE_LABELS = [
    "📊  Dashboard",
    "🧮  Budget planning",
    "🎯  Budget tracking",
    "💳  Transactions",
    "🏦  Accounts",
    "🚩  Goals & debts",
    "🔮  Forecast",
    "📈  Reports",
    "⚙️  Settings",
]


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory):
    """A populated database shared by every page check."""
    directory = tmp_path_factory.mktemp("uidata")
    path = directory / "smoke.db"
    os.environ["PFA_DB_PATH"] = str(path)
    os.environ["PFA_DATA_DIR"] = str(directory)
    os.environ["PFA_BACKUP_DIR"] = str(directory / "backups")

    from database.database import get_session_factory, init_db, reset_engine_cache

    reset_engine_cache()
    init_db(path)
    session = get_session_factory(path)()
    try:
        from demo.demo_data import load_demo_data

        # Match what a real first launch produces. A shorter window hid a
        # runaway-compounding crash on Goals & debts, because the demo credit
        # card had not yet grown past the point where its minimum payment
        # stops covering the monthly interest.
        load_demo_data(session, months_back=18, months_forward=6,
                       today=date.today())
        session.commit()
    finally:
        session.close()
    yield path
    reset_engine_cache()
    for key in ("PFA_DB_PATH", "PFA_DATA_DIR", "PFA_BACKUP_DIR"):
        os.environ.pop(key, None)


def _run(app_test):
    app_test.run()
    if app_test.exception:
        messages = "\n".join(
            f"{item.type}: {item.message}\n{item.stack_trace}"
            for item in app_test.exception
        )
        pytest.fail(f"the app raised while rendering:\n{messages}")
    return app_test


def test_app_starts(demo_db):
    at = _run(AppTest.from_file(str(ROOT / "app.py"), default_timeout=180))
    assert at.sidebar.radio, "the navigation radio should be in the sidebar"
    assert at.markdown, "the page should render some content"


@pytest.mark.parametrize("label", PAGE_LABELS)
def test_every_page_renders(demo_db, label):
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=240)
    _run(at)
    at.sidebar.radio[0].set_value(label)
    _run(at)
    assert at.markdown or at.dataframe or at.metric, f"{label} rendered nothing"


def test_dashboard_shows_the_headline_metrics(demo_db):
    at = _run(AppTest.from_file(str(ROOT / "app.py"), default_timeout=240))
    labels = {metric.label for metric in at.metric}
    assert any("Cash available" in label for label in labels)
    assert any("Net worth" in label for label in labels)
    assert any("Savings rate" in label for label in labels)


def test_period_navigation_does_not_break_the_dashboard(demo_db):
    at = _run(AppTest.from_file(str(ROOT / "app.py"), default_timeout=240))
    previous = next(
        (button for button in at.button if button.label == "‹"), None)
    if previous is None:
        pytest.skip("period stepper not present in this render")
    previous.click()
    _run(at)
    assert at.metric


def test_empty_database_renders_the_first_run_state(tmp_path, monkeypatch):
    """A brand new book must not crash — it should invite the user in."""
    path = tmp_path / "empty.db"
    monkeypatch.setenv("PFA_DB_PATH", str(path))
    monkeypatch.setenv("PFA_DATA_DIR", str(tmp_path))

    from database.database import get_session_factory, init_db, reset_engine_cache

    reset_engine_cache()
    init_db(path)
    session = get_session_factory(path)()
    try:
        from services import settings_service

        # Mark onboarded so the demo loader does not fill it in.
        settings_service.mark_onboarded(session, True)
        session.commit()
    finally:
        session.close()

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
    _run(at)
    text = " ".join(item.value for item in at.markdown)
    assert "waiting for its first numbers" in text or "Dashboard" in text
    reset_engine_cache()


# --------------------------------------------------------------------------
# Table shaping
# --------------------------------------------------------------------------
def test_tables_with_missing_values_still_convert_to_arrow():
    """Streamlit serialises every dataframe through Arrow.

    A numeric column holding a mix of ints and a placeholder string becomes
    dtype ``object``, Arrow rejects it, and the table silently fails to render.
    Missing integers therefore have to stay ``None``.
    """
    import pandas as pd
    import pyarrow as pa

    from calculations.money import ZERO
    from services.common import SettingsSnapshot
    from ui.components import Formatter

    fmt = Formatter(SettingsSnapshot())
    rows = [
        {"name": "Cleared", "months": 20, "interest": ZERO, "when": date(2026, 1, 1)},
        {"name": "Never clears", "months": None, "interest": None, "when": None},
    ]
    columns = [("name", "Strategy", "text"), ("months", "Months", "int"),
               ("interest", "Total interest", "money"), ("when", "Payoff", "date")]

    shaped = []
    for row in rows:
        record = {}
        for source, header, kind in columns:
            value = row.get(source)
            if kind == "money":
                record[header] = fmt.money(value) if value is not None else "—"
            elif kind == "date":
                record[header] = fmt.date(value)
            elif kind == "int":
                record[header] = int(value) if value is not None else None
            else:
                record[header] = value if value is not None else ""
        shaped.append(record)

    pa.Table.from_pandas(pd.DataFrame(shaped))       # must not raise
    assert shaped[1]["Months"] is None
    assert shaped[1]["Total interest"] == "—"


def test_money_table_renders_a_never_clears_row(demo_db):
    """The real path: the strategies table has a null Months for a stuck debt."""
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=240)
    _run(at)
    at.sidebar.radio[0].set_value("🚩  Goals & debts")
    _run(at)
    assert at.dataframe, "the strategy comparison table should render"


# --------------------------------------------------------------------------
# Markdown safety
# --------------------------------------------------------------------------
def _latex_pairs(text: str) -> list[str]:
    """Spans Streamlit's markdown would render as maths instead of money.

    Two unescaped ``$`` in one block make everything between them LaTeX, so
    ``R$ 1.234,50 · -R$ 98,76`` loses both symbols and sets the amount in a
    serif italic. Escaped ``\\$`` is fine and is what the app should emit.
    """
    import re

    return re.findall(r"(?<!\\)\$[^$\n]{1,80}(?<!\\)\$", text)


@pytest.mark.parametrize("label", PAGE_LABELS)
def test_no_page_renders_money_as_latex(demo_db, label):
    """No screen may hand Streamlit two bare ``$`` in one markdown block.

    Blocks the app renders as raw HTML — the status pills — are exempt: the
    frontend parses those as a single HTML node and never looks for maths
    inside, which is why the pills have always shown their currency symbols.
    """
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=240)
    _run(at)
    at.sidebar.radio[0].set_value(label)
    _run(at)

    offenders: list[str] = []
    blocks = list(at.markdown) + list(at.caption) + list(at.warning) + \
        list(at.success) + list(at.error) + list(at.info)
    for block in blocks:
        if getattr(block, "allow_html", False):
            continue
        offenders += [f"{label}: …{span}…"
                      for span in _latex_pairs(str(getattr(block, "value", "")))]
    for button in at.button:
        offenders += [f"{label} (button): …{span}…"
                      for span in _latex_pairs(str(button.label))]

    assert not offenders, (
        "currency symbols are being parsed as LaTeX — wrap the text in "
        "ui.md():\n" + "\n".join(offenders))


def test_no_markdown_call_carries_two_bare_money_values():
    """The static half of the same rule, for branches a render cannot reach.

    Rendering only exercises the states the demo data happens to produce — the
    "this debt never clears" panel, for one, needs a debt whose interest
    outruns its payment. Reading the source catches those too: any markdown
    call whose text interpolates two amounts must use ``fmt.md_money``.
    """
    import ast

    markdown_calls = {"markdown", "caption", "write", "info", "warning",
                      "error", "success", "toast"}
    symbol_producing = {"money", "signed_money", "format_money"}
    escaping = {"md", "md_money"}

    def bare_amounts(node) -> int:
        """Count amounts that reach markdown still carrying a raw ``$``.

        Anything already inside ``ui.md(...)`` or ``fmt.md_money(...)`` is
        escaped, so that subtree is pruned rather than counted.
        """
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in escaping:
                return 0
            if name in symbol_producing:
                return 1
        return sum(bare_amounts(child) for child in ast.iter_child_nodes(node))

    offenders = []
    for path in sorted((ROOT / "ui").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) not in markdown_calls or not node.args:
                continue
            if bare_amounts(node.args[0]) >= 2:
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        "these markdown calls interpolate two currency symbols and will render "
        "as LaTeX — use fmt.md_money() or ui.md():\n  " + "\n  ".join(offenders))


def test_md_escapes_only_the_currency_symbol():
    from ui.components import md

    assert md("R$ 1,00 and R$ 2,00") == r"R\$ 1,00 and R\$ 2,00"
    assert md("**bold** stays bold") == "**bold** stays bold"
    assert md(12) == "12"
