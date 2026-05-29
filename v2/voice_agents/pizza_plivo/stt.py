"""Deepgram streaming STT session.

One session per call. Audio in via send(); finalised utterances out via
the on_final callback (awaited when ``speech_final=True`` arrives).

Endpointing settings:
  - endpointing=600       — short pause → is_final fires (segment stable)
  - utterance_end_ms=2500 — 2.5s silence → speech_final fires (customer
                            actually done; agent can take its turn)

We buffer is_final segments and only flush to on_final when speech_final
lands. That lets a customer pause mid-sentence for up to ~2.5s without
the agent jumping in.

Customer-supplied keyterms (proper nouns / domain vocab) are optional —
pass them via the ``keyterms`` constructor arg. Generic speech works
fine without any.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from deepgram import (
    DeepgramClient,
    LiveOptions,
    LiveTranscriptionEvents,
)

log = logging.getLogger("pizza_plivo.stt")


OnFinal = Callable[[str], Awaitable[None]]
OnActivity = Callable[[], None]


class DeepgramSession:
    def __init__(
        self,
        api_key: str,
        on_final: OnFinal,
        *,
        on_activity: OnActivity | None = None,
        keyterms: list[str] | None = None,
        model: str = "nova-3",
    ) -> None:
        if not api_key:
            raise ValueError("DEEPGRAM_API_KEY is required")
        self._client = DeepgramClient(api_key)
        self._conn = self._client.listen.asyncwebsocket.v("1")
        self._on_final = on_final
        self._on_activity = on_activity
        self._buffer: list[str] = []
        self._keyterms = list(keyterms or [])
        self._model = model

        self._conn.on(LiveTranscriptionEvents.Open, self._h_open)
        self._conn.on(LiveTranscriptionEvents.Close, self._h_close)
        self._conn.on(LiveTranscriptionEvents.Transcript, self._h_transcript)
        self._conn.on(LiveTranscriptionEvents.UtteranceEnd, self._h_utterance_end)
        self._conn.on(LiveTranscriptionEvents.Error, self._h_error)

    # ─────────────────────────── public ──────────────────────────────────

    async def start(self) -> None:
        options = LiveOptions(
            model=self._model,
            language="en-US",
            encoding="mulaw",
            sample_rate=8000,
            interim_results=True,
            smart_format=True,
            punctuate=True,
            numerals=False,
            endpointing=600,
            utterance_end_ms=2500,
            vad_events=True,
            keyterm=self._keyterms,
        )
        ok = await self._conn.start(options)
        if ok is False:
            raise RuntimeError("Deepgram connection failed to start")

    async def send(self, audio: bytes) -> None:
        await self._conn.send(audio)

    async def close(self) -> None:
        try:
            await self._conn.finish()
        except Exception:
            log.exception("deepgram finish failed")

    # ─────────────────────────── handlers ────────────────────────────────

    async def _h_open(self, *_a: Any, **_kw: Any) -> None:
        log.info("dg open")

    async def _h_close(self, *_a: Any, **_kw: Any) -> None:
        log.info("dg close")

    async def _h_transcript(self, _self: Any, result: Any, **_kw: Any) -> None:
        try:
            alts = getattr(result.channel, "alternatives", None) or []
            if not alts:
                return
            text = (alts[0].transcript or "").strip()
            if not text:
                return

            is_final = bool(getattr(result, "is_final", False))
            speech_final = bool(getattr(result, "speech_final", False))

            if self._on_activity is not None:
                try:
                    self._on_activity()
                except Exception:
                    log.exception("on_activity raised")

            if speech_final:
                self._buffer.append(text)
                full = " ".join(self._buffer).strip()
                self._buffer = []
                log.info("utterance: %s", full)
                await self._on_final(full)
            elif is_final:
                self._buffer.append(text)
                log.debug("segment buffered: %s", text)
            else:
                log.debug("interim: %s", text)
        except Exception:
            log.exception("transcript handler error")

    async def _h_utterance_end(self, *_a: Any, **_kw: Any) -> None:
        # Safety net: if Deepgram signals UtteranceEnd but we never saw
        # speech_final, flush whatever's buffered so the agent isn't
        # left hanging.
        if not self._buffer:
            return
        full = " ".join(self._buffer).strip()
        self._buffer = []
        log.info("utterance_end fallback: %s", full)
        await self._on_final(full)

    async def _h_error(self, _self: Any, error: Any, **_kw: Any) -> None:
        log.error("dg error: %s", error)


__all__ = ["DeepgramSession"]
