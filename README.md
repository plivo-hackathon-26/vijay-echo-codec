# plivo-mirror

**A real-time policy firewall for LLM voice agents.** It sits between an agent's
LLM and the outside world and stops bad output **before** it reaches the caller
or fires an irreversible action — prevention, not post-call detection.

`pip install plivo-mirror` · published on PyPI.

---

## Where things are

| Path | What it is | Status |
|---|---|---|
| **[`v4/`](v4/)** | **The dual-boundary firewall — current line.** A ground-up rebuild as a *firewall* (speech guard + action guard) over a validated session state. | **`0.4.0rc1`** (pre-release) |
| [`v3/`](v3/) | The shipped three-tier scorer (Tier-0 regex → Tier-1 NLI → Tier-2 LLM judge) + LiveKit `SupervisedAgent`. | `0.3.x` (latest stable) |
| [`v1/`](v1/), [`v2/`](v2/) | Original hackathon demo + the first library iteration. | Archaeology |
| [`demo-frontend/`](demo-frontend/) | Demo UI. | — |

> All four share the same PyPI package name. `pip install plivo-mirror` gives the
> stable **0.3.x**; `pip install --pre plivo-mirror` (or `==0.4.0rc1`) gives **v4**.

---

## v4 — the dual-boundary firewall

### The six failures it targets
fabricated facts · unauthorized commitments · wrong-action-vs-intent ·
compliance/disclosure gaps · prompt injection · persona drift.

### How it works
The caller's committable values are validated and written to **`SessionState`** —
the single source of truth, kept **outside the model's context**. Every turn, the
agent's planned reply is buffered and run through two boundaries (the first guard
to object wins):

**① Speech guard** — what it's about to *say* (tokens → TTS):
1. **Deterministic** — instant code check for forbidden / required phrases (`FORBID:` / `REQUIRE:`); hard-block on a hit.
2. **Risk-span tagger** — flags risky spans (price · number · commitment · name); no span → ~0 ms pass.
3. **NLI semantic tier** — does the reply contradict the customer *or* a known fact? (catches ignored negations, fabricated hours).
4. **Grounded verifier** — a separate, stateless LLM-judge: is the claim supported by FACTS + POLICIES?

**② Action guard** — what it's about to *do* (tool call → execution), deterministic ~0 ms:
1. **False-completion** — claims "done" with no backing tool call.
2. **Arg ↔ state** — tool arguments must match validated state (wrong item/amount → block).
3. **Authorization** — a *separate* service decides what the caller may do (the model never authorizes itself — the prompt-injection defense).
4. **Validators + zero-argument** — business rules live in code; tools fire with **zero arguments**, reading values straight from state.

**Intervention** — on a violation, a **deflection filler** is spoken first (no LLM,
covers latency), then the **same LLM** regenerates a correct reply from the facts —
never restating the wrong value (avoids the "pink-elephant" trap) — and it's
**re-verified** before being voiced.

### Results (hard eval: 65 violations + 64 clean near-misses, live, gpt-5.4-mini)
| metric | before (lexicon gate) | after (v4) |
|---|---|---|
| catch / recall | 35% | **68%** |
| F1 | 0.47 | **0.73** |
| precision | 0.70 | **0.80** |

Latency: clean turns add ~0 ms guard compute; the grounded verifier runs only on
flagged spans (a filler covers its latency). The NLI tier adds ~0.9 s on clean
committal turns with the precise model — an opt-in **speculative mode** moves that
off the first-audio path.

---

## Quickstart (v4)

```bash
cd v4 && pip install --pre -e ".[openai,livekit]"      # + [nli] for the semantic tier (torch/transformers)
pytest tests/                                           # unit tests
python -m plivo_mirror.eval --mode deterministic        # the eval harness (gate ceiling, no LLM)
```

Drop-in LiveKit agent (~5 lines):

```python
from plivo_mirror import Firewall
from plivo_mirror.adapters.livekit import SupervisedAgent

firewall = Firewall.from_env(policies=POLICIES)          # 1
class MyAgent(SupervisedAgent):                          # 2
    def __init__(self): super().__init__(firewall=firewall, instructions=PROMPT)  # 3
    @function_tool
    async def place_order(self): ...                     # 4  (reads items from STATE, not args)
# session.start(agent=MyAgent())                         # 5
```

A runnable example lives in [`v4/examples/livekit_quickstart/`](v4/examples/livekit_quickstart/).

## Understand it visually
- **[`v4/v4_overview.html`](v4/v4_overview.html)** — interactive explainer (Problems · Architecture · Eval tabs, with animated turns).
- **[`v4/firewall_explorer.html`](v4/firewall_explorer.html)** — hands-on playground: type a turn, watch the verdict.
- **[`v4/v4_flow.md`](v4/v4_flow.md)** / `v4_flow.excalidraw` — the full runtime flow.

---

## License
MIT (v4 — see `v4/pyproject.toml`). Earlier lines retain their own license files.
