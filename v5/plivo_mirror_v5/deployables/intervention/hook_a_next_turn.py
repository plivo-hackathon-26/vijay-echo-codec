"""Hook A — next-turn correction injection.

On a firing L2 verdict at/above ``config.intervene_severity``, build a
``[CORRECTION: …]`` system message from the verdict's evidence and inject
it into the active agent's chat context (the LiveKit pattern:
``current_agent.chat_ctx.copy()`` + ``update_chat_ctx(...)``), so the
agent self-corrects on its NEXT turn. Above a per-call threshold it
escalates to ``handoff`` instead.

BE CLEAR ABOUT WHAT THIS IS: the wrong utterance was ALREADY SPOKEN by the
time Hook A runs. This is **containment, not prevention** — the agent
corrects itself one turn later. Prevention (gating synthesis itself) is
Hook B's job, which is experimental in v5 (see ``hook_b_pre_tts.py``).

The hook is an ``InterventionHandler``: the observer (mode="intervene")
awaits it with the ``TurnResult`` and emits whatever ``Action`` it returns.
"""

from __future__ import annotations

import inspect
from typing import Protocol, runtime_checkable

from plivo_mirror_v5.engine.config import EngineConfig
from plivo_mirror_v5.engine.verdict import (
    Action,
    TurnResult,
    Verdict,
    severity_at_least,
)


@runtime_checkable
class ChatContextLike(Protocol):
    """The slice of livekit.agents ChatContext Hook A touches."""

    def copy(self) -> "ChatContextLike": ...
    def add_message(self, *, role: str, content: str) -> None: ...


@runtime_checkable
class AgentLike(Protocol):
    """The slice of a livekit.agents Agent Hook A touches.
    ``update_chat_ctx`` may be sync or async (LiveKit's is async)."""

    @property
    def chat_ctx(self) -> ChatContextLike: ...

    def update_chat_ctx(self, ctx: ChatContextLike): ...


def build_correction_message(verdicts: list[Verdict]) -> str:
    """The ``[CORRECTION: …]`` system message, framed around the evidence.
    Deterministic, template-only — no LLM in the intervention path."""
    lines = []
    for v in verdicts:
        ev = v.evidence
        if ev is None:
            continue
        if ev.claim_type == "action":
            lines.append(
                f"You told the caller an action was done, but the system shows"
                f" '{ev.source}' is {ev.truth_value}. Tell the caller it has NOT"
                f" completed yet and recover (retry it or set expectations)."
            )
        else:
            lines.append(
                f"You told the caller '{ev.spoken_value}' for {ev.claim_type},"
                f" but the verified value from {ev.source} is"
                f" '{ev.truth_value}'. State the correct value."
            )
    body = " ".join(lines)
    return (
        "[CORRECTION: Your previous reply contained an error. "
        f"{body} Correct yourself briefly and naturally in your next reply; "
        "do not blame the caller.]"
    )


class HookANextTurn:
    """``intervention_handler`` for the observer.

    Decision policy (deliberately simple; swap via ``decide``):
    - qualifying verdicts → ``correct`` (inject the system message);
    - more than ``handoff_after`` interventions in one call → ``handoff``
      (a human takes over; injection has clearly not stuck).
    """

    HOOK = "A"

    def __init__(
        self,
        agent: AgentLike,
        config: EngineConfig | None = None,
        *,
        handoff_after: int = 3,
    ) -> None:
        self.agent = agent
        self.config = config or EngineConfig(mode="intervene")
        self.handoff_after = handoff_after
        self.interventions_this_call = 0

    async def __call__(self, result: TurnResult) -> Action:
        qualifying = [
            v for v in result.fired_verdicts
            if severity_at_least(v.severity, self.config.intervene_severity)
        ]
        if not qualifying:
            return Action(taken="none")

        self.interventions_this_call += 1
        if self.interventions_this_call > self.handoff_after:
            # TODO: deliver call context to the human (warm handoff) — the
            # transfer mechanics are transport-specific and post-v5.
            return Action(taken="handoff", hook=self.HOOK)

        correction = build_correction_message(qualifying)
        ctx = self.agent.chat_ctx.copy()
        ctx.add_message(role="system", content=correction)
        maybe_coro = self.agent.update_chat_ctx(ctx)
        if inspect.isawaitable(maybe_coro):
            await maybe_coro
        return Action(taken="correct", hook=self.HOOK, correction_text=correction)


class FakeChatContext:
    """Test stand-in for livekit ChatContext."""

    def __init__(self, messages: list[dict] | None = None) -> None:
        self.messages = list(messages or [])

    def copy(self) -> "FakeChatContext":
        return FakeChatContext(self.messages)

    def add_message(self, *, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})


class FakeAgent:
    """Test stand-in for a livekit Agent."""

    def __init__(self) -> None:
        self._chat_ctx = FakeChatContext()
        self.update_calls = 0

    @property
    def chat_ctx(self) -> FakeChatContext:
        return self._chat_ctx

    async def update_chat_ctx(self, ctx: FakeChatContext) -> None:
        self._chat_ctx = ctx
        self.update_calls += 1
