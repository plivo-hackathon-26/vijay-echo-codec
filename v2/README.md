# plivo-mirror

> Silent supervisor for Plivo voice agents — detects bad agent behaviour in real time, intervenes mid-call with a self-correction the caller never knows is happening, and ships with a tool-call gate that blocks irreversible side effects before they fire.

[![PyPI](https://img.shields.io/pypi/v/plivo-mirror.svg)](https://pypi.org/project/plivo-mirror/)
[![License](https://img.shields.io/pypi/l/plivo-mirror.svg)](LICENSE)

## What it does

You already have a Plivo voice agent — STT → LLM → TTS — running over a `<Stream>` WebSocket. Sometimes the agent gets it wrong: captures a retracted item, agrees to a refund it shouldn't, calls `place_order` with the wrong arguments. Today you find out when the customer complains.

`plivo-mirror` watches every agent turn in real time. It runs a small fast LLM scorer **in parallel** with your TTS encoding, and when the agent is about to do the wrong thing, Mirror:

1. Calls `send_clear_audio` to flush the agent's queued audio
2. Speaks a polite buffer line ("Sorry, let me make sure I got that right...")
3. Generates a correction sentence with the same LLM
4. Waits for the buffer's `playedStream` checkpoint (exact, not estimated)
5. Speaks the correction
6. Hands control back to your agent with a one-shot system note so the next turn re-orients correctly

If the agent decides to call an irreversible tool — `place_order`, `charge_card`, `book_flights`, `cancel_subscription`, `send_email`, `process_payment`, `transfer_funds`, `send_sms` — the **pre-tool-call gate** scores the tool args against the customer's intent and blocks the call if they don't match. Mid-call, before any money moves.

## v1 highlights

- **Single-layer LLM scorer.** No regex rule engines. You write 3-5 plain-English policies; Mirror compiles them into a judging prompt.
- **Tiered scoring.** Cheap heuristics decide if the scorer LLM needs to run. ~80% cost reduction on average traffic.
- **Streaming-native.** Score the first sentence as your LLM streams to TTS — well before the full response is on the wire. Compatible with OpenAI Realtime, Gemini Live, Deepgram Voice Agent.
- **Pre-tool-call gate.** Block bad side effects, not just bad speech.
- **WebSocket-native intervention.** Uses Plivo's `send_clear_audio` + `send_media` + `send_checkpoint` primitives. Zero sleep heuristics.
- **Pluggable everything.** LLM client, state store, TTS sink — all behind small protocols.
- **Zero import-time side effects.** Importing `plivo_mirror` never monkey-patches anything. Safe to embed.
- **Replay CLI** for offline policy + threshold tuning before you go live.

## 30-second quickstart

```bash
pip install plivo-mirror[openai,plivo]
```

```python
from plivo_mirror import Supervisor, MirrorConfig
from plivo_mirror.llm.openai import OpenAIClient

supervisor = Supervisor(MirrorConfig(
    llm=OpenAIClient(api_key="...", model="gpt-4o-mini"),
    policies=[
        "Never confirm a refund — transfer to a human supervisor.",
        "Always read the order back before calling place_order.",
        "Do not promise delivery times.",
    ],
    intervention_threshold=0.7,
))

# In your existing Plivo Stream handler:
async with supervisor.attach(handler, tts_provider=my_tts) as sup:
    @handler.on_start
    async def _(event):
        sup.bind_call(event.start.call_id)

    # When your agent produces a turn:
    verdict = await sup.review_turn(
        customer_text=user_text,
        primary_text=agent_text,
        tool_calls=tool_intents,
    )
    if verdict.should_intervene:
        await sup.intervene(verdict)   # Mirror speaks the correction
    else:
        await sup.speak(agent_text)    # happy path

    await handler.start()
```

Full runnable example at [`examples/quickstart/`](examples/quickstart/).

## Tune before going live

Replay recorded calls against your policies and see exactly which turns would have triggered intervention:

```bash
python -m plivo_mirror.replay calls.json \
    --policies policies.txt \
    --threshold-sweep 0.5,0.6,0.7,0.8
```

Pick the threshold with the right intervention rate for your domain.

## Architecture (one-line tour)

```
plivo_mirror/
├── supervisor.py            # Supervisor + CallSupervisor — the public surface
├── config.py                # MirrorConfig (pydantic) — the only config object
├── context.py               # SupervisorContext, Verdict, TurnPayload, ToolCallIntent
├── scorer/
│   ├── llm.py               # LLMScorer — the one detection layer
│   ├── pregate.py           # tiered cost-saver (heuristic gate)
│   ├── streaming.py         # streaming-aware variant
│   └── tool_gate.py         # pre-tool-call gate
├── policy/compiler.py       # plain-English policies → judging prompt
├── intervention/
│   ├── orchestrator.py      # clear → buffer → correction sequencing
│   ├── generator.py         # LLM correction text
│   └── templates.py         # generic fallback templates
├── voice/tts/
│   ├── ws_inject.py         # PlivoStreamTTSSink — primary path
│   └── plivo_speak.py       # PlivoRESTTTSSink — fallback for unidi streams
├── plivo/
│   ├── stream_sdk.py        # plivo-stream binding
│   └── raw_ws.py            # raw FastAPI WebSocket adapter
├── state/
│   ├── base.py              # StateStore protocol (async)
│   └── memory.py            # InMemoryStateStore (default)
├── llm/
│   ├── base.py              # LLMClient protocol
│   └── openai.py            # OpenAI + Azure auto-detect
└── replay.py                # offline tuning CLI
```

## Configuration reference

| Field | Default | Notes |
|---|---|---|
| `llm` | required | An `LLMClient` implementation. Bring your own; `OpenAIClient` ships built-in. |
| `policies` | one required | Plain-English rules. Mutually exclusive with `judging_prompt`. |
| `judging_prompt` | — | Full prompt override for power users. `{customer_text}`, `{primary_response}`, `{tool_calls_json}`, `{history_summary}` slots. |
| `intervention_threshold` | `0.7` | `Verdict.score >= threshold` triggers intervention. |
| `cooldown_s` | `10.0` | Seconds to suppress further interventions after one fires. |
| `semantic_review_timeout_s` | `4.0` | Per-turn scorer timeout. Fails open. |
| `tiered_scoring_enabled` | `True` | Cheap heuristic pre-gate. Saves ~80% of scorer LLM calls. |
| `streaming_mode` | `False` | Score the first-sentence boundary mid-stream (for OpenAI Realtime / Gemini Live style). |
| `tool_gate_enabled` | `True` | Inspect tool calls before they fire. |
| `irreversible_tools` | sensible default list | Tool names that always go through the gate, regardless of `tool_gate_enabled`. |
| `buffer_text` | "Sorry, let me make sure I got that right..." | Played while the correction is generated. |
| `tenant_id` | `None` | Threaded through `SupervisorContext`; v2 keys per-tenant state on it. |
| `secrets` | `None` | Optional callable for Vault / AWS-SM secret resolution. |

## What's NOT in v1

These are explicitly v2 — designed-around but not implemented yet:

- **Post-call failure reports** (`reports/` package) — `Verdict.should_report` is already plumbed.
- **Fix-as-PR pipeline** (`fixers/` package) — auto-opens GitHub/GitLab PRs to fix the underlying agent prompt.
- **Durable state** (`state/redis.py`, `state/postgres.py`) — slot in behind the existing `StateStore` protocol.
- **Multi-tenant SaaS posture** — `tenant_id` is already on `SupervisorContext`; v2 wires the rest.
- **Auto-derived judging prompt** from the customer's existing agent system prompt (zero-config UX). v1 requires explicit `policies` or `judging_prompt`.

Also explicitly **not** in v1:
- Domain-specific defaults. No pizza vocabulary, no "ordering" recipe, no "booking" recipe. Generic library; bring your own policies.
- A dashboard. Mirror is a library, not a service. The legacy hackathon dashboard lives at the repo root as a reference; see `examples/legacy_demo/`.
- A dollar-value-saved calculator. The legacy code had pizza-shop churn math; it doesn't generalise and isn't a library concern.

## Compatibility

- Python 3.10+
- Plivo Stream API with **bidirectional** WebSocket (`bidirectional="true"`) for the primary intervention path. Use `PlivoRESTTTSSink` as a fallback on unidirectional streams (no `clear_audio`; estimated playback duration).
- OpenAI + Azure OpenAI built-in; any other provider via the `LLMClient` protocol (one async method).

## Running the tests

```bash
pip install plivo-mirror[dev]
pytest tests/unit/
```

The unit tests cover the LLM scorer across three distinct domains (pizza, flight booking, customer support), the tiered pre-gate heuristics, streaming, the tool gate, and a no-import-side-effects check.

## Legacy hackathon code

The original hackathon implementation lives at the repository root (`main.py`, `agent/`, `agents/`, `dashboard/`, `mirror/`, `voice/`, `db.py`, `prompts.py`). It's preserved for archaeology but **not** packaged in the wheel — `pyproject.toml` excludes it. See [`examples/legacy_demo/README.md`](examples/legacy_demo/README.md) for context.

## License

Apache-2.0.
