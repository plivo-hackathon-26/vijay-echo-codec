---
description: Compare two plivo-mirror eval scorecards (before/after a change) and report the deltas in recall, precision, false-intervention rate, F1, and which failure categories moved. Use after re-running the eval following a fix.
---

# /scorecard-diff — before/after comparison

Compare two scorecard JSON files and show what the change actually did.

## Usage
`/scorecard-diff <before.json> <after.json>`
`$ARGUMENTS` holds the two paths (default to `v3/scorecard_before.json v3/scorecard_after.json` if omitted, or the two most recent `v3/scorecard*.json`).

## What to do
1. Read both JSON files (fields: `counts` {TP,FP,TN,FN}, `recall`, `precision`, `f1`, `false_intervention_rate`, `accuracy`, `latency_ms`, `by_category`, `false_positives`, `false_negatives`).
2. Print a delta table:

   | Metric | Before | After | Δ |
   |---|---|---|---|
   | Recall | … | … | ±… |
   | Precision | … | … | ±… |
   | False-intervention rate | … | … | ±… |
   | F1 / Accuracy | … | … | ±… |
   | Latency p50/p95 | … | … | ±… |

3. **Category movement:** list categories whose recall changed — newly-caught (FN→TP) and newly-missed/regressed (TP→FN), and any new false alarms (TN→FP).
4. **Verdict in one line:** did the change help on balance? Flag it as a regression if recall rose but precision/false-alarm rate got worse.

Keep it to the table + the movement list + the one-line verdict. No filler.
