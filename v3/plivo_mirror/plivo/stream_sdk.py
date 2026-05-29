"""Plivo Stream SDK binding.

This is the natural integration path for a developer who already uses
``plivo-stream`` for their voice agent. Mirror attaches to their
handler, wires a TTS sink to it, and exposes a small ``CallSupervisor``
they call from their agent loop.

The primary v1 surface is::

    supervisor = Supervisor(config)

    @app.websocket("/stream")
    async def my_handler(ws):
        handler = PlivoFastAPIStreamingHandler(ws)
        async with supervisor.attach(handler, tts_provider=my_tts) as sup:
            @handler.on_start
            async def _(event):
                sup.bind_call(event.start.call_id)

            # Your agent loop. Whenever your agent produces a response,
            # let Mirror review it:
            verdict = await sup.review_turn(
                customer_text=user_text,
                primary_text=agent_text,
                tool_calls=tool_intents,
            )
            if verdict.should_intervene:
                await sup.intervene(verdict)
            else:
                await sup.speak(agent_text)

            await handler.start()

For users on the raw FastAPI WebSocket path, see
``plivo_mirror.plivo.raw_ws.RawPlivoWSAdapter`` which exposes the same
shape on top of a bare ``WebSocket``.
"""

from __future__ import annotations

from typing import Any

from plivo_mirror.voice.tts.ws_inject import PlivoStreamTTSSink, TTSProvider


def build_tts_sink(handler: Any, tts_provider: TTSProvider) -> PlivoStreamTTSSink:
    """Convenience wrapper: pair a plivo-stream handler with a customer
    TTS provider to produce a TTSSink ready for ``Supervisor.attach``.
    """
    return PlivoStreamTTSSink(handler, tts_provider)


__all__ = ["build_tts_sink", "PlivoStreamTTSSink", "TTSProvider"]
