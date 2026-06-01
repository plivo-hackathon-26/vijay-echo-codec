"""v4 core contracts — the data + protocols every component is built
against. Pure data and interfaces; no behaviour beyond small construction
helpers. Everything that flows through the dual-boundary firewall is one
of these.

Locked in Phase 1. Components (guards, verifier, runtime, adapter) depend
only on this module and ``state.session`` — never on each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    Protocol,
    runtime_checkable,
)

if TYPE_CHECKING:  # avoid a runtime import cycle (SessionState imports Policy)
    from plivo_mirror.state.session import SessionState


# A guard's decision about a single turn.
#   pass    — release the planned reply / let the action fire, untouched.
#   correct — substitute an agent-voice correction before voicing.
#   block   — stop the reply/action; re-confirm, fix from state, or escalate.
Decision = Literal["pass", "correct", "block"]


@dataclass
class ToolCallIntent:
    """A tool the agent intends to call but has NOT executed yet. The
    action guard inspects these before any irreversible side effect fires.

    Under the zero-argument principle the executor reads its arguments
    from ``SessionState``; ``args`` here is the model's *proposed* intent,
    which the action guard checks for consistency against state.
    """

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    irreversible: bool = False
    tool_call_id: str | None = None


@dataclass
class HistoryTurn:
    """One past turn in the conversation."""

    role: Literal["customer", "agent"]
    text: str


@dataclass
class Verdict:
    """A guard's output. Shaped so the runtime can act on it without any
    further LLM round-trip."""

    decision: Decision
    reason: str = ""
    policy_id: str | None = None
    span: str = ""             # the flagged text span (empty when whole-turn)
    confidence: float = 1.0    # the guard's confidence in THIS decision
    spoken_correction: str = ""  # agent-voice replacement (for correct/block)

    @property
    def intervened(self) -> bool:
        """True when this verdict changes the happy path (correct or block)."""
        return self.decision != "pass"

    @classmethod
    def pass_(cls, reason: str = "ok", *, confidence: float = 1.0) -> "Verdict":
        return cls(decision="pass", reason=reason, confidence=confidence)

    @classmethod
    def correct(
        cls,
        *,
        reason: str,
        spoken_correction: str,
        policy_id: str | None = None,
        span: str = "",
        confidence: float = 1.0,
    ) -> "Verdict":
        return cls(
            decision="correct",
            reason=reason,
            spoken_correction=spoken_correction,
            policy_id=policy_id,
            span=span,
            confidence=confidence,
        )

    @classmethod
    def block(
        cls,
        *,
        reason: str,
        policy_id: str | None = None,
        span: str = "",
        spoken_correction: str = "",
        confidence: float = 1.0,
    ) -> "Verdict":
        return cls(
            decision="block",
            reason=reason,
            policy_id=policy_id,
            span=span,
            spoken_correction=spoken_correction,
            confidence=confidence,
        )


@dataclass
class Policy:
    """A plain-English policy compiled into a runnable check object.

    ``check`` is a pure, deterministic function over a ``TurnContext`` that
    returns a violating ``Verdict`` or ``None``. Policies with ``check is
    None`` are *verifier-only*: their ``text`` becomes evidence for the
    grounded verifier (Phase 2). Business logic lives in ``check`` — never
    in a prompt.
    """

    id: str
    text: str
    check: Callable[["TurnContext"], "Verdict | None"] | None = None

    def run(self, ctx: "TurnContext") -> "Verdict | None":
        """Run the compiled check, if any. Returns None for verifier-only
        policies or when the policy does not apply to this turn."""
        if self.check is None:
            return None
        return self.check(ctx)


@dataclass
class TurnContext:
    """The per-turn bundle both guards inspect. Carries the source of
    truth (``state``) plus what the agent is *about* to do this turn."""

    state: "SessionState"
    planned_reply: str = ""
    tool_intents: list[ToolCallIntent] = field(default_factory=list)
    customer_text: str = ""
    # Optional top-K logprobs for the planned reply, populated by the
    # adapter ONLY when the agent LLM exposes them. The confidence signal
    # reads this; when absent, confidence is treated as unknown and the
    # router escalates any risky span (documented degraded mode).
    logprobs: Any | None = None


@runtime_checkable
class Guard(Protocol):
    """The single interface both the speech guard and the action guard
    implement. The runtime invokes ``inspect`` on each boundary."""

    async def inspect(self, context: TurnContext) -> Verdict: ...


__all__ = [
    "Decision",
    "ToolCallIntent",
    "HistoryTurn",
    "Verdict",
    "Policy",
    "TurnContext",
    "Guard",
]
