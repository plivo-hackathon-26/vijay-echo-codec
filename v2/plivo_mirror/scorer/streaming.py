"""StreamingScorer — scores the agent's response at the first sentence
boundary mid-stream, not after the full response.

Modern voice stacks (OpenAI Realtime, Gemini Live, Deepgram Voice Agent)
stream LLM tokens to TTS the moment they arrive. By the time you have
the "full response" to score, the customer is already hearing the first
half of it. This scorer fires the moment we have a stable first
sentence — usually 30-80 tokens in, well before TTS has caught up.

The consumer feeds incremental deltas; the scorer returns ``None`` while
it's still buffering, then a single ``Verdict`` once the boundary lands.
After yielding the verdict the scorer is "spent" for this turn — the
consumer should construct a fresh one for the next turn.

If no boundary lands before the stream ends, ``flush()`` runs the
scorer on whatever was accumulated. Guarantees one verdict per turn.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace

from plivo_mirror.context import SupervisorContext, TurnPayload, Verdict
from plivo_mirror.scorer.llm import LLMScorer

log = logging.getLogger("plivo_mirror.scorer.streaming")


_BOUNDARY_RE = re.compile(r"[.!?](\s|$)|;\s")
# Don't fire on tiny first sentences ("Sure.", "Got it.") — they're never
# the moment something goes wrong. Require some signal.
_MIN_BOUNDARY_CHARS = 25


class StreamingScorer:
    """Wraps an LLMScorer with stream-aware buffering."""

    def __init__(self, inner: LLMScorer) -> None:
        self._inner = inner
        self._buffer = ""
        self._spent = False

    async def feed(
        self,
        delta: str,
        turn: TurnPayload,
        ctx: SupervisorContext,
    ) -> Verdict | None:
        """Append ``delta`` to the buffer. If we just crossed a sentence
        boundary, score now and return a Verdict; otherwise return None.

        Returns None once the scorer has already produced a verdict for
        this turn (no double-firing)."""
        if self._spent:
            return None

        self._buffer += delta
        if not self._has_boundary(self._buffer):
            return None

        return await self._fire(turn, ctx)

    async def flush(
        self, turn: TurnPayload, ctx: SupervisorContext
    ) -> Verdict | None:
        """Force a scoring pass on whatever's accumulated. Called at the
        end of the stream when no boundary was ever hit. Returns None
        if a verdict was already produced earlier in the stream."""
        if self._spent:
            return None
        return await self._fire(turn, ctx)

    # ─────────────────────────── internals ───────────────────────────────

    def _has_boundary(self, text: str) -> bool:
        if len(text) < _MIN_BOUNDARY_CHARS:
            return False
        return bool(_BOUNDARY_RE.search(text))

    async def _fire(
        self, turn: TurnPayload, ctx: SupervisorContext
    ) -> Verdict:
        self._spent = True
        log.debug(
            "streaming-scorer firing at %d chars (call=%s)",
            len(self._buffer),
            ctx.call_uuid[:8],
        )
        # Build a turn snapshot whose primary_text is the partial stream.
        partial = replace(
            turn,
            primary_text=self._buffer.strip(),
            is_partial=True,
            is_first_sentence_boundary=True,
        )
        return await self._inner.score(partial, ctx)


__all__ = ["StreamingScorer"]
