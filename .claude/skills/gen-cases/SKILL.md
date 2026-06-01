---
description: Generate new hard labeled eval cases for a failure category and append them to the dataset. Usage /gen-cases <category> <count>. Delegates to the eval-case-author subagent, then validates the dataset.
---

# /gen-cases — grow the eval dataset

Add hard, balanced labeled cases to the benchmark.

## Usage
`/gen-cases <category> <count>` — e.g. `/gen-cases price_hallucination 6`
`$ARGUMENTS` = the category (or comma-separated categories) and a count. If the count is omitted, default to 4 per category.

## What to do
1. Delegate to the **eval-case-author** subagent with the category + count. Instruct it to:
   - follow `v3/datasets/LABELING.md`,
   - match the style/domain/menu of `v3/datasets/eval_v2.jsonl`,
   - write `difficulty: hard` cases,
   - **pair every violation with a clean near-miss of the same shape**,
   - append to `v3/datasets/eval_v2.jsonl` with unique `<category>_NN` ids.
2. After it returns, confirm validation passed:
   ```bash
   cd /Users/vijay.krishna/Desktop/vijay-echo-codec/v3
   PYTHONPATH=. python3 -m plivo_mirror.eval datasets/eval_v2.jsonl --validate
   ```
3. Report: how many violation + clean cases were added, the new total, and the new balance ratio.

Goal each time: harder coverage and balance ratio kept near 1.0 — never pad with easy cases just to grow the count.
