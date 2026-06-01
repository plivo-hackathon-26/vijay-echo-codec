"""Tier 0 — deterministic checks. No ML, no LLM, no network.

Every check is a pure function that runs in microseconds. Catches the
classes of failures that don't need a model:

  • Tool-arg consistency: did the agent's place_order include items
    the customer never mentioned (or omit items the customer DID
    mention)?
  • Number / date / quantity consistency: agent fabricated a number
    that wasn't in the customer's request, OR conflicts with one.
  • Policy keyword tripwires: customer said "refund" and the agent's
    response doesn't mention "transfer" or "supervisor" — fire the
    handoff policy.
  • Contradiction markers: customer used "actually", "wait", "no" —
    the response must reflect the *later* preference.

This tier returns either:
  • a hard Verdict (intervention or pass) → short-circuit
  • None → escalate to Tier 1
"""

from plivo_mirror.scorer.tier0.arithmetic import ArithmeticConsistencyCheck
from plivo_mirror.scorer.tier0.base import Tier0Check, Tier0Result
from plivo_mirror.scorer.tier0.consistency import (
    NumberConsistencyCheck,
    QuantityConsistencyCheck,
)
from plivo_mirror.scorer.tier0.contradiction import ContradictionMarkerCheck
from plivo_mirror.scorer.tier0.policy_tripwires import PolicyTripwireCheck
from plivo_mirror.scorer.tier0.tool_arg_check import ToolArgConsistencyCheck

__all__ = [
    "Tier0Check",
    "Tier0Result",
    "ToolArgConsistencyCheck",
    "NumberConsistencyCheck",
    "QuantityConsistencyCheck",
    "ArithmeticConsistencyCheck",
    "PolicyTripwireCheck",
    "ContradictionMarkerCheck",
]
