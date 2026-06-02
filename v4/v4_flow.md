# plivo-mirror v4 — full runtime flow (in depth)

> Two views: an editable **Excalidraw** scene (`v4_flow.excalidraw` — open at excalidraw.com → *Open*) and the **Mermaid** diagram below (renders on GitHub). Then a step-by-step narrative of exactly what happens on one turn.

## Diagram

```mermaid
flowchart TD
    caller["📞 Caller speaks"] --> stt["STT (Deepgram)<br/>speech → text"]
    stt --> extract["extract_state<br/>validate committable values"]
    extract -. write .-> STATE
    extract --> inject["Grounding injection<br/>read-only state summary → ctx"]
    STATE -. read .-> inject
    inject --> agent["Agent LLM<br/>buffer full reply + tool intents"]
    agent --> ctx["TurnContext<br/>state · reply · tools · customer"]
    ctx --> review

    subgraph STATE["🟦 SESSION STATE — single source of truth (outside the model)"]
      direction TB
      s1["validated entities · confirmed intent<br/>known facts · committed actions · spoken log"]
    end

    subgraph review["review_turn — first guard that intervenes wins"]
      direction TB
      subgraph speech["① Speech guard (tokens → TTS)"]
        direction TB
        det["Deterministic<br/>FORBID / REQUIRE → block"] -->|pass| spans["Risk-span tagger<br/>price·number·commit·name"]
        spans -->|no span| committal["Committal gate<br/>skip question / refusal"]
        committal -->|committal| nli["NLI semantic signal<br/>(a) reply vs CUSTOMER  (b) reply vs KNOWN FACTS<br/>contradiction? → synth span"]
        spans -->|span| verifier
        nli -->|contradiction| verifier["Grounded verifier (stateless LLM judge)<br/>FACTS+POLICY+customer → supported?"]
      end
      subgraph action["② Action guard (tool → execution) — only if speech passed"]
        direction TB
        fc["False-completion<br/>'done' w/ no tool"] --> argst["Arg ↔ state<br/>consistency"]
        argst --> authz["Authorization<br/>(separate service)"]
        authz --> valid["Param/policy validators<br/>business rules in CODE"]
        valid --> zero["Tools fire ZERO-ARG<br/>(read from state)"]
      end
      verifier -->|speech PASS| fc
    end

    STATE -. facts .-> nli
    STATE -. facts .-> verifier
    STATE -. compare .-> argst
    STATE -. args .-> zero

    verifier --> verdict{{"VERDICT<br/>pass · correct · block"}}
    zero --> verdict
    verdict -->|clean| passout["PASS → speak reply<br/>+ fire tools"]
    verdict -->|flagged| interv

    subgraph interv["Intervention engine"]
      direction TB
      filler["1. Deflection filler — spoken FIRST (no LLM)"]
      filler --> structured["2a. STRUCTURED: state can answer<br/>→ template (NO LLM)"]
      filler --> packet["2b. OPEN: correction packet<br/>correct facts, NO echo of wrong value"]
      packet --> regen["3. Regenerate via MAIN LLM<br/>+ real customer turn"]
      structured --> reverify["4. Re-verify (loop, cap 2)<br/>accept · else deflect / escalate"]
      regen --> reverify
    end
    reverify -. re-check .-> verifier

    passout --> tts["TTS (ElevenLabs)"]
    reverify --> tts
    tts --> hear["📞 Caller hears SAFE output"]

    persona["Cross-cutting: persona guard (length+tone, prompt re-inject, escalate)<br/>· intent memory (hold real intent across turns, clear on commit)"]
```

## What happens on one turn (step by step)

1. **Caller speaks → STT.** Deepgram turns audio into the customer's text.

2. **State grounding (the structural backbone).** `extract_state` pulls *committable* values out of the caller's turn (amounts, dates, on-menu items…), **validates** them, and writes them to **`SessionState`** — which lives **outside the model's context**. This is the single source of truth. *(v4's first defense: the model never owns the truth.)*

3. **Grounding injection.** A read-only summary of confirmed state (+ any held intent / persona re-injection) is injected into the chat context every turn — so the agent is reminded of the facts, but can't mutate them.

4. **Agent LLM.** The model produces its planned reply (and any tool intents). The adapter **buffers the whole stream** so both guards can inspect the full reply before a word is voiced. A `TurnContext` bundles `{state, planned_reply, tool_intents, customer_text}`.

5. **`review_turn` — the dual boundary.** Speech guard runs first; **the first guard that intervenes wins** and the pending tool calls are dropped.

   **① Speech guard** (tokens → TTS):
   - **Deterministic** — compiled `FORBID:`/`REQUIRE:` policy checks. A hard hit → **block** immediately (verifier never runs).
   - **Risk-span tagger** — flags consequential spans (prices, numbers, commitment words, names). *No span* → the zero-latency pass path… unless:
   - **Committal gate → NLI** — if the reply is *affirmative/committal* (not a question/refusal), the **semantic signal** (local cross-encoder NLI) runs **two checks**: (a) does the reply contradict the **customer's stated request**? (ignored negation, dropped modifier), and (b) does it contradict a **known fact**? (e.g. facts say "open until 9 PM", reply says "until midnight" — fabricated hours/availability with no number). Either contradiction synthesizes a flagged span → verifier. *(The committal gate is what stopped over-firing on off-topic chatter; the fact-check (b) is the fact-hallucination lever that lifted F1 to 0.733.)*
   - **Grounded verifier** — the only expensive call, on flagged spans only. A **separate, stateless** LLM-judge entailment call: *FACTS + POLICIES + customer request → supported?* Supported → **pass**; unsupported → **correct**. *(Stateless + separate = it can't rationalize the agent's own output.)*

   **② Action guard** (tool call → execution) — runs only if speech passed; deterministic, ~0 ms:
   - **False-completion** (claims "done" with no backing tool call), **arg↔state consistency** (proposed args vs validated state), **authorization separation** (a *separate service* decides what the caller may do — the prompt-injection defense), **param/policy validators** (business rules in code).
   - Tools then fire **zero-argument**: the executor reads validated values from state, never from the model.

6. **Verdict → act.**
   - **pass** → speak the reply + fire the tools.
   - **correct / block** → the **intervention engine**.

7. **Intervention (deflect → regenerate → re-verify).**
   - **Deflection filler** is yielded to TTS **first** (needs no LLM) — it's the "first beat" spoken while the real answer is produced, covering latency.
   - **Structured** (state can answer) → template the corrected reply from validated state, **no LLM**. **Open** → build a **correction packet** (the *correct* facts + the rule, framed so it **never restates the wrong value** — the pink-elephant guarantee) and re-prompt the **main LLM** with the *real* customer turn.
   - **Re-verify** the candidate back through the speech guard (+ echo check). Accept on pass; else regenerate (cap 2). On non-convergence: **deflect** (safe filler) by default, or escalate via warm handoff.

8. **TTS → caller hears the safe output.**

**Cross-cutting:** a **persona guard** tracks length + tone, re-injects a system-prompt summary at intervals, and escalates past a threshold; **intent memory** holds the caller's real intent across turns and auto-clears on commit.

## Timing modes — when the speech guard runs vs TTS
The expensive tier is the NLI model (~0.9 s on clean *committal* turns with the precise large model). Two modes decide *when* it runs relative to first audio:

- **Synchronous (default).** Review *then* voice — the reply is held until the guard clears it, so a violation is **prevented before it's voiced**. The ~0.9 s NLI sits on the first-audio path for clean committal turns.
- **Speculative (opt-in: `SupervisedAgent(speculative=True)`).** On a **no-tool, lexically-clean, non-deterministic-hit** turn, voice the reply **immediately** and run NLI + verifier **off the first-audio path**, emitting a correction only if it fires. ~0 ms perceived latency. **Tradeoff:** a semantic/fact slip on such a turn is **corrected-after, not prevented**. Tool calls, lexical risk spans (numbers/commitments), and `FORBID/REQUIRE` hits **always stay synchronous** (prevented).

(We chose the large NLI model for precision — a threshold sweep showed the small model can't be tuned to acceptable precision; speculative mode is how you reclaim its latency without losing that precision.)

## The six failures this flow defends (and where)
| failure | defended at |
|---|---|
| fabricated facts | grounded verifier + state grounding |
| unauthorized commitments | risk-span (commitment words) → verifier |
| wrong-action-vs-intent | action guard: arg↔state + zero-argument tools |
| compliance / disclosure gaps | deterministic `REQUIRE:` |
| prompt injection | authorization separation + zero-argument tools |
| persona drift | persona guard (length/tone, re-injection, escalation) |
