"""Entity validators — pure code, never prompts.

As soon as the caller gives a committable value (item, name, amount,
date), it is validated HERE and written to ``SessionState`` outside the
model's context. Each validator returns a ``ValidatedEntity`` (normalized,
typed) or ``None`` if the raw value does not validate.

Validators are intentionally conservative: a value that does not cleanly
parse returns ``None`` rather than guessing. Richer NLU date/amount
extraction belongs upstream (the agent's intent extraction); these
validators are the last line that decides what is allowed into state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

EntityKind = Literal["item", "name", "amount", "date"]


@dataclass(frozen=True)
class ValidatedEntity:
    """A committable value that passed validation. ``value`` is the
    normalized, typed form (``Decimal`` for amount, ``date`` for date,
    ``str`` for item/name); ``raw`` is the original text."""

    kind: EntityKind
    value: Any
    raw: str


# ── amount ────────────────────────────────────────────────────────────

_AMOUNT_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def validate_amount(raw: str | None) -> ValidatedEntity | None:
    """Parse a monetary amount to a non-negative ``Decimal``.

    Accepts ``"$12.50"``, ``"12 dollars"``, ``"1,299.00"``, ``"7"``.
    Rejects negatives and anything without a numeric core.
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    for token in ("dollars", "dollar", "usd", "$", ","):
        s = s.replace(token, "")
    s = s.strip()
    m = _AMOUNT_NUM_RE.search(s)
    if not m:
        return None
    try:
        val = Decimal(m.group(0))
    except InvalidOperation:
        return None
    if val < 0:
        return None
    return ValidatedEntity(kind="amount", value=val, raw=str(raw))


# ── date ──────────────────────────────────────────────────────────────

# US-first ordering for the ambiguous numeric form; documented choice.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%b %d %Y",
    "%B %d %Y",
    "%d %b %Y",
    "%d %B %Y",
)


def validate_date(raw: str | None) -> ValidatedEntity | None:
    """Parse a calendar date. Accepts ISO (``2026-06-15``), US numeric
    (``06/15/2026``), and common month-name forms. Returns ``None`` on
    anything it cannot parse unambiguously."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt).date()
            return ValidatedEntity(kind="date", value=d, raw=str(raw))
        except ValueError:
            continue
    try:
        d = date.fromisoformat(s[:10])
        return ValidatedEntity(kind="date", value=d, raw=str(raw))
    except ValueError:
        return None


# ── name ──────────────────────────────────────────────────────────────

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{0,98}$")


def validate_name(raw: str | None) -> ValidatedEntity | None:
    """Validate a personal name: starts with a letter, only letters and
    common name punctuation, whitespace collapsed. Rejects empty/numeric."""
    if raw is None:
        return None
    s = " ".join(str(raw).split())
    if not s or not _NAME_RE.match(s):
        return None
    return ValidatedEntity(kind="name", value=s, raw=str(raw))


# ── item ──────────────────────────────────────────────────────────────


def validate_item(
    raw: str | None, *, catalog: set[str] | None = None
) -> ValidatedEntity | None:
    """Validate an order item. Normalizes whitespace + case. When a
    ``catalog`` is supplied, the item must be in it (case-insensitive) —
    this is how off-menu inventions get rejected at the state boundary."""
    if raw is None:
        return None
    norm = " ".join(str(raw).split()).lower()
    if not norm:
        return None
    if catalog is not None and norm not in {c.lower() for c in catalog}:
        return None
    return ValidatedEntity(kind="item", value=norm, raw=str(raw))


# ── dispatch ──────────────────────────────────────────────────────────

_VALIDATORS = {
    "amount": validate_amount,
    "date": validate_date,
    "name": validate_name,
    "item": validate_item,
}


def validate(kind: EntityKind, raw: str | None) -> ValidatedEntity | None:
    """Dispatch to the validator for ``kind``. Raises ``KeyError`` for an
    unknown kind (a programming error, not a data error)."""
    return _VALIDATORS[kind](raw)


__all__ = [
    "EntityKind",
    "ValidatedEntity",
    "validate",
    "validate_amount",
    "validate_date",
    "validate_name",
    "validate_item",
]
