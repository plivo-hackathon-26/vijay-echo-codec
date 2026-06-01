---
description: Run the plivo-mirror eval harness over a labeled dataset and summarize the scorecard — catch rate, false-intervention rate, precision, latency, and the false-negative / false-positive lists. Use to benchmark Mirror or to measure the effect of a change.
---

# /eval — run the Mirror benchmark

Run the eval harness and report the result clearly.

## Default command (when `$ARGUMENTS` is empty)
Run the full v2 set, writing scorecard artifacts:

```bash
cd /Users/vijay.krishna/Desktop/vijay-echo-codec
[ -f venv/bin/activate ] && source venv/bin/activate
set -a && . ./.env && set +a
cd v3
PYTHONPATH=. python3 -m plivo_mirror.eval datasets/eval_v2.jsonl \
  --policies datasets/policies_v2.txt --model "${OPENAI_MODEL}" --out scorecard
```

## With arguments
`$ARGUMENTS` is appended to the command, so callers can override anything:
- `/eval --limit 10` → smoke-test the first 10 cases
- `/eval datasets/eval_v2.jsonl --policies datasets/policies_v3.txt --out scorecard_after` → measure a fix
- `/eval --validate` → check dataset coverage/balance with no API spend

If `$ARGUMENTS` names a dataset/policies/out, use them; otherwise fall back to the defaults above. Always keep `PYTHONPATH=.`, load `.env`, and use `${OPENAI_MODEL}` (the Azure gpt-5.4-mini deployment — the full gpt-5.4 times out).

## After it runs
Summarize, don't dump:
1. Headline — recall, false-intervention rate, precision, F1, latency p50/p95.
2. The weak families (recall by category, lowest first).
3. The false-negative and false-positive ids with the judge's one-line reason.
4. If a previous scorecard exists, note the delta (or suggest `/scorecard-diff`).
