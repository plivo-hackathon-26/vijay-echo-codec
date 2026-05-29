"""PlivoRESTTTSSink — REST-based fallback TTS sink.

Used when the customer is on a non-bidirectional Plivo stream and
cannot send audio bytes back via WebSocket. Calls Plivo's call-control
``calls.speak()`` REST endpoint, which has built-in TTS.

Trade-off vs the WS-inject sink:
  - No checkpoint event — REST returns when audio is QUEUED, not when
    it finishes playing. We fall back to an estimated playback
    duration based on a configurable chars-per-second rate. This is
    the legacy approach the demo code used; preserved here so customers
    not yet on bidirectional streams aren't blocked.
  - No clear_audio — REST has no "interrupt queued audio" primitive.
    ``clear_audio()`` is a no-op; the buffer line will play to
    completion before the correction speaks.

Recommend customers move to bidirectional streams + the WS-inject sink
for proper intervention timing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("plivo_mirror.voice.tts.plivo_speak")


class PlivoRESTTTSSink:
    def __init__(
        self,
        *,
        call_uuid: str,
        auth_id: str,
        auth_token: str,
        voice: str = "WOMAN",
        language: str = "en-US",
        chars_per_second: float = 13.0,
    ) -> None:
        try:
            from plivo import RestClient
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "PlivoRESTTTSSink requires the `plivo` package. "
                "Install with: pip install plivo-mirror[plivo]"
            ) from e

        self._client = RestClient(auth_id, auth_token)
        self._call_uuid = call_uuid
        self._voice = voice
        self._language = language
        self._cps = max(1.0, chars_per_second)
        # Estimated time at which the most recently queued audio will
        # finish playing. We use this in ``wait_checkpoint`` as a proxy
        # for the missing playedStream event.
        self._last_queue_finish_monotonic: float = 0.0
        self._lock = asyncio.Lock()

    async def clear_audio(self) -> None:
        # REST has no clear-queued-audio primitive. Best we can do:
        # log a warning so it's discoverable.
        log.warning(
            "clear_audio() called on PlivoRESTTTSSink — REST cannot flush "
            "queued audio. Use the WS-inject sink for true interruption."
        )

    async def speak(self, text: str, *, checkpoint: str | None = None) -> None:
        if not text:
            return
        async with self._lock:
            await asyncio.to_thread(self._speak_blocking, text)
            # Compute estimated finish time so wait_checkpoint can fake it.
            from time import monotonic
            est_duration = max(2.0, len(text) / self._cps) + 0.5
            self._last_queue_finish_monotonic = monotonic() + est_duration

    async def wait_checkpoint(
        self, name: str, *, timeout_s: float = 10.0
    ) -> bool:
        from time import monotonic
        remaining = max(0.0, self._last_queue_finish_monotonic - monotonic())
        # Cap at the caller's timeout so a runaway estimate doesn't hang
        # the call.
        sleep_for = min(remaining, timeout_s)
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
        return True

    # ─────────────────────────── internals ───────────────────────────────

    def _speak_blocking(self, text: str) -> Any:
        return self._client.calls.speak(
            call_uuid=self._call_uuid,
            text=text,
            voice=self._voice,
            language=self._language,
        )


__all__ = ["PlivoRESTTTSSink"]
