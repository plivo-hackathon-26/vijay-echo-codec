"""Hook B — pre-TTS gated hold. PREVENTION: nothing wrong reaches TTS.

Where Hook A is containment (the wrong utterance was already spoken), Hook
B sits **between the LLM output and TTS** and decides per pending utterance:

1. **L2 deterministic diff** (µs) — a firing verdict at/above
   ``intervene_severity`` holds the utterance immediately; no model needed.
2. **Assertiveness gate** (µs, ``engine/gate.py``) — utterances that assert
   nothing (chitchat, questions, acks) release at ~0 ms added latency.
3. **Inline grounded judge** — assertive turns wait for the grounded
   entailment judge (facts + policies + recent turns → violation y/n).
   Hard timeout; **fail-open** (timeout/error → release) because a stuck
   judge must degrade to shadow-mode behaviour, never to a stuck call.

On a hold, ``CorrectionRetryLoop`` produces the corrected reply:
filler line first (host speaks it while we work), violation packet to the
MAIN voice LLM (host-supplied ``regenerate``), re-gate the candidate (plus
a pink-elephant echo check: a candidate restating the flagged wrong value
fails), cap retries, then a safe handoff line.

Latency contract (the reason gated hold is viable where v2's
judge-every-turn was not): non-assertive turns never wait on a model;
assertive turns pay one judge call; only actual holds pay regeneration.

``StubPreTTSGate`` (L2-only) remains for installs without a model.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol, runtime_checkable

from plivo_mirror_v5.engine import Engine, SessionState
from plivo_mirror_v5.engine.gate import AssertivenessGate
from plivo_mirror_v5.engine.verdict import (
    Evidence,
    TurnInput,
    Verdict,
    new_verdict_id,
    severity_at_least,
)


@dataclass
class GateDecision:
    """What the gate tells the TTS pipeline to do with the pending text."""

    release: bool                       # True → synthesize as-is
    replacement_text: str | None = None  # spoken instead when held
    verdicts: list[Verdict] = field(default_factory=list)
    held_by: str | None = None          # "L2" | "JUDGE" when held
    assertive: bool = False             # did the turn reach/qualify for the judge
    judge_latency_ms: float | None = None
    judge_error: str | None = None      # set when the judge failed open


@runtime_checkable
class PreTTSGate(Protocol):
    """Sits between the LLM output stream and TTS."""

    async def gate(
        self, text: str, claims: list[dict], state: SessionState
    ) -> GateDecision: ...


@runtime_checkable
class TurnJudge(Protocol):
    """Grounded turn judge — ``LLMPostCallJudge`` satisfies this. Sync by
    design (the gate runs it in a thread with a timeout)."""

    def judge_turn(self, turns: list[dict], agent_turn_index: int) -> dict: ...


HELD_FALLBACK = "Let me double-check that for you — one moment."
HANDOFF_FALLBACK = (
    "I want to be sure you get accurate information on this — "
    "let me connect you with a teammate who can confirm it."
)


class StubPreTTSGate:
    """L2-only reference gate: deterministic, no model, no network. The
    floor every install gets; ``JudgedPreTTSGate`` builds on top of it."""

    HOOK = "B"

    def __init__(self, engine: Engine, *, call_id: str = "pre-tts") -> None:
        self.engine = engine
        self.call_id = call_id
        self._counter = 0

    def _l2_verdicts(
        self, text: str, claims: list[dict], state: SessionState
    ) -> list[Verdict]:
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
        )
        return self.engine.l2.check(turn, state, ctx)

    async def gate(
        self, text: str, claims: list[dict], state: SessionState
    ) -> GateDecision:
        verdicts = self._l2_verdicts(text, claims, state)
        firing = [
            v for v in verdicts
            if v.fired and severity_at_least(v.severity, self.engine.config.intervene_severity)
        ]
        if firing:
            return GateDecision(release=False, replacement_text=HELD_FALLBACK,
                                verdicts=verdicts, held_by="L2")
        return GateDecision(release=True, verdicts=verdicts)


class JudgedPreTTSGate(StubPreTTSGate):
    """Gated hold: L2 (µs) → assertiveness gate (µs) → grounded judge.

    The judge call is sync (OpenAI client) — it runs via ``to_thread`` under
    ``wait_for`` so the event loop stays free and the timeout is hard. The
    judge needs conversation context; feed it with ``note_turn`` as turns
    commit (the LiveKit adapter / simulator does this).
    """

    def __init__(
        self,
        engine: Engine,
        judge: TurnJudge,
        *,
        call_id: str = "pre-tts",
        gate_check: AssertivenessGate | None = None,
    ) -> None:
        super().__init__(engine, call_id=call_id)
        self.judge = judge
        self.assertiveness = gate_check or AssertivenessGate()
        self._history: list[dict] = []   # [{role, text, tool_calls?}]

    # -- conversation context for the judge ---------------------------------

    def note_turn(self, role: str, text: str,
                  tool_calls: list[dict] | None = None) -> None:
        turn: dict = {"role": role, "text": text}
        if tool_calls:
            turn["tool_calls"] = tool_calls
        self._history.append(turn)

    def _judge_window(self, pending_text: str) -> tuple[list[dict], int]:
        keep = self.engine.config.inline_judge_history_turns
        window = self._history[-keep:] if keep else []
        turns = [*window, {"role": "agent", "text": pending_text}]
        return turns, len(turns) - 1

    # -- the gate ------------------------------------------------------------

    async def gate(
        self, text: str, claims: list[dict], state: SessionState
    ) -> GateDecision:
        # 1. Deterministic floor — a hard L2 hit never waits on a model.
        decision = await super().gate(text, claims, state)
        if not decision.release:
            return decision

        # 2. Non-assertive turns release at ~0 ms.
        gate_result = self.assertiveness.check(text, claims)
        if not gate_result.assertive:
            return decision
        decision.assertive = True

        # 3. Assertive → grounded judge, hard timeout, FAIL-OPEN.
        turns, idx = self._judge_window(text)
        start = time.perf_counter()
        try:
            judged = await asyncio.wait_for(
                asyncio.to_thread(self.judge.judge_turn, turns, idx),
                timeout=self.engine.config.inline_judge_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001  (asyncio.TimeoutError included)
            decision.judge_latency_ms = (time.perf_counter() - start) * 1000.0
            decision.judge_error = type(exc).__name__
            return decision  # fail-open: degrade to shadow, never block the call
        decision.judge_latency_ms = (time.perf_counter() - start) * 1000.0

        if not judged.get("violation"):
            return decision

        verdict = Verdict(
            verdict_id=new_verdict_id(),
            detector="JUDGE",
            fired=True,
            severity="high",
            latency_ms=decision.judge_latency_ms,
            evidence=Evidence(
                claim_type=judged.get("category") or "judge_violation",
                spoken_value=text,
                truth_value=None,
                source="inline_judge",
                extra={"reason": judged.get("reason", ""),
                       "gate_reasons": gate_result.reasons},
            ),
        )
        decision.release = False
        decision.replacement_text = HELD_FALLBACK
        decision.verdicts = [*decision.verdicts, verdict]
        decision.held_by = "JUDGE"
        return decision


# -- the correction loop -------------------------------------------------------

# Host-supplied: re-prompts the MAIN voice LLM with the correction packet as
# a system/developer message and returns the candidate reply text.
RegenerateFn = Callable[[str, int], Awaitable[str]]


@dataclass
class LoopOutcome:
    final_text: str
    released: bool                 # False → final_text is the handoff line
    filler_text: str | None        # speak this first when not None
    attempts: int                  # regeneration attempts consumed
    decisions: list[GateDecision]  # every gate decision, first → last


def build_violation_packet(verdicts: list[Verdict]) -> str:
    """The correction packet for the main LLM. States the violation and the
    verified truth; instructs — but the candidate is also re-gated, so a
    bad regeneration cannot slip through on instruction alone."""
    lines = []
    for v in verdicts:
        ev = v.evidence
        if ev is None or not v.fired:
            continue
        if v.detector == "JUDGE":
            lines.append(ev.extra.get("reason") or "The reply was not supported by the verified facts.")
        elif ev.claim_type == "action":
            lines.append(
                f"You claimed an action was done, but the system shows '{ev.source}'"
                f" is {ev.truth_value}. Tell the caller it has NOT completed and recover."
            )
        else:
            lines.append(
                f"The {ev.claim_type} you stated is wrong; the verified value from"
                f" {ev.source} is '{ev.truth_value}'. State the correct value."
            )
    return (
        "[CORRECTION: Your draft reply was held before the caller heard it. "
        + " ".join(lines)
        + " Produce a corrected reply now. Do NOT repeat the wrong value; do not"
          " mention this correction process.]"
    )


def _echoes_flagged(candidate: str, verdicts: list[Verdict]) -> bool:
    """Pink-elephant check: a candidate restating a flagged wrong value is a
    failed attempt even if the gate would pass it (e.g. ref drift)."""
    lowered = candidate.casefold()
    for v in verdicts:
        if not v.fired or v.evidence is None or v.detector == "JUDGE":
            continue
        spoken = (v.evidence.spoken_value or "").casefold().lstrip("$€£")
        if spoken and spoken in lowered:
            return True
    return False


class CorrectionRetryLoop:
    """Drives one pending utterance through gate → (filler + regenerate +
    re-gate)* → release | handoff. Transport-agnostic: the host yields
    ``filler_text`` to TTS first, then ``final_text``."""

    HOOK = "B"

    def __init__(
        self,
        gate: PreTTSGate,
        regenerate: RegenerateFn,
        *,
        claim_extractor=None,           # re-extract claims for candidates
        max_retries: int | None = None,
        filler: str = HELD_FALLBACK,
        handoff_line: str = HANDOFF_FALLBACK,
    ) -> None:
        self.gate = gate
        self.regenerate = regenerate
        self.claim_extractor = claim_extractor
        engine = getattr(gate, "engine", None)
        self.max_retries = (
            max_retries if max_retries is not None
            else (engine.config.inline_judge_max_retries if engine else 2)
        )
        self.filler = filler
        self.handoff_line = handoff_line

    def _claims_for(self, text: str) -> list[dict]:
        if self.claim_extractor is None:
            return []
        return self.claim_extractor.extract_from_text(text)

    async def run(
        self, text: str, claims: list[dict], state: SessionState
    ) -> LoopOutcome:
        decisions: list[GateDecision] = []
        decision = await self.gate.gate(text, claims, state)
        decisions.append(decision)
        if decision.release:
            return LoopOutcome(final_text=text, released=True,
                               filler_text=None, attempts=0, decisions=decisions)

        held = decision
        for attempt in range(1, self.max_retries + 1):
            packet = build_violation_packet(held.verdicts)
            candidate = await self.regenerate(packet, attempt)
            if _echoes_flagged(candidate, held.verdicts):
                continue  # pink elephant: candidate restates the wrong value
            redecision = await self.gate.gate(
                candidate, self._claims_for(candidate), state
            )
            decisions.append(redecision)
            if redecision.release:
                return LoopOutcome(final_text=candidate, released=True,
                                   filler_text=self.filler, attempts=attempt,
                                   decisions=decisions)
            held = redecision

        # Non-convergence: a safe handoff line, never the flagged reply.
        return LoopOutcome(final_text=self.handoff_line, released=False,
                           filler_text=self.filler, attempts=self.max_retries,
                           decisions=decisions)
