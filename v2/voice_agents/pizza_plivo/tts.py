"""ElevenLabs TTS provider.

Converts text to mulaw 8kHz audio bytes — exactly what Plivo's
bidirectional Stream expects. ElevenLabs supports the ``ulaw_8000``
output format natively, so no resampling is needed.

Exposes a callable that matches plivo_mirror.voice.tts.ws_inject.TTSProvider:

    async def tts_provider(text: str) -> bytes

Usage:

    tts = ElevenLabsTTS(api_key=..., voice_id="...")
    audio_bytes = await tts(text)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("pizza_plivo.tts")


# A neutral default voice (Rachel) — customer overrides with voice_id
# from their own ElevenLabs library.
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
DEFAULT_MODEL_ID = "eleven_turbo_v2_5"


class ElevenLabsTTS:
    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str = DEFAULT_VOICE_ID,
        model_id: str = DEFAULT_MODEL_ID,
    ) -> None:
        if not api_key:
            raise ValueError("ELEVENLABS_API_KEY is required for ElevenLabsTTS")
        try:
            from elevenlabs.client import ElevenLabs  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "ElevenLabsTTS requires the `elevenlabs` package. "
                "Install with: pip install elevenlabs"
            ) from e
        self._client = ElevenLabs(api_key=api_key)
        self._voice_id = voice_id
        self._model_id = model_id

    async def __call__(self, text: str) -> bytes:
        """Synthesize text → mulaw 8kHz bytes."""
        if not text:
            return b""
        return await asyncio.to_thread(self._synthesise_blocking, text)

    def _synthesise_blocking(self, text: str) -> bytes:
        # ElevenLabs SDK v1+ — convert() returns an iterator of bytes.
        try:
            chunks = self._client.text_to_speech.convert(
                voice_id=self._voice_id,
                model_id=self._model_id,
                text=text,
                output_format="ulaw_8000",
            )
        except Exception:
            log.exception("ElevenLabs convert() failed")
            return b""
        return b"".join(chunks) if chunks else b""


__all__ = ["ElevenLabsTTS", "DEFAULT_VOICE_ID", "DEFAULT_MODEL_ID"]
