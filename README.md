# Plivo Mirror

A silent AI supervisor that watches voice agent phone calls in real-time, catches failures via pattern detection, and makes the primary agent self-correct mid-call — without the customer ever knowing.

## Hackathon Context

- **Event:** Plivo Hackathon 2026 (Fri 22 May 3PM → Sat 23 May 3PM)
- **Track:** for-agents
- **Tagline:** Post-mortem is for funerals. Mirror is the ambulance — voice agents that self-correct mid-call.

## Architecture

- Custom voice agent built on Plivo AudioStream (NOT Plivo Vibe / Voice Agent product)
- Plivo provides: phone number + bidirectional AudioStream WebSocket
- We build: STT (Deepgram), LLM agent (Claude Sonnet), TTS (Plivo Speak), Mirror supervisor

## Stack

- Python 3.11
- FastAPI + uvicorn (HTTP + WebSocket server)
- Plivo SDK (AudioStream + Speak XML)
- Deepgram SDK (real-time STT — added in Phase 1)
- OpenAI SDK (gpt-5-mini for primary agent and Mirror — added Phase 1/2)
- SQLite via stdlib (single source of truth)
- HTMX + Tailwind via CDN (dashboard — Phase 5)
- ngrok or cloudflared (tunnel for Plivo webhooks)

## Design Principles

- Flat code. No sub-agents, no skills framework, no over-abstraction.
- Two LLM call sites only: primary agent generation, Mirror correction generation.
- Mirror's pattern checks are pure Python — no LLM calls during healthy conversations.
- SQLite is the single source of truth for all metrics and state.
- Pre-cache audio for known demo paths. Sonnet is fallback for unknown failures.

## Directory Structure

mirror/
├── voice/         # Plivo AudioStream handler, STT, TTS streaming
├── agent/         # Primary agent loop + rigged prompt
├── mirror/        # Pattern checks + intervention pipeline
├── dashboard/     # FastAPI routes + HTML templates
├── db.py          # SQLite schema and helpers
├── main.py        # FastAPI app entrypoint
├── prompts.py     # All LLM prompts in one place
└── .env


## Phases (current: Phase 1)

0. **Setup** — phone call answers, says "Hello"  ✅
1. **Broken primary agent** — fails reliably on rigged input  ← CURRENT
2. Mirror pattern checks — silent observation, no intervention
3. Intervention pipeline — buffer + cached audio + correction
4. Sleep
5. Dashboard + fleet view + SSE updates
6. Webhook + hallucination scenario (#2)
7. Rehearsal + backup video
8. Buffer + final polish

When working in this repo, build only what is needed for the current phase. Do not build ahead. Each phase has a clear definition-of-done before moving forward.

## Credentials

Plivo creds + API keys are in 1Password → `hackathon-2026` vault.

## Demo Scenarios

Two deterministic failure scenarios for the demo:

1. **Contradiction:** Customer says "Large pepperoni, actually no mushroom only, no pepperoni" → primary agent confidently confirms both → Mirror catches the contradiction → agent self-corrects.

2. **Hallucination:** Customer asks "Can you check my last order?" → primary agent has no order lookup tool → starts inventing details → Mirror catches missing-tool pattern → graceful handoff to human.

Both scenarios use pre-cached buffer audio + pre-cached correction audio for reliability. Sonnet remains in the architecture for production unknown-pattern handling.

---

## Phase 0 — Setup & "Hello from Pizza Plivo"

A real call to the Plivo number reaches this FastAPI server through an ngrok tunnel and Plivo speaks `"Hello from Pizza Plivo"` before hanging up.

### 1. Create the venv and install dependencies

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Then edit `.env` and fill in:

- `PLIVO_AUTH_ID` — from Plivo Console → Account → Keys & Credentials
- `PLIVO_AUTH_TOKEN` — same page
- `PLIVO_PHONE_NUMBER` — the E.164 number you rented, e.g. `+12025551234`
- `PUBLIC_HOST` — leave blank for now; fill in after step 5

### 3. Start the server

```bash
uvicorn main:app --reload --port 8000
```

You should see uvicorn listening on `http://127.0.0.1:8000`.

### 4. Verify the endpoints locally

In another terminal:

```bash
curl http://localhost:8000/health
# {"status":"ok"}

curl -i -X POST http://localhost:8000/voice/answer
# HTTP/1.1 200 OK
# content-type: application/xml
# <?xml version="1.0" encoding="UTF-8"?>
# <Response>
#   <Speak voice="Polly.Matthew-Neural">Hello from Pizza Plivo</Speak>
#   <Hangup/>
# </Response>
```

### 5. Start ngrok and capture the public URL

```bash
ngrok http 8000
```

Copy the `https://` forwarding URL (e.g. `https://abc123.ngrok.io`). Put just the host (`abc123.ngrok.io`) into `PUBLIC_HOST` in `.env` if you want it recorded — Phase 0 doesn't read it, but later phases will.

### 6. Configure the Plivo number's Answer URL

Exact navigation in the Plivo Console:

1. Plivo Console → **Phone Numbers** (left sidebar) → **Your Numbers**
2. Click the number you rented
3. Under **Application Type**, select **XML Application**
4. Click **Create New Application** (or pick an existing one)
   - **Application Name:** `mirror-phase-0`
   - **Answer URL:** `https://<your-ngrok-host>/voice/answer`
   - **Answer Method:** `POST`
   - **Hangup URL:** leave blank
5. Save the application, then make sure it's assigned to the number under **Phone Numbers → Your Numbers → [your number] → Plivo Application**
6. Save the number

### 7. (Trial accounts only) Verify your personal phone number

Plivo trial accounts can only call **sandbox / verified** destinations.

- Plivo Console → **Phone Numbers** → **Sandbox Numbers** → **Add Number**
- Enter your personal phone in E.164 format
- Plivo will call you with a 6-digit code; enter it to verify
- Once verified, you can dial the Plivo number from that phone

### 8. Dial it

Call your Plivo number from the verified phone. You should hear "Hello from Pizza Plivo" and the call should hang up. The uvicorn terminal should show a `POST /voice/answer` log line with the `CallUUID`.

### Troubleshooting

- **Plivo says "application error" / call drops silently** — Answer URL must use **POST**, not GET. Re-check the application config.
- **ngrok URL works in browser but Plivo can't reach it** — make sure you copied the **https://** URL, not the http one. Plivo refuses non-TLS webhooks.
- **Phone rings but nothing speaks** — check the uvicorn logs. If no `POST /voice/answer` line appears, the Answer URL or application isn't attached to the number. If it appears but you hear silence, the XML is malformed — re-run the curl test in step 4.
- **Trial account: call doesn't connect** — your personal number isn't verified in the Plivo sandbox. See step 7.
- **ngrok URL changed after restart** — free ngrok rotates the subdomain each run. Update the Answer URL in the Plivo application every time you restart ngrok, or use a reserved domain.

### Phase 0 Definition of Done

1. `uvicorn main:app --reload --port 8000` starts cleanly.
2. `curl http://localhost:8000/health` → `{"status":"ok"}`.
3. `curl -X POST http://localhost:8000/voice/answer` → the XML above with `Content-Type: application/xml`.
4. Dialing the Plivo number plays "Hello from Pizza Plivo" and hangs up.

---

## Phase 1 — Broken Primary Agent

A real Plivo call now opens an AudioStream WebSocket. Caller audio (mulaw 8 kHz)
is streamed to Deepgram for STT; finalized utterances run through an OpenAI
`gpt-5-mini` tool-use loop with a **deliberately rigged** system prompt that
makes the agent confidently confirm contradictory orders. Agent responses are
injected back onto the live call via Plivo's `Call.speak` REST API.

### What was added

- `voice/stream.py` — AudioStream WebSocket handler at `/voice/stream`
- `voice/stt.py` — Deepgram async streaming session (mulaw / 8 kHz / nova-2)
- `voice/tts.py` — `speak_on_call(call_uuid, text)` via Plivo REST
- `agent/primary.py` — gpt-5-mini agent with `place_order` / `calculate_total` tools
- `prompts.py` — rigged system prompt + opening greeting
- `db.py` — SQLite schema (`calls`, `turns`, `orders`) + helpers
- `main.py` — `/voice/answer` now returns `<Speak>` + `<Stream>` XML and
  records the call before responding

### Architectural notes

- **TTS path:** Plivo REST `client.calls.speak()` injects agent speech onto
  the live call. Simpler than encoding mulaw frames and pushing them back
  through the bidirectional WebSocket. ~1-2s latency on speak start.
- **Self-transcription guard:** an `asyncio.Lock` is held during the agent's
  turn and for an estimated speak duration (≈ `len(text)/15` seconds) so the
  agent's own TTS audio — if it ever loops back via the bidirectional stream
  — won't be transcribed as a customer utterance.
- **Greeting:** spoken via `<Speak>` in the answer XML *before* `<Stream>`
  opens, then seeded into the agent's in-memory transcript history. ~0.5-1s
  natural pause between greeting end and stream open is acceptable.
- **Voice:** defaulted to `WOMAN` (generic Plivo voice) rather than
  `Polly.Matthew-Neural`. Phase 0 showed Polly neural voices produced no
  audio in this account; swap back once we know which Polly voices work.
- **DB writes** are sync `sqlite3`; fine for single-call testing. Will
  revisit if concurrent calls become a thing in later phases.

### 1. Install new dependencies

```bash
source venv/bin/activate
pip install -r requirements.txt
```

New: `deepgram-sdk`, `openai`. Old deps stay.

### 2. Add new env vars to `.env`

```
DEEPGRAM_API_KEY=...     # from https://console.deepgram.com → API Keys
OPENAI_API_KEY=sk-...    # from https://platform.openai.com → API Keys
```

Make sure your OpenAI account has billing enabled and that `gpt-5-mini` is
available to it — the free trial credit is not enough for sustained voice
calls.

### 3. Restart the server

Kill the old `uvicorn` (Ctrl+C in its terminal), then:

```bash
uvicorn main:app --reload --port 8000
```

You should see two new log lines on startup:

```
... mirror db initialized at mirror.db
... mirror.stream  (or similar — module loaded)
```

### 4. Sanity-check routes and schema

In another terminal:

```bash
curl http://localhost:8000/health
# {"status":"ok","phase":1,"public_host":"..."}

curl -i -X POST http://localhost:8000/voice/answer
# 200 OK with <Response><Speak>...</Speak><Stream ...>wss://.../voice/stream?call_uuid=</Stream></Response>

sqlite3 mirror.db ".schema"
# Should list calls, turns, orders tables
```

### 5. Verify the WebSocket route is registered

```bash
curl -sS http://localhost:8000/openapi.json | python -c "import sys,json; print('\n'.join(json.load(sys.stdin)['paths'].keys()))"
# Should print: /health, /voice/answer
```

(WebSocket routes don't appear in OpenAPI — confirm by trying to connect.
Easiest way is just to dial the number and watch the logs.)

### 6. Dialing test

Make sure `ngrok` is still running and `PUBLIC_HOST` in `.env` matches the
current ngrok forwarding host (no scheme, no trailing slash, e.g.
`powdery-conical-faceted.ngrok-free.dev`). If ngrok was restarted, update
`.env` and restart `uvicorn`.

Dial your Plivo number from your verified phone. Conversation script:

1. You should hear: **"Welcome to Pizza Plivo, what can I get for you?"**
2. After the greeting, say (in one breath):
   > "I'd like a large pepperoni, actually no, change that to mushroom only, no pepperoni."
3. Agent should confidently respond with something like:
   > "Great, one large pepperoni with mushroom, coming right up!"
   — the key is that **both** pepperoni and mushroom appear in the confirmation.
4. Say: **"That's all."**
5. Agent should state the total and say goodbye.
6. Hang up.

### 7. Verify each DoD step

```bash
# 1 row in calls with started_at and ended_at
sqlite3 mirror.db "SELECT call_uuid, caller, started_at, ended_at, status FROM calls ORDER BY started_at DESC LIMIT 3;"

# Alternating customer / agent turns
sqlite3 mirror.db "SELECT role, text FROM turns ORDER BY id DESC LIMIT 20;"

# THE BUG: most recent order should contain BOTH pepperoni AND mushroom
sqlite3 mirror.db "SELECT call_uuid, items_json, created_at FROM orders ORDER BY id DESC LIMIT 5;"
```

Phase 1 is done when:

- `calls.ended_at` is populated for the call you just made
- `turns` has alternating customer / agent rows
- `orders.items_json` for the latest call contains both `"pepperoni"` and
  `"mushroom"` (the rigged-failure bug)
- Reproducing the contradiction script gives the wrong order in ≥4/5 calls

### What you should hear vs. troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Greeting plays but then dead silence | WebSocket never connected. Check `uvicorn` logs for `ws disconnect` or no `stream start` line. `PUBLIC_HOST` mismatch is the usual cause. |
| Greeting plays, you talk, no response | Deepgram never emits `is_final`. Check logs for `dg final:` lines. If absent, likely codec mismatch (Deepgram needs `encoding=mulaw, sample_rate=8000`) or `DEEPGRAM_API_KEY` missing. |
| Agent transcribes your audio but stays silent | `OPENAI_API_KEY` missing/invalid, or `gpt-5-mini` not available on the account. Check logs for `agent error`. |
| Agent generates text but caller hears nothing | `speak_on_call` failed. Check logs for `speak_on_call failed` — usually a Plivo auth issue or wrong `call_uuid`. |
| Agent transcribes itself and double-replies | Self-loop. The `asyncio.Lock` + `len(text)/15s` heuristic should prevent this; if it triggers, the speak duration estimate is too short. Bump the divisor down. |
| Agent correctly handles the contradiction | The rigged prompt isn't biased enough — strengthen the "assume they want ALL items mentioned" rule. |

