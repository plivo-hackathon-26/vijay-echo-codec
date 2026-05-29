"""Intervention orchestrator.

Sequences the WebSocket-native mid-call correction:

  1. ``clear_audio`` — flush whatever upstream TTS has queued
  2. ``speak(buffer_text, checkpoint="mirror_buffer")``
  3. in parallel: generate correction text via the LLM
  4. ``wait_checkpoint("mirror_buffer")`` — Plivo tells us buffer audio
     finished playing exactly (no sleep heuristics)
  5. ``speak(correction_text, checkpoint="mirror_done")``
  6. set cooldown so an immediate confirmation turn doesn't re-trigger
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import HistoryTurn, SupervisorContext, Verdict
from plivo_mirror.intervention.generator import CorrectionGenerator
from plivo_mirror.intervention.templates import (
    DEFAULT_BUFFER,
    DEFAULT_TOOL_BLOCK_CORRECTION,
    fallback_correction,
)
from plivo_mirror.state.base import StateStore
from plivo_mirror.voice.tts.base import TTSSink

log = logging.getLogger("plivo_mirror.intervention.orchestrator")


@dataclass
class InterventionResult:
    correction_text: str
    latency_ms: int
    used_fallback: bool


class InterventionOrchestrator:
    """Runs the buffer → correction sequence on the TTSSink."""

    def __init__(
        self,
        config: MirrorConfig,
        generator: CorrectionGenerator,
        state: StateStore,
    ) -> None:
        self._config = config
        self._gen = generator
        self._state = state

    async def handle(
        self,
        verdict: Verdict,
        history: list[HistoryTurn],
        tts: TTSSink,
        ctx: SupervisorContext,
    ) -> InterventionResult:
        """Execute the intervention. Never raises; falls back on any
        component failure so the call never goes silent."""
        started = time.monotonic()
        buffer_text = self._config.buffer_text or DEFAULT_BUFFER
        used_fallback = False

        # Step 1+2: clear queued audio, speak the buffer line with checkpoint.
        try:
            await tts.clear_audio()
        except Exception:
            log.exception("clear_audio failed (continuing)")
        try:
            await tts.speak(buffer_text, checkpoint="mirror_buffer")
        except Exception:
            log.exception("buffer speak failed (continuing)")

        # Step 3: generate correction in parallel while buffer plays.
        gen_task = asyncio.create_task(self._gen.generate(verdict, history, ctx))

        # Step 4: wait for the buffer audio to actually finish on the
        # wire. Bounded to avoid hanging forever if the checkpoint event
        # never arrives.
        try:
            await tts.wait_checkpoint("mirror_buffer", timeout_s=6.0)
        except Exception:
            log.exception("wait_checkpoint failed (continuing)")

        # Resolve correction text.
        try:
            correction_text = await gen_task
            if not correction_text:
                correction_text = self._default_correction(verdict)
                used_fallback = True
        except Exception:
            log.exception("correction generator crashed — using fallback")
            correction_text = self._default_correction(verdict)
            used_fallback = True

        # Step 5: speak the correction.
        try:
            await tts.speak(correction_text, checkpoint="mirror_done")
            await tts.wait_checkpoint("mirror_done", timeout_s=20.0)
        except Exception:
            log.exception("correction speak failed")

        # Step 6: cooldown so the customer's immediate confirmation
        # doesn't re-trigger another intervention.
        deadline = time.monotonic() + self._config.cooldown_s
        try:
            await self._state.set_cooldown(ctx.call_uuid, deadline)
        except Exception:
            log.exception("set_cooldown failed (continuing)")

        # Step 7: install a one-shot post-correction override for the
        # next primary turn. This is the seam the user's primary agent
        # can read via ``Supervisor.consume_override(call_uuid)``.
        override = self._build_override_note(verdict, correction_text)
        try:
            await self._state.set_override(ctx.call_uuid, override)
        except Exception:
            log.exception("set_override failed (continuing)")

        latency_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "intervention complete (call=%s latency=%dms fallback=%s)",
            ctx.call_uuid[:8],
            latency_ms,
            used_fallback,
        )
        return InterventionResult(
            correction_text=correction_text,
            latency_ms=latency_ms,
            used_fallback=used_fallback,
        )

    # ─────────────────────────── internals ───────────────────────────────

    def _default_correction(self, verdict: Verdict) -> str:
        if verdict.blocked_tool:
            return DEFAULT_TOOL_BLOCK_CORRECTION
        return fallback_correction(verdict.evidence)

    def _build_override_note(self, verdict: Verdict, correction: str) -> str:
        intent = (verdict.evidence or {}).get("customer_intent", "")
        violation = (verdict.evidence or {}).get("violation_summary", "")
        return (
            "MIRROR CORRECTION CONTEXT (applies to the NEXT turn only):\n"
            f"You just spoke this confirmation question: \"{correction}\"\n"
            f"Customer's actual intent (Mirror's reading): {intent or '(unknown)'}\n"
            f"What had gone wrong: {violation or '(unspecified)'}\n\n"
            "Decision rules for THIS turn:\n"
            "1. If the customer's next message confirms (\"yes\", \"that's right\", "
            "\"correct\", \"please\"): proceed with their actual intent above. "
            "Do NOT re-extract items / destinations / values from the earlier "
            "contradictory turn.\n"
            "2. If the customer denies or asks a follow-up: ask one short "
            "clarifying question. Do not commit to anything yet.\n"
            "This override applies ONCE and is then discarded."
        )


__all__ = ["InterventionOrchestrator", "InterventionResult"]
