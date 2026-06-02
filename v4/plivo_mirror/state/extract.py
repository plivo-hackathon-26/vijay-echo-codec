"""Generic entity extraction: the caller's turn → validated ``SessionState``.

Deterministic, regex-based, **zero added model latency**. Captures universal
committable value types out of the CUSTOMER's utterance and writes them to
``SessionState`` via the existing validators (``state/entities.py``). This is
the real implementation behind ``SupervisedAgent.extract_state`` (which was a
no-op) — it is the structural backbone of both defenses:

- the **action guard** can now compare proposed tool args against validated
  state (it had nothing to compare against before), and
- the **speech verifier** sees the caller-supplied values it must ground a
  read-back against.

No business logic lives here. WHAT counts as a valid amount/date is decided by
the validators; reference business facts (catalog, prices, hours) are
code-owned config seeded as ``SessionState.known_facts`` — never extracted
from the model. The capture schema is configurable; the default covers the
two universally-committable types with robust validators (money, date). Add a
``CaptureRule`` (key + ``EntityKind`` + pattern) to capture more — the value
still passes through the matching validator before it is allowed into state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from plivo_mirror.state.entities import EntityKind
from plivo_mirror.state.session import SessionState


@runtime_checkable
class EntityExtractor(Protocol):
    """Pulls committable values out of a customer turn and writes the
    validated ones into ``state``. Deterministic; never calls a model."""

    def extract(self, customer_text: str, state: SessionState) -> None: ...


@dataclass(frozen=True)
class CaptureRule:
    """One capture: store the matched text under ``key`` as ``EntityKind``.

    ``pattern`` is matched against the customer text; ``group`` selects which
    regex group is the raw value (0 = whole match). The raw value is handed to
    the validator for ``kind`` — a value that does not validate is dropped, so
    a rule can never write an unvalidated value into state.
    """

    key: str
    kind: EntityKind
    pattern: re.Pattern[str]
    group: int = 0


# Default schema — universal committable types with robust validators.
# Money: "$12.50", "12 dollars", "1,299". Date: ISO / US-numeric / month-name.
_MONEY_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d{1,2})?"
    r"|\b\d[\d,]*(?:\.\d{1,2})?\s*(?:dollars?|usd|bucks)\b",
    re.I,
)
_DATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}"
    r"(?:,?\s+\d{4})?\b",
    re.I,
)

DEFAULT_RULES: tuple[CaptureRule, ...] = (
    CaptureRule(key="amount", kind="amount", pattern=_MONEY_RE),
    CaptureRule(key="date", kind="date", pattern=_DATE_RE),
)


class RegexEntityExtractor:
    """Deterministic ``EntityExtractor``. For each rule, the FIRST match in
    the customer text is validated and written under the rule's key. A rule
    whose value fails validation (or finds no match) leaves state unchanged —
    conservative by design: it never guesses a value into the source of truth.

    Multi-value turns ("$9 and $3") capture only the first value per key; give
    distinct keys/patterns to capture more. This is a deliberate floor, not a
    full NLU — richer extraction is the customer's job and plugs in by passing
    a different ``EntityExtractor`` to the ``Firewall``.
    """

    def __init__(self, rules: tuple[CaptureRule, ...] | None = None) -> None:
        self._rules = rules if rules is not None else DEFAULT_RULES

    def extract(self, customer_text: str, state: SessionState) -> None:
        text = customer_text or ""
        if not text.strip():
            return
        for rule in self._rules:
            m = rule.pattern.search(text)
            if not m:
                continue
            try:
                raw = m.group(rule.group)
            except IndexError:  # rule points at a group the pattern lacks
                continue
            # ``write_entity`` validates; an invalid value returns None and
            # leaves state untouched.
            state.write_entity(rule.key, rule.kind, raw)


__all__ = ["EntityExtractor", "RegexEntityExtractor", "CaptureRule", "DEFAULT_RULES"]
