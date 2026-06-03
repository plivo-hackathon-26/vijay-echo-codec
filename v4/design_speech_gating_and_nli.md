# v4 design note — forecasting-based span gating (#1) + sentence-segmented NLI (#4)

*Status: proposal. Source: deep-research run 2026-06-02 (`safety nets for voice
agents`, directions #1 and #4), wired against the v4 code as of this commit.*

This note turns two research directions into concrete, code-level changes against
the **real** v4 speech path:

- **Direction #1 — forecasting-based span gating.** Decide a span is risky from
  the *prefix*, and gate/cover it before it is voiced — instead of buffering the
  whole reply, paying the verifier round-trip, and only then speaking.
- **Direction #4 — sentence-segmented NLI.** Run the semantic (NLI) recall tier
  at *sentence* granularity (SummaC's finding) instead of scoring the whole reply
  as one hypothesis.

Both are framed against the locked contracts (`contracts.py`) and the constraint
that **business logic stays in code, prompts do NLU only**. Neither requires
white-box model access (the research's strongest hallucination signals — hidden-
state probes, semantic-entropy probes — need logprobs the LiveKit+Azure stack
does not expose, so they stay in the FUTURE column; see `CLAUDE.md`).

Citations: SummaC (Laban et al., TACL 2022, arXiv 2111.09525); StreamGuard
"Predict, Don't React" (arXiv 2604.03962); SCM (arXiv 2506.09996); Kelp (arXiv
2510.09694); NeMo Guardrails streaming
(developer.nvidia.com/blog/stream-smarter-and-safer).

---

## 0. Where we are today (the baseline these changes move)

From the latest live NLI scorecard (`scorecard_eval_v2_nli.json`,
gpt-5.4-mini + DeBERTa-large NLI @ 0.55, eval_v2 = 65 violations / golden = 21):

| metric | value | what it means |
|---|---|---|
| catch rate (induced) | **0.615** (40/65) | recall on induced violations |
| missed_at_gate | **19** | never reached the verifier — the lexicon/NLI **recall ceiling** |
| missed_at_verifier | 6 | flagged but the judge said "supported" |
| false-intervention (golden) | **0.143** (3/21) | precision on good calls |
| NLI tier latency / committal clean turn | **p50 900 ms / p95 1260 ms** | the only tier that runs on clean turns |
| clean-turn added compute (with NLI) | **p50 921 ms / p95 2531 ms** | pure overhead on a turn that passes |
| time-to-first-audio on flagged turns | **p50 1523 ms / p95 5324 ms** | the deflection filler is NOT reaching TTS until *after* review |

Two problems jump out, and they map one-to-one onto the two directions:

1. **The recall ceiling is at the gate (19 misses), and it's worst exactly where
   sentence granularity matters.** Per category: `negation_ignored` 8/8 (NLI is
   great at single-clause negation), but `conditional_ignored` **1/7** and
   `compound_modifier_dropped` **4/7**. Those are multi-clause replies where the
   violating clause is *diluted* by the rest of the sentence. → **Direction #4.**

2. **First audio on a flagged turn is p50 1.5 s / p95 5.3 s — the verifier round
   trip is paid *before* any sound.** The deflection filler was designed to cover
   that latency, but structurally it can't: `review_turn` (incl. the verifier
   call) fully completes before `intervene_stream` yields the filler. → **Direction
   #1.**

---

## 1. The current speech path (so the changes are unambiguous)

```
adapter.llm_node()                              supervised_agent.py:142
  ├─ inject read-only state grounding           :159
  ├─ run agent LLM, BUFFER full reply           :176-179   ← whole reply, then act
  ├─ persona guard                              :208
  ├─ [speculative mode] release + review off-path :232-262
  └─ review_turn(ctx)  ── SYNCHRONOUS ──────────:267
        └─ runtime/loop.review_turn             loop.py:14
             ├─ speech_guard.inspect(ctx)       speech.py:152
             │    1. deterministic block?       :155
             │    2. lexical risk spans?  tag_risk_spans  :169
             │         └─ none → SEMANTIC tier (NLI)  :174   ← Direction #4 lives here
             │    3. confidence gate (inert: no logprobs) :188
             │    4. grounded verifier (the expensive call) :216
             └─ action_guard.inspect(ctx)
  └─ if intervened: intervene_stream(...)        :295  → engine.stream_intervention
        ├─ yield deflection_filler(verdict)      engine.py:156   ← spoken FIRST...
        └─ await run_intervention(...)           engine.py:158   ← ...but only after review already returned
```

The NLI tier (`speech.py:130-150` → `semantic.py:153-186`) and the deflection
timing (`engine.py:138-173`) are the two seams we touch.

---

## 2. Direction #4 — sentence-segmented NLI

### 2.1 The finding, in one paragraph

SummaC's central result: **NLI only works for inconsistency detection at
sentence granularity.** Earlier NLI-based fact-checkers underperformed because of
a *granularity mismatch* — NLI models are trained on sentence pairs, but they were
being asked to score a whole document/summary as one hypothesis. The fix:
segment into sentences, score each (premise-sentence, hypothesis-sentence) pair,
and aggregate. That single change took SummaC to SOTA (74.4% balanced accuracy,
+5 points).

### 2.2 What v4 does today (the mismatch we have)

`SpeechGuard._semantic_contradiction` (`speech.py:130-150`) builds:

```python
premises  = [context.customer_text] + _relevant_facts(reply, state, cap=2)
hypothesis = reply              # <-- the WHOLE reply, possibly multi-sentence
return self._semantic.contradicts_any(premises, hypothesis).contradiction
```

and `NLICrossEncoderSignal.contradicts_any` (`semantic.py:153-186`) scores each
premise against that one whole-reply hypothesis in a single batched pass,
truncating at `max_length=256`.

This is *exactly* the document-level mistake SummaC warns about:

- **Dilution → missed recall.** "I've cancelled the late-night order; we're open
  until 9, so it'll go out tomorrow at 8am." The contradicting clause ("open
  until 9" vs a 10pm fact, or acting on the wrong branch of a conditional) is one
  clause inside a fluent multi-clause reply. Scored as one hypothesis, the
  contradiction signal is averaged down below 0.55. This is why
  `conditional_ignored` is 1/7 and `compound_modifier_dropped` is 4/7.
- **Truncation → silently dropped evidence.** A reply longer than 256 tokens is
  cut; the offending tail clause may never be scored at all.
- **Benign-clause noise → occasional FP.** The trailing "Is there anything else?"
  or a greeting is fed into the same hypothesis blob; it can nudge the score
  either way. (`_reply_is_committal` already drops trailing questions at the
  *whole-reply* level, but not per-sentence inside the hypothesis.)

### 2.3 The change

**Segment the hypothesis (reply) into declarative sentences, score the full
premise × reply-sentence cross-product in one batched pass, and max-pool.** Flag
on the single highest-contradiction (premise, sentence) pair — and report that
*sentence* as the flagged span.

`NLICrossEncoderSignal` gains a sentence-aware path (reusing the splitter already
in `speech.py`):

```python
# semantic.py
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

def _declarative_sentences(text: str) -> list[str]:
    sents = [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]
    decl = [s for s in sents if not s.endswith("?")]   # drop questions
    return decl or sents                                # fall back if all questions

def contradicts_any(self, premises, hypothesis):
    prems = [p.strip() for p in (premises or []) if p and p.strip()]
    hyps  = _declarative_sentences(hypothesis)          # SummaC: hypothesis = sentences
    if not prems or not hyps or not self._ensure_loaded():
        return SemanticResult(contradiction=False, hypothesis=(hypothesis or "").strip())

    import torch
    # full cross-product, ONE batched forward pass
    pairs = [(p, h) for p in prems for h in hyps]        # |prems| × |sentences|
    with torch.no_grad():
        inputs = self._tok([p for p, _ in pairs], [h for _, h in pairs],
                           truncation=True, max_length=self.max_length,
                           padding=True, return_tensors="pt")
        scores = torch.softmax(self._model(**inputs).logits, dim=-1)[:, self._contra_idx]
        best = int(torch.argmax(scores)); score = float(scores[best])
    bp, bh = pairs[best]
    return SemanticResult(contradiction=score >= self.threshold, score=score,
                          premise=bp, hypothesis=bh)     # bh = the offending SENTENCE
```

`_semantic_contradiction` is unchanged in shape — it still hands all premises +
the reply to `contradicts_any` in one call. The segmentation is internal to the
signal, so `NoSemanticSignal` and any custom `SemanticSignal` are unaffected
(`SemanticResult.hypothesis` simply becomes the offending sentence rather than the
whole reply).

### 2.4 The downstream payoff (this is the part that compounds)

Today, when the semantic tier fires, the guard synthesizes a **whole-reply** span
(`speech.py:176`):

```python
spans = [RiskSpan(text=reply, kind="semantic", start=0, end=len(reply))]
```

With sentence-level NLI, `SemanticResult.hypothesis` is the *specific contradicting
sentence*. Flag **that** as the span:

```python
res = self._semantic.contradicts_any(premises, reply)   # return the result, not just .contradiction
if res.contradiction:
    span_text = res.hypothesis or reply
    start = reply.find(span_text)
    spans = [RiskSpan(text=span_text, kind="semantic",
                      start=max(start, 0), end=max(start, 0) + len(span_text))]
```

That precise span then flows through the whole intervention machinery and makes
*all of it* sharper:

- **`GroundingEvidence.flagged_spans`** (`speech.py:200`, `verifier/base.py`) →
  the verifier is pointed at the exact clause instead of "(whole reply)". Cleaner
  judge prompt, better precision, fewer `missed_at_verifier`.
- **Pink-elephant echo check** (`engine._reverify` :86, `packet.echoes`) → the
  no-echo check is scoped to the offending sentence, so regeneration is only
  forced to avoid *that* clause, not the whole (mostly fine) reply.
- **`correction_for_spans`** (`correction.py:31`) → still picks the
  `"semantic"` agent-voice line, unchanged, but now provably about one clause.

### 2.5 Cost & honesty

- **Latency.** More pairs per turn (`|prems| × |sentences|`, typically 3 × 1–3 =
  3–9 short sequences vs today's 3 longer ones), but still **one** batched forward
  pass. Per-sequence cost *drops* (short sentences never hit the 256-token
  truncation), so net latency is ≈ flat — measure it: the eval already isolates
  this as `nli_tier_ms_per_turn` (today p50 900 ms / p95 1260 ms). Watch that it
  does not regress.
- **Precision / verifier load.** Max-pool over more pairs is recall-oriented and
  can route a few more turns to the verifier (watch `verifier_hit_rate`, today
  0.54). That is acceptable *because the verifier is the precision backstop* — the
  NLI tier is recall-only by design (`semantic.py` docstring). If verifier-hit
  climbs too far, the SummaC-Conv reduction (a learned/thresholded histogram over
  the score distribution instead of a raw max) is the documented next step;
  raising `--nli-threshold` is the cheap lever.
- **Stays generic.** Sentence splitting and max-pooling carry no vertical
  vocabulary — consistent with the "no domain coupling" rule.

---

## 3. Direction #1 — forecasting-based span gating

### 3.1 The finding, in one paragraph

The streaming-guardrails wave (SCM early-stop, Kelp per-token scoring, StreamGuard
*forecasting*) reframes safety from "classify the finished output" to "decide on a
**partial prefix** and intervene before the rest is emitted." StreamGuard's
specific move — *predict the expected risk of likely continuations from the
current prefix* — is the right primitive for us, because Mirror must **buffer and
substitute a grounded correction**, not merely halt. The shared motivation across
all of them (and NVIDIA/AWS docs) is the **exposure window**: a full-sequence
checker forces a lose-lose choice — hold the whole response (latency) or stream it
before the verdict (risky). The fix is to gate the *risky span*, not the whole
turn.

### 3.2 The honest mapping to our architecture

`CLAUDE.md` is explicit and we keep it that way: the LiveKit adapter **buffers the
full reply** before yielding, because LiveKit buffers at the `llm_node` boundary
and there is no clean hook to suppress a sub-span once TTS has begun. True
token-streaming-until-a-risky-span (early-release) is a **FUTURE** capability. So
forecasting maps onto a *spectrum* of three designs, increasing in aggressiveness
and in how much of LiveKit they need. We ship the first now, build toward the
third.

```
                       buffer→review→speak (TODAY)
  design A  ── speculative verifier dispatch (overlap verifier w/ buffering)   ← ship now, no precision cost
  design B  ── prefix-forecast early deflection (filler ∥ verifier, gated)     ← ship now, small precision cost
  design C  ── true early-release span gating (release clean prefix, hold span) ← FUTURE, needs pre-TTS hook
                       grounding-before-speaking (ENDGAME)
```

### 3.3 Design A — speculative verifier dispatch (ship now)

**Problem it fixes:** on a flagged turn, `review_ms` *includes* the full verifier
round-trip, and first audio (the deflection filler) only comes after that — p50
1.5 s. The verifier is an LLM call (~1–3 s); nothing overlaps it.

**The move:** the lexical tagger is ~0 ms and already tells us a span is risky the
moment it appears. So during the buffering loop (`supervised_agent.py:176-179`),
the instant the *partial* text contains a risky span, **dispatch the verifier
speculatively** against the prefix-through-that-span + the (already-known) state
facts — overlapping the verifier round-trip with the tail of generation and the
grounding work. When buffering completes, if no *new* risky span appeared in the
tail, reuse the in-flight result instead of starting a fresh call.

```python
# supervised_agent.py — inside the buffering loop
import asyncio
verify_task = None
async for chunk in default_stream:
    buffered.append(chunk)
    _accumulate(chunk, text_parts, tool_intents)
    if verify_task is None and not tool_intents:
        partial = "".join(text_parts)
        spans = tag_risk_spans(partial)               # ~0 ms
        if spans:
            verify_task = asyncio.create_task(
                self._firewall.speech_guard.verify_spans(partial, spans, self._state,
                                                          self._last_customer_text))
# after buffering: if the full reply added no new risky span, await verify_task;
# else discard it and run the normal synchronous review on full_text.
```

This needs a small `SpeechGuard.verify_spans(...)` helper that factors out steps 3–4
of `inspect` (build `GroundingEvidence`, call the verifier) so it can run on a
prefix. **No precision cost** (we still verify before voicing; we only *start*
earlier), and it directly attacks the 1.5 s first-audio number by hiding the
verifier behind the tail of token generation. Honest limit: replies are short, so
the overlap saves the *tail-generation* time, not the whole verifier time — design
B is what removes the rest.

### 3.4 Design B — prefix-forecast early deflection (ship now, gated)

**The move:** decouple the deflection filler from the verdict. The filler
(`correction_for_spans`) needs only the *span kind*, not the verifier result. So
the instant a **high-liability** span (a `commitment` word — refund/guarantee/
approved) appears in the prefix, begin speaking the agent-voice filler *in parallel
with* the verifier, exactly as `engine.stream_intervention` was designed to (filler
first, answer second) — but triggered by the *forecast* (risky span present) rather
than the completed verdict.

- If the verifier returns **unsupported** → the filler has covered the round-trip;
  follow with the grounded answer. **First audio drops from ~1.5 s to ~filler
  latency (≈0).**
- If the verifier returns **supported** → we spoke one unnecessary "let me confirm
  that" beat. That is the precision cost.

**Why it must be gated, with numbers:** `lexicon_fire_rate` is 0.333 and many of
those spans are *legitimate* (a real `$40` that is in FACTS). Pre-emitting a filler
on every risky span would add a "let me double-check" to ~1/3 of turns — too
aggressive. So gate design B to:

1. **`commitment`-kind spans only** (the highest-liability, lowest-base-rate
   class — these are the unauthorized-promise failures, where a half-second of
   caution is *appropriate* even when grounded), and/or
2. **the `speculative=True` path only** (`supervised_agent.py:232`), where we have
   already accepted "corrected-after" semantics.

This is the literal StreamGuard "predict, don't react" trade — act on the prefix
forecast, accept a bounded false-positive cost — scoped to the one span class
where the cost is benign.

### 3.5 Design C — true early-release span gating (FUTURE)

The endgame, and what forecasting really unlocks. When LiveKit exposes a pre-TTS
hook (or we move gating below `llm_node`): stream the **clean prefix** to TTS
immediately; the moment the prefix *forecasts* a risky span, **hold the buffer at
the span boundary**, run `verify_spans` on just that span, and either release it or
splice in the grounded correction. This is the only design with **zero added
first-audio latency on clean turns** (it removes the full-reply-buffering caveat in
`CLAUDE.md`), and the forecasting model is what makes it safe — you must predict
the upcoming span is risky *in time to hold it* before its audio is emitted. NeMo
Guardrails' streaming mode (`chunk_size`/`context_size` sliding window) is the
closest shipping reference design for the chunk-boundary bookkeeping.

Do **not** claim "zero added audible latency" until design C lands — same rule as
`CLAUDE.md`.

### 3.6 The forecasting signal itself

Designs A/B route on the existing ~0 ms lexical tagger applied to the *prefix* —
no new model. The genuine "forecast" upgrade (a tiny head that predicts "this
reply will assert a checkable fact/commitment" from the prefix) only pays off under
design C, where you must decide before seeing the rest of the reply. It slots
behind the existing `ConfidenceSignal` interface (`signal.py`) exactly like the
white-box probes do — a documented FUTURE swap, not built now.

---

## 4. How we measure each change (no vibes)

The eval (`eval.py`) already emits everything needed; run before/after with
`/scorecard-diff`:

| change | metric that should move | direction | guardrail metric (must NOT worsen) |
|---|---|---|---|
| #4 sentence NLI | `catch_rate`, esp. `conditional_ignored` / `compound_modifier_dropped` fire-rate | ↑ | `false_intervention_rate` (golden) flat; `nli_tier_ms_per_turn` flat |
| #4 precise span | `missed_at_verifier` | ↓ | — |
| #1 design A | `time_to_first_audio_flagged` p50 | ↓ | `catch_rate` unchanged (no precision change) |
| #1 design B | `time_to_first_audio_flagged` p50 → ≈ filler | ↓↓ | `false_intervention_rate` rise bounded to commitment turns |

`_TimingSemantic` (`eval.py:179`) isolates the NLI tier so #4's latency claim is
falsifiable; the `time_to_first_audio_flagged` / `time_to_corrected_answer_flagged`
buckets isolate #1's. Use `--mode deterministic` (oracle verifier) to read the pure
recall lift of #4 at perfect precision first, then `--mode live` for the real
precision/latency picture.

---

## 5. Sequencing & risk

1. **#4 sentence-segmented NLI** — *do first.* Smallest change (internal to
   `NLICrossEncoderSignal`), directly lifts the worst recall categories, and makes
   every downstream span more precise. Low risk: NLI stays recall-only, verifier
   is still the precision gate. Gate behind the existing `--nli` flag; default off.
2. **#4 precise span synthesis** — same PR or immediately after; pure precision
   win, touches `speech.py:174-176` + threads `SemanticResult` through.
3. **#1 design A (speculative dispatch)** — next; needs a `verify_spans` refactor
   of `SpeechGuard.inspect` steps 3–4 and `asyncio` in the adapter. No behavior
   change, only timing. Keep it behind a flag and verify `catch_rate` is identical.
4. **#1 design B (early deflection)** — opt-in, commitment-spans-only, document the
   precision trade like the speculative-NLI path is documented.
5. **#1 design C (early-release)** — FUTURE; gated on a LiveKit pre-TTS hook.

Every step keeps the package importable and tests green (working rule), keeps
business logic in code, and adds nothing outside the six target failure modes.
