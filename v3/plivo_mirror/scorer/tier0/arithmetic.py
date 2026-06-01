"""Arithmetic consistency check (Tier 0).

The agent states a total, change, or per-item math that is simply wrong —
"a $9 pizza and a $3 Coke, that's $15 total." Arithmetic is deterministic,
so it does not belong in an LLM tier: the judge pattern-matches "looks
like a total" and never actually computes. This check recomputes the
result from the numbers in the conversation and fires only when it can
derive an unambiguous expected value that the agent contradicts.

Precision-first: when the structure is ambiguous (multiple plausible
results, no clear operands, itemised breakdown we can't pin to one
figure), it returns None and defers to the later tiers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from plivo_mirror.context import SupervisorContext, TurnPayload, Verdict
from plivo_mirror.scorer.tier0.base import Tier0Result
from plivo_mirror.scorer.tier0.consistency import _NUMBER_WORDS

_WORDS = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))

# A single money operand, captured non-overlapping left-to-right so
# "at nine dollars" yields 9 once (not twice). Branch order matters.
_OPERAND_RE = re.compile(
    r"(?:\b(?:with|paying|pay|gave|tender(?:ed)?)\s+(?:a\s+)?\$?(\d+(?:\.\d{1,2})?|" + _WORDS + r")\b)"
    r"|(?:\b(?:at|for)\s+\$?(\d+(?:\.\d{1,2})?|" + _WORDS + r")\b)"
    r"|(?:\$\s*(\d+(?:\.\d{1,2})?))"
    r"|(?:\b(\d+(?:\.\d{1,2})?|" + _WORDS + r")\s+(?:dollars?|bucks?|rupees?|each)\b)",
    re.IGNORECASE,
)
_NUM_TOKEN_RE = re.compile(r"\b(\d+(?:\.\d{1,2})?|" + _WORDS + r")\b", re.IGNORECASE)

# Result-figure anchors: a money amount sitting next to one of these is
# the agent's asserted answer.
_RESULT_AFTER_RE = re.compile(
    r"(?:total|comes? to|that'?s|that is|altogether|grand total|you owe|owe|it'?s)"
    r"\D{0,12}\$?\s*(\d+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_RESULT_BEFORE_RE = re.compile(
    r"\$?\s*(\d+(?:\.\d{1,2})?)\s*(?:back|in change|change)\b", re.IGNORECASE
)

_TOTAL_KW = ("total", "comes to", "come to", "altogether", "owe", "what do i pay", "what's the damage")
_CHANGE_KW = ("change", "get back", "back?")


def _to_num(tok: str | None) -> float | None:
    if not tok:
        return None
    tok = tok.strip().lstrip("$").lower()
    if tok in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[tok])
    try:
        return float(tok)
    except ValueError:
        return None


def _operands(text: str) -> list[float]:
    """Currency-marked money operands, in order, no double counting."""
    out: list[float] = []
    for m in _OPERAND_RE.finditer(text or ""):
        v = _to_num(next((g for g in m.groups() if g is not None), None))
        if v is not None:
            out.append(round(v, 2))
    return out


def _all_numbers(text: str) -> list[float]:
    out: list[float] = []
    for m in _NUM_TOKEN_RE.finditer(text or ""):
        v = _to_num(m.group(1))
        if v is not None:
            out.append(v)
    return out


def _result_amount(agent_text: str) -> float | None:
    """The single figure the agent asserts as the answer, or None if it
    can't be pinned to exactly one."""
    cands: list[float] = []
    for rx in (_RESULT_AFTER_RE, _RESULT_BEFORE_RE):
        for m in rx.finditer(agent_text or ""):
            v = _to_num(m.group(1))
            if v is not None:
                cands.append(round(v, 2))
    if cands:
        uniq = set(cands)
        return cands[0] if len(uniq) == 1 else None
    # Fall back to a lone money figure anywhere in the reply.
    monies = _operands(agent_text)
    return monies[0] if len(set(monies)) == 1 else None


@dataclass
class ArithmeticConsistencyCheck:
    """Fires when the agent's stated total/change contradicts the math
    derivable from the numbers in the turn.

    Handles three common shapes, precision-first:
      • sum of item prices vs a stated total
      • quantity × unit-price vs a stated total
      • tendered − cost vs a stated change
    Anything it can't pin to one expected value → None (defer).
    """

    name: str = "arithmetic_consistency"

    def evaluate(
        self, turn: TurnPayload, ctx: SupervisorContext
    ) -> Tier0Result:
        cust = turn.customer_text or ""
        agent = turn.primary_text or ""
        blob = f"{cust}\n{agent}".lower()

        asks_total = any(k in blob for k in _TOTAL_KW)
        asks_change = any(k in blob for k in _CHANGE_KW)
        if not (asks_total or asks_change):
            return Tier0Result(verdict=None, check_name=self.name)

        asserted = _result_amount(agent)
        if asserted is None:
            return Tier0Result(verdict=None, check_name=self.name)

        prices = _operands(cust)
        expected: float | None = None
        kind = "total"

        if asks_change and len(prices) >= 2:
            expected = round(max(prices) - min(prices), 2)
            kind = "change"
        elif asks_total:
            if len(prices) >= 2:
                expected = round(sum(prices), 2)
            elif len(prices) == 1:
                price_vals = set(prices)
                counts = [
                    int(v) for v in _all_numbers(cust)
                    if v not in price_vals and 1 <= v <= 50 and float(v).is_integer()
                ]
                if counts:
                    expected = round(prices[0] * max(counts), 2)

        if expected is None:
            return Tier0Result(verdict=None, check_name=self.name)
        if abs(asserted - expected) <= 0.01:
            return Tier0Result(verdict=None, check_name=self.name)

        return Tier0Result(
            verdict=Verdict(
                score=0.9,
                reason=(
                    f"agent stated {kind} {asserted:g} but the correct "
                    f"{kind} is {expected:g}"
                ),
                should_intervene=True,
                suggested_correction="",
                evidence={
                    "tier": "tier0",
                    "check": self.name,
                    "kind": kind,
                    "asserted": asserted,
                    "expected": expected,
                    "operands": prices,
                },
                should_report=True,
            ),
            check_name=self.name,
        )


__all__ = ["ArithmeticConsistencyCheck"]
