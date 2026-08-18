"""Read and write application settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from database.models import AppSettings
from database.seed import get_or_create_settings
from schemas.validation import SettingsIn
from services.common import SettingsSnapshot, apply_fields, settings_snapshot


def get_settings_row(session: Session) -> AppSettings:
    return get_or_create_settings(session)


def get_settings(session: Session) -> SettingsSnapshot:
    return settings_snapshot(session)


def update_settings(session: Session, payload: dict[str, Any]) -> SettingsSnapshot:
    """Validate then persist. Unknown keys are ignored, not an error."""
    row = get_settings_row(session)
    current = {
        "base_currency": row.base_currency,
        "date_format": row.date_format,
        "show_cents": row.show_cents,
        "first_day_of_month": row.first_day_of_month,
        "fiscal_year_start_month": row.fiscal_year_start_month,
        "budget_method": row.budget_method,
        "carry_over_surplus": row.carry_over_surplus,
        "income_availability_rule": row.income_availability_rule,
        "income_cutoff_day": row.income_cutoff_day,
        "warning_threshold_pct": row.warning_threshold_pct,
        "critical_threshold_pct": row.critical_threshold_pct,
        "variance_tolerance_pct": row.variance_tolerance_pct,
        "forecast_months": row.forecast_months,
        "theme": row.theme,
        "backup_dir": row.backup_dir,
    }
    current.update({k: v for k, v in payload.items() if k in current})
    validated = SettingsIn(**current)
    apply_fields(row, validated.model_dump())
    session.flush()
    return settings_snapshot(session)


def mark_onboarded(session: Session, value: bool = True) -> None:
    row = get_settings_row(session)
    row.onboarded = value
    session.flush()


def set_dashboard_prefs(session: Session, prefs: dict[str, Any]) -> None:
    row = get_settings_row(session)
    merged = dict(row.dashboard_prefs or {})
    merged.update(prefs)
    row.dashboard_prefs = merged
    session.flush()


def get_dashboard_prefs(session: Session) -> dict[str, Any]:
    row = get_settings_row(session)
    return dict(row.dashboard_prefs or {})


def resolve_backup_dir(session: Session) -> Path:
    import config

    row = get_settings_row(session)
    if row.backup_dir:
        candidate = Path(row.backup_dir).expanduser()
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate.resolve()
        except OSError:
            pass
    config.ensure_dirs()
    return config.BACKUP_DIR
