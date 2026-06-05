# plivo-mirror — Project Documentation

**A grounded verification and live-intervention layer for LLM voice agents.**

Version: v5 (0.5.1) · Status: feature-complete, wrapped 2026-06-05, awaiting
production pickup · Author: Vijay Krishna S · Repo: `vijay-echo-codec`

---

## Table of contents

1. [The problem](#1-the-problem)
2. [What Mirror does (in one diagram)](#2-what-mirror-does)
3. [Key concepts](#3-key-concepts)
4. [Architecture](#4-architecture)
5. [The detection engine](#5-the-detection-engine)
6. [The grounded LLM judge](#6-the-grounded-llm-judge)
7. [Intervention: stopping the failure](#7-intervention)
8. [Telemetry & the monitoring dashboard](#8-telemetry--the-monitoring-dashboard)
9. [Integration guide](#9-integration-guide)
10. [Configuration reference](#10-configuration-reference)
11. [Evaluation: methodology & results](#11-evaluation)
12. [Example agents](#12-example-agents)
13. [Known limitations & the path to production](#13-known-limitations--the-path-to-production)
14. [Repository layout & project history](#14-repository-layout--project-history)

---

## 1. The problem

LLM voice agents fail in ways that are rare per-call but catastrophic
per-incident:

- **Fabricated facts** — quoting a wrong price, a wrong policy, invented hours.
- **False completions** — "I've cancelled that for you" when no tool ever ran.
- **Unauthorized commitments** — promising refunds/waivers nobody approved.
- **Vishing / prompt injection** — a caller *talks the model into* moving
  money or waiving fees ("I'm a supervisor, code 7-7-3").
- **Ignored context** — confirming onions after the caller said *no* onions.
- **Compliance gaps** — required disclosures never spoken.

Existing tools (Hamming, Coval, Langfuse, Arize, Vapi/Retell built-ins)
detect these **after the call** with an ungrounded LLM-judge *opinion*.
Mirror's thesis: most of these failures can be caught **deterministically,
against the customer's own ground truth, with an auditable receipt — and
many can be stopped before the caller ever hears them or the money moves.**

## 2. What Mirror does

```
                        ┌─────────────────────────────────────────────┐
 caller ──── STT ────►  │                YOUR VOICE AGENT             │
                        │   LLM ──draft──► [pre-TTS GATE] ──► TTS ────┼──► caller
                        │    │                  ▲                     │
                        │    └─tool call──► [TOOL GATE] ──► tool body │
                        └─────────│────────────│─────────────│────────┘
                                  ▼            ▼             ▼
                        ┌─────────────────────────────────────────────┐
                        │            MIRROR  (attach_mirror)          │
                        │  L1 input gate → L2 deterministic diff      │
                        │      → grounded LLM judge (recall layer)    │
                        │  every verdict = {spoken, truth, source}    │
                        └───────────────────┬─────────────────────────┘
                                            ▼  async telemetry (never blocks)
                        ┌─────────────────────────────────────────────┐
                        │   BACKEND (FastAPI+SQLite) + DASHBOARD      │
                        │   live calls · receipts · reviewer ✓/✗ ·    │
                        │   measured precision · post-call audit ·    │
                        │   systemic patterns · audio playback        │
                        └─────────────────────────────────────────────┘
```

Three deployment rungs — *observe → observe live → prevent* — selected per
agent from the dashboard, no code change:

| mode | what happens on a violation | use when |
|---|---|---|
| **shadow** | flagged as `would_have` on the dashboard; post-call judge audits every call | building trust, measuring your real failure rate |
| **shadow + live judge** (`MIRROR_SHADOW_JUDGE=1`) | same, but factual errors flag **during** the call (~1.5 s after the sentence) | live ops monitoring |
| **intervene** | the draft is **held before TTS** and corrected; unauthorized tools are **blocked before execution** | production prevention |

## 3. Key concepts

**The receipt.** Every verdict carries
`{claim_type, spoken_value, truth_value, source}` — e.g.
`{price, "$59.99", "79.99", reference.plan.turbo.price_per_month}`. The
dashboard renders it verbatim. This is the product differentiator: not an
opinion, a diff.

**Three truth sources** (all deterministic, exact-keyed — never vector search):
- `reference.*` — static per-agent facts (menu, prices, policies, hours),
  registered once in the dashboard → `ReferenceStore`.
- `session.*` — facts validated *during this call* (the total a tool
  computed, the address the caller confirmed) → `SessionState`. Only host
  code writes here.
- `tool.*` — the committed tool-call log ("I've cancelled it" is diffed
  against whether `cancel_booking` actually fired and didn't error).

**Authorization separation** (the vishing/prompt-injection defense): a
guarded tool may only fire when an *authorizing fact* exists in session
state — and only host code (after real verification) can write it. The
model never authorizes itself; no phrasing can conjure the fact.

**Arbitration: deterministic wins.** If L2 and the judge both rule on the
same claim, L2 stands and the judge's verdict is suppressed — recorded in
`suppressed_by`, so the suppression itself is auditable.

**Fail-open everywhere.** Judge timeout → draft released. Backend down →
telemetry spooled/dropped, call unaffected. Registry unreachable → attach
still succeeds in shadow. The call always outranks the monitor.

## 4. Architecture

One engine, two deployables, one integration:

```
plivo_mirror_v5/
  engine/                 # pure detection — stdlib-only, offline-capable
    session_state.py      #   per-call validated facts + tool log + snapshots
    reference.py          #   static facts, exact keyed lookup
    claims.py             #   claim extraction (lexicon + optional LLM)
    layers/l1_*.py        #   input-integrity gate
    layers/l2_*.py        #   deterministic diff + 5 policy checks
    arbitration.py        #   deterministic-wins suppression
    gate.py               #   assertiveness gate (which turns pay the judge)
    tool_gate.py          #   pre-execution allow/deny for tools
    policy.py             #   PolicyPack: business rules as code/config
  auditor/                # the grounded LLM judge (+ TwoStageJudge)
  integrations/           # LiveKit adapter, pre-TTS runner, recording
  telemetry/              # OTel-shaped records, sinks (HTTP/threaded/OTel)
  deployables/
    monitoring/           # FastAPI backend + React dashboard
    intervention/         # Hook A (next-turn) + Hook B (pre-TTS gated hold)
```

Design invariants (do not regress):
- The **engine never emits telemetry and never takes actions** — routing is
  the deployables' job, selected by the observer's `mode`.
- L2 diffs against an **immutable state snapshot** with a stable
  `snapshot_id` — every verdict is auditable against exactly the state
  version it saw. Draft evaluations (pre-TTS) leave **no state residue**.
- **Business logic, pricing, policy live in code** (`PolicyPack`), never in
  prompts. Prompts are for tone and NLU only.
- Only L2 is inline-safe (budget 50 ms, asserted in tests; measured p95
  ≈ 0.1 ms). Everything model-in-the-loop is async or gated.
- The adapter imports nothing from livekit at module level — the engine and
  all 185 tests run offline with no keys.

## 5. The detection engine

### L1 — input integrity (a gate, not a detector)

Runs on **user** turns. Two jobs: (1) ASR confidence below threshold marks
the input untrusted, so L2 downgrades mismatches to `info` while the agent
may be answering a *mis-transcribed* question; (2) readback corrections
("no, I said 4 pm") are written into session state. All L1 verdicts are
`info` — audit markers, never alarms.

### L2 — deterministic diff (the workhorse, µs)

For every claim in an agent turn with a structured referent, resolve truth
from one of the three sources and diff (`values_match`: numeric-aware —
"$79.99" == 79.99 — falling back to normalized text). Claims with no
resolvable referent are **outside L2 jurisdiction** → the judge's job.

In parallel, five **policy checks** from the agent's `PolicyPack`:

| check | failure it kills | example rule |
|---|---|---|
| `arg_bindings` | wrong-action-vs-intent | `cancel_service.account_id` must equal `session.account.id` |
| `tool_authorization` | prompt injection / vishing | `cancel_booking(waive_fee=true)` requires `session.auth.fee_waiver_authorized` |
| `commitments` | unauthorized promises | /waive\|full refund/ needs an authorizing fact (negated contexts exempt — a retraction never re-flags) |
| `disclosures` | compliance gaps | "when discussing cancellation, must mention 'effective'"; "say 'recorded' by agent-turn 2" |
| `persona_forbidden` | prompt leakage / drift | "my instructions say…", "as an AI…" |

### Claim extraction (live calls)

Eval fixtures attach claims by hand; live calls extract them. The live
default is deliberately conservative: **action claims only**
(speech-vs-action via host-supplied `action_verbs`), because lexicon
attribution of numbers to reference keys produced real false positives —
factual language is the judge's jurisdiction. An `LLMClaimExtractor`
(constrained to your reference keys, never shown truth values, lexicon
fallback on any failure) is available and is what the eval harness uses.

### The assertiveness gate

Decides which agent turns pay the judge: anything with a number,
commitment language, completion language, a capability assertion, or an
extracted claim is "assertive". Measured: 91.4 % of violation turns pay the
judge; 60.6 % of clean turns do too — that is the recall-vs-cost trade, and
it is deliberate (a false-negative here silently exempts a turn from
judgment). A measured dead end is documented in `gate.py`: skipping
L2-verified-clean claims collapsed violation assertiveness 97 %→44 %
(right value ≠ right in conversational context) — do not retry it.

## 6. The grounded LLM judge

One judge implementation, one grounded-entailment prompt, **three duty
stations**: pre-TTS gating (intervene), live flag-only (shadow +
`MIRROR_SHADOW_JUDGE=1`), and the post-call audit (`MIRROR_AUTO_AUDIT=1`).

What makes it safe(r) than a generic LLM judge:
- **Grounded**: it judges ONLY against the registered facts + policies +
  the agent's own system prompt, with explicit not-violation rules
  (honest "I don't know", scope refusals, paraphrases, courtesies).
- **Abstains when ungrounded**: no facts, no policies, no prompt → it
  refuses to judge rather than invent violations.
- **Burden of proof on the violation**: ambiguous evidence → not a flag.
- **Never trusted with anything irreversible**: arbitration lets L2
  overrule it; tool blocking never depends on it; it fails open.
- **Swappable Protocol** (`judge_turn(turns, idx) → {violation, category,
  reason}`): a fine-tuned small model drops in as a one-line swap.

**TwoStageJudge** (`MIRROR_JUDGE=two_stage`): k concurrent votes from a
cheap model; unanimous verdicts stand (the strong model is never paid);
split votes escalate once to the strong model. Built because Azure
deployments ignore `temperature` and borderline verdicts flip run-to-run
(±3–5 pts between fresh eval runs) — voting turns that variance into a
detectable signal and spends the expensive model only on the uncertain band.

## 7. Intervention

### Hook B — pre-TTS gated hold (prevention)

Sits between the LLM's draft and TTS. Three checks in strict cost order:
L2 (µs, hard hit holds instantly) → assertiveness gate (µs, chitchat
releases at ~0 ms) → grounded judge (≈1.3 s p50, hard timeout, fail-open).

On a hold: the caller hears a filler line; a violation packet (the correct
value + the rule, never restating the wrong value) re-prompts the main LLM;
the candidate is **re-gated** plus pink-elephant-checked (a "correction"
that repeats the wrong price fails); max 2 retries, then a safe handoff
line. **The caller never hears the violation.** Tool-call streams pass
through untouched — actions belong to the ToolGate.

### ToolGate — pre-execution blocking (the action boundary)

`gate.check(name, args, state)` runs **before the tool body**: (1)
authorization separation — a guarded call (e.g. `waive_fee=true`) with no
host-written authorizing fact is denied; (2) argument consistency — args
contradicting validated session facts are denied. Deny returns a reason, a
policy id, and a safe spoken refusal; the blocked call lands in the tool
log as an **error**, so a later "I've processed it" claim diffs dirty.
Deterministic, µs, no model — it works even if every LLM is down.

### Auto-wiring (zero agent code changes)

In intervene mode `attach_mirror` automatically (best-effort, guarded,
verified against livekit-agents 1.5.x):
- routes the agent's default `llm_node` through the pre-TTS gate — a
  host's own `llm_node` override is never clobbered;
- wraps every `@function_tool` named in the policy with the ToolGate,
  schema-identical (the LLM sees the exact same tool signature).

Any failure degrades silently to the documented manual patterns; attach
never fails because of wiring.

### Hook A — next-turn correction (containment fallback)

When the wrong utterance was already spoken (shadow→intervene transitions,
gate misses): a `[CORRECTION: …]` system message + an immediately generated
corrected reply ("Actually — one moment…"). Honest caveat: it depends on
the main LLM obeying the override; an adversarial system prompt can win.
Escalation/warm-handoff delivery is interface-only.

## 8. Telemetry & the monitoring dashboard

**Records** are OTel-shaped (call = trace, turn = span, verdict/action =
span events, plus counters/histograms) and sink-agnostic: in-memory
(tests), HTTP → backend, or real OTLP via the `otel` extra.

**The agent-side pipeline never blocks a call**: records go through a
`ThreadedSink` — bounded queue (default 10 000; full → drop-oldest, counted
and logged), optional JSONL **disk spool** that parks records the backend
rejects and replays them on reconnect.

**The backend** (FastAPI + SQLite/WAL, single service, serves the built
React frontend) ingests batches, write-protects via `MIRROR_API_KEY`,
optional regex PII redaction, Slack-compatible alert webhook, and runs the
post-call judge (auto or on demand).

**The dashboard** (call-ID-keyed, `call_id` == the LiveKit room id):
- live call view: transcript timeline, per-turn verdicts with receipts,
  the signal strip, audio recording playback (`MIRROR_RECORD=1`)
- fleet view: flagged rate, interventions, per-day trends, failure
  categories, agent-version comparison
- **reviewer loop**: ✓/✗ on every flag → live per-detector precision
  measured on *your* traffic (not a benchmark claim)
- **systemic patterns**: the same `{spoken, truth, source}` across N calls
  ("same wrong price in 23 calls since Tuesday's prompt change")
- agent registry: facts/policies/prompt registration, the intervene
  toggle (applies at next call start), the integration snippet

## 9. Integration guide

Full walk-through: `QUICKSTART.md` (5 minutes) and `docs/CONNECT_CLOUD.md`
(laptop / LiveKit Cloud / your infra). The short version:

```bash
pip install "plivo-mirror-v5[agent]"
```

```python
from plivo_mirror_v5.integrations import attach_mirror

# in your LiveKit entrypoint, after ctx.connect():
observer = attach_mirror(
    session,
    room_id=ctx.room.name,            # call_id == LiveKit room id
    backend_url=os.environ["MIRROR_BACKEND_URL"],
    agent_id="support-bot-prod",      # ← matches the dashboard registration
    agent=my_agent,                   # enables dashboard-toggled intervene + auto-wiring
    # optional:
    config=EngineConfig(policy=POLICY),          # local PolicyPack
    action_verbs={"cancel_order": ["cancelled", "canceled"]},
    room=ctx.room, record=True,                  # audio levels + recording
    shadow_judge=my_judge,                       # explicit live shadow judge
)
ctx.add_shutdown_callback(lambda: observer.close())
await session.start(agent=my_agent, room=ctx.room)
```

Config resolution: explicit args > dashboard registration > defaults.
Facts/policies/prompt/mode all come from the registration, so flipping
INTERVENE in the dashboard changes the **next** call with no redeploy.

Events consumed (duck-typed): `conversation_item_added` (both roles),
`function_tools_executed` (tools land in state BEFORE the agent speaks
about them), `close`. Evaluation is scheduled off the event loop and
serialized per call — `add_item` returns in microseconds.

## 10. Configuration reference

**Agent side** (the process running `attach_mirror`):

| var | effect |
|---|---|
| `MIRROR_BACKEND_URL` | dashboard/backend base URL |
| `MIRROR_API_KEY` | sent as `X-API-Key` when the backend enforces one |
| `MIRROR_SHADOW_JUDGE=1` | shadow mode: grounded judge flags factual errors during the call (flag-only, fail-open) |
| `MIRROR_JUDGE=two_stage` | voting judge; `OPENAI_MODEL_FAST` = cheap model, `MIRROR_JUDGE_VOTES` = k (default 3) |
| `MIRROR_RECORD=1` | capture call audio for dashboard playback |
| `MIRROR_TELEMETRY_QUEUE_MAX` | telemetry queue bound (default 10000, drop-oldest) |
| `MIRROR_TELEMETRY_SPOOL` | JSONL path: park + replay telemetry across backend outages |
| `OPENAI_API_KEY/_BASE_URL/_MODEL` | judges + optional LLM claim extractor (Azure quirks handled: no `max_tokens`, no `tool_choice`, `json_object` OK) |

**Backend side** (the dashboard service):

| var | effect |
|---|---|
| `MIRROR_DB` | SQLite path (WAL auto-enabled) |
| `MIRROR_API_KEY` | write-protect ingest/registration/labels |
| `MIRROR_CORS_ORIGINS` | restrict CORS (default `*` for demo) |
| `MIRROR_AUTO_AUDIT=1` | judge every call at call end |
| `MIRROR_ALERT_WEBHOOK` | Slack-compatible webhook on high-severity flags |
| `MIRROR_REDACT_PII=1` | regex redaction (email/SSN/phone/card) before storage |
| `MIRROR_MAX_INGEST_BATCH` | ingest batch cap (default 500) |
| `OPENAI_*` | post-call judge credentials |

## 11. Evaluation

**Methodology.** 180 labeled cases (`v3/datasets/eval_v1.jsonl`,
`eval_v2.jsonl`, `v4/datasets/golden_v1.jsonl`): 81 violations across 28
failure categories + 99 clean calls (the false-alarm budget). Each case
replays through the REAL pipeline — LLM claim extraction (constrained to
reference keys, never shown truth values) → engine → assertiveness gate →
grounded judge. Two harnesses:

- `eval/run_eval.py` — offline, no keys, fixture claims; CI-style
  regression (catch / false-alarm / latency budget).
- `eval/run_v4_set.py` — the full 180-case live harness; LLM results
  cached in `.v4set_cache.json`; `--no-cache` forces a fresh run.

**Final numbers** (fresh `--no-cache` run, 2026-06-05, 373 live LLM calls):

| layer | catch (81 violations) | false alarms (99 clean) |
|---|---|---|
| inline deterministic (µs; p50 0.06 ms, p95 0.10 ms) | 8.6 % — by design | 0 % v1 · 1.6 % v2 · 4.8 % golden |
| pre-TTS gated hold | **80.2 %** | 0 % · 6.2 % · 4.8 % |
| post-call judge | **84.0 %** | 0 % · 4.7 %* · 0 % |
| **combined** | **86.4 %** | — |

*one eval_v2 "false positive" is a dataset labeling error the judge got right.

100 %-caught categories: false_completion (pure L2), negation_ignored 9/9,
compound_modifier_dropped 8/8, correction_ignored 4/4, quantity_error 4/4,
price/policy/promo/hours/order-status hallucinations. Known weak:
conditional_ignored 5/8, unconfirmed_irreversible 1/3, math_total_error
2/4, repetition_loop 0/1.

**Honesty notes.** These are static-set numbers, not live-traffic metrics —
the true production base rate is unmeasured (the planned shadow pilot
measures it). Azure judge variance is ±3–5 pts between fresh runs; an
earlier run measured 81.5/89.2/90.8. v4's baseline on the same sets was
35.4 % catch with 9.5 % false-intervention — v5 roughly doubled catch while
cutting false alarms.

## 12. Example agents

Four domains, **zero core changes between them** — the generalization proof:

| agent | domain | what it demos |
|---|---|---|
| `aurora_agent` | ISP support | the basic wiring; wrong-price + false-completion catches |
| `skyline_flight_agent` | airline | the vishing demo: an over-permissive prompt that waives fees for anyone claiming authority — ToolGate blocks the waiver, commitments flag the promise; fully registry-driven (no local config) |
| `northwind_bank_agent` | banking | unverified $2k transfer (authorization), fee waiver (authorization+commitment), fabricated APR (price); verified transfer stays clean |
| `wellspring_clinic_agent` | clinic | the good-agent test: 6/7 calls clean incl. a medical-advice refusal a naive judge over-flags |

`examples/run_demo.py` drives scripted calls through the engine + backend
with no keys — the product is reviewable without a live agent.

## 13. Known limitations & the path to production

Deliberately honest (also in `docs/ROADMAP.md` / `docs/HANDOFF.md`):

1. **Auto-wiring validated against livekit-agents 1.5.16 + 185 unit tests,
   not yet by live mic calls across all examples** — first task on pickup.
2. **Static-set metrics** — run the shadow pilot on real traffic to get the
   true base rate; the reviewer loop then measures real precision AND
   accumulates the fine-tune dataset.
3. **Platform**: single-tenant, SQLite, auth opt-in, PII redaction opt-in,
   no retention policy, ephemeral DB on the free Render tier. All known,
   scoped in the roadmap (Postgres, RBAC, retention, persistent disk).
4. **English-only lexicons** (gate / extractor / negation guards) — regex
   machinery is generic; word lists need a locale config surface.
5. **Judge cost/latency**: ~1.3 s p50 per assertive turn; two-stage judge
   built but not yet benchmarked live; the end state is a fine-tuned small
   guard model behind the same Protocol (~100 ms, near-zero marginal cost).
6. **Hook A depends on LLM compliance**; warm-handoff is interface-only.

Production pickup order: live-mic validation → real-traffic shadow pilot →
platform hardening → deterministic gap-closers (math totals, repetition
loop, gate-exempt cases) → two-stage live benchmark → guard-model fine-tune.

## 14. Repository layout & project history

```
v1/   hackathon original — archaeology only
v3/   plivo-mirror 0.3.x (PyPI) — three-tier scorer; v5 reuses only its eval sets
v4/   dual-boundary firewall — the defenses v5 ported into L2 policy checks
v5/   THIS project — plivo-mirror-v5 (PyPI)
  plivo_mirror_v5/   engine · auditor · integrations · telemetry · deployables
  examples/          four demo agents + run_demo.py
  eval/              offline + live harnesses, scorecard
  tests/             185 tests, all offline, ~2 s
  docs/              HANDOFF · ROADMAP · CONNECT_CLOUD · architecture diagrams
  DOCUMENTATION.md   this file
  PRODUCTION.md      measured numbers + ops notes
  QUICKSTART.md      5-minute integration
```

Evolution in one line each:
- **v1** proved the demo; **v2** (concept) showed judge-every-turn kills
  voice latency; **v3** shipped the three-tier scorer to PyPI; **v4**
  showed a narrow risk-lexicon starves the judge (35 % catch) but built the
  right deterministic defenses; **v5** kept v4's deterministic floor,
  replaced tiering with the assertiveness-gated grounded judge, added the
  receipts/dashboard/review loop, pre-TTS gating, and pre-execution tool
  blocking — and doubled catch while cutting false alarms.
```
