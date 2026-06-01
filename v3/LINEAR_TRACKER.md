# Plivo-Mirror — Linear Issue Pack

Paste-ready issues for the **Plivo-Mirror** milestone. Each issue has a
title, description, suggested labels, and a status. Grouped by phase so
the timeline reads cleanly.

> **Linear target (for the agent after restart):**
> - Org/team: plivo-cx
> - Project: intern-projects (`a8fc410deaa7`)
> - Milestone: `3913e7e0-56ec-4b5e-aa7b-2b67d5ef0dd6`
> - URL: https://linear.app/plivo-cx/project/intern-projects-a8fc410deaa7/overview#milestone-3913e7e0-56ec-4b5e-aa7b-2b67d5ef0dd6
>
> **Decisions:**
> - Assign ALL issues to vijay.krishna.
> - Create the 3 infra/ops items as real issues (label `blocked-external`).
> - Manager-insightful descriptions: include WHY it matters, not just what.
> - Add comments where there's useful extra context (blockers, dependencies).

Legend for status → Linear column:
- ✅ Done
- 🔵 In Progress
- ⚪ Backlog / Todo

---

## 📄 PROJECT NOTE (paste into the milestone's Overview / a Linear Document)

**Plivo-Mirror — real-time voice-agent supervisor**

plivo-mirror is a Python library (`pip install plivo-mirror`, currently
v0.3.0 on PyPI) that supervises voice AI agents in real time. It sits
between an agent's LLM and its text-to-speech, reads what the agent is
about to say, scores it against plain-English policies using a
three-tier judge, and substitutes a corrected reply before the customer
hears the bad one.

**Architecture — three-tier scoring (MirrorJudge):**
- Tier 0 — deterministic checks (regex / illegal tool-args), ~0 ms
- Tier 1 — HF DeBERTa zero-shot NLI classifier, ~500 ms, cheap pre-filter
- Tier 2 — LLM judge (Azure / OpenAI / HF / Atla), ~1.5–5 s, only runs on
  the uncertain middle band (0.2 ≤ P ≤ 0.85)

**v0.3.0 headline:** customer integration collapsed from ~1000 lines of
hand-rolled glue to ~5 lines via `Supervisor.from_env()` +
`SupervisedAgent`.

**Repo:** `vijay-echo-codec/v3/`
**PyPI:** https://pypi.org/project/plivo-mirror/0.3.0/

---

## ✅ DONE — v0.3.0 shipped

### Issue: Ship plivo-mirror v0.3.0 to PyPI
**Status:** ✅ Done
**Labels:** release, milestone
**Description:**
Published plivo-mirror 0.3.0 to production PyPI (wheel + sdist, twine
check passed, clean-venv install verified). 196 unit tests passing.
Headline of the release: customer integration reduced to ~5 lines.
Link: https://pypi.org/project/plivo-mirror/0.3.0/

---

### Issue: Build three-tier scoring architecture (MirrorJudge)
**Status:** ✅ Done
**Labels:** core, architecture
**Description:**
Implemented the MirrorJudge orchestrator combining:
- Tier 0: deterministic checks
- Tier 1: HF DeBERTa zero-shot NLI classifier (pre-filter)
- Tier 2: LLM judge
Escalation logic: Tier 1 returns P(violation); <0.2 = pass, >0.85 =
intervene, 0.2–0.85 = escalate to Tier 2. Keeps the expensive LLM judge
off the hot path for most turns.

---

### Issue: Add Supervisor.from_env() auto-detection
**Status:** ✅ Done
**Labels:** core, dx
**Description:**
One-line supervisor setup. Auto-detects the best Tier-2 judge from env
vars in priority order (Atla → Azure → OpenAI → HF). Override knobs:
`MIRROR_TIER2=azure|openai|hf|atla|none`, `MIRROR_DISABLE_TIER1=1`.
12 unit tests covering priority order + overrides.

---

### Issue: Ship LiveKit adapter (SupervisedAgent)
**Status:** ✅ Done
**Labels:** adapter, livekit
**Description:**
`plivo_mirror.adapters.livekit.SupervisedAgent` — drop-in replacement
for LiveKit's Agent. Overrides llm_node to buffer the LLM stream, score
it, and yield original-or-correction. Handles on_enter/on_exit lifecycle,
chat-context extraction across LiveKit v1.x shapes, cooldown for
preemptive generation, and sticky intent-note injection. Installable via
`pip install "plivo-mirror[livekit]"`.

---

### Issue: Built-in Tier-2 judges (Azure / OpenAI-compatible / HF / Atla)
**Status:** ✅ Done
**Labels:** core, judges
**Description:**
Four built-in judges sharing one Azure-content-filter-safe JUDGE_PROMPT
that forces agent-voice corrections and concrete order summaries:
AzureOpenAIJudge, OpenAICompatibleJudge, HuggingFaceLLMJudge,
AtlaSeleneJudge. Customers no longer hand-write a judge.

---

### Issue: Public text-quality filters + agent-voice corrections
**Status:** ✅ Done
**Labels:** core, quality
**Description:**
`plivo_mirror.text.is_customer_voice` and `is_meta_description` +
`Verdict.spoken_correction()`. Guarantees the spoken correction is
agent-voice, never echoing the customer ("I'd like X") or a third-person
meta-description ("The customer said X"). Both observed as real bugs with
Azure gpt-5.4-mini during integration. 30 unit tests.

---

### Issue: Sticky intent-note across turns
**Status:** ✅ Done
**Labels:** core, conversation
**Description:**
After an intervention the LLM loses the customer's real intent. Added
CallSupervisor.set_intent_note / consume_intent_note / clear_intent_note —
persists the intent for ~3 turns so the agent doesn't ask the customer to
repeat themselves. Auto-clears on tool commit. 8 unit tests.

---

### Issue: Docs — MIGRATION.md + livekit_quickstart example
**Status:** ✅ Done
**Labels:** docs
**Description:**
Wrote 0.2.0 → 0.3.0 migration guide and a ~60-line working
examples/livekit_quickstart/ (agent.py + README) demonstrating the
5-line integration.

---

## ✅ DONE — Validation harnesses

### Issue: Build healthcare voice agent (prescription refill) for testing
**Status:** ✅ Done
**Labels:** validation, demo
**Description:**
LiveKit pharmacy-refill agent at ~/Desktop/livekit-healthcare-agent with
a mock pharmacy DB (allergy traps, discontinued/expired Rx, drug-name
confusion). Finding: gpt-5.4 was too cautious to fail interestingly —
correctly refused the discontinued/allergy amoxicillin. Good for safety,
weak for a failure demo. Pivoted to food ordering.

---

### Issue: Build pizza voice agent (Crave Plivo) + plug in Mirror
**Status:** ✅ Done
**Labels:** validation, demo
**Description:**
LiveKit pizza-ordering agent at ~/Desktop/livekit-food-agent. Mirror
integrated in 5 lines. Confirmed working: on "my wife wants mushroom but
I'd like BBQ chicken", bare agent ordered both; Mirror caught it
(score=0.95, Policy 2) and substituted "Got it — one BBQ chicken pizza
for you." Un-rigged the prompt to test real (not artificial) failures.

---

## 🔵 IN PROGRESS — Measure real catch rate

### Issue: Measure Mirror catch rate on 10 curated hard scenarios
**Status:** 🔵 In Progress
**Labels:** validation, metrics
**Description:**
Run the un-rigged pizza agent through 10 scenarios known to trip LLMs
(mid-utterance correction, third-party preference, conditional order,
time-delayed change, compound modifiers, ambiguous quantity, off-menu
invention, policy distance, implicit-history hallucination, math-under-
load). For each: record bare-LLM tool args, Mirror verdict, ✅/❌/🟡.
Tally tells us: optimize latency (8+/10), fix policies (5–7), or
fine-tune (<5). Scenarios documented in livekit-food-agent/README.md.

---

### Issue: Build structured turn-logger (transcripts → JSONL)
**Status:** ⚪ Todo
**Labels:** tooling, data
**Description:**
Add a logger to the food agent that writes every turn to JSONL:
{scenario, customer_text, agent_reply, mirror_intervened, score,
violated_policies, correction}. Makes the scenario run produce a clean
dataset instead of hand-copying from terminal logs. Doubles as the seed
training set for any future fine-tuning.

---

## ⚪ BACKLOG — Performance & accuracy

### Issue: Reduce intervention latency (2–5 s pause)
**Status:** ⚪ Backlog
**Labels:** performance
**Description:**
Supervised pause is the main UX cost (Azure judge ~5 s, Gemini Flash
~1.5 s when healthy). Levers: run Tier 1 + Tier 2 concurrently; skip the
correction-generator when the judge already returned a good
suggested_correction; offer a faster default judge. Quantify each.

---

### Issue: Evaluate fine-tuned Tier-2 hallucination detector
**Status:** ⚪ Backlog
**Labels:** research, ml
**Description:**
The Tier-2 slot accepts any judge implementing the protocol. A small
fine-tuned model (Llama-Guard-style) trained on labelled voice-agent
failures would give ~100 ms inference, no per-call LLM cost, and higher
domain accuracy. BLOCKED ON DATA: needs a few hundred labelled
(customer_text, agent_reply, verdict) examples — produced by the
catch-rate measurement above. Only justified if measurement shows the
general LLM judge is weak (<5/10), not just slow. Swap-in is one line:
MirrorJudge(tier2=YourFineTunedJudge()).

---

### Issue: Auto-derive judging policies from the agent's system prompt
**Status:** ⚪ Backlog
**Labels:** dx, research
**Description:**
Zero-config UX idea: synthesize the judging policies from the customer's
existing agent system prompt, so they don't have to write POLICIES by
hand. Was a planned v2 direction; revisit after catch-rate data.

---

## ⚪ BACKLOG — Infra / ops notes (track as issues or notes)

### Issue: Azure gpt-5.4 (full) deployment unusable for real-time voice
**Status:** ⚪ Note
**Labels:** infra, blocked-external
**Description:**
gpt-5.4 (full model) on the hackathon Azure resource times out (20+ s)
on even trivial prompts — quota/capacity issue. gpt-5.4-mini responds in
~2 s and works fine. Action: ask infra team to raise quota on gpt-5.4 or
standardize everyone on -mini.

---

### Issue: Gemini free tier 503/504 under load
**Status:** ⚪ Note
**Labels:** infra, blocked-external
**Description:**
Gemini Flash judged faster (~1.5 s) than Azure (~5 s) but the free tier
returns 503/504 under load — not viable for a stable demo without paid
billing. Re-enable behind a paid Google billing account if we want the
latency win.

---

### Issue: Atla judge endpoint reliability (SSL issues)
**Status:** ⚪ Note
**Labels:** infra, blocked-external
**Description:**
Atla dashboard had SSL issues; API endpoint reliability uncertain. Forced
Azure via MIRROR_TIER2=azure as a workaround. Revisit if we want Atla
Selene as the default judge.
