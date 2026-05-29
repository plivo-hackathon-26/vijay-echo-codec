"""PlivoStreamTTSSink — the primary v1 TTS path.

Uses the plivo-stream SDK's bidirectional WebSocket primitives:

  - ``send_clear_audio()``        — flush whatever was queued upstream
  - ``send_media(bytes)``         — queue audio chunks
  - ``send_checkpoint(name)``     — ask Plivo to emit a ``playedStream``
                                    event once the audio finishes
  - ``on_played_stream(name)``    — we register a handler that fires
                                    our internal asyncio.Event

This eliminates the legacy "sleep for char-count / 13.0 seconds" hack
because Plivo tells us exactly when audio finished playing.

Customer brings their own TTS — we don't ship one. Constructor takes a
``tts_provider: Callable[[str], Awaitable[bytes]]`` that converts text
into the audio bytes the Plivo stream expects (mulaw 8kHz by default).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("plivo_mirror.voice.tts.ws_inject")


TTSProvider = Callable[[str], Awaitable[bytes]]


# Sentinel exception identifiers for "the caller already hung up".
# Different WS layers raise different errors for the same condition;
# we treat them all as "skip this audio send, not an error".
_WS_CLOSED_FRAGMENTS = (
    "after sending 'websocket.close'",      # uvicorn / starlette
    "after sending websocket.disconnect",
    "WebSocket is not connected",
    "Cannot call \"send\" once a close",
    "Connection is closed",
)


def _is_ws_closed_error(exc: BaseException) -> bool:
    """Detect the family of 'WS already closed' errors we want to swallow
    instead of stack-tracing."""
    msg = str(exc) or ""
    return any(frag in msg for frag in _WS_CLOSED_FRAGMENTS)


class PlivoStreamTTSSink:
    """Bidirectional WS TTS sink for plivo-stream SDK handlers.

    Pass either a ``plivo_stream`` ``PlivoFastAPIStreamingHandler`` (or
    equivalent) AND a customer-supplied ``tts_provider`` callable.
    """

    def __init__(
        self,
        handler: Any,
        tts_provider: TTSProvider,
    ) -> None:
        self._handler = handler
        self._tts = tts_provider
        # checkpoint name → asyncio.Event that resolves when Plivo's
        # playedStream callback fires for that name.
        self._checkpoints: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        self._wire_handler_events()

    # ─────────────────────────── TTSSink API ─────────────────────────────

    async def clear_audio(self) -> None:
        fn = getattr(self._handler, "send_clear_audio", None)
        if fn is None:
            log.warning("handler has no send_clear_audio; skipping clear")
            return
        try:
            await _maybe_await(fn())
        except Exception as e:
            if _is_ws_closed_error(e):
                log.info("clear_audio skipped: caller hung up")
                return
            raise

    async def speak(self, text: str, *, checkpoint: str | None = None) -> None:
        if not text:
            return
        audio = await self._tts(text)
        if not audio:
            log.warning("tts_provider returned empty bytes for %r", text[:60])
            return

        send_media = getattr(self._handler, "send_media", None)
        if send_media is None:
            raise RuntimeError(
                "handler has no send_media — is this a plivo-stream SDK "
                "handler with bidirectional=true?"
            )

        # Prime the checkpoint Event BEFORE sending so we never race a
        # super-fast playback that finishes before we register.
        if checkpoint is not None:
            await self._prime_checkpoint(checkpoint)

        try:
            await _maybe_await(send_media(audio))
        except Exception as e:
            if _is_ws_closed_error(e):
                log.info("speak skipped: caller hung up before %r played", text[:60])
                return
            raise

        if checkpoint is not None:
            send_cp = getattr(self._handler, "send_checkpoint", None)
            if send_cp is None:
                log.warning(
                    "handler has no send_checkpoint; wait_checkpoint will time out"
                )
            else:
                try:
                    await _maybe_await(send_cp(name=checkpoint))
                except Exception as e:
                    if _is_ws_closed_error(e):
                        log.info("send_checkpoint skipped: caller hung up")
                        return
                    raise

    async def precompute(self, text: str) -> bytes | None:
        """Pre-render the TTS audio without sending. Lets the
        CallSupervisor parallelise TTS encode with the Mirror scorer."""
        if not text:
            return None
        audio = await self._tts(text)
        return audio or None

    async def play_precomputed(
        self, audio: bytes, *, checkpoint: str | None = None
    ) -> None:
        """Send bytes previously returned by ``precompute()``."""
        if not audio:
            return
        send_media = getattr(self._handler, "send_media", None)
        if send_media is None:
            raise RuntimeError(
                "handler has no send_media — is this a plivo-stream SDK "
                "handler with bidirectional=true?"
            )
        if checkpoint is not None:
            await self._prime_checkpoint(checkpoint)
        try:
            await _maybe_await(send_media(audio))
        except Exception as e:
            if _is_ws_closed_error(e):
                log.info("play_precomputed skipped: caller hung up")
                return
            raise
        if checkpoint is not None:
            send_cp = getattr(self._handler, "send_checkpoint", None)
            if send_cp is not None:
                try:
                    await _maybe_await(send_cp(name=checkpoint))
                except Exception as e:
                    if _is_ws_closed_error(e):
                        return
                    raise

    async def wait_checkpoint(
        self, name: str, *, timeout_s: float = 10.0
    ) -> bool:
        async with self._lock:
            event = self._checkpoints.get(name)
            if event is None:
                event = asyncio.Event()
                self._checkpoints[name] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            log.warning("wait_checkpoint(%s) timed out after %.1fs", name, timeout_s)
            return False
        finally:
            async with self._lock:
                self._checkpoints.pop(name, None)

    # ─────────────────────────── internals ───────────────────────────────

    async def _prime_checkpoint(self, name: str) -> None:
        async with self._lock:
            self._checkpoints.setdefault(name, asyncio.Event())

    def _wire_handler_events(self) -> None:
        """Hook ``on_played_stream`` so we resolve the asyncio.Event."""
        on_played = getattr(self._handler, "on_played_stream", None)
        if on_played is None:
            log.warning(
                "handler has no on_played_stream; wait_checkpoint will always time out. "
                "Are you on an old plivo-stream-sdk?"
            )
            return

        async def _on_played(event_or_name: Any, *args: Any) -> None:
            # plivo-stream calls with the event/name; tolerate both shapes
            name = _extract_checkpoint_name(event_or_name) or _extract_checkpoint_name(args)
            if not name:
                return
            async with self._lock:
                ev = self._checkpoints.get(name)
            if ev is not None:
                ev.set()
            else:
                log.debug("playedStream(%s) received but no waiter", name)

        try:
            on_played(_on_played)  # decorator-style registration
        except TypeError:
            # Some SDK versions expose .on_played_stream as a setter / handler
            try:
                setattr(self._handler, "on_played_stream", _on_played)
            except Exception:
                log.exception("could not register on_played_stream handler")


# ─────────────────────────── helpers ─────────────────────────────────────


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it's awaitable; otherwise return it as-is. SDKs
    expose sync and async variants of the same methods; tolerate both."""
    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        return await value
    return value


def _extract_checkpoint_name(obj: Any) -> str | None:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return obj.get("name") or obj.get("checkpoint")
    if hasattr(obj, "name"):
        try:
            return getattr(obj, "name") or None
        except Exception:
            return None
    if isinstance(obj, (list, tuple)) and obj:
        return _extract_checkpoint_name(obj[0])
    return None


__all__ = ["PlivoStreamTTSSink", "TTSProvider"]
