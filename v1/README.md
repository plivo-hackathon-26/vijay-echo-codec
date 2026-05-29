# v1 — Plivo Mirror hackathon demo (legacy)

This is the original Plivo Hackathon 2026 submission. It's a single
FastAPI app + HTMX/Tailwind dashboard + SQLite-backed call store + two
deliberately-rigged demo agents (pizza-plivo, travel-plivo) that
showcase Mirror catching and self-correcting agent failures live.

**Use this directory** to run the original 4-scenario demo
(contradiction → self-correct → confirmation → corrected order +
travel variant + missing-tool handoff + semantic catch).

**Do not use this directory** for building new things on top of
Plivo Mirror. The new pip-installable library lives at `../v2/` and is
domain-agnostic, single-layer LLM-scorer based, streaming-aware, and
ships with a pre-tool-call gate. Everything in this directory is
pizza-coupled, regex-driven, and intended as a demo harness only.

## Run the demo

```bash
cd v1
source ../venv/bin/activate          # the shared venv at repo root
pip install -r requirements.txt
uvicorn main:app --port 8000 --reload
```

In another shell:

```bash
ngrok http 8000
```

Set `PUBLIC_HOST=<your-ngrok-host>` in `../.env`, point your Plivo
number's XML at `https://<ngrok-host>/voice/answer`, and call the
number. Visit `http://localhost:8000/` for the dashboard.

## What's in here

```
v1/
├── main.py              FastAPI app — /voice/answer + /voice/stream
├── prompts.py           All LLM prompts (pizza-coupled)
├── db.py                SQLite schema + helpers (single source of truth)
├── requirements.txt     Legacy dependencies
├── pytest.ini           Legacy pytest config
├── mirror.db            SQLite DB (gitignored)
├── agent/primary.py     Rigged pizza agent (CRITICAL ITEM-CAPTURE RULE)
├── agents/travel/       Rigged SkyPlivo flight-booking agent
├── voice/
│   ├── stream.py        Raw WS handler with manual JSON parsing
│   ├── stt.py           Deepgram nova-3 wiring + keyterm boosts
│   └── tts.py           Plivo REST calls.speak()
├── mirror/
│   ├── patterns.py      Pizza-specific regex rule engine
│   ├── semantic.py      Pizza-hardcoded LLM semantic reviewer
│   ├── evaluator.py     Pattern orchestrator + cooldown
│   ├── interventions.py Sleep-heuristic buffer/correction pacer
│   ├── canned_corrections.py
│   ├── state.py         Process-local pending/cooldown/override state
│   ├── reporter.py      Post-call failure-report generator
│   ├── applier.py       Apply-fix-as-PR pipeline (gh CLI + allowlist)
│   ├── backfill.py
│   ├── report_hook.py   Installs db.end_call monkey-patch at import
│   └── value_model.py   Pizza-shop dollar-saved math
├── dashboard/
│   ├── routes.py        Main / fleet view / call detail / compare
│   ├── fixes_routes.py  /fixes review page + apply/dismiss
│   ├── mirror_toggle.py 6 monkey-patches at import time
│   ├── agent_router.py  Per-call agent dispatch monkey-patch
│   ├── sse.py           SSE broadcaster (polling)
│   ├── stats.py         Aggregate SQL for stat cards
│   └── templates/       Jinja2 — base, index, call_detail, compare, fixes, docs
└── tests/               Original tests (test_patterns, test_evaluator, ...)
```

## What v2 changed vs v1

| v1 (this directory) | v2 (`../v2/plivo_mirror/`) |
|---|---|
| Pizza vocabulary hardcoded in `mirror/patterns.py` | Domain-agnostic; customer supplies `policies=[...]` |
| Two-layer detection (regex → semantic LLM) | Single LLM scorer + cheap heuristic pre-gate |
| Monkey-patches at import time (6+ functions) | Zero import-time side effects |
| Plivo REST `speak()` + sleep heuristics | WebSocket `send_clear_audio` + `send_media` + `send_checkpoint` |
| Process-local in-memory state only | `StateStore` protocol; in-memory default; v2 of v2 adds Redis |
| Azure OpenAI + Deepgram + Plivo + GitHub hardcoded | All pluggable via small protocols |
| FastAPI app + dashboard + DB included | Library only; bring your own app |
| No tool-call gate | Pre-tool-call gate blocks irreversible tools |
| Turn-based scoring | Streaming-native scoring (mid-stream sentence-boundary fire) |

See `../v2/README.md` for the new library's public API.

## Caveat

The legacy code uses `mirror.db` at the repo root by default. After
the v1/v2 split, that file lives at `v1/mirror.db` — and the code
expects to find it as `mirror.db` relative to the current working
directory. Make sure to `cd v1` before running `uvicorn`.
