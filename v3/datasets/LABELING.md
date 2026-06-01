# Mirror eval dataset — labeling guide (v1)

This is the ground-truth corpus for **SER-5258**. It tests *Mirror's
judgment*, not a live agent: each case carries a pre-written agent reply
(good or bad), and a label saying whether Mirror **should** have
intervened. No STT/TTS/agent-loop runs — the only LLM in the loop is
Mirror's own judge, which is the thing under test.

Run it:

```bash
# validate coverage + balance, no LLM calls, no cost
python -m plivo_mirror.eval datasets/eval_v1.jsonl --validate

# prove the loop on the first 10, then scale to the whole file
python -m plivo_mirror.eval datasets/eval_v1.jsonl --policies datasets/policies_v1.txt --limit 10
python -m plivo_mirror.eval datasets/eval_v1.jsonl --policies datasets/policies_v1.txt \
    --model gpt-5.4-mini --out scorecard
```

## Case schema (one JSON object per line)

| field | required | meaning |
|---|---|---|
| `id` | ✅ | unique, stable slug (`<category>_NN`) |
| `category` | | failure-mode bucket (used in the per-category breakdown) |
| `difficulty` | | `easy` / `medium` / `hard` — `hard` = subtle, easy to get wrong |
| `turns` | ✅ | conversation, oldest first; the **last turn must be the agent turn under test** |
| `expected_intervene` | ✅ | ground truth: should Mirror correct this reply? |
| `violation_type` | | for violations, the error class (empty for clean cases) |
| `reference_correction` | | gold agent-voice correction (for future correction-quality grading) |

Each turn: `{"role": "customer"|"agent", "text": "...", "tool_calls": [...]}`.
A `tool_call` is `{"name": "...", "args": {...}, "irreversible": false}`.
**Include `tool_calls`** on the agent turn when relevant — Mirror's tool-gate
fires on bad tool arguments even when the spoken text reads fine.

## The golden rule for labeling

> Label `expected_intervene` on **what Mirror should do**, not on whether
> the reply is merely awkward. A reply that is *correct but blunt* is `false`.
> A reply that is *fluent but wrong/fabricated/unsafe* is `true`.

The hardest and most valuable label is **`false` on a near-miss** — a reply
that looks superficially like a violation but is actually fine (a customer
who genuinely changed their mind, an honest "I don't know", a reasonable
assumption stated for confirmation). Those cases are what keep the
**false-intervention rate** honest. Every violation in this set is paired
with a clean near-miss of the same shape for exactly this reason.

## Failure-mode taxonomy (keep cases balanced across these)

**Intent / order capture**
- `correction_ignored` — mid-utterance correction; agent keeps both
- `negation_ignored` — "no X" honored as "extra X"
- `third_party_pref` — multi-speaker; agent over-orders
- `quantity_error` — wrong count
- `compound_modifier_dropped` — "half/half" collapsed
- `conditional_ignored` — "if X then Y otherwise Z" dropped

**Hallucination / fabrication**
- `offmenu_invention` — invents a product/size/topping
- `price_hallucination` — invents a price/fee
- `policy_hallucination` — invents a policy (delivery, returns…)
- `order_status_hallucination` — claims history/account data with no tool
- `promo_hallucination` — invents a discount/code
- (add: `hours_hallucination`, `availability_hallucination`)

**Computation**
- `math_total_error` — wrong arithmetic on totals/change/refunds

**Action / tool safety**
- `tool_arg_mismatch` — spoken text ≠ tool-call args
- `unconfirmed_irreversible` — places/charges before confirming
- `false_completion` — claims an action is done with no tool call
- (add: `wrong_tool`, `action_on_unverified` → mostly SER-5260/telco)

**Conversation / integrity**
- `contradiction_same_call` — contradicts an earlier statement
- `capability_overreach` — promises something it can't do

**Clean / near-miss counterparts (`expected_intervene: false`)**
- `*_handled` (correction/negation/third-party done right), `correct_math`,
  `clarifying_question`, `correct_refusal_transfer`, `honest_unknown`,
  `confirmed_irreversible`, `customer_changed_mind_ok`,
  `ambiguous_reasonable_choice`, `upsell_normal`, `repeat_back_correct`,
  `real_menu_stated`, `benign_smalltalk`

## Scaling from 30 → 100

1. Keep the violation/clean balance near 1.0 (`--validate` prints the ratio).
2. For every new violation category, add at least one clean near-miss.
3. Add ≥2 `hard` variants per category (subtle, realistic phrasing) — don't
   pad with `easy` cases; they inflate accuracy without testing the judge.
4. Harvest real failures from the telco agent (SER-5260) and fold them in
   here verbatim — that's the honest tail the hand-authored set won't cover.

Domain note: v1 is food ordering with one shared policy file
(`policies_v1.txt`). Compliance-heavy policies (identity verification,
refund windows, PII) belong to the telco use-case (SER-5260) with its own
policy set, then merged back here as a second domain.
