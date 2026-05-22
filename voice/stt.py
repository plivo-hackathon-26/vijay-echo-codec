import logging

from deepgram import (
    DeepgramClient,
    LiveOptions,
    LiveTranscriptionEvents,
)

log = logging.getLogger("mirror.stt")


class DeepgramSession:
    """One streaming Deepgram session per call.

    Audio in: send raw mulaw 8 kHz bytes via `send()`.
    Transcripts out: `on_final(text)` is awaited whenever Deepgram
    marks an utterance as is_final.
    """

    def __init__(self, api_key: str, on_final):
        self._client = DeepgramClient(api_key)
        self._conn = self._client.listen.asyncwebsocket.v("1")
        self._on_final = on_final

        async def _on_open(_self, *args, **kwargs):
            log.info("dg open")

        async def _on_close(_self, *args, **kwargs):
            log.info("dg close")

        async def _on_transcript(_self, result, **kwargs):
            try:
                alts = getattr(result.channel, "alternatives", None) or []
                if not alts:
                    return
                text = (alts[0].transcript or "").strip()
                if not text:
                    return
                if getattr(result, "is_final", False):
                    log.info("dg final: %s", text)
                    await self._on_final(text)
                else:
                    log.info("dg interim: %s", text)
            except Exception:
                log.exception("transcript handler error")

        async def _on_error(_self, error, **kwargs):
            log.error("dg error: %s", error)

        self._conn.on(LiveTranscriptionEvents.Open, _on_open)
        self._conn.on(LiveTranscriptionEvents.Close, _on_close)
        self._conn.on(LiveTranscriptionEvents.Transcript, _on_transcript)
        self._conn.on(LiveTranscriptionEvents.Error, _on_error)

    async def start(self) -> None:
        options = LiveOptions(
            model="nova-2-phonecall",
            language="en-US",
            encoding="mulaw",
            sample_rate=8000,
            interim_results=True,
            smart_format=True,
            endpointing=1200,
            keywords=[
                "pepperoni:3",
                "mushroom:3",
                "cheese:3",
                "pizza:2",
                "large:2",
                "small:2",
            ],
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
