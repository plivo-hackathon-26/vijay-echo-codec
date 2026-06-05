# plivo-mirror v5 — production notes

One-line: a grounded verification layer for LLM voice agents — every claim
the agent speaks is diffed against the customer's own facts, state and tool
log, with a `{spoken, truth, source}` receipt, optional live in-call
self-correction, and a post-call judge backstop.

## What is unique (market-verified, June 2026)

Researched against Hamming, Coval, Roark, Cekura, Langfuse, Arize Phoenix,
Vapi and Retell built-ins:

1. **Deterministic grounded receipts** — competitors' "hallucination
   detection" is an ungrounded LLM-judge opinion. Mirror diffs spoken claims
   against the customer's own ground truth and shows the receipt. *Verified
   absent in all eight.*
2. **Live in-call intervention** — flag → the agent interrupts itself,
   retracts, and states the correct value. Everyone else is post-call or
   alert-only. *Verified absent in all eight.*
3. **Measured production precision** — every flag carries reviewer ✓/✗;
   the fleet page shows live precision computed on YOUR traffic, per
   detector. Competitors publish static benchmark claims. *Verified absent.*
4. **Receipts export** — per-call audit-grade evidence packet (violations +
   truth sources + reviews + interventions) for compliance teams.
5. Cross-call **systemic patterns with receipts** ("same wrong price in
   23 calls since Tuesday's prompt change — here are the calls").

Alert webhooks exist too — table stakes, but our payload carries the
grounded receipt, not a judge guess.

## Measured numbers (180 labeled cases; fresh `--no-cache` run, 2026-06-05)

All-set weighted (81 violations / 99 clean across eval_v1 + eval_v2 +
golden_v1); per-set splits in `eval/scorecard_v4set.json`:

| layer | catch | false alarms |
|---|---|---|
| inline deterministic (µs) | 7/81 (8.6%) — low recall, by design | 0% eval_v1 · 1.6% eval_v2 · 4.8% golden (1 case; extractor variance) |
| + inline judge, gated hold | 65/81 (**80.2%**) — eval_v2 alone 81.5% | 0% eval_v1 · 6.2% eval_v2 · 4.8% golden |
| post-call judge (production backstop) | 68/81 (**84.0%**) — eval_v2 alone 86.2% | 0% eval_v1 · 4.7%* eval_v2 · 0% golden |
| combined | 70/81 (**86.4%**) | — |

*one "false positive" is a dataset labeling error the judge got right.

An earlier (partially cached) run measured 81.5 / 89.2 / 90.8 — the ±3-5pt
spread between runs is exactly the Azure no-temperature judge variance
described below; the numbers above are the reproducible fresh-run floor,
not the best run.

Dataset composition (the single number above hides it): 180 cases =
eval_v1 + eval_v2 + golden_v1 → **81 labeled violations, 99 clean** calls;
truth split mirrors the architecture (numeric facts → L2 ReferenceStore,
prose facts ground the judge). These are static-set, attached-claims
numbers, not live-traffic metrics. Reproduce fresh (live judge calls, costs
API credits) with:

    venv/bin/python v5/eval/run_v4_set.py --no-cache

Judge verdicts vary run-to-run on Azure (no temperature control) — expect
a few points of movement between fresh runs; `MIRROR_JUDGE=two_stage`
exists precisely to damp that variance.

## Production configuration (all env vars, all optional)

| var | effect |
|---|---|
| `MIRROR_DB` | SQLite file path (WAL enabled automatically) |
| `MIRROR_API_KEY` | write-protect ingest/registration/labels (X-API-Key) |
| `MIRROR_CORS_ORIGINS` | restrict CORS (default `*` for demo) |
| `MIRROR_ALERT_WEBHOOK` | Slack-compatible webhook on high-severity flags + interventions |
| `MIRROR_AUTO_AUDIT=1` | judge every call automatically at call end |
| `MIRROR_MAX_INGEST_BATCH` | ingest batch cap (default 500) |
| `OPENAI_API_KEY/_BASE_URL/_MODEL` | judge + claim-extractor credentials |
| `MIRROR_SHADOW_JUDGE=1` | shadow-mode inline judge, FLAG-ONLY: factual errors surface as `would_have` DURING the call, not only post-call (assertive agent turns only; fail-open; ~1 judge call per assertive turn) |
| `MIRROR_JUDGE=two_stage` | self-consistency judge: k fast votes (concurrent), unanimous wins, a split escalates once to the strong model — mitigates Azure's no-temperature verdict flip |
| `OPENAI_MODEL_FAST` / `MIRROR_JUDGE_VOTES` | the two-stage fast model + vote count (default 3) |
| `MIRROR_TELEMETRY_QUEUE_MAX` | bound on the agent-side telemetry queue (default 10000; full → drop-oldest, counted) |
| `MIRROR_TELEMETRY_SPOOL` | JSONL spool path: records the backend rejects are parked and replayed on reconnect instead of dropped |

Deploy: single service — FastAPI serves API + built frontend
(`render.yaml` blueprint included), or `uvicorn ...backend.app:app`.

## Known limitations (deliberate honesty — do not oversell)

- **Intervene mode now auto-wires both boundaries at attach time**
  (best-effort, guarded; an unrecognized SDK shape degrades to the
  documented manual patterns):
  - *speech*: the pre-TTS gate (Hook B) routes through the agent's default
    `llm_node` automatically — a host's own `llm_node` override is never
    clobbered;
  - *action*: tools named in the policy's `tool_authorization`/
    `arg_bindings` are wrapped so `ToolGate.check` runs BEFORE the side
    effect — the unauthorized $2,000 transfer is **blocked**, not just
    corrected after the money moved. Blocked calls land in the tool log as
    errors, so a later "I've done it" claim diffs dirty.
  Live end-to-end mic validation of the auto-wiring across agents is still
  pending (validated against livekit-agents 1.5.x surface + unit tests).
- **English-only lexicons** in the claim extractor / assertiveness gate /
  L1 correction markers — non-English agents degrade to judge-only
  detection. Locale lexicon injection is the designed fix (regex machinery
  is already generic; word lists need a config surface).
- **SQLite** — right up to ~tens of thousands of calls and a single
  backend instance; beyond that, Postgres migration (schema is portable).
- **No PII redaction yet** — transcripts stored verbatim; deploy behind
  your access controls. Retention policy not implemented.
- Judge precision varies run-to-run (no temperature control on Azure
  deployments) — which is exactly why the live measured-precision loop
  exists.
- Warm-handoff delivery (post-escalation context transfer) is interface-only.
