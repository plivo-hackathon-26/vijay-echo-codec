# pizza-plivo — example voice agent supervised by `plivo-mirror`

A clean, production-shape Plivo voice agent that takes pizza orders.
Built fresh from the Plivo docs pattern (Deepgram STT → OpenAI LLM →
ElevenLabs TTS, bidirectional `<Stream>` WebSocket) and supervised by
`plivo-mirror`.

This is the demo the new library is designed for. Mirror watches every
agent turn, scores it against five plain-English policies, and
intervenes mid-call when the agent is about to make a mistake.

## What's in here

```
pizza_plivo/
├── main.py             FastAPI app — /voice/answer (XML) + /voice/stream (WS)
├── agent.py            Primary LLM agent (place_order, calculate_total)
├── stt.py              Deepgram nova-3 streaming session
├── tts.py              ElevenLabs TTS provider (mulaw 8kHz output)
├── mirror_config.py    Builds the plivo_mirror.Supervisor singleton
├── policies.txt        Plain-English policies Mirror enforces
├── requirements.txt    Runtime deps (FastAPI, Deepgram, OpenAI, ElevenLabs, Plivo)
└── .env.example        Required env vars
```

The agent prompt is **sensible** — no rigged "capture every item" trick.
Mirror catches whatever real mistakes the LLM makes naturally on
ambiguous customer turns.

## Run it

From the repo root:

```bash
cd v2
source ../venv/bin/activate

# Install plivo_mirror itself (editable from v2/)
pip install -e ".[openai,plivo,dev]"

# Install this voice agent's deps
cd voice_agents/pizza_plivo
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# edit .env with your Plivo / Deepgram / OpenAI / ElevenLabs credentials
# (PUBLIC_HOST is your ngrok hostname, without https://)

# Start the server
uvicorn main:app --port 8000 --reload
```

In another shell:

```bash
ngrok http 8000
# copy the host (e.g. abc123.ngrok.app), update PUBLIC_HOST in .env, restart uvicorn
```

In your Plivo console, point your number's Voice App at
`https://<ngrok-host>/voice/answer`.

Call the number and say:

> "Hi, I'd like a large pepperoni pizza. Actually, just mushroom only."

Expected: Mirror catches the agent if it tries to include pepperoni in
the order, plays a confirmation question, and only places the correct
order after the customer confirms.

## Tuning

- **Policies** — edit `policies.txt` (or `mirror_config.py`'s `POLICIES`
  list) and restart. No code changes elsewhere needed.
- **Intervention threshold** — `PLIVO_MIRROR_THRESHOLD` env var. Higher
  = fewer interventions, lower = more. Default `0.7`.
- **Replay** — record some calls, dump them as transcript JSON, run
  `python -m plivo_mirror.replay transcript.json --policies policies.txt
  --threshold-sweep 0.5,0.7,0.9` to pick the right threshold before
  going live.

## Architecture in one diagram

```
                   ┌────────────── Plivo call ───────────────┐
                   │                                          │
            ┌──────▼─── /voice/answer (XML) ──────────┐       │
            │                                          │       │
            │  <Stream bidirectional="true" ...>       │       │
            │       wss://<host>/voice/stream          │       │
            └──────────────────┬───────────────────────┘       │
                               │                                │
                          ┌────▼────────────┐                  │
                          │  /voice/stream  │ ◄────── audio ───┘
                          │  (FastAPI WS)   │
                          └────┬──────┬─────┘
                               │      │
                  ┌────────────┘      └──────────────┐
                  ▼                                   ▼
            ┌──────────┐                       ┌─────────────┐
            │ Deepgram │                       │  RawWSAdapter│
            │   STT    │                       │   ┌─────────┴───┐
            │ on_final │──text─►┌──────────────▼─► │ Supervisor  │
            └──────────┘        │ Primary Agent  │ │  ─────────  │
                                │ (OpenAI LLM)   │ │ pre_gate    │
                                │ + tool calls   │ │ LLM scorer  │
                                └──────┬─────────┘ │ tool_gate   │
                                       │           └──┬────────┬─┘
                                       ▼              ▼        ▼
                                 (text, tools)   intervene   speak
                                       │              │        │
                                       └──────┬───────┴────┬───┘
                                              ▼            ▼
                                     ┌─────────────┐ ┌────────────────┐
                                     │ ElevenLabs  │ │ send_clear_audio│
                                     │ TTS (mulaw) │ │ send_media      │
                                     └─────────────┘ │ send_checkpoint │
                                              │     └─────────┬──────┘
                                              └──────► Plivo WS bytes
```

Every agent turn goes through Mirror's pipeline before any audio
reaches the customer. On intervention, Mirror's WebSocket-native
primitives (`send_clear_audio` + buffered correction + `send_checkpoint`)
flush queued audio and replace it with the corrective question. No
sleep heuristics; the checkpoint event tells us exactly when buffer
audio finished playing.
