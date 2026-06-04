# plivo-mirror v5

A real-time firewall that verifies a voice agent's **output** against ground
truth — did the agent say a wrong price, state a wrong policy, or claim an
action it did not take.

Unlike post-call eval tools (LLM-judge over a transcript), v5 checks the
agent's spoken claims against **deterministic ground truth held in-stack**
(session state, structured reference data, tool-call results), with
explainable evidence. It is the inverse of LiveKit's observer pattern: we
watch the **agent**, not the caller.

## Architecture

One detection engine, three layers in strict precedence + arbitration:

| Layer | Role | Truth source | Cost |
|---|---|---|---|
| **L1** input integrity | a *gate*, not a detector — marks untrusted ASR input, writes readback corrections to state | ASR confidence, caller corrections | ~0 |
| **L2** deterministic diff | **PRIMARY** — wrong price/policy/hours, account contradiction, speech-vs-action divergence | session state · reference store · tool log | µs, no model, inline-safe |
| **L3** claim-grounding NLI | SECONDARY — free-form prose claims only | unstructured KB via retrieval + NLI | model in loop, async only |

**Arbitration: deterministic wins.** L3 fires only on claims L2 has no
jurisdiction over; suppression is recorded in `suppressed_by` so it is
auditable. Every L2 verdict carries `{claim_type, spoken_value, truth_value,
source}` — the frontend renders this evidence verbatim.

### Two deployables, one engine, one integration

- **Monitoring** (`deployables/monitoring/`) — the engine runs in *shadow*
  mode; verdicts are emitted as telemetry (call = OTel trace, turn = span,
  verdict/action = span events) into a FastAPI backend and rendered in a
  call-ID-keyed React frontend. `call_id` == the LiveKit room id.
- **Live intervention** (`deployables/intervention/`) — the same engine runs
  inline; a firing verdict triggers Hook A (next-turn `[CORRECTION: …]`
  injection / hold / handoff). Hook B (pre-TTS gate) is an experimental
  interface stub.

Both are powered by the single observer in
`plivo_mirror_v5/integrations/livekit_observer.py`; a `mode` config flag
(`"shadow" | "intervene"`) selects where verdicts are routed — nothing else
changes.

## Layout

```
v5/
  plivo_mirror_v5/
    engine/             # the library — the shared detection core
    telemetry/          # OTel-shaped emission, used by both deployables
    integrations/       # the single LiveKit observer (+ FakeSession)
    deployables/
      monitoring/       # FastAPI backend + React (Vite) frontend
      intervention/     # Hook A (next-turn), Hook B (pre-TTS stub)
    auditor/            # post-call LLM-judge — interface + stub only
  eval/                 # fixtures + run_eval.py (catch / false-alarm / latency)
  tests/
```

## Quickstart

From the repo root (uses the shared root venv, matching v1–v4):

```bash
venv/bin/pip install -e v5            # core is stdlib-only
venv/bin/python -m pytest v5/tests    # all offline; no network, no keys
venv/bin/python v5/eval/run_eval.py   # catch rate / false alarms / latency
```

Monitoring backend + frontend:

```bash
venv/bin/pip install -e 'v5[monitoring]'
venv/bin/python -m uvicorn plivo_mirror_v5.deployables.monitoring.backend.app:app --port 8500
# replay a fixture call through the shadow observer into the backend:
venv/bin/python v5/plivo_mirror_v5/deployables/monitoring/replay_fixture.py
# frontend:
cd v5/plivo_mirror_v5/deployables/monitoring/frontend && npm install && npm run dev
```

## Design invariants (do not regress)

- L2 is **exact keyed lookup**, never vector search. Vector retrieval exists
  only as the L3 retriever over the *unstructured* KB.
- Session state (runtime per-call validated facts) ≠ knowledge base (static
  per-agent prose). Complementary, never substitutes.
- Only L2 is inline-safe (budget ~50ms, asserted in tests); L3 runs off the
  hot path. The LLM-judge auditor is offline-only (interface stub in v5).
- L2 always diffs against a state **snapshot** (`state_snapshot_id` on every
  `TurnResult`) — diff timing is auditable.
- The engine never emits telemetry and never takes actions; routing is the
  deployables' job.

## Eval

`eval/fixtures/` ships two calls: **induced** (deliberately injected
failures — wrong price, wrong policy, claimed-but-unfired action, ungrounded
prose, low-ASR gating) and **organic** (clean turns + subtle failures).
`run_eval.py` reports catch rate per layer, false-alarm rate on clean agent
turns, and per-layer latency percentiles with the L2 inline-budget check.
