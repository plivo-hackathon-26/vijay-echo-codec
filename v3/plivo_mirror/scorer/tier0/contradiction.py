"""Contradiction-marker check.

Pure signal: customer used an explicit retraction marker ("actually",
"wait", "scratch that") AND the agent's response is parroting back
content from BEFORE the marker. This is the classic "captured the
retracted item" failure that's expensive to catch with an LLM and
trivial to catch with text comparison.

Conservative: only fires when there's strong signal in both halves of
the comparison. Otherwise defers to Tier 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from plivo_mirror.context import SupervisorContext, TurnPayload, Verdict
from plivo_mirror.scorer.tier0.base import Tier0Result
from plivo_mirror.scorer.tier0.tool_arg_check import _content_tokens


# This verbal check works off a coarse before/after split of the
# customer's utterance. It tolerates the imprecision because it ALSO
# requires the agent to have echoed the post-retraction tokens (the
# "captured both" guard below), which keeps its false-positive rate low.
# (The tool-arg check, which fires at 0.98 with no such guard, uses the
# stricter ``_retracted_items`` signal instead.)
_RETRACTION_MARKERS = (
    "actually",
    "wait",
    "no, ",
    "no. ",
    "scratch that",
    "change that",
    "cancel that",
    "instead",
    "make it",
    "just ",
    "only ",
    "never mind",
    "forget that",
    "skip that",
)


def _split_on_retraction(text: str) -> tuple[str, str] | None:
    """Split the utterance on its LATEST retraction marker.

    Returns (before, after) text, or None if there's no marker.
    """
    if not text:
        return None
    lower = text.lower()
    best_end = -1
    for marker in _RETRACTION_MARKERS:
        idx = lower.rfind(marker)
        if idx == -1:
            continue
        end = idx + len(marker)
        if end > best_end:
            best_end = end
    if best_end == -1:
        return None
    return text[:best_end], text[best_end:]


@dataclass
class ContradictionMarkerCheck:
    """Fires when the customer retracted something and the agent's
    planned RESPONSE includes the retracted token.

    Complements ToolArgConsistencyCheck (which checks tool args). This
    catches the case where the agent doesn't fire a tool but still
    confirms the wrong thing verbally — e.g.
        Customer: "Large pepperoni, actually just mushroom only"
        Agent:    "Got it, one large pepperoni and one mushroom!"
        ↑ no tool call, but the agent re-uttered the retracted item
    """

    name: str = "contradiction_marker"
    min_evidence_tokens: int = 1
    score: float = 0.93

    def evaluate(
        self, turn: TurnPayload, ctx: SupervisorContext
    ) -> Tier0Result:
        split = _split_on_retraction(turn.customer_text or "")
        if split is None:
            return Tier0Result(verdict=None, check_name=self.name)

        before, after = split
        before_tokens = _content_tokens(before)
        after_tokens = _content_tokens(after)
        retracted = before_tokens - after_tokens
        if not retracted:
            return Tier0Result(verdict=None, check_name=self.name)

        primary_tokens = _content_tokens(turn.primary_text or "")
        hits = primary_tokens & retracted
        if len(hits) < self.min_evidence_tokens:
            return Tier0Result(verdict=None, check_name=self.name)

        # Bonus precision: if the agent's response ALSO contains the
        # post-retraction tokens, this is a clear "captured both" case
        # rather than a benign acknowledgement.
        if not (primary_tokens & after_tokens):
            # Agent doesn't reflect the retraction — might just be
            # acknowledging the original. Defer to Tier 1.
            return Tier0Result(verdict=None, check_name=self.name)

        return Tier0Result(
            verdict=Verdict(
                score=self.score,
                reason=(
                    f"agent re-stated retracted token(s) {sorted(hits)} "
                    f"after customer's retraction"
                ),
                should_intervene=True,
                suggested_correction="",
                evidence={
                    "tier": "tier0",
                    "check": self.name,
                    "retracted_tokens": sorted(retracted),
                    "agent_repeated_tokens": sorted(hits),
                    "customer_text": turn.customer_text,
                    "primary_text": turn.primary_text,
                },
                should_report=True,
            ),
            check_name=self.name,
            evidence={"hits": sorted(hits)},
        )


__all__ = ["ContradictionMarkerCheck"]
