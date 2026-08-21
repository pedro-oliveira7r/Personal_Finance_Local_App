"""Read and write application settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from database.models import AppSettings
from database.seed import get_or_create_settings
from schemas.validation import SettingsIn
from services.common import ServiceError, SettingsSnapshot, apply_fields, settings_snapshot


def get_settings_row(session: Session) -> AppSettings:
    return get_or_create_settings(session)


def get_settings(session: Session) -> SettingsSnapshot:
    return settings_snapshot(session)


def update_settings(session: Session, payload: dict[str, Any]) -> SettingsSnapshot:
    """Validate then persist. Unknown keys are ignored, not an error."""
    row = get_settings_row(session)
    current = {
        "base_currency": row.base_currency,
        # Omitting this from the whitelist would make it silently unsettable:
        # BaseIn ignores unknown keys, so no error would ever surface.
        "active_currencies": list(row.active_currencies or [row.base_currency]),
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
    if validated.base_currency != row.base_currency:
        _rebase_primary_currency(session, row.base_currency, validated.base_currency)
        # The validator kept the old primary in the list (it had no way to know
        # the book was being rebased), and apply_fields below would write that
        # stale pair straight back over the restamp.
        validated.active_currencies = [validated.base_currency]

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


def _rebase_primary_currency(session: Session, old: str, new: str) -> None:
    """Switch the book's primary currency, or explain why it cannot move.

    Every stored exchange rate is quoted against the primary, and no amount is
    ever converted, so moving it under a book that holds real money would
    silently re-label that money. But a book that has not started yet — the
    seeded starter accounts, empty and untouched — must be free to pick its
    currency, otherwise a new user could never leave the default behind.
    """
    from sqlalchemy import func, select as _select

    from database.models import Account, ExchangeRate, Transaction

    new = new.upper()
    stragglers = list(session.execute(
        _select(Account).where(func.upper(Account.currency) != new)
    ).scalars())
    if not stragglers:
        return

    in_use = []
    for account in stragglers:
        moved = session.execute(
            _select(func.count()).select_from(Transaction).where(
                (Transaction.account_id == account.id)
                | (Transaction.to_account_id == account.id)
            )
        ).scalar() or 0
        if moved or account.opening_balance:
            in_use.append(account.name)
    if in_use:
        raise ServiceError(
            f"{len(in_use)} account(s) still hold {old} — "
            f"{', '.join(in_use[:3])}{'…' if len(in_use) > 3 else ''}. Amounts are "
            f"never converted, and every exchange rate on file is quoted against "
            f"{old}, so the primary currency can only change while the book is empty."
        )

    # Only untouched accounts remain: restamp them and drop the now-meaningless
    # rate history, which was quoted against a primary that no longer exists.
    for account in stragglers:
        account.currency = new
    session.execute(ExchangeRate.__table__.delete())
    session.flush()
