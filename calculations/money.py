"""Exact monetary arithmetic and locale-aware formatting.

Every monetary value in the application is a :class:`decimal.Decimal`
quantised to two places with ``ROUND_HALF_UP`` (the rounding people expect
from money, unlike Python's default banker's rounding). Binary floats are
accepted at the boundaries — user input, CSV import — and converted through
``str()`` so ``0.1`` becomes ``Decimal("0.1")`` and not
``Decimal("0.1000000000000000055511151231257827")``.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Sequence

from constants import CURRENCY_FORMATS

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")
ONE = Decimal("1")
HUNDRED = Decimal("100")

#: Values smaller than this are treated as zero when judging whether a budget
#: balances. Half a cent — tighter than any real rounding error we produce.
EPSILON = Decimal("0.005")


def D(value: object) -> Decimal:
    """Coerce anything sane into a :class:`Decimal` without float noise."""
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return ZERO
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ZERO
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return ZERO
    try:
        return Decimal(text)
    except InvalidOperation:
        return parse_money(text)


#: A ceiling on any monetary value. Input validation already caps user entry far
#: below this; the point of the limit here is that a runaway calculation — a debt
#: compounding for fifty years at a rate its payment never covers — produces a
#: number with hundreds of digits, and ``quantize`` raises ``InvalidOperation``
#: past the decimal context's 28 significant digits. Saturating keeps a screen
#: rendering with an obviously-absurd figure instead of crashing it outright.
MAX_MONEY = Decimal("999999999999999.99")


def q(value: object, places: int = 2) -> Decimal:
    """Quantise to ``places`` decimals using ROUND_HALF_UP.

    Total: never raises. Non-finite input becomes zero, and anything beyond
    :data:`MAX_MONEY` saturates at that limit.
    """
    exp = Decimal(1).scaleb(-places)
    amount = D(value)
    if not amount.is_finite():
        return ZERO.quantize(exp)
    if amount > MAX_MONEY:
        amount = MAX_MONEY
    elif amount < -MAX_MONEY:
        amount = -MAX_MONEY
    try:
        return amount.quantize(exp, rounding=ROUND_HALF_UP)
    except InvalidOperation:  # pragma: no cover - the guards above cover it
        return ZERO.quantize(exp)


def money(value: object) -> Decimal:
    """Alias of :func:`q` at 2 decimal places — the canonical money form."""
    return q(value, 2)


def money_sum(values: Iterable[object]) -> Decimal:
    total = ZERO
    for value in values:
        total += D(value)
    return money(total)


def safe_div(numerator: object, denominator: object, default: Decimal = ZERO) -> Decimal:
    """Division that returns ``default`` instead of exploding on zero."""
    den = D(denominator)
    if den == 0:
        return default
    return D(numerator) / den


def pct_of(part: object, whole: object, default: Decimal = ZERO) -> Decimal:
    """``part`` as a percentage of ``whole``, rounded to 2 decimals.

    Uses the magnitude of ``whole`` so a negative denominator (e.g. a credit
    balance) does not silently flip the sign of the ratio.
    """
    whole_d = D(whole)
    if whole_d == 0:
        return default
    return q(D(part) / abs(whole_d) * HUNDRED, 2)


def apply_pct(value: object, percent: object) -> Decimal:
    """Increase ``value`` by ``percent`` percent (negative shrinks it)."""
    return money(D(value) * (ONE + D(percent) / HUNDRED))


def is_zero(value: object, tolerance: Decimal = EPSILON) -> bool:
    return abs(D(value)) < tolerance


def clamp(value: object, low: object | None = None, high: object | None = None) -> Decimal:
    result = D(value)
    if low is not None:
        result = max(result, D(low))
    if high is not None:
        result = min(result, D(high))
    return result


def allocate(total: object, weights: Sequence[object]) -> list[Decimal]:
    """Split ``total`` proportionally to ``weights`` losing not one cent.

    The largest remainders receive the leftover cents, so the returned list
    always sums back to exactly ``money(total)``.
    """
    total_d = money(total)
    weight_d = [D(w) for w in weights]
    weight_total = sum(weight_d)
    if not weight_d:
        return []
    if weight_total == 0:
        # Nothing to weight by: spread evenly.
        weight_d = [ONE] * len(weight_d)
        weight_total = D(len(weight_d))

    raw = [total_d * w / weight_total for w in weight_d]
    floored = [r.quantize(CENTS, rounding="ROUND_DOWN") for r in raw]
    remainder = total_d - sum(floored)
    if remainder != 0:
        step = CENTS if remainder > 0 else -CENTS
        # Order indices by the size of the fractional part they lost.
        order = sorted(
            range(len(raw)),
            key=lambda i: (raw[i] - floored[i]),
            reverse=remainder > 0,
        )
        cents_to_spread = int((abs(remainder) / CENTS).to_integral_value())
        for offset in range(cents_to_spread):
            floored[order[offset % len(order)]] += step
    return floored


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------
_DEFAULT_FMT = {"symbol": "", "thousands": ",", "decimal": ".", "prefix": True, "space": True}


def _currency_format(currency: str) -> dict:
    fmt = CURRENCY_FORMATS.get((currency or "").upper())
    if fmt:
        return fmt
    generic = dict(_DEFAULT_FMT)
    generic["symbol"] = (currency or "").upper()
    return generic


def format_money(
    value: object,
    currency: str = "BRL",
    *,
    places: int = 2,
    show_symbol: bool = True,
    signed: bool = False,
    parentheses_for_negative: bool = False,
    compact: bool = False,
) -> str:
    """Render a monetary value the way a human in that locale expects.

    >>> format_money(Decimal("1234.5"), "BRL")
    'R$ 1.234,50'
    >>> format_money(Decimal("-1234.5"), "USD")
    '-$1,234.50'
    """
    fmt = _currency_format(currency)
    amount = q(value, places)
    negative = amount < 0
    amount = abs(amount)

    if compact:
        digits = _compact_digits(amount, fmt)
    else:
        digits = _group_digits(amount, places, fmt)

    if show_symbol and fmt["symbol"]:
        gap = " " if fmt["space"] else ""
        body = f"{fmt['symbol']}{gap}{digits}" if fmt["prefix"] else f"{digits}{gap}{fmt['symbol']}"
    else:
        body = digits

    if negative:
        return f"({body})" if parentheses_for_negative else f"-{body}"
    if signed and amount != 0:
        return f"+{body}"
    return body


def _group_digits(amount: Decimal, places: int, fmt: dict) -> str:
    text = f"{amount:.{places}f}"
    if places > 0:
        whole, _, frac = text.partition(".")
    else:
        whole, frac = text, ""
    grouped = f"{int(whole):,}".replace(",", "\x00").replace("\x00", fmt["thousands"])
    return f"{grouped}{fmt['decimal']}{frac}" if frac else grouped


def _compact_digits(amount: Decimal, fmt: dict) -> str:
    """1.234.567,89 -> 1,23 M (thousands/millions/billions)."""
    for threshold, suffix in ((Decimal("1e9"), "B"), (Decimal("1e6"), "M"), (Decimal("1e3"), "k")):
        if amount >= threshold:
            scaled = q(amount / threshold, 1)
            return f"{_group_digits(scaled, 1, fmt)} {suffix}"
    return _group_digits(amount, 2, fmt)


def format_pct(value: object, places: int = 1, *, signed: bool = False) -> str:
    amount = q(value, places)
    prefix = "+" if signed and amount > 0 else ""
    return f"{prefix}{amount:.{places}f}%"


_NON_NUMERIC = re.compile(r"[^\d,.\-+]")


def parse_money(text: object, currency: str | None = None) -> Decimal:
    """Best-effort parse of user/CSV input into an exact Decimal.

    Handles ``R$ 1.234,56``, ``1,234.56``, ``1234,56``, ``(1.234,56)`` and
    bare ``1234``. Ambiguous input is resolved by assuming the *last*
    separator is the decimal one when it is followed by 1-2 digits.
    """
    if isinstance(text, Decimal):
        return text
    if isinstance(text, (int, float)):
        return D(text)
    raw = str(text or "").strip()
    if not raw:
        return ZERO

    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = _NON_NUMERIC.sub("", raw)
    if cleaned.startswith("-") or "-" in cleaned:
        negative = negative or cleaned.lstrip("+").startswith("-")
    cleaned = cleaned.replace("+", "").replace("-", "")
    if not cleaned:
        return ZERO

    last_comma = cleaned.rfind(",")
    last_dot = cleaned.rfind(".")
    decimal_pos = max(last_comma, last_dot)

    if decimal_pos == -1:
        normalised = cleaned
    else:
        tail = cleaned[decimal_pos + 1:]
        if len(tail) in (1, 2) and tail.isdigit():
            normalised = cleaned[:decimal_pos].replace(",", "").replace(".", "") + "." + tail
        else:
            # Separator is a thousands mark (e.g. "1.234" or "1,234,000").
            normalised = cleaned.replace(",", "").replace(".", "")

    try:
        result = Decimal(normalised or "0")
    except InvalidOperation:
        return ZERO
    return -result if negative else result


def to_float(value: object) -> float:
    """For Plotly / Pandas, which cannot chart Decimals. Never for maths."""
    return float(D(value))
