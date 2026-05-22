import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response

import db
from prompts import GREETING
from voice.stream import handle_audio_stream
from dashboard.routes import router as dashboard_router
from dashboard.fixes_routes import router as fixes_router

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("mirror")

PUBLIC_HOST = (
    os.getenv("PUBLIC_HOST", "")
    .strip()
    .removeprefix("https://")
    .removeprefix("http://")
    .rstrip("/")
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("db initialized at %s", db.DB_PATH)
    if not PUBLIC_HOST:
        log.warning("PUBLIC_HOST is not set; <Stream> URL will be invalid")
    yield


app = FastAPI(title="Plivo Mirror — Phase 1", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "phase": 1, "public_host": PUBLIC_HOST}


def _answer_xml(call_uuid: str) -> str:
    qs = f"?call_uuid={call_uuid}" if call_uuid else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"  <Speak>{GREETING}</Speak>\n"
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
        except Exception as e:
            log.warning("form parse failed: %s", e)

    call_uuid = form.get("CallUUID", "")
    caller = form.get("From", "")
    to = form.get("To", "")

    log.info(
        "%s /voice/answer call=%s from=%s to=%s",
        method,
        call_uuid,
        caller,
        to,
    )

    if call_uuid:
        db.create_call(call_uuid, caller, to)
        db.add_turn(call_uuid, "agent", GREETING)

    return Response(content=_answer_xml(call_uuid), media_type="text/xml")


@app.post("/voice/answer")
async def voice_answer_post(request: Request):
    return await _answer(request, "POST")


@app.get("/voice/answer")
async def voice_answer_get(request: Request):
    return await _answer(request, "GET")


@app.websocket("/voice/stream")
async def voice_stream(ws: WebSocket):
    await handle_audio_stream(ws)


app.include_router(dashboard_router)
app.include_router(fixes_router)
