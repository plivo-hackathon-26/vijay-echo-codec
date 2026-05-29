# Build your first Plivo voice agent + integrate plivo-mirror

A two-phase tutorial that takes you from "empty folder" to "voice agent
catching its own mistakes mid-call." Domain: **burger booking** (so
you're not just copying the pizza demo).

You'll do it in two passes:

- **Phase 1** — Build a working burger voice agent. No Mirror. Just
  STT → LLM → TTS over Plivo's bidirectional `<Stream>`. ~30 minutes.
- **Phase 2** — Drop plivo-mirror in. ~3 file changes, ~10 minutes.
  Same agent, now supervised — irreversible tool calls gated, policy
  violations corrected mid-call.

Pick an empty folder and follow along.

---

## Prerequisites (collect these first)

| What | Where to get it |
|---|---|
| Python 3.10+ | `python3 --version` — must be 3.10+ (3.9 will fail; `deepgram-sdk` and `plivo-mirror` both need `match` statements). On macOS, the default `python3` from Xcode CLI is usually 3.9. Use `brew install python@3.11` and create the venv with `/opt/homebrew/bin/python3.11 -m venv venv` to be safe. |
| Plivo account + a phone number | https://console.plivo.com — create an account, buy a voice-enabled number |
| Deepgram API key | https://console.deepgram.com — sign up, copy "API Keys" |
| OpenAI API key (or Azure) | https://platform.openai.com/api-keys |
| ElevenLabs API key | https://elevenlabs.io/app/settings/api-keys |
| ngrok | `brew install ngrok` (or download from ngrok.com) — needed to expose your local server to Plivo |

---

# PHASE 1 — Build the burger agent (no Mirror)

## Step 1.1 — Set up the project

```bash
mkdir burger-agent && cd burger-agent
# Use Python 3.11 explicitly — Apple's default python3 is 3.9 which
# is too old (deepgram-sdk + plivo-mirror need match statements).
/opt/homebrew/bin/python3.11 -m venv venv
source venv/bin/activate
python --version                  # should print Python 3.11.x
pip install fastapi 'uvicorn[standard]' python-dotenv \
            python-multipart \
            'deepgram-sdk>=3.7,<4' 'openai>=1.50' \
            elevenlabs plivo
```

> `python-multipart` is required because Plivo POSTs `/voice/answer` with form-encoded fields (`CallUUID`, `From`, `To`, etc.). Without it, FastAPI's `request.form()` raises an AssertionError on the first call.

## Step 1.2 — Project structure

Create these files (we'll fill them in below):

```
burger-agent/
├── main.py              FastAPI WS handler — orchestrates everything
├── agent.py             The LLM + tools
├── stt.py               Deepgram session wrapper
├── tts.py               ElevenLabs TTS provider
├── .env                 Your credentials (DON'T commit this)
└── .env.example         Template
```

## Step 1.3 — `.env.example`

```env
# Plivo
PLIVO_AUTH_ID=
PLIVO_AUTH_TOKEN=
PUBLIC_HOST=                 # your ngrok host, no https://

# Deepgram STT
DEEPGRAM_API_KEY=

# OpenAI (or Azure OpenAI)
OPENAI_API_KEY=
OPENAI_API_URL=              # set if Azure: https://<resource>.openai.azure.com/openai/v1
OPENAI_MODEL=gpt-4o-mini

# ElevenLabs TTS
ELEVENLABS_API_KEY=your-elevenlabs-api-key
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM

LOG_LEVEL=INFO
```

Then `cp .env.example .env` and fill in your real values.

## Step 1.4 — `agent.py` (the LLM with tools)

```python
"""Burger ordering voice agent — Phase 1 (no Mirror)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import AsyncOpenAI

log = logging.getLogger("burger.agent")


SYSTEM_PROMPT = """\
You are the voice agent for Burger Plivo, a burger ordering service.
Take orders over the phone in a warm, natural, professional way.

CONVERSATION STYLE:
- Speak like a real human at a burger joint.
- Keep responses SHORT — one sentence, two max.
- Let the customer finish before you respond; don't interrupt.
- Use natural acknowledgements: "got it", "absolutely", "sure thing".

YOUR JOB:
- Take the customer's burger order: the burger(s), size, and any sides.
- If the customer changes their mind, the LATEST preference wins.
- Read the order back before calling place_order.
- Then call calculate_total, tell them the total, and wrap up.

YOUR TOOLS:
- place_order(items: list of strings, sides: list of strings)
- calculate_total(items: list of strings, sides: list of strings)

You CANNOT process refunds, modify past orders, or accept payment info.
For any of those, transfer to a human supervisor — don't invent details.
"""


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "place_order",
            "description": "Submit the burger order to the kitchen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array", "items": {"type": "string"},
                        "description": "List of burger items, e.g. ['large cheeseburger']",
                    },
                    "sides": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Optional sides, e.g. ['fries']",
                    },
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_total",
            "description": "Calculate the total cost in USD.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "string"}},
                    "sides": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["items"],
            },
        },
    },
]


_PRICES = {"cheeseburger": 8.0, "double": 12.0, "veggie": 9.0, "chicken": 10.0}
_SIDE_PRICES = {"fries": 4.0, "rings": 5.0, "salad": 6.0}
_DEFAULT = 9.0
_LARGE_MOD = 2.0


def _price_item(item: str) -> float:
    item_l = item.lower()
    base = _DEFAULT
    for k, p in _PRICES.items():
        if k in item_l:
            base = p
            break
    if "large" in item_l:
        base += _LARGE_MOD
    return base


def _price_side(side: str) -> float:
    side_l = side.lower()
    for k, p in _SIDE_PRICES.items():
        if k in side_l:
            return p
    return 4.0


def _exec_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "place_order":
        items = args.get("items") or []
        sides = args.get("sides") or []
        log.info("place_order items=%s sides=%s", items, sides)
        return {"status": "placed", "order_id": "BURGER-DEMO"}
    if name == "calculate_total":
        items = args.get("items") or []
        sides = args.get("sides") or []
        total = round(
            sum(_price_item(i) for i in items)
            + sum(_price_side(s) for s in sides),
            2,
        )
        return {"total": total, "currency": "USD"}
    return {"error": f"unknown tool: {name}"}


class PrimaryAgent:
    def __init__(self, *, api_key: str, model: str, base_url: str | None = None):
        normalised = (base_url or "").strip().rstrip("/") or None
        if normalised and not normalised.startswith(("http://", "https://")):
            normalised = "https://" + normalised
        self._client = AsyncOpenAI(api_key=api_key, base_url=normalised)
        self._model = model

    async def run(self, transcript: list[dict[str, str]]) -> str:
        """Phase 1 version — executes tools inline, returns final text."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
        for t in transcript:
            role = "user" if t["role"] == "customer" else "assistant"
            messages.append({"role": role, "content": t["text"]})

        final_text = ""
        for _ in range(3):
            resp = await self._client.chat.completions.create(
                model=self._model, messages=messages, tools=TOOLS, tool_choice="auto",
            )
            msg = resp.choices[0].message
            if msg.tool_calls:
                messages.append({
                    "role": "assistant", "content": msg.content,
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name,
                                      "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ],
                })
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = _exec_tool(tc.function.name, args)
                    messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    })
                continue
            final_text = (msg.content or "").strip()
            break
        return final_text or "Sorry, could you say that again?"
```

## Step 1.5 — `stt.py` (Deepgram)

```python
"""Deepgram nova-3 streaming STT."""

from __future__ import annotations
import logging
from typing import Any, Awaitable, Callable

from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents

log = logging.getLogger("burger.stt")


class DeepgramSession:
    def __init__(self, api_key: str, on_final: Callable[[str], Awaitable[None]]):
        self._client = DeepgramClient(api_key)
        self._conn = self._client.listen.asyncwebsocket.v("1")
        self._on_final = on_final
        self._buffer: list[str] = []

        self._conn.on(LiveTranscriptionEvents.Transcript, self._on_t)
        self._conn.on(LiveTranscriptionEvents.UtteranceEnd, self._on_end)

    async def start(self):
        await self._conn.start(LiveOptions(
            model="nova-3", language="en-US",
            encoding="mulaw", sample_rate=8000,
            interim_results=True, smart_format=True, punctuate=True,
            numerals=False, endpointing=600, utterance_end_ms=2500,
            vad_events=True,
        ))

    async def send(self, audio: bytes):
        await self._conn.send(audio)

    async def close(self):
        try:
            await self._conn.finish()
        except Exception:
            pass

    async def _on_t(self, _self: Any, result: Any, **kw: Any):
        alts = getattr(result.channel, "alternatives", None) or []
        if not alts:
            return
        text = (alts[0].transcript or "").strip()
        if not text:
            return
        if bool(getattr(result, "speech_final", False)):
            self._buffer.append(text)
            full = " ".join(self._buffer).strip()
            self._buffer = []
            log.info("utterance: %s", full)
            await self._on_final(full)
        elif bool(getattr(result, "is_final", False)):
            self._buffer.append(text)

    async def _on_end(self, *_a: Any, **_kw: Any):
        if not self._buffer:
            return
        full = " ".join(self._buffer).strip()
        self._buffer = []
        await self._on_final(full)
```

## Step 1.6 — `tts.py` (ElevenLabs)

```python
"""ElevenLabs TTS — returns mulaw 8kHz bytes ready for Plivo."""

from __future__ import annotations
import asyncio
import logging

log = logging.getLogger("burger.tts")


class ElevenLabsTTS:
    def __init__(self, *, api_key: str, voice_id: str):
        from elevenlabs.client import ElevenLabs
        self._client = ElevenLabs(api_key=api_key)
        self._voice_id = voice_id

    async def __call__(self, text: str) -> bytes:
        if not text:
            return b""
        return await asyncio.to_thread(self._synth, text)

    def _synth(self, text: str) -> bytes:
        try:
            chunks = self._client.text_to_speech.convert(
                voice_id=self._voice_id,
                model_id="eleven_turbo_v2_5",
                text=text,
                output_format="ulaw_8000",
            )
        except Exception:
            log.exception("ElevenLabs failed")
            return b""
        return b"".join(chunks)
```

## Step 1.7 — `main.py` (Phase 1, NO Mirror)

```python
"""Burger voice agent — Phase 1. Direct agent → TTS, no Mirror."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

load_dotenv(find_dotenv())
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("burger")

from agent import PrimaryAgent
from stt import DeepgramSession
from tts import ElevenLabsTTS


GREETING = "Hey, welcome to Burger Plivo! What can I get started for you?"
PUBLIC_HOST = (os.getenv("PUBLIC_HOST", "").strip()
               .removeprefix("https://").removeprefix("http://").rstrip("/"))

_agent = PrimaryAgent(
    api_key=os.environ["OPENAI_API_KEY"],
    model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    base_url=os.getenv("OPENAI_API_URL"),
)
_tts = ElevenLabsTTS(
    api_key=os.environ["ELEVENLABS_API_KEY"],
    voice_id=os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
)

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok", "public_host": PUBLIC_HOST}


def _xml(call_uuid: str) -> str:
    qs = f"?call_uuid={call_uuid}" if call_uuid else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'
        f'  <Speak>{GREETING}</Speak>\n'                # Phase 1: Plivo Polly greeting
        '  <Stream bidirectional="true" streamTimeout="86400" keepCallAlive="true"'
        ' contentType="audio/x-mulaw;rate=8000">'
        f'wss://{PUBLIC_HOST}/voice/stream{qs}'
        '</Stream>\n'
        '</Response>'
    )


@app.post("/voice/answer")
async def voice_answer(request: Request):
    form = dict(await request.form())
    call_uuid = form.get("CallUUID", "")
    log.info("/voice/answer call=%s from=%s", call_uuid, form.get("From"))
    return Response(content=_xml(call_uuid), media_type="text/xml")


@app.websocket("/voice/stream")
async def voice_stream(ws: WebSocket):
    await ws.accept()
    call_uuid = ws.query_params.get("call_uuid", "")
    log.info("WS open call=%s", call_uuid[:8])

    stream_id: str | None = None
    transcript: list[dict[str, str]] = [{"role": "agent", "text": GREETING}]
    agent_lock = asyncio.Lock()

    async def send_audio(audio: bytes):
        if not audio or stream_id is None:
            return
        payload = {
            "event": "playAudio", "streamId": stream_id,
            "media": {
                "contentType": "audio/x-mulaw", "sampleRate": 8000,
                "payload": base64.b64encode(audio).decode("ascii"),
            },
        }
        await ws.send_text(json.dumps(payload))

    async def on_customer_turn(text: str):
        if not text or agent_lock.locked():
            return
        async with agent_lock:
            log.info("customer: %s", text)
            transcript.append({"role": "customer", "text": text})
            await asyncio.sleep(0.3)
            try:
                response = await _agent.run(transcript)
            except Exception:
                log.exception("agent crashed")
                return
            log.info("agent: %s", response)
            transcript.append({"role": "agent", "text": response})
            audio = await _tts(response)
            await send_audio(audio)

    dg = DeepgramSession(os.environ["DEEPGRAM_API_KEY"], on_customer_turn)
    try:
        await dg.start()
    except Exception:
        log.exception("dg start failed")
        await ws.close()
        return

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event = data.get("event")
            if event == "start":
                meta = data.get("start", {})
                stream_id = data.get("streamId") or meta.get("streamId")
                log.info("stream start: streamId=%s", stream_id)
            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                if payload:
                    try:
                        audio = base64.b64decode(payload)
                    except Exception:
                        continue
                    await dg.send(audio)
            elif event == "stop":
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws loop error")
    finally:
        await dg.close()
        try:
            await ws.close()
        except Exception:
            pass
```

## Step 1.8 — Run it

In one terminal:

```bash
source venv/bin/activate
uvicorn main:app --port 8000 --reload
```

In another terminal:

```bash
ngrok http 8000
```

Copy the ngrok URL — the host part WITHOUT `https://`. Update `PUBLIC_HOST` in `.env`. **Restart uvicorn** (env vars are read at startup).

In the **Plivo console** → Applications → create a new app:
- Answer URL: `https://<your-ngrok-host>/voice/answer`
- Method: `POST`
- Bind it to your Plivo number under Phone Numbers.

Call your Plivo number. You should hear the Polly greeting (different voice — we'll fix that in Phase 2) and then an ElevenLabs voice for everything after.

Try saying: *"I'd like a large cheeseburger with fries"* — the agent should read it back, then place the order.

### Test the failure mode that Mirror will catch (no Mirror yet)

Say: *"I'd like a double cheeseburger, **actually just a veggie burger**."*

Depending on which LLM model + temperature you're on, the agent may or may not get this right. If it places **both** items, that's the exact mistake plivo-mirror is designed to catch in Phase 2.

---

# PHASE 2 — Add plivo-mirror

## Step 2.1 — Install plivo-mirror

```bash
pip install "plivo-mirror[openai,plivo]"
```

Verify:

```bash
python -c "import plivo_mirror; print(plivo_mirror.__version__)"
# OK 0.1.0
```

## Step 2.2 — Create `policies.txt`

```
Never confirm a refund — always transfer the caller to a human supervisor instead.
Always read the customer's order back to them before calling place_order.
If the customer changes their mind, the LATEST stated preference wins. The retracted item is NOT part of the order.
Treat third-party preferences ('my wife wants X', 'my friend ordered Y') as context only — the caller's order is what THEY personally said they want.
Do not promise specific delivery times. Refer to the kitchen estimate if asked.
```

## Step 2.3 — Create `mirror_config.py`

```python
"""Wire the plivo_mirror Supervisor for the burger agent."""

from __future__ import annotations
import os

from dotenv import find_dotenv, load_dotenv
load_dotenv(find_dotenv())

from plivo_mirror import Supervisor, MirrorConfig
from plivo_mirror.llm.openai import OpenAIClient


POLICIES = [
    "Never confirm a refund — always transfer the caller to a human supervisor instead.",
    "Always read the customer's order back to them before calling place_order.",
    "If the customer changes their mind, the LATEST stated preference wins. The retracted item is NOT part of the order.",
    "Treat third-party preferences ('my wife wants X') as context only — the caller's order is what THEY personally said they want.",
    "Do not promise specific delivery times.",
]

supervisor: Supervisor = Supervisor(MirrorConfig(
    llm=OpenAIClient(
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        base_url=os.getenv("OPENAI_API_URL"),
    ),
    policies=POLICIES,
    intervention_threshold=0.7,
    cooldown_s=10,
    irreversible_tools=["place_order", "charge_card", "process_refund"],
))
```

## Step 2.4 — Refactor `agent.py` — let plivo-mirror drive the tool loop

Replace the bottom half of `agent.py` (the `PrimaryAgent.run()` method).
Keep the `SYSTEM_PROMPT`, `TOOLS`, and `_exec_tool` you already have —
just rename `TOOLS` → `TOOL_SPECS` and wrap `_exec_tool` into a dict:

```python
# In agent.py — replace the PrimaryAgent class and rename TOOLS → TOOL_SPECS.

TOOL_SPECS = TOOLS                              # rename (or just rename the list above)
TOOL_EXECUTORS = {
    "place_order":    lambda args: _exec_tool("place_order", args),
    "calculate_total": lambda args: _exec_tool("calculate_total", args),
}
IRREVERSIBLE_TOOLS = ("place_order",)


class PrimaryAgent:
    """Phase 2 — delegates the tool-use loop to plivo_mirror so the
    tool-gate fires BEFORE place_order executes."""

    def __init__(self, *, api_key: str, model: str, base_url: str | None = None):
        from openai import AsyncOpenAI
        normalised = (base_url or "").strip().rstrip("/") or None
        if normalised and not normalised.startswith(("http://", "https://")):
            normalised = "https://" + normalised
        self._client = AsyncOpenAI(api_key=api_key, base_url=normalised)
        self._model = model

    async def run_supervised(self, *, supervisor, customer_text: str,
                             system_note: str | None = None):
        return await supervisor.run_supervised_loop(
            llm_client=self._client,
            model=self._model,
            system_prompt=SYSTEM_PROMPT,
            tool_specs=TOOL_SPECS,
            tool_executors=TOOL_EXECUTORS,
            customer_text=customer_text,
            extra_system_note=system_note,
            irreversible=IRREVERSIBLE_TOOLS,
        )
```

## Step 2.5 — Refactor `main.py` — wire the Supervisor

Three changes:

**(a)** Add the imports + remove the old `<Speak>` greeting line + pre-render
the greeting via the new lifespan handler:

```python
# At the top of main.py — add these imports
from contextlib import asynccontextmanager
from plivo_mirror.plivo.raw_ws import RawPlivoWSAdapter
from mirror_config import supervisor

# Pre-rendered greeting bytes — filled by lifespan startup.
_GREETING_AUDIO: bytes = b""


@asynccontextmanager
async def _lifespan(app):
    global _GREETING_AUDIO
    _GREETING_AUDIO = await _tts(GREETING)
    log.info("greeting pre-rendered: %d bytes", len(_GREETING_AUDIO))
    yield


app = FastAPI(lifespan=_lifespan)
```

**(b)** Drop `<Speak>` from the XML so the greeting comes via ElevenLabs
on the WebSocket instead of Plivo Polly:

```python
def _xml(call_uuid: str) -> str:
    qs = f"?call_uuid={call_uuid}" if call_uuid else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'                                  # <— removed the <Speak> line
        '  <Stream bidirectional="true" streamTimeout="86400" keepCallAlive="true"'
        ' contentType="audio/x-mulaw;rate=8000">'
        f'wss://{PUBLIC_HOST}/voice/stream{qs}'
        '</Stream>\n'
        '</Response>'
    )
```

**(c)** Replace the entire `voice_stream` handler with the Supervisor-wired
version:

```python
@app.websocket("/voice/stream")
async def voice_stream(ws: WebSocket):
    await ws.accept()
    call_uuid = ws.query_params.get("call_uuid", "")
    log.info("WS open call=%s", call_uuid[:8])

    adapter = RawPlivoWSAdapter(ws, content_type="audio/x-mulaw", sample_rate=8000)
    agent_lock = asyncio.Lock()
    greeting_pushed = False

    async with supervisor.attach(handler=adapter, tts_provider=_tts) as sup:
        if call_uuid:
            sup.bind_call(call_uuid)
        sup.note_agent_turn(GREETING)

        async def on_customer_turn(text: str):
            text = (text or "").strip()
            if not text or agent_lock.locked():
                return
            async with agent_lock:
                log.info("customer: %s", text)
                sup.note_customer_turn(text)
                await asyncio.sleep(0.3)
                override = await sup.consume_override()

                # The whole agent loop runs through plivo_mirror.
                # Tools are gated by the supervisor BEFORE they fire.
                try:
                    result = await _agent.run_supervised(
                        supervisor=sup, customer_text=text, system_note=override,
                    )
                except Exception:
                    log.exception("agent crashed")
                    return

                if result.blocked:
                    log.warning("tool-gate blocked tools=%s reason=%r",
                                [tc.name for tc in result.tool_intents],
                                result.block_verdict.reason if result.block_verdict else "")
                    return

                # Mirror reviews the response in parallel with TTS encoding,
                # then either speaks it or speaks a corrective question.
                outcome = await sup.review_and_speak(
                    customer_text=text, primary_text=result.text,
                    tool_calls=result.tool_intents,
                )
                log.info("turn done — intervened=%s score=%.2f",
                         outcome.intervened, outcome.verdict.score)

        dg = DeepgramSession(os.environ["DEEPGRAM_API_KEY"], on_customer_turn)
        try:
            await dg.start()
        except Exception:
            log.exception("dg start failed")
            await ws.close()
            return

        try:
            while True:
                raw = await ws.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event = data.get("event")
                if event == "start":
                    meta = data.get("start", {}) or {}
                    sid = data.get("streamId") or meta.get("streamId")
                    if sid:
                        adapter.set_stream_id(sid)
                    if not greeting_pushed:
                        greeting_pushed = True
                        await adapter.send_media(_GREETING_AUDIO)
                        log.info("greeting pushed (%d bytes)", len(_GREETING_AUDIO))
                elif event == "media":
                    payload = data.get("media", {}).get("payload", "")
                    if payload:
                        try:
                            audio = base64.b64decode(payload)
                        except Exception:
                            continue
                        await dg.send(audio)
                elif event == "playedStream":
                    await adapter.dispatch_inbound(data)
                elif event == "stop":
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("ws loop error")
        finally:
            await dg.close()
            try:
                await ws.close()
            except Exception:
                pass
```

## Step 2.6 — Restart uvicorn and dial in

```bash
# Ctrl+C the running uvicorn, then
uvicorn main:app --port 8000 --reload
```

Look for these new startup lines:

```
INFO httpx HTTP Request: POST https://api.elevenlabs.io/.../convert "HTTP/1.1 200 OK"
INFO burger greeting pre-rendered: ~30000 bytes
INFO:     Application startup complete.
```

Call your Plivo number. The greeting should now be in the **ElevenLabs voice**
(not Polly), and you should see new log lines per turn:

```
INFO burger customer: I want a double cheeseburger, actually just veggie
INFO burger tool-gate verdict score=0.92 intervene=True tools=['place_order']
INFO plivo_mirror.intervention.orchestrator intervention complete (latency=...ms)
```

That's plivo-mirror catching the agent before it fires `place_order` with both items.

## Test cases that should trigger Mirror

| Say into the phone | Mirror should… |
|---|---|
| "Large cheeseburger, **actually just a veggie**." | Catch policy 3 — retracted item still in order. |
| "**My friend wants a chicken sandwich** but I'd like a veggie." | Catch policy 4 — third-party preference. |
| "I want a **refund** for my last order." | Catch policy 1 — refund must transfer to human. |

You should hear:
1. The agent starts to respond
2. Mirror cuts in: "Sorry, let me make sure I got that right — just a moment..."
3. Then: "Just to confirm — you'd like a veggie burger, no cheeseburger — is that right?"
4. You say "yes" — agent places only the veggie order.

## What to look for in the logs

Per turn:
- `customer: ...` — what STT heard
- `tool-gate verdict score=X intervene=Y` — Mirror's decision BEFORE tools fire
- `tool-gate blocked tools=[...]` — if blocked, tools never executed
- `intervention complete (latency=Xms)` — Mirror finished speaking the correction
- `turn done — intervened=True/False score=X` — final outcome

---

## You're done

You just built a Plivo voice agent and supervised it with plivo-mirror.
**The complete diff from Phase 1 → Phase 2 was:**

- Added 1 file (`mirror_config.py`, ~30 lines)
- Added 1 file (`policies.txt`, 5 lines)
- Replaced `agent.py:PrimaryAgent.run()` with `run_supervised()` (~15 lines)
- Updated `main.py`: removed `<Speak>` greeting, added lifespan pre-render, replaced inline agent call with `sup.run_supervised_loop()` + `sup.review_and_speak()` (~40 lines net)

No custom Mirror integration code. The library handles tool gating, policy
compilation, scorer LLM calls, intervention orchestration, parallel TTS,
cooldown, post-correction overrides — all of it.

## Next things to try

1. **Tune the threshold offline.** Record a few calls (or hand-write
   transcripts as JSON) and run:
   ```bash
   python -m plivo_mirror.replay calls.json --policies policies.txt --threshold-sweep 0.5,0.6,0.7,0.8,0.9
   ```
2. **Change the policies file** — try removing the "third-party preferences"
   policy and see Mirror stop catching that case. Then add it back.
3. **Add your own irreversible tool** — say `charge_card` — and add it to
   `mirror_config.py`'s `irreversible_tools` list. The tool-gate will
   automatically run on it without any other code change.
4. **Read `docs.html`** in the plivo-mirror v2 repo for the full API reference.
