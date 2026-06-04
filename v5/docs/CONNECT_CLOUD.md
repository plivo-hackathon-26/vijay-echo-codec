# Connect a cloud-hosted LiveKit agent to plivo-mirror

Mirror's backend is plain HTTPS — the agent can run ANYWHERE that can
reach the dashboard URL. Nothing about Mirror requires the agent to run
locally. Three hosting options, same 4 steps each.

## The 4 steps (identical everywhere)

1. **Register** the agent in the dashboard (⚙ agents & intervene):
   agent id + system prompt + facts + policies. Copy the snippet.
2. **Install** Mirror in the agent's environment:
   ```bash
   pip install "git+https://github.com/plivo-hackathon-26/vijay-echo-codec#subdirectory=v5"
   # (or vendor the v5/plivo_mirror_v5 package into your image)
   ```
3. **Wire** `attach_mirror(...)` in your entrypoint with:
   ```python
   backend_url=os.environ["MIRROR_BACKEND_URL"],   # the dashboard URL
   agent_id="<your-registered-id>",
   agent=my_agent,                                  # dashboard-toggled intervene
   ```
   For pre-TTS gating (flagged drafts never spoken), add the 8-line
   `llm_node` override from `examples/skyline_flight_agent/agent.py`.
4. **Set env** where the agent runs:
   ```
   MIRROR_BACKEND_URL=https://<your-dashboard-host>
   MIRROR_API_KEY=...        # only if the backend sets one
   LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
   OPENAI_* / DEEPGRAM_* / ELEVEN_*  (your agent's own stack)
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
- The repo is private: `pip install git+…` inside a Docker build needs a
  GitHub token (`docker build --secret`), or vendor the package.
- Intervene toggle applies at CALL START (the worker fetches the
  registered config when each call attaches) — flips affect the next
  call, not in-flight ones.
