"""Hook B — pre-TTS gate. **EXPERIMENTAL: interface + stub only in v5.**

Where Hook A is containment (the wrong utterance was already spoken), Hook
B is PREVENTION: it must sit **between the LLM output stream and TTS**,
run L2 over the streamed text *before* synthesis, and hold or replace the
utterance on a firing verdict.

Why it is only an interface in v5:
- it requires intercepting the LiveKit pipeline between ``llm_node`` and
  ``tts_node`` (transport surgery this phase does not wire);
- only L2 is fast enough to sit there (µs); L1/L3 must stay out;
- holding synthesis trades latency for safety on flagged turns — that
  trade needs real-call measurement before it ships.

# TODO: wire into a real TTS path (LiveKit tts_node wrapper) — post-v5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from plivo_mirror_v5.engine import Engine, SessionState
from plivo_mirror_v5.engine.verdict import (
    TurnInput,
    Verdict,
    severity_at_least,
)


@dataclass
class GateDecision:
    """What the gate tells the TTS pipeline to do with the pending text."""

    release: bool                       # True → synthesize as-is
    replacement_text: str | None = None  # spoken instead when held
    verdicts: list[Verdict] = field(default_factory=list)


@runtime_checkable
class PreTTSGate(Protocol):
    """Sits between the LLM output stream and TTS. ``gate`` must complete
    within the inline budget (L2 only — never L3, never a network call)."""

    async def gate(
        self, text: str, claims: list[dict], state: SessionState
    ) -> GateDecision: ...


HELD_FALLBACK = "Let me double-check that for you — one moment."


class StubPreTTSGate:
    """Reference stub: runs ONLY the engine's L2 layer over the pending
    utterance and holds synthesis on a qualifying verdict. Not wired to a
    real TTS path — it exists so the interface is exercised by tests and
    the contract is pinned before the transport work lands."""

    HOOK = "B"

    def __init__(self, engine: Engine, *, call_id: str = "pre-tts") -> None:
        self.engine = engine
        self.call_id = call_id
        self._counter = 0

    async def gate(
        self, text: str, claims: list[dict], state: SessionState
    ) -> GateDecision:
        self._counter += 1
        turn = TurnInput(
            turn_id=f"{self.call_id}-gate{self._counter}",
            call_id=self.call_id,
            turn_index=self._counter,
            role="agent",
            transcript=text,
            claims=claims,
        )
        # L2 only: build the ctx the engine would, but skip L1/L3 entirely.
        from plivo_mirror_v5.engine.layers.base import LayerContext  # noqa: PLC0415

        ctx = LayerContext(
            config=self.engine.config,
            snapshot=state.snapshot(),
            reference=self.engine.reference,
            kb=None,
        )
        verdicts = self.engine.l2.check(turn, state, ctx)
        firing = [
            v for v in verdicts
            if v.fired and severity_at_least(v.severity, self.engine.config.intervene_severity)
        ]
        if firing:
            return GateDecision(release=False, replacement_text=HELD_FALLBACK,
                                verdicts=verdicts)
        return GateDecision(release=True, verdicts=verdicts)
