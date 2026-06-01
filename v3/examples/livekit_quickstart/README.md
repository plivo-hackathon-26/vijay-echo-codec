# LiveKit + plivo-mirror quickstart

A complete supervised LiveKit voice agent in ~60 lines. Catches its
own mistakes mid-call.

## What changed in v0.3.0

In v0.2.0 you had to hand-write ~1000 lines of glue (custom
`mirror_supervisor.py`, llm_node override, ChatContext mutation,
cooldown bookkeeping, customer-voice / meta-description filters,
sticky-intent-note plumbing).

In v0.3.0 it's:

```python
from plivo_mirror import Supervisor
from plivo_mirror.adapters.livekit import SupervisedAgent

supervisor = Supervisor.from_env(policies=[...])

class MyAgent(SupervisedAgent):
    def __init__(self):
        super().__init__(supervisor=supervisor, instructions=PROMPT)
```

That's it. The adapter handles:

- Customer-text extraction from LiveKit's ChatContext (across v1.x
  shape changes).
- LLM-stream buffering so Mirror can inspect tool_calls before they
  fire.
- Sticky intent-note injection on the post-correction turn so the
  agent doesn't ask the caller to repeat themselves.
- Cooldown to suppress duplicate corrections when LiveKit's
  preemptive-generation re-invokes `llm_node`.
- Agent-voice / meta-description filtering on the spoken correction.
- Skip-on-empty greeting turns.

## Setup

```bash
pip install "plivo-mirror[livekit,openai] ~= 0.3.0"

# Required for LiveKit
export LIVEKIT_URL=...
export LIVEKIT_API_KEY=...
export LIVEKIT_API_SECRET=...

# Required for the voice pipeline
export DEEPGRAM_API_KEY=...
export ELEVENLABS_API_KEY=...

# Required for the LLM (Mirror auto-detects which one to use)
# Azure (recommended)
export AZURE_OPENAI_API_KEY=...
export AZURE_OPENAI_ENDPOINT=https://x.openai.azure.com
export AZURE_OPENAI_DEPLOYMENT=gpt-5-mini
# …or vanilla OpenAI
export OPENAI_API_KEY=sk-...
# …or Hugging Face Inference
export HF_API_KEY=hf_...
```

`Supervisor.from_env()` picks the first available provider in
priority order **Atla → Azure → OpenAI → Hugging Face**, or you can
force one with `MIRROR_TIER2=azure|openai|hf|atla|none`.

## Run

```bash
python agent.py dev
```

…then connect a LiveKit web/iOS/Android client to the worker room.

## What Mirror catches in this example

| Customer | Mirror | Outcome |
|---|---|---|
| *"BLT, no wait — cheese sandwich"* | flags contradiction | Mirror speaks *"Got it — one cheese sandwich. Anything else?"* and the `place_order` tool sees `["cheese sandwich"]` only |
| *"I want a cheese sandwich for myself and a BLT for my friend"* | flags multiple recipients | Mirror confirms both items before tool fires |
| *"Place my usual"* | flags missing context | Mirror offers a human handoff |

## Going further

- Add custom policies for your domain (`POLICIES = [...]` at the top
  of `agent.py`).
- Drop in a stricter judge: `MIRROR_TIER2=hf` with a 70B Llama model
  for production accuracy.
- Inspect verdicts post-hoc by setting `MIRROR_REPORT_SINK=sqlite:///path/to/mirror.db`.
