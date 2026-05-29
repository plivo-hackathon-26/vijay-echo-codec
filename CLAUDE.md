# Project context

**plivo-mirror is becoming a self-correcting voice agent framework** —
the only voice agent stack that catches its own mistakes mid-call and
ships PRs to fix the underlying code. We are not building a demo. We
are building a product that competes with LiveKit Agents, Pipecat,
Vapi, Retell, and OpenAI Realtime — with one thing they don't have:
**runtime self-correction + self-improvement loop**.

0.1.0 shipped on PyPI 2026-05-27 as a supervisor library. 0.2.x+ turns
it into a full agent runtime. The supervisor core (scorer, tool-gate,
intervention orchestrator, Fix-PR pipeline) stays — it IS the wedge —
and the framework grows around it.

## North star

> *"Your voice agent ships, runs, catches its own mistakes mid-call,
>   opens PRs to fix the underlying prompt. Voice agents that get
>   better every call."*

## What we are NOT doing anymore

- Talking about demos. The product is the target — the demo is just
  one artifact along the way.
- Pizza/sandwich/burger domain coupling. The framework is generic;
  vertical templates ship separately later if at all.
- "Bring your own agent" library framing. We're shipping the agent
  too, with the supervisor baked in.

## Repo layout (READ THIS FIRST)

The repository is split into two top-level directories:

- **`v1/`** — original hackathon code. Preserved for archaeology only.
  Do not modify; do not extend. New work happens in `v2/`.

- **`v2/`** — the published `plivo-mirror` package (`plivo_mirror/`),
  streaming-aware, pre-tool-call gate, zero import-time side effects.
  Run with `cd v2 && pip install -e ".[openai,plivo,dev]"` then
  `pytest tests/`. Public API documented in `v2/README.md`.

Both directories share the repo-root `venv/`, `.env`, `.env.example`,
`.hackathon.json`, and this `CLAUDE.md`.

## What this project IS

**plivo-mirror** — the voice agent framework that fixes itself.

Three things every customer gets from a single `pip install`:
1. **An agent runtime.** STT → LLM → tool-use → TTS, transport-agnostic
   (Plivo PSTN, LiveKit WebRTC, Twilio, SIP, browser SDK). Three-line
   setup: `Agent(system_prompt=..., tools=..., policies=...).run()`.
2. **A supervisor watching every turn.** Tiered scoring (cheap classifier
   → expensive scorer), tool-gate that vets irreversible tool calls
   before they fire, mid-call intervention that clears queued audio and
   speaks a correction the caller hears as part of the same turn.
3. **A self-improvement loop.** Every call that intervenes generates a
   structured FailureReport; on approval (or via auto-PR in CI) the
   framework opens a real GitHub PR with the fix to the agent's prompt
   or code.

Tagline framing: *monitoring is a post-mortem; plivo-mirror is the
ambulance + the vaccine + the surgeon who learns from the operation.*

## The roadmap (high level)

- **0.2.x — Agent runtime.** Build the `mirror.Agent(...)` facade.
  Bundle STT/LLM/TTS/tool plumbing. Move the manual WS handler code
  out of customer-land into the framework. Customer's mental load:
  zero glue code, three lines of config.
- **0.3.x — Transport plugins.** First-class adapters for LiveKit,
  Twilio, generic SIP, and a browser SDK for embedded voice. Plivo
  stays as the reference.
- **0.4.x — Policy DSL.** Replace plain-English `policies=[...]` with
  a typed, declarative DSL (`policy("name", "rule", severity=...,
  on_violation="transfer_human")`). Version-controllable, lintable,
  separately deployable from the agent prompt.
- **0.5.x — Auto-PR pipeline.** Today the Fix-PR is human-approved.
  Add a confidence-gated auto-PR mode that opens (but does not merge)
  PRs without human intervention; CI runs replay tests against the
  recorded call before approving the PR.
- **0.6.x — Durable state + multi-tenant.** Redis state store, tenant
  isolation, per-tenant policies + tool registries. Required for any
  customer running > 1 process or > 1 product line.
- **1.0 — General availability.** Real customers in production. SLA.
  Pricing. Docs site at plivo-mirror.dev. Replay/audit dashboard.

## Where 0.1.0 ended up

Shipped on PyPI 2026-05-27. ~75 unit tests + 8 integration tests, all
green. Has the supervisor surface (`Supervisor`, `CallSupervisor`,
`MirrorConfig`), the tool-gate, the streaming scorer, the report
generator, the SQLite report sink, the GitHub fixer. The only missing
piece for the agent-framework vision is the runtime/orchestrator on
top — `mirror.Agent(...)`.

## Working principles for 0.2.x+

- **No demo code in the package.** Examples live in `v2/examples/` and
  are documentation, not product. The supervisor + agent runtime must
  be production-grade — typed, tested, no domain coupling.
- **Every new feature lands behind an integration test.** The 75
  existing unit tests are scaffolding; the 8 new supervisor tests
  prove the composition works. Same bar for the agent runtime.
- **Latency is a feature.** Pregate stays <50μs, scorer call hidden
  inside TTS encode window, intervention buffer < 800ms perceived
  pause. Any change that regresses these must justify itself.
- **Transport portability is non-negotiable.** Any feature that ties
  the supervisor to a specific transport (Plivo, LiveKit, anything)
  goes in `plivo_mirror.transports.{name}` not the core.

## Required: .hackathon.json

Every repo has a `.hackathon.json` at root. The hackathon scoreboard polls
this file — it must stay valid.

Schema:
```
{
  "tagline": "one-line pitch, < 140 chars",
  "track": "for-agents | by-agents",
  "demo_url": "optional — link to live demo or video"
}
```

Tracks:
- **for-agents**: agent is the user of Plivo (CLI, MCP, debug tools) ← this project
- **by-agents**: agent builds/operates Plivo itself (PR bots, triage, on-call copilots)

If `tagline` is empty during any session, remind the user. The scoreboard
ranks blank entries last.

## Credentials

Plivo creds + API keys are in 1Password → `hackathon-2026` vault.
The `.env` at repo root holds runtime config (gitignored).

## Stack

- Python 3.11 + FastAPI + uvicorn (async)
- Plivo (AudioStream WS + REST `speak` for TTS)
- Deepgram nova-3 (STT, mulaw 8kHz, `speech_final` + utterance buffer)
- Azure OpenAI gpt-5-mini via OpenAI SDK with `base_url` override
- SQLite (single source of truth — file is `mirror.db`, gitignored)
- HTMX + Tailwind via CDN (dashboard)
- `gh` CLI + git (for the "Apply" pipeline that opens real PRs)
- ngrok (tunnel to expose `/voice/answer` to Plivo)

## File layout

```
main.py                  FastAPI app, /voice/answer XML + /voice/stream WS
db.py                    SQLite schema + helpers (single source of truth)
prompts.py               All LLM prompts (primary, correction, semantic
                         reviewer, report generator, apply fix)

agent/primary.py         Pizza-Plivo primary agent (RIGGED — see below)
agents/travel/           SkyPlivo travel agent (RIGGED in the same shape)

voice/stream.py          WebSocket handler + per-turn flow
voice/stt.py             Deepgram client (speech_final + buffer pattern)
voice/tts.py             plivo.calls.speak() wrapper

mirror/patterns.py       Pure-Python regex pattern rules
                         (contradiction, missing_tool_request, repetition)
mirror/evaluator.py      Pattern orchestrator + cooldown
mirror/semantic.py       LLM-based reviewer (runs AFTER primary, BEFORE speak)
mirror/interventions.py  Buffer + LLM correction orchestrator
mirror/state.py          Thread-safe per-call state (cooldown, override)
mirror/reporter.py       Post-call failure report generator (LLM)
mirror/report_hook.py    db.end_call wrapper → schedules reporter
mirror/applier.py        Approve & Apply pipeline (LLM rewrite → branch → PR)
mirror/backfill.py       CLI / endpoint for past-call reports
mirror/value_model.py    Dollar-value calculations (churn + support + reputation)
mirror/__init__.py       Installs report_hook at package import

dashboard/__init__.py    Installs mirror_toggle + agent_router hooks at import
dashboard/routes.py      Main dashboard endpoints (/, /calls/*, /compare, SSE)
dashboard/fixes_routes.py /fixes review page + apply/dismiss/backfill routes
dashboard/mirror_toggle.py  Global toggle + 6 monkey-patches (the load-bearing module)
dashboard/agent_router.py   Per-call agent dispatch via voice.stream.run_turn patch
dashboard/sse.py         SSE broadcaster (polling-based, with backfill)
dashboard/stats.py       Read-only aggregate SQL
dashboard/templates/     Jinja2: base, index, call_detail, compare,
                         fixes, docs (pitch deck), partials/
```

### Recently-added (additive) features

- **Reset button** in the dashboard header — `POST /admin/wipe-data`
  → `db.wipe_all_data()` deletes every row from all six tables (schema
  preserved) and `sse.reset_state()` clears the SSE poller watermarks.
  Refuses with HTTP 409 if any call has `status='in_progress'` so a
  live demo can't be truncated mid-conversation.
- **Per-agent STT keyterm boosts** — `voice/stt.py` exposes
  `KEYTERMS_PIZZA` / `KEYTERMS_TRAVEL`. `voice/stream.py` looks up
  `calls.agent_name` on WS connect and passes the right list to
  Deepgram. Without this the travel agent ran on pizza vocabulary and
  garbled city names ("Goa", "Jaipur" etc.).
- **Profit/loss chart modal** — click the "Customer value saved today"
  card on `/` to open a Chart.js modal plotting cumulative saved
  (Mirror ON) vs lost (Mirror OFF + wrong_order) over today. Backed by
  `mirror/value_model.calculate_timeseries_today()` and
  `GET /api/value-saved/timeseries`.
- **Pitch deck at `/slides`** — two-slide standalone deck
  (`dashboard/templates/docs.html`, does NOT extend `base.html`).
  Scroll-snap + keyboard nav. Subtle "pitch" link in the dashboard
  footer points to it. Mounted at `/slides` not `/docs` because
  FastAPI auto-serves Swagger at `/docs`.

## Database tables

- `calls` — call_uuid, caller, started_at, ended_at, status, agent_name,
  mirror_enabled, final_outcome
- `turns` — id, call_uuid, role (customer|agent), text, timestamp
- `orders` — id, call_uuid, items_json, created_at
- `mirror_events` — id, call_uuid, turn_id, pattern_name, severity,
  evidence, intervention_needed, timestamp
- `interventions` — id, call_uuid, pattern_name, strategy, buffer_text,
  correction_text, latency_ms, timestamp
- `failure_reports` — id, call_uuid, pattern_name, severity, summary,
  root_cause, proposed_fix_text, proposed_file, suggested_diff,
  confidence, status (pending|applied|dismissed), applied_pr_url,
  applied_at, dismissed_by, dismissed_at, created_at

## The non-invasive hook architecture (load-bearing)

Most cross-cutting features in this repo are installed as **monkey-patches
at module import time** rather than edits to the call-flow code. This is
intentional: it lets us layer Mirror's behaviour without touching the
agent / voice / mirror-core code paths, and it's easy to disable a layer
by skipping its import.

Patches stack in this order (driven by import order in `main.py`):

1. **`mirror/__init__.py`** → `report_hook.install_hook()`
   - Wraps `db.end_call` to schedule `generate_failure_report` as a
     fire-and-forget asyncio task.

2. **`dashboard/__init__.py`** → `mirror_toggle.install_hooks()`
   - `db.create_call` → stamps `agent_name` + `mirror_enabled` on the row,
     freezes per-call state.
   - `db.end_call` → computes `final_outcome` (post-hoc pattern scan for
     OFF calls), forgets per-call state.
   - `mirror.evaluator.evaluate` → no-op when Mirror is OFF (returns []).
   - `mirror.state.get_intervention_pending` → defense in depth, returns
     None when OFF.
   - `mirror.semantic.review_response` → skips LLM call when OFF.
   - `mirror.interventions.handle_intervention` → no-op when OFF.

3. **`dashboard/__init__.py`** → `agent_router.install_hooks()`
   - `voice.stream.run_turn` → dispatcher that reads `calls.agent_name`
     and routes to the right agent's `run_turn` (pizza or travel).
   - `voice.stream.run_correction_turn` → same dispatch for corrections.
   - `main.GREETING` + `voice.stream.GREETING` → swapped when the
     dashboard active-agent changes.

**Concurrency note:** semantic reviewer reads call_uuid via a
`contextvars.ContextVar` set by the evaluator earlier in the same async
task — safe across overlapping calls. Don't replace with a
module-global; it WILL break for >1 concurrent call.

## What's intentionally broken (do NOT "fix")

The primary agents (`agent/primary.py`, `agents/travel/primary.py`) are
**deliberately rigged**. Their system prompts instruct the LLM to capture
every item / destination / date mentioned in a single utterance,
regardless of correction markers. This is the failure Mirror exists to
catch. Mirror's whole demo depends on this brokenness.

If you see code in those primaries that looks "wrong" (especially the
ALL-CAPS "CRITICAL ITEM-CAPTURE RULE" sections in the prompts) — leave
it. It's the bait.

## Apply pipeline guardrails

`mirror/applier.py` opens real GitHub PRs. It is intentionally hemmed in:

- **`ALLOWED_FILES`** is a literal set; Mirror cannot rewrite anything
  outside it. Currently: `prompts.py`, `agent/primary.py`,
  `agents/travel/primary.py`, `agents/travel/prompts.py`.
- Refuses if `git status --porcelain` is non-empty.
- Refuses if current branch isn't `main`.
- Refuses if `proposed_file` doesn't exist or its resolved path escapes
  `REPO_ROOT`.
- LLM rewrites the full file. The result is `ast.parse()`-validated
  before any commit. Syntax-broken files never get pushed.
- Refuses if the LLM returned empty or unchanged content.
- All-or-nothing rollback on failure: `git reset --hard` → checkout main
  → delete the orphan branch.

If you add a new agent and want Mirror to be able to fix its prompts,
add the path to `ALLOWED_FILES`. Do NOT bypass the allowlist.

## Azure OpenAI quirks (paid in real time, learn from them)

This Azure deployment of `gpt-5-mini` rejects several common OpenAI
params with a `400 BadRequest`. We've already hit these and the codebase
works around them:

- ❌ `max_tokens=…`        → use nothing, or `max_completion_tokens=…`
- ❌ `tool_choice="none"`  → just omit `tools` entirely (no tools = no
                             tool calls possible)
- ⚠️ `temperature=…`       → silently ignored on some deployments
- ✅ `response_format={"type":"json_object"}` is supported

When in doubt, mirror the call shape used in `mirror/semantic.py` —
that's the safe pattern.

## Running locally

```bash
source venv/bin/activate
uvicorn main:app --port 8000 --reload     # backend
ngrok http 8000                            # in another shell, expose for Plivo
```

Visit `http://localhost:8000/` for the dashboard.

`.env` needs at minimum:
```
OPENAI_API_KEY=...
OPENAI_API_URL=https://plivo-hack-2026-resource.cognitiveservices.azure.com/openai/v1
OPENAI_MODEL=gpt-5.4-mini
DEEPGRAM_API_KEY=...
PLIVO_AUTH_ID=...
PLIVO_AUTH_TOKEN=...
PUBLIC_HOST=<your-ngrok-host>      # without https://
```

## Demo scenarios that are known to work

| Customer says | Mirror layer | Outcome |
|---|---|---|
| *"Large pepperoni, actually mushroom only, no pepperoni"* | Pattern (contradiction) | Buffer + correction; final order = mushroom only |
| *"Can you check my last order?"* | Pattern (missing_tool_request, handoff) | Canned handoff: "I can transfer you" |
| *"My wife wants pepperoni but I'd like mushroom"* | Semantic | Third-party preference detected; corrects to mushroom |
| *"Book Mumbai Friday, actually Delhi Saturday"* (travel-plivo) | Semantic only (no pattern vocab for travel) | Corrects to Delhi Saturday |

## Hackathon-specific reminders

- `mirror.db` is gitignored — fresh clones start empty.
- `gh auth status` should be logged in; the Apply pipeline depends on it.
- `pytest -q` should always be 71+ passing. If a test fails, fix or
  delete the test — don't disable it silently.
- Don't `git push --force`. Ever. Real PRs depend on the branch history
  being clean.
- Don't merge Mirror-generated PRs without reading the diff. The LLM is
  not infallible; the allowlist + `ast.parse()` are safety nets, not
  guarantees.

## Where to look first when something breaks

| Symptom | First place to look |
|---|---|
| Phone connects but agent is silent | `voice/stream.py` log + Deepgram API key |
| Mirror doesn't fire | `mirror/evaluator.py` (pattern) or `mirror/semantic.py` log |
| Intervention plays but audio overlaps | `mirror/interventions.py` buffer-duration calc |
| `/fixes` page is empty after a call | uvicorn log for `report_hook` line; run `python -m mirror.backfill` |
| Apply button errors | `mirror/applier.py`: `gh auth status`, allowlist check, dirty tree check |
| Dashboard stat cards blank | `dashboard/templates/index.html` JS shape-guards (event bubble issue) |
| Reports show wrong agent | `db.py::_FAILURE_REPORTS_SELECT` JOIN with calls |
