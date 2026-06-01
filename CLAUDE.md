# plivo-mirror — project context

**plivo-mirror** is a real-time **policy firewall for LLM voice agents**.
It sits between an agent's LLM and the outside world and stops bad output
*before* it reaches the caller or fires an irreversible action. Published
on PyPI (`pip install plivo-mirror`); v3 (0.3.x) is the currently-shipped
line. **v4 is a major version of the SAME package** — a ground-up rebuild
of the supervisor as a *dual-boundary firewall*. This file is the durable
source of truth for v4. Re-read it before starting every build phase.

---

## Repo layout (READ FIRST)

- **`v1/`** — original hackathon code. Archaeology only. Do not modify.
- **`v3/`** — the currently-shipped `plivo-mirror` package (0.3.x on PyPI):
  a three-tier scorer (Tier 0 regex → Tier 1 HF NLI → Tier 2 LLM judge),
  `Supervisor.from_env()`, LiveKit `SupervisedAgent`. **Reference v3 for
  the public surface only — NOT for the scorer.** v4 does not copy or
  reuse the three-tier scorer.
- **`v4/`** — the new dual-boundary firewall. Built from scratch. All new
  work happens here.

Read **only** this v3 file for the integration *shape* we must preserve
(the ~5-line LiveKit adapter), not its internals:
`v3/plivo_mirror/adapters/livekit/supervised_agent.py`.

The repo root shares `venv/`, `.env`, `.env.example`, and this `CLAUDE.md`.

---

## What v4 is

A real-time, **dual-boundary policy firewall** for LLM voice agents. It
guards two boundaries before anything reaches the caller or the world:

- **Speech boundary** (LLM tokens → TTS): false facts, unauthorized verbal
  commitments, missing disclosures.
- **Action boundary** (tool call → execution): wrong/unauthorized actions,
  prompt-injection-driven tool calls, policy violations.

### Target failures — the ONLY things v4 solves for

`fabricated facts` · `unauthorized commitments` · `wrong-action-vs-intent`
· `compliance/disclosure gaps` · `prompt injection` · `persona drift`.

If a proposed feature does not directly defend one of these six, it does
not belong in v4.

---

## Core contracts (locked before any component is built)

Everything depends on these. They are defined first; components are built
against them.

- **`SessionState`** — the single source of truth for a call. Holds:
  validated entities, confirmed intent, compiled policies, the
  committed-action log, and what has already been spoken.
- **`Verdict`** — a guard's decision: `decision ∈ {pass, correct, block}`,
  `reason`, `policy_id`, the flagged `span`, `confidence`, and an
  agent-voice `spoken_correction`.
- **`Guard` protocol** — one interface both guards implement:
  `inspect(context) -> Verdict`.
- **`Policy`** — a plain-English policy compiled into a runnable check
  object. Business logic lives in the compiled check, never in a prompt.
- **Per-turn control loop** — how the adapter invokes the speech guard and
  the action guard, and where each can pass / correct / block.

---

## Architecture

### Session state = single source of truth

As soon as the caller gives a committable value (item, name, amount,
date), **validate it and write it to `SessionState`, OUTSIDE the model's
context.** Tool calls **READ from state and never accept arguments from
the model** — the **zero-argument principle**. This is the structural
backbone of both the wrong-action and prompt-injection defenses.

### Speech guard (in-stream, parallel with speculative TTS)

1. **Free signal layer** — runs at ~0ms:
   - **Confidence signal** (behind an interface; see constraints).
   - **Risk-span tagger** — flags numbers, names, prices, and commitment
     words (refund / discount / eligible / guarantee …).
   - **Deterministic checks** — forbidden phrases, required disclosures
     present.
2. **Router**:
   - confident + no risky span → **pass** (zero latency),
   - risky + uncertain span → **escalate that span only**,
   - hard deterministic hit → **block immediately**.
3. **Grounded verifier** — the *only* expensive call, scoped to flagged
   spans: "is this claim/commitment supported by {state, policy, retrieved
   facts}? yes/no + which policy." Swappable `Protocol` (see constraints).
4. **Intervention** — suppress/gate the risky span before it is voiced and
   substitute an agent-voice, grounded correction.

### Action guard (mostly deterministic, ~0ms)

1. **Consistency check** — tool args vs validated state, AND the spoken
   reply vs the action. Mismatch → block.
2. **Authorization separation** — a **SEPARATE service** decides what the
   caller is *permitted* to do. The model reasons about intent; it never
   authorizes. **This is the prompt-injection defense.**
3. **Parameter/policy validation** — business rules in code (refund within
   policy, amount caps, step ordering).
4. **Intervention** — block execution, then re-confirm, correct args from
   state, or escalate.

### Intervention & regeneration

When a guard intervenes, the firewall produces a real corrected answer —
not just a deflection:

1. **Deflection filler first** — the verdict's agent-voice line (needs no
   LLM) is *yielded to the TTS stream before the regeneration is awaited*
   (`engine.stream_intervention` → `Firewall.intervene_stream`), so it is
   already being spoken while the grounded answer is produced. It is the
   "first beat", NOT the answer.
2. **Correction packet** (`intervention/packet.py`) — built from
   `{state, violation, policy}`, framed around the CORRECT facts + the
   rule. **Pink-elephant guarantee:** it never restates the flagged (wrong)
   value (`assert_no_echo`).
3. **Structured vs open**:
   - *Structured* (state can answer) → template the corrected reply from
     validated state — **NO LLM** (`engine.template_corrected_reply`).
   - *Open* → re-prompt the **main voice LLM** with the packet as a
     SYSTEM/developer message + the REAL customer turn — **never a
     synthesized customer turn** (`regenerate.LLMReplyGenerator`).
4. **Re-verify** the candidate through the speech guard (plus a
   pink-elephant echo check). Accept on pass; otherwise regenerate.
5. **Cap retries** (default 2). On non-convergence, **escalate via
   `build_handoff`** (`engine.run_intervention` → `Firewall.intervene`).

Two-part delivery: the adapter yields the filler, then the grounded
answer. Under single-LLM, regeneration runs on the SAME model as the agent
and verifier.

### Cross-cutting (folded into the relevant phases)

- **Intent memory** — after an intervention, hold the caller's real intent
  for the next few turns; auto-clear on commit.
- **Session/persona guard** — track length + tone, re-inject a
  system-prompt summary at intervals, trigger escalation past a threshold.
- **Escalation** — warm handoff with context delivery enforced in code.
- **Observability plane** — async, off the hot path.

---

## Hard constraints

- **Zero added COMPUTE latency on clean turns.** The deterministic signal
  layer is ~0 ms and the expensive verifier runs ONLY on flagged
  consequential spans (never on a clean turn). Honest caveat for the
  current LiveKit adapter: it buffers the full LLM reply before yielding,
  so first-audio waits on full-reply buffering on every turn — the guards
  add no *compute* latency on clean turns, but token-streaming-until-a-
  risky-span (early-release) is a documented FUTURE enhancement, not yet
  implemented. Do not claim "zero added audible latency" until early-
  release lands.
- **All business logic, pricing, and policy live in CODE — never in
  prompts.** Prompts are for tone, intent extraction, and NLU only.
- **Verifier is a swappable `Protocol`.** Ship a default impl using
  LLM-as-judge over an OpenAI-compatible endpoint with a grounded
  entailment prompt. NO fine-tuned model yet — leave the Protocol open so
  a small hosted model or a fine-tune can drop in later as a one-line swap.
- **Single-LLM by default.** One configured model — the voice agent's
  model — serves all three LLM roles: (a) the agent's replies, (b) the
  grounded verifier, (c) regeneration/correction. `Firewall.from_env(...,
  model=...)` defaults the verifier to that SAME model + endpoint + creds.
  The verifier ROLE is never removed — it's a function, just pointed at the
  same model.
  - **Self-evaluation guardrail (what makes single-LLM safe):** even on one
    model, the verifier MUST be a SEPARATE, STATELESS call with the
    grounded-ENTAILMENT prompt (FACTS + POLICIES + reply → supported y/n +
    which policy). It must NOT run as the agent persona, and NOT as an
    in-context "grade your own last reply" step — a model grading its own
    just-produced output in the same context rationalizes it
    (self-consistency bias) and the check is worthless. Fresh invocation,
    judge prompt, reply-vs-state. (`LLMJudgeVerifier` enforces this: a new
    2-message system-entailment + user-evidence call.)
  - **Swappable-judge escape hatch:** single-LLM is the DEFAULT, not a
    hard-wire. `verifier_model=` points only the verifier at a different
    model; `verifier=` injects a fully custom judge/fine-tune.
  - **Keep the verifier scoped to flagged spans ONLY** (router design) so a
    single endpoint isn't hit on every clean turn — matters under the Azure
    capacity limits noted below.
- **Confidence signal is behind an interface.** Prefer token-entropy / a
  semantic-entropy probe IF the agent model exposes logprobs or hidden
  states; otherwise fall back to top-K logprob entropy for API-only models.
  **Reality today (do not overstate):** in the LiveKit + Azure-gpt-5-mini
  setup the agent's token logprobs are NOT available (LiveKit's `llm_node`
  streams `ChatChunk`s without them; the reasoning model doesn't expose
  them), so `TurnContext.logprobs` is never populated, `confidence` is a
  constant `0.0`, the confidence gate never passes, and **routing is
  risk-span (lexicon) driven ONLY**. Semantic-entropy / logprob routing is
  a documented **FUTURE** capability — the code path
  (`guards/speech.SpeechGuard.inspect` step 3 + `guards/signal.py`) stays
  intact behind a TODO and activates the moment a real `ConfidenceSignal`
  or populated logprobs are supplied. No doc claims it runs today.
- **Speculative speech:** implement true mid-stream gating of the risky
  span IF LiveKit's TTS pipeline supports intercepting before that span is
  voiced. If not, fall back to gating the FULL reply on flagged turns only.
  **Document which you implemented and why — do not fake it.**

---

## Deliverable + metrics

- An importable package + a working ~example agent + a regression suite
  wired to the **existing** hard-scenario eval sets.
- **Eval sets live at** `v3/datasets/eval_v1.jsonl` (the "30-case" set;
  currently 36 lines) and `v3/datasets/eval_v2.jsonl` (the "100-case" set;
  currently 134 lines). v4's regression suite wires to these — it does not
  fabricate scenarios. (Label schema: `v3/datasets/LABELING.md`.)
- **Metrics the suite must emit:** catch rate; false-intervention rate (on
  a golden set of GOOD calls); audible-latency p50/p95; verifier-hit rate
  (fraction of turns that reach the grounded verifier).
- **There is no "Tier 2" in v4** — it is a single grounded verifier.

---

## Build order (done-bar per phase; pause for approval each time)

- **Phase 0** — Write this CLAUDE.md + propose the plan. *(now)*
- **Phase 1** — Contracts + session state store. Done = contracts defined,
  state store with unit tests, package imports clean.
- **Phase 2** — Speech guard (signal + router + verifier interface +
  default verifier + correction). Done = unit tests pass.
- **Phase 3** — Action guard (consistency + authz separation +
  validation). Done = unit tests pass.
- **Phase 4** — LiveKit adapter (~5-line shape) wiring both guards + the
  speculative-speech decision. Done = working example agent.
- **Phase 5** — Observability + regression harness on the 30/100 sets
  emitting the four metrics. Done = suite runs and prints a metrics report.

---

## Working rules

- Keep the package importable and tests green at the END of every phase.
- Build the SMALLEST thing that satisfies each phase's done-bar. Do not
  over-scaffold or add components not in this spec.
- STOP and ask at each phase boundary before continuing.
- If any decision is ambiguous for this repo, ask rather than guess.
- **Do NOT copy or reference the v3 three-tier scorer.** v4 is a clean
  build; reuse v3 only for the public integration shape.

---

## Operational notes (carried over — still apply to v4)

### Stack

- Python 3.11. Async throughout.
- LiveKit Agents as the reference transport (v3 also supports Plivo).
- Default grounded verifier: an OpenAI-compatible chat endpoint. The
  hackathon creds are **Azure-hosted** `gpt-5-mini` via the OpenAI SDK
  with a `base_url` override.
- Eval/regression harness: plain Python over the JSONL sets above.

### Azure OpenAI quirks (paid in real time — learn from them)

The Azure deployment of `gpt-5-mini` rejects several common OpenAI params
with `400 BadRequest`. The default verifier must work around these:

- ❌ `max_tokens=…`        → use nothing, or `max_completion_tokens=…`
- ❌ `tool_choice="none"`  → omit `tools` entirely (no tools = no tool calls)
- ⚠️ `temperature=…`       → silently ignored on some deployments
- ✅ `response_format={"type":"json_object"}` is supported

### Credentials

Plivo creds + API keys are in 1Password → `hackathon-2026` vault. The
`.env` at repo root holds runtime config (gitignored). The verifier reads
its endpoint/key from the environment (e.g. `OPENAI_API_KEY`,
`OPENAI_BASE_URL`, `OPENAI_MODEL` / the Azure equivalents).

---

## What v4 is NOT doing

- **Not** copying the v3 three-tier scorer (Tier 0/1/2). v4 has a single
  grounded verifier behind a Protocol.
- **Not** coupling to any vertical (pizza / travel / healthcare). The
  firewall is generic; business rules are supplied as compiled policies.
- **Not** putting business logic, pricing, or policy in prompts.
- **Not** adding components outside the six target failures and the spec
  above. Smallest thing that meets each phase's done-bar.
