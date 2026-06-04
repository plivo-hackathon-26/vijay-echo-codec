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

## Measured numbers (180 labeled cases; fresh judge runs)

| layer | catch | false alarms |
|---|---|---|
| inline deterministic (µs) | low recall, by design | ~0 (golden set 0%) |
| + inline judge, gated hold (measured, not yet wired to transport) | 81.5% | golden 0% |
| post-call judge (production backstop) | 89.2% | 4.7% nominal / ~3% effective* |
| combined | 90.8% | — |

*one "false positive" is a dataset labeling error the judge got right.

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

Deploy: single service — FastAPI serves API + built frontend
(`render.yaml` blueprint included), or `uvicorn ...backend.app:app`.

## Known limitations (deliberate honesty — do not oversell)

- **Intervention is catch-and-correct (~1s after the bad sentence), not
  pre-speech blocking.** Pre-TTS gating (Hook B) is built and measured but
  not yet wired into the LiveKit transport. Tool execution is flagged, not
  blocked.
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
