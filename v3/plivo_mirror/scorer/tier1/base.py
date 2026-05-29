"""Tier 1 protocol + result type.

A Tier 1 classifier produces a calibrated probability that the agent's
planned response violates the customer's intent or one of the operator's
policies. Output is a continuous score in [0, 1] plus a confidence band.

Confidence bands map to the orchestrator's decision tree:
  • confidence == "high"   → emit hard verdict (intervene or pass)
  • confidence == "uncertain" → escalate to Tier 2 (LLM judge)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from plivo_mirror.context import SupervisorContext, TurnPayload


Confidence = Literal["high", "uncertain"]


@dataclass
class Tier1Result:
    """Calibrated classifier output.

    Attributes:
        violation_prob: P(turn violates policy / customer intent) ∈ [0, 1].
            A pure passthrough to Verdict.score for compatibility.
        confidence: Whether the classifier is confident enough that the
            orchestrator can short-circuit, or whether it should escalate
            to Tier 2.
        raw: Provider-specific raw response payload (kept for telemetry).
        latency_ms: Wall-clock time spent on the classifier call.
    """

    violation_prob: float
    confidence: Confidence
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0


@runtime_checkable
class Tier1Classifier(Protocol):
    """Async NLI-style classifier. Single round-trip per turn."""

    name: str

    async def classify(
        self, turn: TurnPayload, ctx: SupervisorContext
    ) -> Tier1Result: ...


__all__ = ["Tier1Classifier", "Tier1Result", "Confidence"]
