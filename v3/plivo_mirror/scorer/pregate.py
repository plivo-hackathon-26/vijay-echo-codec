"""Tiered scoring pre-gate.

Deterministic, zero-LLM heuristics that decide whether the LLM scorer
should run for the current turn. Cuts the supervisor's LLM cost ~80%
on average traffic by skipping turns that demonstrably can't go wrong.

Heuristics (any one fires → score):
  1. The agent decided to call a tool this turn.
  2. The agent's response mentions digits, dates, or money symbols
     (high-stakes content).
  3. The customer's last utterance contained a correction marker
     (``actually`` / ``wait`` / ``no`` / ``instead`` / ``scratch`` /
     ``change`` / ``cancel``).
  4. The customer's last two turns lexically disagree — i.e. share
     few content words. Catches "X. Actually Y." style retractions
     even when no explicit marker appears.
  5. The previous turn already triggered an intervention (we want to
     verify the correction landed).

Otherwise → skip the scorer entirely.

These are GENERIC — no domain vocabulary. Correction markers are
English-by-default and configurable via ``markers`` arg if a customer
needs another language.
"""

from __future__ import annotations

import re

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import TurnPayload


_DEFAULT_MARKERS = (
    "actually",
    "wait",
    "no,",
    "no.",
    "not ",
    "instead",
    "scratch",
    "change that",
    "cancel that",
    "make it",
    "just ",
    "only ",
)

_HIGH_STAKES_RE = re.compile(
    r"(?:\b\d+\b"            # any integer
    r"|\$|₹|€|£"             # currency symbols
    # Quantity number words ("two orders", "three flights", "five dollars")
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten"
    r"|eleven|twelve|twenty|thirty|forty|fifty|hundred|thousand)\b"
    # Days / time-of-day
    r"|\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\b(?:tomorrow|tonight|today|yesterday|weekend)\b"
    r"|\bAM\b|\bPM\b"
    # Sensitive-intent keywords — any policy worth writing tends to
    # touch one of these. Domain-neutral, not pizza-specific.
    r"|\b(?:refund|refunds|refunding|refunded"
    r"|cancel|cancelled|cancellation|cancelling"
    r"|charge|charged|charging"
    r"|subscription|subscriptions"
    r"|past\s+order|last\s+order|previous\s+order|recent\s+order"
    r"|delivery\s+(?:status|time)|where\s+is\s+my\s+order"
    r"|transfer|hangup"
    r"|payment|pay|paid|invoice|bill)\b"
    r")",
    re.IGNORECASE,
)

_WORD_RE = re.compile(r"\b[a-zA-Z]{4,}\b")


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    t = " " + text.lower() + " "
    return any(m in t for m in markers)


def _content_words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def _turns_disagree(prev_text: str, curr_text: str) -> bool:
    """Cheap proxy for "customer changed their mind without saying so."

    Returns True when the two utterances share fewer than half of the
    smaller utterance's content words AND each has at least 2 content
    words. Short utterances are excluded to avoid false positives on
    "yes" / "okay" / "thanks".
    """
    a = _content_words(prev_text)
    b = _content_words(curr_text)
    if len(a) < 2 or len(b) < 2:
        return False
    overlap = a & b
    smallest = min(len(a), len(b))
    return len(overlap) < (smallest / 2)


def should_score(
    turn: TurnPayload,
    config: MirrorConfig,
    *,
    markers: tuple[str, ...] = _DEFAULT_MARKERS,
    prev_intervention: bool = False,
) -> tuple[bool, str]:
    """Return (run_scorer, reason).

    ``reason`` is a short label useful for logging / metrics so you can
    see *why* a turn was scored or skipped.
    """
    # Master switch off → always score (preserves correctness when the
    # customer wants maximum coverage and is paying for it).
    if not config.tiered_scoring_enabled:
        return True, "tiered_off"

    # Heuristic 1 — tool calls.
    if turn.tool_calls:
        if config.tiered_force_score_on_tool_call:
            return True, "tool_call_present"
        # Even with the force flag off, score irreversible tool calls.
        for tc in turn.tool_calls:
            if tc.irreversible or tc.name in set(config.irreversible_tools):
                return True, f"irreversible_tool:{tc.name}"

    # Heuristic 5 — previous turn was an intervention; verify the
    # follow-up before letting it speak unchecked.
    if prev_intervention:
        return True, "post_intervention_verify"

    # Heuristic 2 — high-stakes content in EITHER the agent's planned
    # response OR the customer's last utterance. Customer mentions of
    # "refund", "cancel", "charge", "where is my order", etc. should
    # always score even if the agent is about to politely deflect —
    # because the deflection might itself be wrong (e.g. confirming
    # the refund instead of transferring).
    if _HIGH_STAKES_RE.search(turn.primary_text or "") or _HIGH_STAKES_RE.search(
        turn.customer_text or ""
    ):
        return True, "high_stakes_content"

    # Heuristic 3 — explicit correction marker in customer's utterance.
    if _has_marker(turn.customer_text or "", markers):
        return True, "correction_marker"

    # Heuristic 4 — two adjacent customer turns lexically disagree.
    customer_turns = [h for h in turn.history if h.role == "customer"]
    if len(customer_turns) >= 1:
        last_customer = customer_turns[-1].text or ""
        if _turns_disagree(last_customer, turn.customer_text or ""):
            return True, "adjacent_disagreement"

    return False, "skipped_safe"


__all__ = ["should_score"]
