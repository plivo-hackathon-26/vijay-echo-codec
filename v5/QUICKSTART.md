# plivo-mirror v5 — Quickstart (connect your agent in 5 minutes)

Mirror watches your LiveKit voice agent's output, flags wrong facts /
unauthorized actions / policy breaks against your own ground truth, and
(optionally) corrects them live. Your agent runs wherever you host it; the
dashboard is hosted — your agent just points at it over HTTPS.

**Hosted dashboard:** https://plivo-mirror.onrender.com
(first load after idle takes ~30–60s — the free tier sleeps.)

## 1. Register your agent (browser, ~1 min)

Open the dashboard → **⚙ agents & intervene** → **register an agent**:

- **agent id** — any stable name you choose (e.g. `support-bot-prod`). It
  only has to match the `agent_id=` in your code below.
- **system prompt** — paste your agent's own prompt (grounds the judge).
- **facts** — your ground truth as JSON, e.g.
  `{"plan": {"turbo": {"price_per_month": 79.99}}, "policy": {"refund_window_days": 30}}`
- **policies** — one business rule per line.

Copy the integration snippet it shows you.

## 2. Install + wire it into your LiveKit worker (~2 min)

```bash
pip install "plivo-mirror-v5[agent]"
```

In your entrypoint, after `ctx.connect()`:

```python
import os
from plivo_mirror_v5.integrations import attach_mirror

attach_mirror(
    session,
    room_id=ctx.room.name,                     # call_id == LiveKit room id
    backend_url=os.environ["MIRROR_BACKEND_URL"],
    agent_id="support-bot-prod",               # ← matches your registration
    agent=my_agent,                            # enables dashboard-toggled intervene
)
await session.start(agent=my_agent, room=ctx.room)
```

For pre-TTS gating (a flagged reply is corrected before it's spoken), add
the ~8-line `llm_node` override from
`examples/skyline_flight_agent/agent.py`.

## 3. Set env + run (~1 min)

```bash
export MIRROR_BACKEND_URL="https://plivo-mirror.onrender.com"   # the hosted dashboard
# your usual: LIVEKIT_URL/API_KEY/API_SECRET, OPENAI_*, DEEPGRAM_*, ELEVEN_*
python agent.py dev          # or `console` for local mic, or deploy to LiveKit Cloud
```

## 4. Make a call → watch the dashboard

Your call appears in the sidebar within seconds (call_id = the LiveKit room
name). Flagged turns show a `{spoken, truth, source}` receipt. Toggle
**INTERVENE** on the agent card to have it self-correct on the next call.

## Notes (shared sandbox stage)

- Hosting options (laptop / VPS / LiveKit Agents Cloud) and the full guide:
  `docs/CONNECT_CLOUD.md`.
- This is a **shared sandbox** today: all connected agents' calls are
  visible on one dashboard — don't send real customer PII. Per-tenant
  isolation, BYO keys, and retention are the next milestone (see
  `docs/ROADMAP.md`).
- The judge's post-call analysis runs on the dashboard host's LLM keys;
  inline intervention uses your worker's `OPENAI_*` keys.
