"""Filesystem locations and application-wide configuration.

Everything is local. Nothing here points at a network service.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Personal Finance"
APP_TAGLINE = "Zero-based budgeting, planning and forecasting — 100% local"
APP_VERSION = "1.0.0"

#: Project root (the folder holding ``app.py``).
BASE_DIR = Path(__file__).resolve().parent

#: Where the SQLite database and backups live. Override with the
#: ``PFA_DATA_DIR`` environment variable to keep data outside the project
#: folder (useful if the project directory is under version control).
DATA_DIR = Path(os.environ.get("PFA_DATA_DIR", BASE_DIR / "data")).resolve()

#: Real user data.
DB_FILENAME = "finance.db"
#: Separate file used by the demo dataset so real data is never touched.
DEMO_DB_FILENAME = "finance_demo.db"

BACKUP_DIR = Path(os.environ.get("PFA_BACKUP_DIR", DATA_DIR / "backups")).resolve()
EXPORT_DIR = Path(os.environ.get("PFA_EXPORT_DIR", DATA_DIR / "exports")).resolve()

#: Set ``PFA_DEMO=1`` to launch against the demo database.
DEMO_ENV_FLAG = "PFA_DEMO"
#: Set ``PFA_DB_PATH`` to point at an explicit database file (tests use this).
DB_PATH_ENV = "PFA_DB_PATH"


def demo_mode_enabled() -> bool:
    return os.environ.get(DEMO_ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def db_path(demo: bool | None = None) -> Path:
    """Return the absolute path of the SQLite file currently in use."""
    explicit = os.environ.get(DB_PATH_ENV)
    if explicit:
        return Path(explicit).expanduser().resolve()
    if demo is None:
        demo = demo_mode_enabled()
    return DATA_DIR / (DEMO_DB_FILENAME if demo else DB_FILENAME)


def ensure_dirs() -> None:
    for directory in (DATA_DIR, BACKUP_DIR, EXPORT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Defaults applied when a brand new database is created
# --------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "base_currency": "BRL",
    "date_format": "DD/MM/YYYY",
    "decimal_places": 2,
    "first_day_of_month": 1,
    "fiscal_year_start_month": 1,
    "budget_method": "zero_based",
    "income_availability_rule": "earned_period",
    "income_cutoff_day": 25,
    "warning_threshold_pct": "80",
    "critical_threshold_pct": "100",
    "variance_tolerance_pct": "5",
    "forecast_months": 12,
    "theme": "auto",
    "show_cents": True,
    "carry_over_surplus": True,
    "dashboard_default_range": "current_period",
}

#: How many months of planned transactions the recurrence engine materialises
#: ahead of "today" when the user asks for auto-generation.
DEFAULT_GENERATION_HORIZON_MONTHS = 12

#: Hard ceilings so a typo cannot produce a runaway projection.
MAX_FORECAST_MONTHS = 120
MAX_GENERATION_HORIZON_MONTHS = 60
