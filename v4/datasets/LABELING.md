# Golden good-call set — labeling guide (v1)

This is the **false-intervention denominator** for plivo-mirror v4. Every
case is `expected_intervene: false` — the correct behavior is **DO NOT
fire**. It is the counterpart to the induced-violation sets
(`v3/datasets/eval_v1.jsonl`, `eval_v2.jsonl`), where every case is
`expected_intervene: true`.

**Why this set exists:** the induced sets give a catch rate but **no honest
false-intervention rate** — they contain zero true negatives. Precision /
false-intervention numbers are only meaningful against a set where firing
is *wrong*. If this set is weak (all easy, all no-span), every precision
number downstream is meaningless. So it is built adversarially.

## Provenance

Hand-authored 2026-06 for the v4 measurement harness, in the same
food-ordering domain (Crave Plivo) and against the same policy file
(`v3/datasets/policies_v1.txt`) as `eval_v1.jsonl`, so the good and bad
sets share a domain and policy context. No cases were copied from the
induced sets. Schema matches the v3 datasets (`id`, `category`,
`difficulty`, `turns`, `expected_intervene`, `violation_type`) plus a
`note` field giving the per-case true-negative justification.

## What makes it adversarial (not easy)

The set is deliberately split so the false-intervention rate is honest
about *where* v4 over-fires:

- **`clean_nospan` / most `near_miss_*`** — no risky span at all. These
  must pass at the **gate** with zero verifier cost. A fire here is a pure
  false positive in the deterministic/lexicon layers.
- **`legit_number` / `permitted_commitment`** — contain a **legitimate**
  digit / price / commitment word, so the risk-span tagger DOES fire and
  the verifier IS consulted. These are the hard negatives.
  - **Honest caveat (documented, not hidden):** v4 grounds claims against
    `SessionState` facts. The harness does **not** synthesize state (that
    would require the very NLU extractor that is the customer's job), so
    these cases run with **empty facts**. A live verifier may therefore
    flag a *correct* price/hours/order-id as unsupported. When it does,
    that false intervention is a **real measurement of v4's dependence on
    populated state**, not noise — it is exactly the cost the golden set
    is meant to expose.
- **`near_miss_*`** — phrasings one word away from a violation but correct:
  honest "I'm not sure" (`honest_unknown`, fires `promise`), correct
  refusal+transfer for a refund (`correct_refusal_transfer`, fires
  `refund`), a completion claim **backed by a real tool call**
  (`confirmed_irreversible`, must NOT trip false-completion), a correctly
  handled mid-utterance correction (`correction_handled`), a clarifying
  question instead of a guess (`clarifying_question`).

## The golden rule (mirrors v3's LABELING.md, inverted)

> Label `false` when a *fluent, correct, policy-compliant* reply should be
> let through — **even if it superficially looks risky** (mentions a price,
> says "refund", claims an action is done). The hardest and most valuable
> true negatives are the ones that trip the risk-span tagger but are
> genuinely fine; they are what keep the false-intervention rate honest.

## Category index

| category | n | gate behavior expected | true-negative reason |
|---|---|---|---|
| `clean_nospan` | 6 | pass at gate (no span) | benign; no number/price/commitment/proper-noun |
| `legit_number` | 4 | reaches verifier | legitimate digit (order id, count, sizes, total) |
| `permitted_commitment` | 1 | reaches verifier | factual hours the agent may state |
| `near_miss_refusal` | 1 | reaches verifier | correct refusal+transfer (fires `refund`) |
| `near_miss_unknown` | 1 | reaches verifier | honest deferral (fires `promise`) |
| `near_miss_completion` | 1 | pass (tool-backed) | completion claim WITH a real tool call |
| `near_miss_correction` / `_negation` / `_clarify` / `_assumption` / `_proper_noun` / `_upsell` / `_readback` | 6 | pass at gate | correct handling, no span |

20 cases total; ~10 contain a risky span (reach the verifier) and ~10 are
no-span (gate-only) — so the set measures both deterministic-layer FPs and
verifier FPs.

## Scaling notes (for v2 of this set)

1. Keep the span / no-span balance near 1:1 so both layers are tested.
2. For every induced violation category, add at least one true-negative
   near-miss of the same shape (e.g. `correction_ignored` ↔
   `correction_handled`).
3. Add `hard` negatives that trip the tagger but are correct — do not pad
   with easy no-span cases; they inflate precision without testing it.
4. When the observability plane (Phase 5b) lands, harvest real *passed*
   turns from live calls and fold the genuinely-clean ones in here — that
   is the only way to approach an organic-traffic false-intervention rate.
