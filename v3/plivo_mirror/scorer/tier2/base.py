"""Tier 2 protocol + result type.

A Tier 2 judge produces a full Verdict (not just a probability) for the
turns Tier 1 flagged as uncertain. It runs an LLM round-trip, so it
should only be invoked when really needed — the orchestrator handles
that gating.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from plivo_mirror.context import SupervisorContext, TurnPayload, Verdict


@dataclass
class Tier2Result:
    """Tier 2 judge output.

    Attributes:
        verdict: Full Verdict, ready to feed the intervention
            orchestrator without further transformation.
        raw: Provider-specific raw response payload.
        latency_ms: Wall-clock time of the judge call.
    """

    verdict: Verdict
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0


@runtime_checkable
class Tier2Judge(Protocol):
    """Async judge model. One LLM round-trip per call."""

    name: str

    async def judge(
        self,
        turn: TurnPayload,
        ctx: SupervisorContext,
        tier1_violation_prob: float,
    ) -> Tier2Result: ...


__all__ = ["Tier2Judge", "Tier2Result"]
