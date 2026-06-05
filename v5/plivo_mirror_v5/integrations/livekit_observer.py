"""THE single LiveKit observer — powers both deployables.

Registers on the session's ``conversation_item_added`` event and runs the
engine over **agent** turns — the inverse of LiveKit's published observer
example, which filters to *user* turns to steer the agent. We verify the
agent's claims; user turns still flow through the engine because L1 (input
integrity) reads ASR confidence and readback corrections from them — but
no verdict on a user turn ever routes to an action.

Evaluation runs off the event loop (``asyncio.create_task`` +
``asyncio.to_thread``) so the verdict path never blocks the live call.

The ``mode`` flag selects ROUTING ONLY (the engine and the observer code
path are identical either way):

- ``"shadow"``    — Deployable 1: ``action.taken = "would_have"`` when a
  verdict crosses the intervention threshold; everything goes to telemetry.
- ``"intervene"`` — Deployable 2: the verdict is handed to the configured
  ``intervention_handler`` (Hook A / hold / handoff), and the resulting
  action is emitted.

``call_id`` == the LiveKit room/session id; we never mint our own.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol, runtime_checkable

from plivo_mirror_v5.engine import Engine, SessionState
from plivo_mirror_v5.engine.gate import AssertivenessGate
from plivo_mirror_v5.engine.verdict import (
    Action,
    Evidence,
    TurnInput,
    TurnResult,
    Verdict,
    new_verdict_id,
    severity_at_least,
)
from plivo_mirror_v5.telemetry import TelemetryEmitter


@dataclass
class ConversationItem:
    """The slice of a LiveKit conversation item the observer consumes.
    The real adapter maps ``livekit.agents`` items onto this shape."""

    role: str                          # "user" | "agent"
    text: str
    asr_confidence: float | None = None
    claims: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    audio_offset_ms: float | None = None
    audio_duration_ms: float | None = None
    audio_levels: list[float] | None = None   # 0..1 RMS samples (signal view)


@runtime_checkable
class ClaimExtractor(Protocol):
    """Extracts the claims L2/L3 operate on from a conversation item."""

    def extract(self, item: ConversationItem) -> list[dict]: ...


class PassthroughClaimExtractor:
    """Default: trust claims already attached to the item (by the host
    runtime, a tool, or an eval fixture).
    # TODO: real NLU claim extraction (price/policy/action spans) — post-v5.
    """

    def extract(self, item: ConversationItem) -> list[dict]:
        return item.claims


# An intervention handler receives the TurnResult and performs whatever the
# deployable does about it, returning the Action actually taken (Phase 3).
InterventionHandler = Callable[[TurnResult], Awaitable[Action]]


class MirrorObserver:
    def __init__(
        self,
        engine: Engine,
        emitter: TelemetryEmitter,
        *,
        mode: str | None = None,
        agent_id: str = "unknown",
        agent_version: str = "unknown",
        claim_extractor: ClaimExtractor | None = None,
        intervention_handler: InterventionHandler | None = None,
        shadow_judge=None,
    ) -> None:
        """``shadow_judge``: optional ``TurnJudge`` (``judge_turn``) run
        FLAG-ONLY on assertive agent turns the deterministic layer did not
        already flag — closes the real-time factual-recall seam in shadow
        mode (wrong price surfaces as ``would_have`` while the call is
        live, not only post-call). Async, off the hot path, hard timeout,
        fail-open. None (the default) costs nothing."""
        self.engine = engine
        self.emitter = emitter
        self.mode = mode or engine.config.mode
        if self.mode not in ("shadow", "intervene"):
            raise ValueError(f"unknown mode: {self.mode!r}")
        self.agent_id = agent_id
        self.agent_version = agent_version
        self.claim_extractor = claim_extractor or PassthroughClaimExtractor()
        self.intervention_handler = intervention_handler
        self.shadow_judge = shadow_judge
        self._assertiveness = AssertivenessGate()
        self.state: SessionState | None = None
        self.call_id: str | None = None
        self.results: list[TurnResult] = []   # kept for tests/inspection
        self._turn_counter = itertools.count()
        self._pending: set[asyncio.Task] = set()
        # Evaluations mutate shared SessionState (tool log, disclosure
        # counters) — serialize them in dispatch order. _on_item still
        # returns immediately; only the BACKGROUND tasks queue on the lock.
        self._eval_lock = asyncio.Lock()

    # -- wiring ---------------------------------------------------------------

    def attach(self, session) -> None:
        """Bind to a (real or fake) LiveKit session. call_id inherits the
        room id so telemetry joins LiveKit traces + the audio recording."""
        self.call_id = session.room_id
        self.state = SessionState(self.call_id)
        self.emitter.start_call(
            self.call_id,
            agent_id=self.agent_id,
            agent_version=self.agent_version,
        )
        session.on("conversation_item_added", self._on_item)

    def _on_item(self, item: ConversationItem) -> None:
        # Never block the call loop: schedule and return immediately.
        # turn_index is assigned HERE (synchronously) so indices follow
        # dispatch order even though evaluation happens in the background.
        turn_index = next(self._turn_counter)
        task = asyncio.create_task(self._evaluate(item, turn_index))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        """Await all in-flight evaluations (tests / call teardown)."""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=False)

    def close(self, outcome: str = "completed") -> None:
        if self.call_id is not None:
            self.emitter.end_call(self.call_id, outcome=outcome)

    # -- evaluation -------------------------------------------------------------

    async def _evaluate(self, item: ConversationItem, turn_index: int) -> None:
        turn = TurnInput(
            turn_id=f"{self.call_id}-t{turn_index}",
            call_id=self.call_id,
            turn_index=turn_index,
            role=item.role,
            transcript=item.text,
            asr_confidence=item.asr_confidence,
            claims=self.claim_extractor.extract(item),
            tool_calls=item.tool_calls,
        )
        # Serialize: the engine mutates shared SessionState (tool log,
        # disclosure counters, L1 gate) — overlapping evaluations on one
        # call must not interleave, and emit order must match dispatch.
        async with self._eval_lock:
            # The engine call is sync; to_thread keeps the event loop free
            # even when L3 (the only model-in-the-loop layer) is slow.
            result = await asyncio.to_thread(
                self.engine.evaluate_turn, turn, self.state)
            if self.shadow_judge is not None and turn.role == "agent":
                judge_verdict = await self._judge_flag_only(turn, result)
                if judge_verdict is not None:
                    result.verdicts.append(judge_verdict)
            result.action = await self._route(result)
            self.results.append(result)
            self.emitter.turn_span(
                result,
                audio_offset_ms=item.audio_offset_ms,
                audio_duration_ms=item.audio_duration_ms,
                audio_levels=item.audio_levels,
            )

    async def _judge_flag_only(
        self, turn: TurnInput, result: TurnResult
    ) -> Verdict | None:
        """The shadow judge: grounded verdict on an assertive agent turn,
        appended FLAG-ONLY (it routes like any other verdict — in shadow
        that means ``would_have``, never an intervention).

        Cost controls: only assertive turns (AssertivenessGate); skipped
        when L2 already fired at the intervention threshold (deterministic
        wins — no point paying the judge); hard timeout, fail-open. Judge
        calls are already serialized per call by the eval lock."""
        threshold = self.engine.config.intervene_severity
        if any(severity_at_least(v.severity, threshold)
               for v in result.fired_verdicts):
            return None  # inline already flags this turn
        gate = self._assertiveness.check(turn.transcript, turn.claims)
        if not gate.assertive:
            return None
        keep = self.engine.config.inline_judge_history_turns
        history = [{"role": r.role, "text": r.transcript} for r in self.results]
        window = history[-keep:] if keep else []
        turns = [*window, {"role": "agent", "text": turn.transcript}]
        start = time.perf_counter()
        try:
            judged = await asyncio.wait_for(
                asyncio.to_thread(self.shadow_judge.judge_turn,
                                  turns, len(turns) - 1),
                timeout=self.engine.config.inline_judge_timeout_s,
            )
        except Exception:  # noqa: BLE001 — fail-open: a judge outage degrades
            return None    # recall, never a call or its telemetry
        if not judged.get("violation"):
            return None
        return Verdict(
            verdict_id=new_verdict_id(),
            detector="JUDGE",
            fired=True,
            severity="high",
            latency_ms=(time.perf_counter() - start) * 1000.0,
            evidence=Evidence(
                claim_type=judged.get("category") or "judge_violation",
                spoken_value=turn.transcript,
                truth_value=None,
                source="shadow_judge",
                extra={"reason": judged.get("reason", ""),
                       "gate_reasons": gate.reasons,
                       **({"stage": judged["stage"]} if "stage" in judged else {}),
                       **({"votes": judged["votes"]} if "votes" in judged else {})},
            ),
        )

    async def _route(self, result: TurnResult) -> Action:
        """Mode selects routing ONLY — detection already happened."""
        if result.role != "agent":
            return Action(taken="none")
        threshold = self.engine.config.intervene_severity
        crossing = [
            v for v in result.fired_verdicts
            if severity_at_least(v.severity, threshold)
        ]
        if not crossing:
            return Action(taken="none")
        if self.mode == "shadow":
            return Action(taken="would_have")
        if self.intervention_handler is None:
            # Intervene mode with no hook wired: degrade to an alert.
            return Action(taken="alert")
        return await self.intervention_handler(result)


class FakeSession:
    """Stand-in for a LiveKit AgentSession: same event surface the observer
    uses, no runtime required. Tests drive it with ``add_item``."""

    def __init__(self, room_id: str = "room-fake-1") -> None:
        self.room_id = room_id
        self._handlers: dict[str, list[Callable]] = {}

    def on(self, event: str, handler: Callable | None = None):
        if handler is None:  # decorator form, like livekit's session.on
            def register(fn):
                self._handlers.setdefault(event, []).append(fn)
                return fn
            return register
        self._handlers.setdefault(event, []).append(handler)
        return handler

    def add_item(self, item: ConversationItem) -> None:
        for handler in self._handlers.get("conversation_item_added", []):
            handler(item)
