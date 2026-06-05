# plivo-mirror v5 — project handoff (wrap: 2026-06-05)

Project paused at feature saturation; to be picked up for production once
the voice-AI pipeline is stable. This document is the single page a future
engineer needs to resume.

## What this is

A **grounded verification layer for LLM voice agents**. Every claim the
agent speaks is diffed against the customer's own ground truth (facts,
session state, tool log) with a `{spoken, truth, source}` receipt; every
guarded tool call is authorization-checked **before** its side effect.
Three deployment rungs, one engine:

1. **Shadow** — observe only; flags land on the dashboard as `would_have`.
   With `MIRROR_SHADOW_JUDGE=1`, factual errors flag *during* the call.
2. **Intervene** — pre-TTS gate corrects the reply before it is spoken;
   ToolGate blocks unauthorized tools before execution. Auto-wired at
   `attach_mirror` time; zero agent code changes.
3. **Post-call** — the grounded LLM judge audits every call
   (`MIRROR_AUTO_AUDIT=1`); reviewer ✓/✗ feeds live measured precision.

Differentiators verified against Hamming / Coval / Roark / Cekura /
Langfuse / Arize / Vapi / Retell (June 2026): deterministic grounded
receipts · live in-call self-correction · pre-execution tool blocking ·
measured-on-your-traffic precision · receipts export.

## Final measured state (fresh `--no-cache` run, 2026-06-05, 373 live calls)

180 labeled cases (81 violations / 99 clean), weighted:

| layer | catch | false alarms |
|---|---|---|
| inline deterministic (µs, p95 0.1 ms) | 8.6% (by design) | ~0–4.8% |
| pre-TTS gated hold | **80.2%** | 0–6.2% |
| post-call judge | **84.0%** | 0–4.7% |
| combined | **86.4%** | — |

Run-to-run judge variance on Azure is ±3–5 pts (no temperature control);
`MIRROR_JUDGE=two_stage` exists to damp it (built, measured offline, not
yet benchmarked live). Full breakdown: `eval/scorecard_v4set.json`,
reproduce with `venv/bin/python v5/eval/run_v4_set.py --no-cache`.

**Tests: 185 passing**, all offline (`venv/bin/python -m pytest v5/tests`).

## How to run everything

```bash
venv/bin/pip install -e 'v5[agent,monitoring]'
venv/bin/python -m pytest v5/tests                    # 185, offline
venv/bin/python v5/eval/run_eval.py                   # offline fixture eval
venv/bin/python -m uvicorn plivo_mirror_v5.deployables.monitoring.backend.app:app --port 8500
cd v5/plivo_mirror_v5/deployables/monitoring/frontend && npm install && npm run dev
venv/bin/python v5/plivo_mirror_v5/deployables/monitoring/replay_fixture.py  # demo data
# real agent: see QUICKSTART.md / docs/CONNECT_CLOUD.md (4 example agents in examples/)
```

The Render demo deployment is retired; `render.yaml` redeploys it in one
click (note: free tier = ephemeral DB; attach a persistent disk for real
use). PyPI: `plivo-mirror-v5` (0.5.0 published; 0.5.1 is the final code).

## Architecture in 10 lines

- `engine/` — pure detection, stdlib-only, offline. L1 input gate → L2
  deterministic diff (claims vs reference/session/tool-log + 5 policy
  checks) → arbitration (deterministic wins, suppression audited).
  Business logic lives in `PolicyPack` (code/config), never prompts.
- `engine/tool_gate.py` — pre-execution allow/deny; the model never
  authorizes itself (authorizing facts are host-written only).
- `auditor/` — the grounded judge (single prompt, 3 duty stations:
  pre-TTS / shadow-flag / post-call), swappable Protocol, abstains when
  ungrounded, `TwoStageJudge` voting variant.
- `integrations/` — the LiveKit adapter (`attach_mirror`), duck-typed, no
  livekit import in core; recording; audio levels.
- `telemetry/` — OTel-shaped records → bounded ThreadedSink (+ optional
  disk spool) → FastAPI/SQLite backend → React dashboard.

## Honest gaps (the production to-do, in priority order)

1. **Live mic validation** of the intervene auto-wiring across all 4
   example agents (validated vs livekit-agents 1.5.16 + unit tests only).
2. **Real-traffic shadow pilot** — the 86.4% is a static-set number; the
   true production base rate is unmeasured. This pilot also fills the
   review loop and builds the fine-tune dataset.
3. **Platform hardening** — auth/PII-redaction default-on, retention,
   multi-tenancy, Postgres, persistent disk. All known, none started.
4. **Detection gaps** from the final eval: conditionals 5/8,
   unconfirmed-irreversible 1/3, math totals 2/4, repetition loops 0/1,
   5 gate-exempt cases. Math + repetition are deterministic (no model).
5. **Small fine-tuned guard model** — drop-in via the judge Protocol once
   the pilot yields ~1–2k labeled turns. The judge role stays; it just
   gets fast and cheap.
6. **Locale lexicons** — gate/extractor word lists are English-only.

## Repo map

```
v1/ v3/ v4/        earlier generations (v3 = plivo-mirror 0.3.x line on PyPI)
v5/                THIS project (plivo-mirror-v5 on PyPI)
  plivo_mirror_v5/ engine · auditor · integrations · telemetry · deployables
  examples/        aurora (ISP) · northwind bank · wellspring clinic · skyline flight
  eval/            run_eval.py (offline) · run_v4_set.py (180-case live)
  tests/           185 offline tests
  docs/            ROADMAP (limitations+plan) · CONNECT_CLOUD · this file
  PRODUCTION.md    measured numbers + env-var reference
  QUICKSTART.md    5-minute integration
```
