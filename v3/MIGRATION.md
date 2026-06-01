# Migrating from plivo-mirror 0.2.x → 0.3.0

v0.3.0 is **fully backward-compatible at the import level**. Existing
0.2.x integrations keep working unchanged. This guide covers the new
ergonomics, what got moved into the library, and how to delete code
you don't need to own anymore.

## TL;DR

| You had in 0.2.x | What 0.3.0 lets you write |
|---|---|
| `Supervisor(config=MirrorConfig(llm=..., policies=...))` | `Supervisor.from_env(policies=...)` |
| 1000-line `mirror_supervisor.py` (LiveKit) | Inherit `plivo_mirror.adapters.livekit.SupervisedAgent` |
| Custom `is_customer_voice()` regex | `from plivo_mirror.text import is_customer_voice` |
| Custom judge prompt copy-pasted into your repo | Auto-baked into the built-in judges |
| Hand-rolled sticky-intent-note bookkeeping | `CallSupervisor.set_intent_note()` / `consume_intent_note()` |
| Manual Azure-OpenAI or HF judge wiring | `AzureOpenAIJudge` / `HuggingFaceLLMJudge` / `OpenAICompatibleJudge` ship with the package |

Nothing in 0.2.x is removed. The 0.3.0 helpers are additive — adopt
them when you're ready.

---

## 1. Use `Supervisor.from_env()` instead of building `MirrorConfig` by hand

**Before (0.2.x):**

```python
from plivo_mirror import Supervisor, MirrorConfig
from plivo_mirror.scorer import MirrorJudge
from plivo_mirror.scorer.tier1 import HuggingFaceClassifier

scorer = MirrorJudge(
    tier1=HuggingFaceClassifier(api_key=os.environ["HF_API_KEY"]),
    tier2=YourCustomAzureJudge(...),  # you had to write this yourself
)
config = MirrorConfig(llm=DummyLLM(), policies=[...])
supervisor = Supervisor(config=config, scorer=scorer)
```

**After (0.3.0):**

```python
from plivo_mirror import Supervisor

supervisor = Supervisor.from_env(policies=[...])
```

`from_env()` auto-detects the best available judge from env vars in
priority order:

| Provider | Required env vars |
|---|---|
| Atla Selene | `ATLA_API_KEY` |
| Azure OpenAI | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` |
| Vanilla OpenAI / OpenAI-compatible | `OPENAI_API_KEY` |
| Hugging Face Inference | `HF_API_KEY` |

Force a specific provider with `MIRROR_TIER2=atla|azure|openai|hf|none`.
Disable the cheap Tier 1 classifier with `MIRROR_DISABLE_TIER1=1`.

If no credentials are present, `from_env()` returns a Supervisor with
both tiers set to `None` — it'll run, but it won't score anything.
Log line at startup tells you what wired up.

---

## 2. Replace your LiveKit `mirror_supervisor.py` with the adapter

If you copied the v0.2.0 LiveKit example and built your own
`mirror_supervisor.py` (or anything calling `Agent.default.llm_node`
manually), you can delete it.

**Before (0.2.x):**

```python
# ~1000 lines of glue: chat_ctx extraction, llm_node override,
# stream buffering, intent-note injection, cooldown bookkeeping,
# customer-voice filter, meta-description filter, ...
```

**After (0.3.0):**

```python
from plivo_mirror import Supervisor
from plivo_mirror.adapters.livekit import SupervisedAgent

supervisor = Supervisor.from_env(policies=[...])

class MyAgent(SupervisedAgent):
    def __init__(self):
        super().__init__(supervisor=supervisor, instructions=SYSTEM_PROMPT)

    @function_tool
    async def place_order(self, items): ...
```

The adapter handles every glue concern listed in the table above.
Settings like the post-intervention cooldown are configurable via
constructor args (`intervention_cooldown_s=...`).

See `examples/livekit_quickstart/` for the full working example.

---

## 3. Built-in judges (drop your custom ones)

v0.3.0 ships three production-ready Tier-2 judges:

| Class | Use when |
|---|---|
| `plivo_mirror.scorer.tier2.AzureOpenAIJudge` | You're on Azure OpenAI (Plivo hackathon creds) |
| `plivo_mirror.scorer.tier2.OpenAICompatibleJudge` | Vanilla OpenAI, Together, Fireworks, anything with the OpenAI Chat API |
| `plivo_mirror.scorer.tier2.HuggingFaceLLMJudge` | Hugging Face Inference Providers (Llama, Qwen, etc.) |

All three:

- Use the same baked-in `JUDGE_PROMPT` (Azure-content-filter-safe,
  agent-voice-forcing, concrete-order-demanding).
- Return validated `Verdict` objects — no JSON parsing in customer
  code.
- Are wired automatically by `Supervisor.from_env()`.

The prompt was previously something each customer had to hand-write.
That is no longer the case.

---

## 4. Public text-quality helpers

If you were writing your own regex to detect "customer-voice"
corrections, replace it with:

```python
from plivo_mirror.text import is_customer_voice, is_meta_description

if is_customer_voice(verdict.suggested_correction):
    ...fall back to synthesized agent-voice correction
```

These are used internally by `Verdict.spoken_correction()` — call
that directly if you just want the final agent-voice line:

```python
text = verdict.spoken_correction()  # always agent-voice, never raw
```

---

## 5. Sticky intent note (post-correction memory)

When Mirror intervenes mid-turn, the LLM loses track of what the
customer actually wanted (Mirror substituted the response). v0.3.0
adds an in-supervisor sticky note that adapters inject into the next
few turns automatically.

If you're using `SupervisedAgent`, this is automatic. If you're
writing your own adapter:

```python
# After an intervention:
note = verdict.post_correction_context(customer_text)
call_sup.set_intent_note(note, turns=3)

# At the top of every LLM turn in your adapter:
note = call_sup.consume_intent_note()
if note:
    # prepend `note` to the most recent user message in your chat
    # context (Azure ignores mid-conv system messages)
    ...
```

The note auto-clears when a tool commits via
`call_sup.note_committed(...)` — once the order is in, no further
reminder is needed.

---

## 6. No breaking changes

Every public symbol present in 0.2.0 is still present and works the
same way. `Supervisor(config=...)`, `CallSupervisor.review_and_speak`,
`MirrorConfig`, `Verdict`, the `mirror_judge` package — all still
work. v0.3.0 just makes the 80% case dramatically less code.

---

## 7. New environment variables

| Var | Purpose | Values |
|---|---|---|
| `MIRROR_TIER2` | Force a specific tier-2 judge | `atla`, `azure`, `openai`, `hf`, `none` |
| `MIRROR_DISABLE_TIER1` | Skip the cheap Tier-1 classifier even if `HF_API_KEY` is set | `1` |
| `MIRROR_INTERVENTION_THRESHOLD` | Override the default 0.7 confidence threshold | float in `[0, 1]` |

Plus the existing provider keys (`AZURE_OPENAI_*`, `OPENAI_API_KEY`,
`HF_API_KEY`, `ATLA_API_KEY`) — see § 1.

---

## Questions / issues

File a bug at https://github.com/plivo-mirror/plivo-mirror or ping
`@vijay.krishna` on the hackathon Slack.
