"""TTSSink protocol — abstracts the act of putting audio on the wire.

The orchestrator never knows whether audio reaches Plivo via
``send_media`` on a bidirectional WebSocket or via the REST ``speak()``
endpoint. It only knows the protocol below.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TTSSink(Protocol):
    async def clear_audio(self) -> None:
        """Flush any audio queued upstream of the customer's ear."""
        ...

    async def speak(self, text: str, *, checkpoint: str | None = None) -> None:
        """Queue ``text`` for TTS playback. If ``checkpoint`` is given,
        the sink should emit it on the wire after the audio finishes —
        so ``wait_checkpoint`` can resolve."""
        ...

    async def wait_checkpoint(self, name: str, *, timeout_s: float = 10.0) -> bool:
        """Block until the named checkpoint comes back. Returns True if
        it arrived in time, False on timeout."""
        ...

    # ─────────── parallel-TTS optimisation (optional) ────────────────────
    # Sinks that can pre-render audio bytes (e.g. WS-inject via an
    # external TTS provider) should implement these. The default
    # implementations return None / raise — CallSupervisor falls back
    # to sequential speak() automatically.

    async def precompute(self, text: str) -> bytes | None:
        """Pre-render TTS audio for ``text`` without sending it.
        Return None if the sink cannot pre-render (e.g. Plivo REST
        ``speak()`` does TTS inside Plivo). When this returns non-None,
        ``CallSupervisor.review_and_speak`` parallelises TTS with the
        Mirror scorer."""
        return None

    async def play_precomputed(
        self, audio: bytes, *, checkpoint: str | None = None
    ) -> None:
        """Send pre-rendered bytes from a successful ``precompute()``
        call. Only invoked when ``precompute`` returned non-None."""
        raise NotImplementedError("this sink does not support pre-rendered audio")


__all__ = ["TTSSink"]
