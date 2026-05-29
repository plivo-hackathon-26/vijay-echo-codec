"""FastAPI app — Pizza Plivo voice agent supervised by plivo_mirror.

Architecture (single process):

    Plivo call ──XML──► /voice/answer ──Stream bidi──► /voice/stream WS
                                                              │
                                                              ▼
       ┌───── audio (mulaw 8kHz, base64) ─────┐    ┌── raw ws adapter ──┐
       │                                       │    │                     │
       │              Deepgram nova-3 STT      │    │                     │
       │                       │               │    │  PlivoStreamTTSSink │
       │                       ▼               │    │  (clear/media/cp)   │
       │   on_final(text) ─► supervisor.review │    │                     │
       │                       │               │    │                     │
       │           ┌───────────┴───────────┐   │    │                     │
       │           ▼                       ▼   │    │                     │
       │    intervene (Mirror             speak │    │                     │
       │    speaks correction)            (agent│    │                     │
       │                                  text) ────►  ElevenLabs TTS ───┘
       │                                       │
       └───────────────────────────────────────┘

Every agent turn is reviewed by plivo_mirror before audio reaches the
customer. When Mirror's verdict crosses the threshold, intervention
fires via WebSocket-native ``clear_audio`` + ``send_media`` +
``send_checkpoint``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

# Find .env by walking up the directory tree — works whether uvicorn was
# launched from this directory, from v2/, or from the repo root.
load_dotenv(find_dotenv())

from plivo_mirror.plivo.raw_ws import RawPlivoWSAdapter  # noqa: E402

# Local-module imports so `uvicorn main:app` works from this directory.
from agent import PrimaryAgent  # noqa: E402
from mirror_config import supervisor  # noqa: E402
from stt import DeepgramSession  # noqa: E402
from tts import ElevenLabsTTS  # noqa: E402

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("pizza_plivo")


# ─── env / globals ────────────────────────────────────────────────────────

GREETING = "Hey, thanks for calling Pizza Plivo! What can I get started for you?"

PUBLIC_HOST = (
    os.getenv("PUBLIC_HOST", "")
    .strip()
    .removeprefix("https://")
    .removeprefix("http://")
    .rstrip("/")
)

# Build agent + TTS once per process — they're stateless across calls.
_agent = PrimaryAgent(
    api_key=os.environ["OPENAI_API_KEY"],
    model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    base_url=os.getenv("OPENAI_API_URL"),
)
_tts = ElevenLabsTTS(
    api_key=os.environ["ELEVENLABS_API_KEY"],
    voice_id=os.getenv("ELEVENLABS_VOICE_ID", ""),
)

# Pre-rendered greeting bytes — filled in during the FastAPI lifespan
# startup hook so the WebSocket handler can push it within ~50ms of
# the `start` event landing. Same voice as every subsequent agent
# reply — no Polly switch mid-call.
_GREETING_AUDIO: bytes = b""


from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _lifespan(_app):  # type: ignore[no-untyped-def]
    global _GREETING_AUDIO
    _GREETING_AUDIO = await _tts(GREETING)
    log.info(
        "greeting pre-rendered: %d bytes (~%.1fs of audio)",
        len(_GREETING_AUDIO),
        len(_GREETING_AUDIO) / 8000,
    )
    yield


app = FastAPI(
    title="pizza-plivo (plivo_mirror supervised)",
    lifespan=_lifespan,
)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "public_host": PUBLIC_HOST,
        "mirror_threshold": float(os.getenv("PLIVO_MIRROR_THRESHOLD", "0.7")),
    }


# ─── /voice/answer — XML the Plivo number's app points at ─────────────────

def _answer_xml(call_uuid: str) -> str:
    """Plivo XML — no <Speak> here; we push the ElevenLabs greeting
    over the WebSocket the moment Plivo's `start` event lands so the
    voice is consistent end-to-end."""
    qs = f"?call_uuid={call_uuid}" if call_uuid else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        '  <Stream bidirectional="true" streamTimeout="86400" keepCallAlive="true"'
        ' contentType="audio/x-mulaw;rate=8000">'
        f"wss://{PUBLIC_HOST}/voice/stream{qs}"
        "</Stream>\n"
        "</Response>"
    )


async def _answer(request: Request, method: str) -> Response:
    form: dict = {}
    if method == "POST":
        try:
            form = dict(await request.form())
        except Exception:
            log.warning("form parse failed")

    call_uuid = form.get("CallUUID", "")
    caller = form.get("From", "")
    to = form.get("To", "")
    log.info("%s /voice/answer call=%s from=%s to=%s", method, call_uuid, caller, to)
    return Response(content=_answer_xml(call_uuid), media_type="text/xml")


@app.post("/voice/answer")
async def voice_answer_post(request: Request) -> Response:
    return await _answer(request, "POST")


@app.get("/voice/answer")
async def voice_answer_get(request: Request) -> Response:
    return await _answer(request, "GET")


# ─── /voice/stream — the WebSocket that does the real work ────────────────


@app.websocket("/voice/stream")
async def voice_stream(ws: WebSocket) -> None:
    await ws.accept()
    call_uuid = ws.query_params.get("call_uuid", "")
    log.info("WS open call=%s", call_uuid[:8] if call_uuid else "unknown")

    adapter = RawPlivoWSAdapter(ws, content_type="audio/x-mulaw", sample_rate=8000)
    transcript: list[dict[str, str]] = [{"role": "agent", "text": GREETING}]
    agent_lock = asyncio.Lock()
    greeting_pushed = False  # flips True after the first `start` event

    async with supervisor.attach(handler=adapter, tts_provider=_tts) as sup:
        if call_uuid:
            sup.bind_call(call_uuid)
        sup.note_agent_turn(GREETING)

        async def on_customer_turn(text: str) -> None:
            text = (text or "").strip()
            if not text:
                return
            if agent_lock.locked():
                log.info("dropping transcript (agent busy): %s", text)
                return
            async with agent_lock:
                log.info("customer: %s", text)
                sup.note_customer_turn(text)

                # Natural pacing pause before the agent responds.
                await asyncio.sleep(0.3)

                # Pop Mirror's one-shot post-correction note for this turn.
                override = await sup.consume_override()

                # ── Agent loop runs through plivo_mirror's supervised
                #    tool-use helper. Irreversible tools (place_order)
                #    are gated by Mirror BEFORE they fire.
                try:
                    result = await _agent.run_supervised(
                        supervisor=sup,
                        customer_text=text,
                        system_note=override,
                    )
                except Exception:
                    log.exception("primary agent crashed")
                    return

                if result.blocked:
                    # Mirror's tool-gate blocked the tool call. The
                    # supervisor already spoke the correction; nothing
                    # more to do this turn.
                    v = result.block_verdict
                    log.warning(
                        "tool-gate blocked tools=%s score=%.2f reason=%r",
                        [tc.name for tc in result.tool_intents],
                        v.score if v else 0.0,
                        v.reason if v else "",
                    )
                    return

                # ── Speech-side review + parallel TTS + speak.
                outcome = await sup.review_and_speak(
                    customer_text=text,
                    primary_text=result.text,
                    tool_calls=result.tool_intents,
                )
                log.info(
                    "turn done — intervened=%s score=%.2f spoken=%r",
                    outcome.intervened,
                    outcome.verdict.score,
                    (outcome.spoken_text or "")[:80],
                )

        # ── Deepgram STT setup ─────────────────────────────────────────────
        dg_api_key = os.environ["DEEPGRAM_API_KEY"]
        dg = DeepgramSession(dg_api_key, on_final=on_customer_turn)
        try:
            await dg.start()
        except Exception:
            log.exception("deepgram failed to start; closing ws")
            try:
                await ws.close()
            except Exception:
                pass
            return

        # ── inbound frame loop ─────────────────────────────────────────────
        media_count = 0
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("non-json ws frame: %s", raw[:200])
                    continue

                event = data.get("event")
                if event == "start":
                    meta = data.get("start", {}) or {}
                    log.info("stream start: %s", meta)
                    stream_id = (
                        data.get("streamId")
                        or meta.get("streamId")
                        or meta.get("stream_id")
                    )
                    if stream_id:
                        adapter.set_stream_id(stream_id)
                    cu = (
                        meta.get("callId")
                        or meta.get("call_uuid")
                        or meta.get("callUuid")
                    )
                    if cu and not sup.call_uuid:
                        sup.bind_call(cu)

                    # Push the pre-rendered ElevenLabs greeting now —
                    # same voice as every agent reply, zero round-trip
                    # since the bytes were rendered at server startup.
                    if not greeting_pushed:
                        greeting_pushed = True
                        try:
                            await adapter.send_media(_GREETING_AUDIO)
                            log.info(
                                "greeting pushed (%d bytes)",
                                len(_GREETING_AUDIO),
                            )
                        except Exception:
                            log.exception("greeting push failed")

                elif event == "media":
                    payload = data.get("media", {}).get("payload", "")
                    if not payload:
                        continue
                    try:
                        audio = base64.b64decode(payload)
                    except Exception:
                        log.exception("base64 decode failed")
                        continue
                    media_count += 1
                    if media_count % 250 == 0:
                        log.info("media frames=%d (~%.1fs of audio)", media_count, media_count / 50)
                    await dg.send(audio)

                elif event == "playedStream":
                    # Resolve the asyncio.Event so the TTS sink's
                    # wait_checkpoint(...) returns.
                    await adapter.dispatch_inbound(data)

                elif event == "stop":
                    log.info("stream stop call=%s", sup.call_uuid[:8])
                    break

                else:
                    log.debug("unhandled ws event=%s", event)

        except WebSocketDisconnect:
            log.info("ws disconnect call=%s", sup.call_uuid[:8])
        except Exception:
            log.exception("ws loop error call=%s", sup.call_uuid[:8])
        finally:
            await dg.close()
            try:
                await ws.close()
            except Exception:
                pass
            log.info("WS closed call=%s", sup.call_uuid[:8] if sup.call_uuid else "unknown")
