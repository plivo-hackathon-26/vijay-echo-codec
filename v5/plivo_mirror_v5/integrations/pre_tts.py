"""Pre-TTS gating for LiveKit — the flagged draft NEVER reaches the
speaker. This is the live wiring of Hook B (gated hold, measured 81.5%
prevention): the gate sits between ``llm_node`` and TTS, verifies the
draft (L2 µs floor → assertiveness gate → grounded judge), and only
approved text is voiced. The caller hears the filler + the corrected
reply — never the violation.

Integration: ``attach_mirror(..., agent=my_agent)`` in intervene mode sets
``agent._mirror_pre_tts`` to a :class:`PreTTSGateRunner`; the agent's
``llm_node`` override (2 lines, see examples/skyline_flight_agent) routes
its default stream through ``gate_stream``.

Honest latency contract: non-assertive turns pass at ~0 ms (chunks are
re-yielded untouched); assertive turns wait for one judge call (~1.3 s
measured); only actual holds pay regeneration. Tool-call streams pass
through untouched — actions are the L2 tool checks' jurisdiction, not the
speech gate's.

Duck-typed against livekit's ChatChunk/ChatContext; imports nothing from
livekit so the module stays test-runnable offline.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Awaitable, Callable

from plivo_mirror_v5.deployables.intervention.hook_b_pre_tts import (
    CorrectionRetryLoop,
    JudgedPreTTSGate,
)
from plivo_mirror_v5.engine.session_state import SessionState

logger = logging.getLogger("plivo_mirror_v5.pre_tts")

# default_node(chat_ctx) → the agent's unmodified LLM stream for that ctx.
DefaultNode = Callable[[object], AsyncIterator]

_ROLE_MAP = {"assistant": "agent", "user": "user"}


def _chunk_text(chunk) -> str:
    """Extract the text content from a livekit ChatChunk (or plain str)."""
    if isinstance(chunk, str):
        return chunk
    delta = getattr(chunk, "delta", None)
    return getattr(delta, "content", None) or ""


def _chunk_has_tool_calls(chunk) -> bool:
    delta = getattr(chunk, "delta", None)
    return bool(getattr(delta, "tool_calls", None))


def _history_from_ctx(chat_ctx, keep: int) -> list[dict]:
    """The judge's conversation context, rebuilt from the LLM chat context
    (best effort — an unreadable ctx degrades to judging without history)."""
    turns: list[dict] = []
    for item in getattr(chat_ctx, "items", None) or []:
        role = _ROLE_MAP.get(getattr(item, "role", None))
        text = getattr(item, "text_content", None)
        if text is None:
            content = getattr(item, "content", None)
            if isinstance(content, list):
                text = " ".join(c for c in content if isinstance(c, str))
            elif isinstance(content, str):
                text = content
        if role and text:
            turns.append({"role": role, "text": text})
    return turns[-keep:]


class PreTTSGateRunner:
    """Routes an agent's draft replies through the gated hold before TTS."""

    def __init__(
        self,
        gate: JudgedPreTTSGate,
        state: SessionState,
        claim_extractor,
    ) -> None:
        self.gate = gate
        self.state = state
        self.claim_extractor = claim_extractor

    async def gate_stream(self, chat_ctx, default_node: DefaultNode):
        """Buffer the draft, verify it, yield only what may be spoken."""
        chunks, has_tools = [], False
        async for chunk in default_node(chat_ctx):
            chunks.append(chunk)
            has_tools = has_tools or _chunk_has_tool_calls(chunk)

        if has_tools:
            # A tool-call stream is an ACTION, not speech — pass through;
            # the deterministic tool checks own that boundary.
            for chunk in chunks:
                yield chunk
            return

        draft = "".join(_chunk_text(c) for c in chunks)
        if not draft.strip():
            for chunk in chunks:
                yield chunk
            return

        keep = self.gate.engine.config.inline_judge_history_turns
        self.gate.set_history(_history_from_ctx(chat_ctx, keep))

        async def regenerate(packet: str, attempt: int) -> str:
            ctx2 = chat_ctx.copy()
            ctx2.add_message(role="system", content=packet)
            parts = []
            async for chunk in default_node(ctx2):
                parts.append(_chunk_text(chunk))
            return "".join(parts)

        loop = CorrectionRetryLoop(
            self.gate, regenerate,
            claim_extractor=self.claim_extractor,
        )
        try:
            claims = self.claim_extractor.extract_from_text(draft)
            outcome = await loop.run(draft, claims, self.state)
        except Exception:  # noqa: BLE001 — a broken gate must never mute the agent
            logger.exception("pre-TTS gate failed; releasing draft unchanged")
            for chunk in chunks:
                yield chunk
            return

        if outcome.released and outcome.attempts == 0:
            for chunk in chunks:  # clean pass: original chunks, zero added work
                yield chunk
            return

        # Held → the caller hears filler + ONLY the approved correction.
        decision = outcome.decisions[0]
        logger.info("pre-TTS gate HELD draft (%s): %r",
                    decision.held_by, draft[:120])
        if outcome.filler_text:
            yield outcome.filler_text
        yield outcome.final_text
