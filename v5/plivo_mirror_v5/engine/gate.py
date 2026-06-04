"""Assertiveness gate — decides which agent turns pay the inline judge.

The lesson paid for across v2→v4: an inline LLM judge on EVERY turn kills
voice latency (v2), and a narrow risk lexicon deciding WHAT is risky starves
the judge (v4, 35% catch). This gate splits the difference by asking a much
weaker, recall-biased question: *does this utterance assert anything at
all?* Chitchat, questions and acknowledgements release at ~0 ms; anything
carrying a claim, a number, commitment language or completion language pays
the judge. A high hit-rate on assertive turns is acceptable; a single
false-negative here silently exempts a turn from judgment — so when in
doubt, the gate says assertive.

Stdlib-only and deterministic: the engine core stays offline-capable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Anything resembling a figure: money, times, percentages, bare numbers,
# spelled-out small quantities used in offers ("two free months").
_NUMBERISH_RE = re.compile(
    r"[$€£]\s*\d|\d+(?:[.,:]\d+)?\s*(?:%|percent|dollars?|bucks?|days?|weeks?"
    r"|months?|years?|hours?|minutes?|[ap]m\b)|\b\d{2,}\b",
    re.IGNORECASE,
)

# Commitment language — promising the caller something.
_COMMITMENT_RE = re.compile(
    r"\b(refund(?:ed)?|waive[ds]?|credit(?:ed)?|discount(?:ed)?|free\b"
    r"|guarantee[ds]?|promise[ds]?|eligible|entitled|comp(?:ed)?\b"
    r"|no\s+(?:charge|fee|cost)|on\s+the\s+house)\b",
    re.IGNORECASE,
)

# Completion language — claiming an action happened.
_COMPLETION_RE = re.compile(
    r"\b(?:i(?:'ve| have| already)?|we(?:'ve| have)?|it(?:'s| is| has been)?"
    r"|your \w+ (?:has been|is|was))\s+"
    r"(?:just\s+|already\s+|now\s+|successfully\s+)*"
    r"(?:done|completed?|cancell?ed|processed|updated?|changed?|booked"
    r"|scheduled|placed|charged|refunded|upgraded|downgraded|activated"
    r"|deactivated|submitted|applied|sent|removed|added|transferred)\b",
    re.IGNORECASE,
)

# Definitional / capability assertions about the business or product.
_ASSERTION_RE = re.compile(
    r"\b(?:we|our|the (?:plan|policy|fee|price|router|service|store|company))"
    r"\s+(?:offer|include|support|cover|provide|allow|require|charge)s?\b"
    r"|\bpolicy\s+(?:is|says|states)\b",
    re.IGNORECASE,
)


@dataclass
class GateResult:
    assertive: bool
    reasons: list[str] = field(default_factory=list)


class AssertivenessGate:
    """``check(text, claims)`` → GateResult. Deterministic, ~µs."""

    def check(self, text: str, claims: list[dict] | None = None) -> GateResult:
        reasons: list[str] = []
        if claims:
            reasons.append("claims_extracted")
        if _NUMBERISH_RE.search(text):
            reasons.append("numberish")
        if _COMMITMENT_RE.search(text):
            reasons.append("commitment_language")
        if _COMPLETION_RE.search(text):
            reasons.append("completion_language")
        if _ASSERTION_RE.search(text):
            reasons.append("capability_assertion")
        return GateResult(assertive=bool(reasons), reasons=reasons)
