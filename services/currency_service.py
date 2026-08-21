"""Currencies and exchange rates — the only module that applies a rate.

The book holds up to three currencies. One of them is *primary*
(``AppSettings.base_currency``); every rate is quoted against it, as "how many
units of the primary buy one unit of this currency".

Two rules are worth stating plainly, because everything else follows from them:

* **Conversion always uses the latest rate.** Rates are stored as a dated log so
  the user can record one a day, but nothing looks a rate up by date. A new rate
  therefore re-values history — a deliberate trade the user chose, and one the
  UI has to admit to rather than hide.
* **A missing rate raises.** Never ``1.0``, never zero. Silently treating an
  unknown rate as parity turns "I forgot to enter today's euro" into a wrong
  net-worth figure that looks perfectly plausible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from calculations.money import D, ZERO, money
from constants import CURRENCY_FORMATS, SUPPORTED_CURRENCIES
from database.models import Account, AppSettings, ExchangeRate
from services.common import ServiceError

#: How many currencies one book may hold at once.
MAX_CURRENCIES = 3

#: ``Rate`` stores six decimal places; quantise to the same so a derived rate
#: round-trips the column exactly instead of drifting on the way back out.
RATE_PLACES = Decimal("0.000001")


class MissingRateError(ServiceError):
    """No rate on file for a currency we were asked to convert."""


# --------------------------------------------------------------------------
def normalise(code: object) -> str:
    return str(code or "").strip().upper()


def symbol_for(code: str) -> str:
    fmt = CURRENCY_FORMATS.get(normalise(code))
    return fmt["symbol"] if fmt else normalise(code)


def derive_fx_rate(sent: object, received: object) -> Decimal:
    """The effective rate of a transfer: source units per 1 destination unit.

    Derived from both legs rather than typed, so it records what the bank
    actually did — spread and fees included — instead of the headline rate.
    """
    out = D(sent)
    incoming = D(received)
    if incoming <= 0:
        raise ServiceError("The amount that arrived has to be greater than zero.")
    if out <= 0:
        raise ServiceError("The amount sent has to be greater than zero.")
    return (out / incoming).quantize(RATE_PLACES)


# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CurrencyBook:
    """Everything needed to price one book, resolved once per render."""

    primary: str = "BRL"
    active: tuple[str, ...] = ("BRL",)
    #: foreign code -> units of ``primary`` per 1 unit. The primary is absent.
    rates: Mapping[str, Decimal] = None  # type: ignore[assignment]
    #: foreign code -> the date its rate was set.
    as_of: Mapping[str, date] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rates is None:
            object.__setattr__(self, "rates", {})
        if self.as_of is None:
            object.__setattr__(self, "as_of", {})

    @property
    def is_multi(self) -> bool:
        return len(self.active) > 1

    def is_active(self, code: str) -> bool:
        return normalise(code) in self.active

    def has_rate(self, code: str) -> bool:
        code = normalise(code)
        return code == self.primary or code in self.rates

    def symbol(self, code: Optional[str] = None) -> str:
        return symbol_for(code or self.primary)

    def rate_to_primary(self, code: str) -> Decimal:
        code = normalise(code)
        if code == self.primary:
            return Decimal(1)
        rate = self.rates.get(code)
        if rate is None:
            raise MissingRateError(
                f"No exchange rate on file for {code}. Set one on the Dashboard "
                f"before asking for a converted total."
            )
        return rate

    def convert(self, amount: object, frm: str, to: str) -> Decimal:
        """Convert between any two active currencies.

        Same-currency is a no-op that needs no rate at all — important, because
        a single-currency book must never be able to raise here. Cross-pairs
        pivot through the primary and quantise **once**, at the end; rounding at
        each hop loses cents on the way through.
        """
        frm, to = normalise(frm), normalise(to)
        if frm == to:
            return money(amount)
        value = D(amount) * self.rate_to_primary(frm)
        if to != self.primary:
            value = value / self.rate_to_primary(to)
        return money(value)

    def stale_days(self, code: str, today: Optional[date] = None) -> Optional[int]:
        when = self.as_of.get(normalise(code))
        if when is None:
            return None
        return ((today or date.today()) - when).days


# --------------------------------------------------------------------------
def _settings_row(session: Session) -> AppSettings:
    from database.seed import get_or_create_settings

    return session.get(AppSettings, 1) or get_or_create_settings(session)


def active_currencies(session: Session) -> tuple[str, ...]:
    from services.common import _active_currencies

    return _active_currencies(_settings_row(session))


def latest_rates(session: Session, base: str) -> dict[str, tuple[Decimal, date]]:
    """Most recent quote per currency against ``base``.

    One query for the whole book rather than one per currency — at three
    currencies the difference is not performance, it is that the caller gets a
    consistent snapshot and the ``as_of`` dates for free.
    """
    rows = session.execute(
        select(ExchangeRate)
        .where(ExchangeRate.base_currency == normalise(base))
        .order_by(ExchangeRate.currency,
                  ExchangeRate.as_of_date.desc(),
                  ExchangeRate.id.desc())
    ).scalars()
    found: dict[str, tuple[Decimal, date]] = {}
    for row in rows:
        if row.currency not in found:      # ordered desc, so the first wins
            found[row.currency] = (row.rate, row.as_of_date)
    return found


def book(session: Session) -> CurrencyBook:
    row = _settings_row(session)
    from services.common import _active_currencies

    active = _active_currencies(row)
    primary = active[0]
    quotes = latest_rates(session, primary)
    return CurrencyBook(
        primary=primary,
        active=active,
        rates={c: r for c, (r, _) in quotes.items() if c in active},
        as_of={c: d for c, (_, d) in quotes.items() if c in active},
    )


def set_rate(session: Session, code: str, rate: object, *,
             as_of: Optional[date] = None, base: Optional[str] = None,
             source: str = "manual") -> ExchangeRate:
    """Record (or replace) one day's quote."""
    code = normalise(code)
    primary = normalise(base) or active_currencies(session)[0]
    if code == primary:
        raise ServiceError("The primary currency is always worth one of itself.")
    value = D(rate)
    if value <= 0:
        raise ServiceError("An exchange rate has to be greater than zero.")
    when = as_of or date.today()

    existing = session.execute(
        select(ExchangeRate).where(
            ExchangeRate.as_of_date == when,
            ExchangeRate.currency == code,
            ExchangeRate.base_currency == primary,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.rate = value
        existing.source = source
        session.flush()
        return existing

    row = ExchangeRate(as_of_date=when, currency=code, base_currency=primary,
                       rate=value, source=source)
    session.add(row)
    session.flush()
    return row


def rate_history(session: Session, code: str, *, base: Optional[str] = None,
                 limit: int = 60) -> list[ExchangeRate]:
    primary = normalise(base) or active_currencies(session)[0]
    return list(session.execute(
        select(ExchangeRate)
        .where(ExchangeRate.currency == normalise(code),
               ExchangeRate.base_currency == primary)
        .order_by(ExchangeRate.as_of_date.desc(), ExchangeRate.id.desc())
        .limit(limit)
    ).scalars())


def currencies_in_use(session: Session) -> dict[str, int]:
    """Account count per currency, **archived included**.

    Archived accounts still hold money, so they still hold a currency — this is
    what stops a currency being removed while balances are denominated in it.
    """
    counts: dict[str, int] = {}
    for account in session.execute(select(Account)).scalars():
        code = normalise(account.currency)
        counts[code] = counts.get(code, 0) + 1
    return counts


def set_active_currencies(session: Session, codes: Sequence[str]) -> tuple[str, ...]:
    row = _settings_row(session)
    primary = normalise(row.base_currency) or "BRL"

    cleaned: list[str] = [primary]
    for code in codes:
        text = normalise(code)
        if not text or text == primary or text in cleaned:
            continue
        if text not in SUPPORTED_CURRENCIES:
            raise ServiceError(f"{text} is not a currency this app can format.")
        cleaned.append(text)
    if len(cleaned) > MAX_CURRENCIES:
        raise ServiceError(
            f"This app handles at most {MAX_CURRENCIES} currencies at once."
        )

    in_use = currencies_in_use(session)
    for code, count in in_use.items():
        if code not in cleaned:
            raise ServiceError(
                f"{count} account(s) still hold {code}. Move or delete them "
                f"before removing that currency."
            )

    row.active_currencies = list(cleaned)
    session.flush()
    return tuple(cleaned)


def legs_for_transfer(session: Session, from_account_id: Optional[int],
                      to_account_id: Optional[int],
                      amount: object) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """``(to_amount, fx_rate)`` for a transfer, or ``(None, None)`` if same-currency.

    Anything that builds a transfer ``Transaction`` **without** going through
    ``transaction_service.create_transfer`` must call this. Skipping it does not
    fail loudly: the destination is simply credited with the source currency's
    magnitude, which looks like a plausible number and is wrong.

    The rate used is the latest on file, because a transfer being generated
    ahead of time cannot know what it will really settle at. Completing it with
    the true figure re-derives the rate.
    """
    if not (from_account_id and to_account_id):
        return None, None
    from services.common import account_currency_map

    currencies = account_currency_map(session)
    source = currencies.get(from_account_id)
    target = currencies.get(to_account_id)
    if not source or not target or source == target:
        return None, None
    received = book(session).convert(amount, source, target)
    return received, derive_fx_rate(amount, received)
