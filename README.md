# Plivo Mirror

> *Post-mortem is for funerals. Mirror is the ambulance.*

A silent AI supervisor that watches voice-agent phone calls in real time,
catches failures via pattern + semantic detection, makes the agent
self-correct **mid-call** (the customer never knows), writes a post-call
failure report, and — on one human click — opens a real GitHub PR to
fix the underlying prompt.

Built for **Plivo Hackathon 2026** · Track: `for-agents`.

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│  DETECT  │ → │ CORRECT  │ → │   SAVE   │ → │  LEARN   │
└──────────┘   └──────────┘   └──────────┘   └──────────┘
     ↑                                              │
     └──────────────────────────────────────────────┘
          every failure improves the next call
```

---

## Why this exists

Voice agents fail loudly while looking perfect in logs. Today's
observability is *post-mortem*: by the time you read the report, the
customer churned. Mirror flips that — supervision is **live**, fixes
fire in the same call, and the next deploy ships the underlying
correction as a real PR.

| Failure mode                | Today                         | With Mirror                                  |
| --------------------------- | ----------------------------- | -------------------------------------------- |
| Hallucinated commitments    | Caught in a transcript review | Caught and overridden in the same turn       |
| Missed corrections          | "9.4 / 10 — call completed"   | Buffer + corrected response, single voice    |
| Confident wrong answers     | Customer hangs up confused    | Mirror reframes; customer hears one agent    |
| Bad prompts ship indefinitely | Manual code review           | Failure report drafts a fix → human approves → PR opens |

---

## The four beats

**DETECT** — Pure-Python pattern checks on every turn.
`<3 ms` overhead. Zero LLM cost on healthy calls. A second-tier
semantic-LLM reviewer fires *only* when patterns flag, so the LLM bill
scales with failures, not traffic.

**CORRECT** — When Mirror fires, the agent says a short buffer
sentence ("let me just confirm…") which covers the ~2.5 s correction
window. A second LLM call rewrites the next response with Mirror's
evidence injected. The customer hears one continuous voice.

**SAVE** — Outcome retained. The dashboard quantifies it in dollars
(churn risk × order value × lifetime + support-ticket cost + reputation
hit avoided).

**LEARN** — At call end, an LLM writes a failure report: what
happened, root cause, a proposed text edit, and a confidence score. A
reviewer hits **Approve & Apply** in the dashboard, Mirror rewrites the
prompt file, validates with `ast.parse`, pushes a branch, opens a real
GitHub PR. Allowlist-gated; no PR can touch anything outside `prompts.py`
or the two primary agents.

---

## What ships in the repo

| Feature                              | Path                                                          |
| ------------------------------------ | ------------------------------------------------------------- |
| Live voice loop                      | `voice/stream.py`, `voice/stt.py`, `voice/tts.py`             |
| Rigged primary agents                | `agent/primary.py` (pizza), `agents/travel/primary.py`        |
| Pattern detector                     | `mirror/patterns.py`, `mirror/evaluator.py`                   |
| Semantic LLM reviewer                | `mirror/semantic.py`                                          |
| Mid-call intervention                | `mirror/interventions.py`, `mirror/state.py`                  |
| Post-call failure reports            | `mirror/reporter.py`, `mirror/report_hook.py`                 |
| Approve → real GitHub PR             | `mirror/applier.py`                                           |
| Dollar-value model                   | `mirror/value_model.py`                                       |
| Dashboard (live + compare + fixes)   | `dashboard/routes.py`, `dashboard/fixes_routes.py`            |
| Multi-agent dispatch                 | `dashboard/agent_router.py` (monkey-patches the WS dispatcher)|
| Mirror ON/OFF toggle                 | `dashboard/mirror_toggle.py`                                  |
| Profit/loss chart modal              | `mirror/value_model.calculate_timeseries_today` + Chart.js    |
| Pitch deck at `/slides`              | `dashboard/templates/docs.html`                               |

73 tests, all green.

---

## Demo scenarios that work

| Customer says | Layer that catches it | What Mirror does |
| --- | --- | --- |
| *"Large pepperoni, actually mushroom only, no pepperoni"* | Pattern (contradiction) | Buffer + rewritten confirm; order = mushroom only |
| *"Can you check my last order?"* | Pattern (missing tool) | Canned handoff: "I can transfer you to the team" |
| *"My wife wants pepperoni but I'd like mushroom"* | Semantic LLM | Third-party preference detected; corrects to mushroom |
| *"Book Mumbai Friday, actually Delhi Saturday"* (travel) | Semantic LLM | Corrects to Delhi Saturday — same machinery, different domain |

The pizza & travel agents are **intentionally rigged** to capture every
item / destination mentioned in a single utterance. That's the bait
Mirror exists to catch — please don't "fix" the primaries.

---

## Stack

- Python 3.11 · FastAPI · uvicorn (async, single process)
- Plivo: AudioStream WS + REST `speak`
- Deepgram nova-3 (mulaw 8 kHz, per-agent keyterm boosts)
- Azure OpenAI gpt-5-mini via the OpenAI SDK (`base_url` override)
- SQLite (file: `mirror.db`, gitignored)
- HTMX + Tailwind + Chart.js — all via CDN, no build step
- `gh` CLI + `git` for the PR pipeline
- ngrok / cloudflared for the Plivo webhook tunnel

---

## Run it locally

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in the secrets — see "Required env vars" below

uvicorn main:app --port 8000 --reload         # terminal 1
ngrok http 8000                                # terminal 2
```

Set the ngrok host in `.env` as `PUBLIC_HOST=<host-without-https>`,
point your Plivo number's Answer URL at `https://<host>/voice/answer`,
and dial in.

### Required env vars

```
OPENAI_API_KEY=...
OPENAI_API_URL=https://<azure-resource>.cognitiveservices.azure.com/openai/v1
OPENAI_MODEL=gpt-5-mini
DEEPGRAM_API_KEY=...
PLIVO_AUTH_ID=...
PLIVO_AUTH_TOKEN=...
PLIVO_PHONE_NUMBER=+1XXXXXXXXXX
PUBLIC_HOST=<your-ngrok-host>          # without scheme, without trailing /
```

`gh auth status` should report logged-in — the Apply pipeline shells
out to `gh pr create`.

---

## Where to click

| URL                 | What you see                                                       |
| ------------------- | ------------------------------------------------------------------ |
| `/`                 | Live dashboard — stat cards, recent calls, SSE event feed          |
| `/calls/<uuid>`     | Single call: transcript, Mirror events, interventions, dollar saved|
| `/compare`          | Side-by-side: same scenario with Mirror OFF vs ON                  |
| `/fixes`            | Pending failure reports → Approve & Apply → real PR opens          |
| `/slides`           | The 2-slide pitch deck (arrow keys, `esc` to close)                |
| Click the $ card    | Profit / loss line chart for today                                 |
| `pitch` link, footer | Subtle link to `/slides` from any dashboard page                  |

---

## Architecture notes

**Non-invasive hooks.** Most cross-cutting Mirror features are installed
as monkey-patches at module-import time (see `dashboard/__init__.py`
and `mirror/__init__.py`). This lets the supervisor layer over agent /
voice / mirror-core code paths without editing them — disabling a layer
is just `not importing` it.

**One concurrent-call assumption.** Per-call state is keyed by
`call_uuid` in a thread-locked dict (`mirror/state.py`); the semantic
reviewer reads the call's UUID via a `contextvars.ContextVar` set by
the pattern evaluator earlier in the same async task. Concurrency-safe
for the demo's ≤1 concurrent call. Will need an `asyncio.Lock` upgrade
for multi-call production.

**Allowlist-gated PR pipeline.** `mirror/applier.py` refuses to rewrite
anything outside a literal `ALLOWED_FILES` set (currently:
`prompts.py`, `agent/primary.py`, `agents/travel/primary.py`,
`agents/travel/prompts.py`). The LLM rewrite is `ast.parse()`-validated
before any commit; syntax-broken files never get pushed. Refuses if
the working tree is dirty or the current branch isn't `main`. All-or-
nothing rollback on failure.

**Azure OpenAI quirks.** Several common OpenAI params are rejected by
this deployment (`max_tokens=`, `tool_choice="none"`); `mirror/semantic.py`
documents the safe call shape.

---

## Tests

```bash
source venv/bin/activate
pytest -q
# 73 passed
```

---

## Hackathon submission

- **Tagline.** *Post-mortem is for funerals. Mirror is the ambulance — voice agents that catch their own failures and self-correct mid-call.*
- **Track.** `for-agents` — the agent is the user of Plivo (CLI / voice / dashboard supervision tooling).
- **Demo path.** Dial in → live dashboard shows turns + Mirror events in real time → contradiction utterance triggers buffer + correction → call ends → failure report appears on `/fixes` → one click opens a real PR against the rigged prompt.

Built in 24 hours at Plivo Hackathon 2026.
