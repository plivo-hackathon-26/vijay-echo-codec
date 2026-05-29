"""Tool-argument consistency check.

The single biggest class of voice-agent failure that doesn't need an LLM
to detect: the customer mentioned items {A, B}, but the agent's
place_order tool call includes {A, B, C} — where C was never mentioned,
or was explicitly retracted ("actually, no C").

Tier 0 catches this with O(n) set comparison after a tiny regex pass to
extract candidate item tokens from the customer's utterance(s).

We deliberately don't try to be exhaustive — when uncertain, we return
None and let Tier 1/2 handle it. This check has near-zero false
positives because it only fires when there's an *explicit* mismatch:
the tool args contain tokens the customer never said AND there's
evidence of retraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from plivo_mirror.context import SupervisorContext, TurnPayload, Verdict
from plivo_mirror.scorer.tier0.base import Tier0Check, Tier0Result


# A retraction marker means the customer changed their mind — anything
# said BEFORE the marker is no longer part of the order.
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
    """Find the LATEST retraction marker in the customer's utterance.

    Returns (before, after) text on the latest marker, or None if there's
    no retraction.
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


_TOKEN_RE = re.compile(r"[a-zA-Z]{3,}")


def _content_tokens(text: str) -> set[str]:
    """Lowercase content-word tokens (≥3 chars). Drops stopwords."""
    if not text:
        return set()
    return {w.lower() for w in _TOKEN_RE.findall(text)} - _STOPWORDS


_STOPWORDS = {
    "the", "and", "for", "you", "with", "have", "want", "would",
    "like", "get", "give", "this", "that", "these", "those", "but",
    "yes", "yeah", "okay", "alright", "actually", "wait", "scratch",
    "change", "cancel", "instead", "just", "only", "never", "mind",
    "forget", "skip", "let", "make", "got", "one", "two", "please",
    "thanks", "thank", "sure", "alright", "going", "gonna",
}


def _items_from_tool_args(args: dict) -> list[str]:
    """Extract string items from any list-of-strings field in tool args.

    Mirror is intentionally generic: we don't assume the tool has an
    'items' field. Any list[str] in the args is treated as candidate
    content.
    """
    out: list[str] = []
    for v in (args or {}).values():
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    out.append(item)
        elif isinstance(v, str):
            out.append(v)
    return out


@dataclass
class ToolArgConsistencyCheck:
    """Fires when the tool args contain a retracted item from the
    customer's utterance.

    Only triggers when:
      1. Customer's last utterance has a retraction marker.
      2. The tool args contain item-tokens that appear ONLY in the
         pre-retraction half (i.e. the customer said it, then took it
         back, and the agent included it anyway).
    """

    name: str = "tool_arg_retracted_item"

    def evaluate(
        self, turn: TurnPayload, ctx: SupervisorContext
    ) -> Tier0Result:
        if not turn.tool_calls:
            return Tier0Result(verdict=None, check_name=self.name)

        split = _split_on_retraction(turn.customer_text or "")
        if split is None:
            return Tier0Result(verdict=None, check_name=self.name)

        before_text, after_text = split
        before_tokens = _content_tokens(before_text)
        after_tokens = _content_tokens(after_text)
        retracted = before_tokens - after_tokens
        if not retracted:
            return Tier0Result(verdict=None, check_name=self.name)

        # Now check the tool args for any retracted token.
        for tc in turn.tool_calls:
            arg_items = _items_from_tool_args(tc.args)
            arg_tokens: set[str] = set()
            for item in arg_items:
                arg_tokens |= _content_tokens(item)
            hits = arg_tokens & retracted
            if hits:
                hit_word = sorted(hits)[0]
                return Tier0Result(
                    verdict=Verdict(
                        score=0.98,
                        reason=f"tool {tc.name!r} includes retracted item: {hit_word!r}",
                        should_intervene=True,
                        suggested_correction="",  # let the generator produce one
                        blocked_tool=tc.name,
                        evidence={
                            "tier": "tier0",
                            "check": self.name,
                            "tool_name": tc.name,
                            "retracted_tokens": sorted(retracted),
                            "tool_arg_tokens": sorted(arg_tokens),
                            "violating_tokens": sorted(hits),
                            "customer_text": turn.customer_text,
                        },
                        should_report=True,
                    ),
                    check_name=self.name,
                    evidence={"hits": sorted(hits)},
                )
        return Tier0Result(verdict=None, check_name=self.name)


__all__ = ["ToolArgConsistencyCheck"]
