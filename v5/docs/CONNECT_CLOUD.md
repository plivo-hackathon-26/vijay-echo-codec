# Connect a cloud-hosted LiveKit agent to plivo-mirror

Mirror's backend is plain HTTPS — the agent can run ANYWHERE that can
reach the dashboard URL. Nothing about Mirror requires the agent to run
locally. Three hosting options, same 4 steps each.

## The 4 steps (identical everywhere)

1. **Register** the agent in the dashboard (⚙ agents & intervene):
   agent id + system prompt + facts + policies. Copy the snippet.
2. **Install** Mirror in the agent's environment (published on PyPI):
   ```bash
   pip install "plivo-mirror-v5[agent]"
   ```
3. **Wire** `attach_mirror(...)` in your entrypoint with:
   ```python
   backend_url=os.environ["MIRROR_BACKEND_URL"],   # the dashboard URL
   agent_id="<your-registered-id>",
   agent=my_agent,                                  # dashboard-toggled intervene
   ```
   In intervene mode, pre-TTS gating (flagged drafts never spoken) and
   the pre-execution ToolGate (unauthorized tools blocked BEFORE their
   side effect) are **auto-wired at attach time** — no agent code changes.
   If you already override `llm_node` yourself, your wiring is kept; the
   manual 8-line pattern in `examples/skyline_flight_agent/agent.py`
   remains the documented fallback.
4. **Set env** where the agent runs:
   ```
   MIRROR_BACKEND_URL=https://<your-dashboard-host>
   MIRROR_API_KEY=...        # only if the backend sets one
   LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
   OPENAI_* / DEEPGRAM_* / ELEVEN_*  (your agent's own stack)

   # optional Mirror knobs (agent side):
   MIRROR_SHADOW_JUDGE=1     # shadow mode: grounded judge flags factual
                             # errors DURING the call (flag-only, fail-open;
                             # ~1 judge call per assertive agent turn)
   MIRROR_JUDGE=two_stage    # voting judge: k fast votes, split escalates
   OPENAI_MODEL_FAST=...     #   to the strong model (damps Azure variance)
   MIRROR_JUDGE_VOTES=3      #   vote count (default 3)
   MIRROR_RECORD=1           # capture call audio for dashboard playback
   MIRROR_TELEMETRY_QUEUE_MAX=10000   # bound on the async telemetry queue
   MIRROR_TELEMETRY_SPOOL=/path.jsonl # park+replay telemetry across
                                      # backend outages instead of dropping
   ```

Calls appear in the dashboard the moment the worker takes a job —
`call_id` == the LiveKit room name, so Mirror joins your LiveKit traces.

## Option A — worker on any machine, rooms on LiveKit Cloud

```bash
python agent.py dev      # registers against your LiveKit Cloud project
```
Anyone can then call the agent via the LiveKit Playground / SIP / your
app. The worker can be a laptop, a VPS, a container — Mirror only needs
outbound HTTPS to `MIRROR_BACKEND_URL`.

## Option B — LiveKit Agents Cloud (fully hosted worker)

Use the Dockerfile in `examples/skyline_flight_agent/`, then:

```bash
lk agent create   # once, in the example dir (needs livekit CLI + auth)
lk agent deploy
```

Set the env vars from step 4 in the LiveKit Cloud agent's settings.
LiveKit runs and scales the worker; Mirror monitoring/intervene rides
along unchanged.

## Option C — your own infra (k8s / ECS / a box)

Run the same container/process under `python agent.py start`. Identical
wiring.

## Requirements & honest notes

- **The dashboard must be on a stable public URL.** The ngrok demo URL
  works but dies with the host machine and rotates on restart — for a
  real test round use the Render blueprint (`render.yaml`, repo root) or
  any host running
  `uvicorn plivo_mirror_v5.deployables.monitoring.backend.app:app`.
- Set `MIRROR_API_KEY` on the backend before sharing the URL beyond your
  team — otherwise anyone can register agents and read transcripts.
- The package is on PyPI (`pip install "plivo-mirror-v5[agent]"`) — no token
  needed. The Dockerfile installs it the same way.
- Shared sandbox: until per-tenant isolation lands, every connected agent's
  calls are visible on the one dashboard — don't send real customer PII.
- Intervene toggle applies at CALL START (the worker fetches the
  registered config when each call attaches) — flips affect the next
  call, not in-flight ones.
