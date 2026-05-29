"""Raw Plivo WebSocket adapter.

Customers who haven't adopted the ``plivo-stream`` SDK (like the legacy
demo in this repo) still get the full Mirror surface — they wrap their
``fastapi.WebSocket`` with this adapter, which exposes the same
plivo-stream-shape methods (``send_media``, ``send_clear_audio``,
``send_checkpoint``, ``on_played_stream``) on top of the raw JSON wire
protocol Plivo uses.

This is intentionally a small shim. We're not reimplementing the SDK
— just enough surface so ``PlivoStreamTTSSink`` works against it
unchanged.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("plivo_mirror.plivo.raw_ws")


CheckpointHandler = Callable[[str], Awaitable[None]]


class RawPlivoWSAdapter:
    """Wrap a FastAPI WebSocket so it exposes plivo-stream-like methods.

    Wire protocol reference: Plivo Streams send/receive JSON frames with
    an ``event`` field — ``start``, ``media``, ``dtmf``, ``stop``,
    ``playedStream``, ``clearedAudio``. The server-to-Plivo events use
    ``event="playAudio"``, ``event="checkpoint"``, ``event="clearAudio"``.
    """

    def __init__(
        self,
        ws: Any,
        *,
        content_type: str = "audio/x-mulaw",
        sample_rate: int = 8000,
    ) -> None:
        self._ws = ws
        self._content_type = content_type
        self._sample_rate = sample_rate
        self._stream_id: str | None = None
        self._on_played: CheckpointHandler | None = None

    # ─────────────────────────── plivo-stream-shape API ──────────────────

    def set_stream_id(self, stream_id: str) -> None:
        """Customer's WS loop receives the start event and tells us the
        stream_id so subsequent server-to-Plivo events carry it."""
        self._stream_id = stream_id

    def on_played_stream(self, handler: CheckpointHandler) -> CheckpointHandler:
        """Decorator-style registration to match plivo-stream's API."""
        self._on_played = handler
        return handler

    async def send_media(self, audio: bytes) -> None:
        if not audio:
            return
        payload = {
            "event": "playAudio",
            "media": {
                "contentType": self._content_type,
                "sampleRate": self._sample_rate,
                "payload": base64.b64encode(audio).decode("ascii"),
            },
        }
        if self._stream_id:
            payload["streamId"] = self._stream_id
        await self._ws.send_text(json.dumps(payload))

    async def send_clear_audio(self) -> None:
        payload: dict[str, Any] = {"event": "clearAudio"}
        if self._stream_id:
            payload["streamId"] = self._stream_id
        await self._ws.send_text(json.dumps(payload))

    async def send_checkpoint(self, name: str) -> None:
        payload: dict[str, Any] = {"event": "checkpoint", "name": name}
        if self._stream_id:
            payload["streamId"] = self._stream_id
        await self._ws.send_text(json.dumps(payload))

    # ─────────────────────────── inbound dispatch ────────────────────────

    async def dispatch_inbound(self, frame: dict) -> None:
        """Customer's WS loop calls this for every inbound frame. We
        route ``playedStream`` events to the registered handler so
        ``PlivoStreamTTSSink.wait_checkpoint`` can resolve.

        Other events (start/media/dtmf/stop) are the customer's
        business — they handle them in their own loop.
        """
        event = frame.get("event")
        if event != "playedStream":
            return
        name = frame.get("name") or frame.get("playedStream", {}).get("name")
        if not name:
            log.debug("playedStream frame missing name: %s", frame)
            return
        if self._on_played is None:
            return
        try:
            await self._on_played(name)
        except Exception:
            log.exception("on_played_stream handler raised")


__all__ = ["RawPlivoWSAdapter"]
