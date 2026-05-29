"""Core dataclasses passed through the supervisor pipeline.

Everything that flows through `pre_gate → scorer → tool_gate → orchestrator`
is one of these. Pure data, no behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ToolCallIntent:
    """A tool the primary agent intends to call but has not yet executed.

    The supervisor's tool-gate inspects these *before* the tool fires so
    irreversible side effects (place_order, charge_card) get a chance to
    be vetoed.
    """

    name: str
    args: dict[str, Any]
    irreversible: bool = False
    tool_call_id: str | None = None


@dataclass
class HistoryTurn:
    """One past turn in the conversation history."""

    role: Literal["customer", "agent"]
    text: str


@dataclass
class TurnPayload:
    """Everything the scorer needs to judge the current agent response.

    Supports both turn-based and streaming modes:
      - turn-based: primary_text is the full agent response, is_partial=False
      - streaming:  primary_text is the accumulated stream so far,
                    is_partial=True until the boundary token arrives
    """

    customer_text: str
    primary_text: str
    tool_calls: list[ToolCallIntent] = field(default_factory=list)
    history: list[HistoryTurn] = field(default_factory=list)
    is_partial: bool = False
    is_first_sentence_boundary: bool = False


@dataclass
class SupervisorContext:
    """Per-call context plumbed through every layer.

    Replaces the ContextVar-based call_uuid threading that the demo code
    uses. Explicit beats implicit.
    """

    call_uuid: str
    tenant_id: str | None = None  # v2 will key state on this; v1 ignores
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class Verdict:
    """The scorer's output. Shaped so the orchestrator can act on it
    without any further LLM round-trip."""

    score: float  # 0.0 = response is fine, 1.0 = certain failure
    reason: str
    should_intervene: bool
    suggested_correction: str = ""
    # Whether this turn should be queued for the (v2) post-call reporter.
    # v1 ignores this; the field reserves the seam.
    should_report: bool = False
    # If the verdict came from the tool-gate, name the offending tool.
    blocked_tool: str | None = None
    # Free-form evidence the orchestrator may surface in correction text.
    evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def no_intervention(cls, reason: str = "ok") -> "Verdict":
        return cls(score=0.0, reason=reason, should_intervene=False)


@dataclass
class TurnOutcome:
    """Returned by ``CallSupervisor.review_and_speak`` so the caller
    knows what was actually spoken on this turn (the agent's planned
    text, or Mirror's correction) without inspecting Verdict + history
    separately."""

    verdict: Verdict
    spoken_text: str
    intervened: bool


__all__ = [
    "ToolCallIntent",
    "HistoryTurn",
    "TurnPayload",
    "SupervisorContext",
    "Verdict",
    "TurnOutcome",
]
