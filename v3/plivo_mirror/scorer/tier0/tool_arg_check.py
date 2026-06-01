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


# Tier 0 only fires on EXPLICIT, high-confidence retractions. Subtler
# cases — anaphoric "change that first one", multi-turn references — are
# deliberately deferred to Tier 1/2 so this check keeps its near-zero
# false-positive rate.
#
# The previous approach split the utterance on any "changed my mind"
# marker and treated every token before it as retracted. That misfired
# two ways: it flagged carried-over modifiers ("large pepperoni — make it
# mushroom, no pepperoni" kept "large" on the new item, yet "large" was
# scored as retracted), and it flagged additions that merely preceded a
# later correction ("a Coke. change that first pizza to mushroom" scored
# "coke" as retracted). Both were correct orders wrongly intervened on.

# Explicit negation of a named item: "no pepperoni", "without onions",
# "drop the cheese", "not the large one". A comma right after the marker
# ("no, make it…") is a discourse marker, not an item negation, so the
# required whitespace after the marker skips it.
_NEGATION_RE = re.compile(
    r"\b(?:no|not|without|hold|drop|skip|remove|cancel|lose|nix)\b"
    r"\s+(?:the\s+|any\s+|more\s+|that\s+|a\s+|an\s+)*"
    r"([a-z]{3,}(?:\s+[a-z]{3,})?)",
    re.IGNORECASE,
)

# Explicit replacement that names the dropped item: "instead of the
# garlic bread", "rather than the fries".
_INSTEAD_OF_RE = re.compile(
    r"\b(?:instead\s+of|rather\s+than)\s+(?:the\s+|a\s+|an\s+)?"
    r"([a-z]{3,}(?:\s+[a-z]{3,})?)",
    re.IGNORECASE,
)

# "Undo what I just said" markers — these retract the item named
# immediately BEFORE the marker in the same utterance. "change that" is
# intentionally NOT here: it is followed by the item to change and
# usually points at an earlier turn, which Tier 0 cannot resolve.
_SCRATCH_PRIOR_MARKERS = (
    "scratch that",
    "cancel that",
    "forget that",
    "forget it",
    "never mind",
    "nix that",
)


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


def _retracted_items(text: str) -> set[str]:
    """Content tokens the customer EXPLICITLY took back in this utterance.

    Three high-confidence signals only: explicit negation ("no X"),
    explicit replacement ("instead of X"), and "scratch that"-style
    markers that undo the item named just before them. Anything subtler
    (anaphora, multi-turn references) returns nothing here and defers to
    Tier 1/2 — Tier 0 must stay near-zero false positive.
    """
    if not text:
        return set()
    retracted: set[str] = set()
    for rx in (_NEGATION_RE, _INSTEAD_OF_RE):
        for m in rx.finditer(text):
            retracted |= _content_tokens(m.group(1))
    lower = text.lower()
    for marker in _SCRATCH_PRIOR_MARKERS:
        start = 0
        while True:
            idx = lower.find(marker, start)
            if idx == -1:
                break
            prior = [
                w for w in _TOKEN_RE.findall(text[:idx].lower())
                if w not in _STOPWORDS
            ]
            if prior:
                retracted.add(prior[-1])
            start = idx + len(marker)
    return retracted - _STOPWORDS


# Fields whose VALUES are items the agent is leaving OUT, not adding.
# These are generic tool-arg conventions (not domain vocabulary): an
# item listed under "exclude"/"remove" is the agent correctly honouring a
# "no X" request, so it must not count as a kept item.
_EXCLUSION_KEYS = frozenset(
    {
        "exclude", "excluded", "exclusions", "exclude_items",
        "remove", "removed", "removals", "without", "omit", "omitted",
        "hold", "no", "minus", "drop", "dropped", "skip", "skipped",
    }
)


def _items_from_tool_args(args: dict) -> list[str]:
    """Extract the string items the tool call would ADD.

    Mirror is intentionally generic: we don't assume the tool has an
    'items' field — any list[str] / str value is candidate content.
    Values under exclusion-semantic keys (``exclude``, ``remove``,
    ``without``, …) are skipped, because an item the agent recorded as an
    exclusion is the opposite of a kept item.
    """
    out: list[str] = []
    for k, v in (args or {}).items():
        if isinstance(k, str) and k.lower() in _EXCLUSION_KEYS:
            continue
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    out.append(item)
        elif isinstance(v, str):
            out.append(v)
    return out


@dataclass
class ToolArgConsistencyCheck:
    """Fires when the tool args still contain an item the customer
    explicitly retracted in their latest utterance.

    Only triggers on explicit, unambiguous retractions (see
    ``_retracted_items``): "no pepperoni", "instead of the garlic bread",
    "a margherita — scratch that". Anaphoric or multi-turn corrections
    return None and defer to Tier 1/2, keeping this check near-zero
    false-positive.
    """

    name: str = "tool_arg_retracted_item"

    def evaluate(
        self, turn: TurnPayload, ctx: SupervisorContext
    ) -> Tier0Result:
        if not turn.tool_calls:
            return Tier0Result(verdict=None, check_name=self.name)

        retracted = _retracted_items(turn.customer_text or "")
        if not retracted:
            return Tier0Result(verdict=None, check_name=self.name)

        # Now check the tool args for any retracted token. A token the
        # agent itself negated in the args ("no olives", "without cheese")
        # is the agent CORRECTLY recording the exclusion, not keeping a
        # retracted item — so subtract the args' own negations before
        # comparing. Without this, "veggie, no olives" → place_order with a
        # "no olives" modifier would false-fire on "olives".
        for tc in turn.tool_calls:
            arg_text = " ".join(_items_from_tool_args(tc.args))
            arg_tokens = _content_tokens(arg_text) - _retracted_items(arg_text)
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
