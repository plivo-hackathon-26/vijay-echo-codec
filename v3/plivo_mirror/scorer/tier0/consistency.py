"""Numeric / quantity consistency checks.

The agent fabricates a price, quantity, or date that doesn't match what
the customer said, or — more commonly — agrees to a number the customer
mentioned but the tool call carries a different number. Tier 0 catches
this with regex extraction + set comparison.

These checks are *precision-first*: when ambiguous, they return None
and defer to Tier 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from plivo_mirror.context import SupervisorContext, TurnPayload, Verdict
from plivo_mirror.scorer.tier0.base import Tier0Check, Tier0Result


_INT_RE = re.compile(r"\b(\d{1,5})\b")
_MONEY_RE = re.compile(r"(?:[$₹€£])\s*(\d+(?:\.\d{1,2})?)|\b(\d+(?:\.\d{1,2})?)\s*(?:dollars?|rupees?|euros?|pounds?)\b", re.IGNORECASE)

# Word→int mapping for the small numbers callers actually say
_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
}
_NUMBER_WORD_RE = re.compile(
    r"\b(" + "|".join(_NUMBER_WORDS.keys()) + r")\b", re.IGNORECASE
)


def _extract_ints(text: str) -> set[int]:
    """Pull explicit integers and number-words out of text."""
    if not text:
        return set()
    out: set[int] = set()
    for m in _INT_RE.finditer(text):
        try:
            out.add(int(m.group(1)))
        except ValueError:
            pass
    for m in _NUMBER_WORD_RE.finditer(text):
        out.add(_NUMBER_WORDS[m.group(1).lower()])
    return out


def _extract_money(text: str) -> set[float]:
    """Pull money amounts (with $ or 'dollars' suffix) out of text."""
    if not text:
        return set()
    out: set[float] = set()
    for m in _MONEY_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        try:
            out.add(round(float(raw), 2))
        except (TypeError, ValueError):
            pass
    return out


def _ints_in_tool_args(args: dict) -> set[int]:
    """Recursively pull integers out of tool args (including from string
    fields like '$42' or 'two')."""
    if not args:
        return set()
    out: set[int] = set()

    def _walk(v):
        if isinstance(v, bool):
            return  # bool is int subclass — skip
        if isinstance(v, int):
            out.add(v)
        elif isinstance(v, float):
            if v.is_integer():
                out.add(int(v))
        elif isinstance(v, str):
            out.update(_extract_ints(v))
        elif isinstance(v, dict):
            for inner in v.values():
                _walk(inner)
        elif isinstance(v, (list, tuple)):
            for inner in v:
                _walk(inner)

    _walk(args)
    return out


@dataclass
class NumberConsistencyCheck:
    """Fires when the AGENT's planned response contains a money amount
    that disagrees with the customer's stated amount.

    Common failure: customer says "I want a refund for the $42 charge"
    and the agent confirms "$24 refund coming right up." Pure number
    transposition — text supervision catches this without an LLM.
    """

    name: str = "number_consistency"

    def evaluate(
        self, turn: TurnPayload, ctx: SupervisorContext
    ) -> Tier0Result:
        customer_money = _extract_money(turn.customer_text or "")
        agent_money = _extract_money(turn.primary_text or "")
        if not customer_money or not agent_money:
            return Tier0Result(verdict=None, check_name=self.name)

        # If the agent mentioned a money figure NOT in the customer's
        # request AND the customer's figures aren't a superset, that's
        # a contradiction worth flagging.
        agent_only = agent_money - customer_money
        if not agent_only:
            return Tier0Result(verdict=None, check_name=self.name)

        # Only fire when the agent's number is meaningfully off (>1% diff
        # from anything the customer said). This avoids false positives
        # on rounding (e.g. $9.99 vs $10).
        for ag in agent_only:
            mismatch = all(
                abs(ag - cu) / max(ag, cu, 0.01) > 0.01
                for cu in customer_money
            )
            if mismatch:
                return Tier0Result(
                    verdict=Verdict(
                        score=0.92,
                        reason=f"agent cited {ag} not in customer's mentioned amounts {sorted(customer_money)}",
                        should_intervene=True,
                        suggested_correction="",
                        evidence={
                            "tier": "tier0",
                            "check": self.name,
                            "customer_money": sorted(customer_money),
                            "agent_money": sorted(agent_money),
                            "fabricated": ag,
                        },
                        should_report=True,
                    ),
                    check_name=self.name,
                )
        return Tier0Result(verdict=None, check_name=self.name)


@dataclass
class QuantityConsistencyCheck:
    """Fires when a tool call contains a quantity integer that doesn't
    appear in the customer's utterance.

    Conservative: only fires when the tool args have a small positive
    integer (likely a quantity / count) AND the customer's utterance has
    a DIFFERENT small int — that's the wrong-quantity-extracted bug.
    """

    name: str = "quantity_consistency"

    def evaluate(
        self, turn: TurnPayload, ctx: SupervisorContext
    ) -> Tier0Result:
        if not turn.tool_calls:
            return Tier0Result(verdict=None, check_name=self.name)

        customer_ints = _extract_ints(turn.customer_text or "")
        if not customer_ints:
            return Tier0Result(verdict=None, check_name=self.name)

        # Filter to quantity-plausible range
        plausible_customer = {i for i in customer_ints if 0 < i <= 99}
        if not plausible_customer:
            return Tier0Result(verdict=None, check_name=self.name)

        for tc in turn.tool_calls:
            arg_ints = _ints_in_tool_args(tc.args)
            plausible_args = {i for i in arg_ints if 0 < i <= 99}
            if not plausible_args:
                continue
            # If tool args have a small int that's NOT in what the
            # customer said, and what the customer said is NOT in the
            # tool args either → mismatch.
            if not (plausible_args & plausible_customer):
                return Tier0Result(
                    verdict=Verdict(
                        score=0.88,
                        reason=(
                            f"tool {tc.name!r} carries quantity {sorted(plausible_args)} "
                            f"not in customer's mentioned numbers {sorted(plausible_customer)}"
                        ),
                        should_intervene=True,
                        suggested_correction="",
                        blocked_tool=tc.name,
                        evidence={
                            "tier": "tier0",
                            "check": self.name,
                            "customer_ints": sorted(plausible_customer),
                            "tool_arg_ints": sorted(plausible_args),
                            "tool_name": tc.name,
                        },
                        should_report=True,
                    ),
                    check_name=self.name,
                )
        return Tier0Result(verdict=None, check_name=self.name)


__all__ = [
    "NumberConsistencyCheck",
    "QuantityConsistencyCheck",
    "_extract_ints",
    "_extract_money",
    "_ints_in_tool_args",
]
