# plivo-mirror v5 — limitations & next-level plan

Written after building two fresh domains (Wellspring clinic, Northwind bank)
on top of the core with **zero engine changes** — the generalization test
passing is the context for everything below.

## What the two-agent demo establishes (use this, not a rigged single agent)

- **Wellspring (good clinic agent):** 6/7 calls CLEAN — including a
  medical-advice refusal (which a naive judge over-flags) and a correct fee
  quote — with 1 honest slip (wrong $120 fee) caught. → proves the hard half:
  **near-zero false alarms on a well-behaved agent.**
- **Northwind (rigged bank agent):** vishing transfer (no identity
  verification) caught as `authorization`; unauthorized overdraft-fee waiver
  caught as `authorization`+`commitment`; fabricated Platinum APR caught as
  `price`; a properly-verified transfer and a correct APR readback stay
  CLEAN. → proves catches across the highest-stakes banking failures **plus
  precision** (same `transfer_funds` tool: flagged unverified, clean verified).

Both domains relate instantly (a clinic, a bank), and neither needed a
single core code change — same `ReferenceStore` + `PolicyPack` +
`attach_mirror`.

---

## Limitations & drawbacks (honest)

### Detection / coverage
1. **Live deterministic recall is low by design (6.2%).** The µs layer
   catches structure (tool args, tool-vs-speech, commitment language); it does
   NOT catch spoken factual errors. Those are the grounded judge's job.
2. **Shadow mode has weak real-time factual recall.** With lexicon
   fact-claims off (they mis-attributed numbers — a real fix), a *live shadow*
   call catches spoken factual hallucinations only via the **post-call** judge,
   not in real time. Real-time factual prevention requires intervene mode + the
   pre-TTS gate. This is a coverage seam, not a bug — but it must be stated.
3. **Pre-TTS gate validated live on one agent (Skyline).** The new agents are
   verified deterministically (scripted) + offline; an end-to-end mic call
   through the gate for clinic/bank is not yet run.

### The judge
4. **Single LLM judge → its own variance/hallucinations.** Azure ignores
   temperature; borderline verdicts flip run-to-run. Mitigated (not removed) by
   the measured-precision loop.
5. **Latency:** ~1.3 s p50 / ~2.4 s p95 on assertive turns behind the filler.
6. **Cost:** one LLM call per assertive turn.
7. **Grounding is only as good as the registered facts/policies.** Garbage or
   incomplete facts → weak judging. No completeness check on registration.

### Intervention
8. **Speech is gated; tool EXECUTION is not blocked.** In the bank demo the
   unauthorized $2,000 transfer still *executes* — Mirror catches and corrects
   the speech, but the money moved. For irreversible actions this is the gap
   that matters most.
9. Two intervention mechanisms (Hook A next-turn + pre-TTS gate) — overlapping;
   correction quality still depends on the main LLM obeying the override.

### Product / ops
10. ngrok-off-a-laptop hosting; SQLite single-instance; opt-in auth; no PII
    redaction/retention; English-only lexicons; no multi-tenant isolation; no
    live repetition/loop detector; measured-precision is empty until humans
    review.

### Evaluation honesty
11. **90.8% is a static-set, single-run, attached-claims number — not a
    live-traffic metric.** The real base rate of these failures on production
    calls is unmeasured (the user's own blocking item).

---

## The "1-in-100, hard to show impact" problem — reframed

Two honest answers:

1. **Severity, not frequency.** The bank demo makes this obvious: the failure
   isn't "1% of calls quote a wrong price" — it's "the rare call where the
   agent gets *vished into moving money* or *waives a fee without authority* or
   *discloses a balance without verification*." Those are five-figure-loss /
   regulatory events. Impact = severity × frequency; a 1% rate of
   compliance-grade failures is a large expected loss. Lead with that.
2. **Measure the real rate (Phase 1).** Run shadow over real traffic + the
   post-call judge on every call → produce the actual failure rate AND a stack
   of receipts. Converts "hard to show" into "here is the number and the
   evidence" — and removes the need to rig a prompt to demo value.

---

## Next-level plan (phased)

### Phase 0 — demo-credible now (days)
- Ship the **two-agent demo** as the standard story (good-agent-clean +
  bank-vishing-caught). Done.
- **Stable host** (Render blueprint already in repo) — retire the ngrok
  dependency.
- **Auth on by default** + a basic PII-redaction toggle on stored transcripts.

### Phase 1 — measure the real base rate (the unlock)
- Shadow over a real partner agent's traffic; `MIRROR_AUTO_AUDIT=1` runs the
  judge on every call. Accumulate: real failure rate, per-category breakdown,
  measured precision from the review loop.
- That labeled stream IS the **voice-agent failure-mode dataset** for the
  fine-tune (review loop already tags confirmed/rejected).

### Phase 2 — judge latency & cost
- Drop in a **faster judge (Haiku)** behind the swappable Protocol; measure the
  precision delta via the review loop before committing.
- **Two-stage judge:** cheap fast model first, escalate only the uncertain band
  to the strong model (v3's tiering idea, applied to the judge).
- **Fine-tune a small guard model** on the Phase-1 dataset → ~100 ms, no
  per-call LLM cost, domain-tuned.

### Phase 3 — close the coverage seams & harden the judge
- **Real-time factual recall in shadow too:** run the grounded judge inline
  (flag-only, async) on assertive turns even in shadow, so shadow real-time
  recall matches the gate — or re-enable the LLM claim extractor with the
  disambiguation fixes.
- **Pre-tool-execution block** for irreversible actions (don't move the money):
  gate the tool call, not just the speech. The adapter already buffers tool
  args; this is the highest-value intervention upgrade.
- **Judge guardrails:** hard-ground on known facts, abstain/escalate on low
  confidence, expose **per-policy precision** so one noisy policy doesn't poison
  trust in the rest.

### Phase 4 — productize & scale
- Postgres, multi-tenant isolation, retention/RBAC.
- Locale lexicons (de-English the gate/extractor).
- Live repetition/loop detector + a thin slice of voice-UX metrics.
- **Replay-with-correction:** re-run a previously-violating call against a new
  agent version and show the fix — turns the eval harness into a customer-facing
  "we closed this failure class" report.

---

## One-line positioning

Competitors do post-call eval with LLM judges + rule layers. v5's defensible
wedge is the **grounded receipt** (`spoken` vs `truth` vs `source`) +
**real-time intervention** + **measured-on-your-traffic precision** — verified
generic across clinic and bank with no core changes. The roadmap's center of
gravity is Phase 1 (real base rate) and Phase 3's pre-tool block (stop the
irreversible action, not just the sentence).
