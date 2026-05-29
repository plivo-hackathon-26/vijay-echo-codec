# Quickstart — supervise a Plivo voice agent in 20 lines

Drop `plivo-mirror` into an existing Plivo voice-agent project to add
real-time supervision, mid-call self-correct, and pre-tool-call gating.

## Install

```bash
pip install plivo-mirror[openai,plivo]
```

## The whole thing

```python
from fastapi import FastAPI, WebSocket
from plivo_stream import PlivoFastAPIStreamingHandler

from plivo_mirror import Supervisor, MirrorConfig
from plivo_mirror.llm.openai import OpenAIClient

supervisor = Supervisor(MirrorConfig(
    llm=OpenAIClient(api_key="...", model="gpt-4o-mini"),
    policies=[
        "Never confirm a refund — transfer to a human supervisor.",
        "Always read the order back before placing it.",
        "Don't promise delivery times.",
    ],
    intervention_threshold=0.7,
))

app = FastAPI()

@app.websocket("/stream")
async def stream(ws: WebSocket):
    handler = PlivoFastAPIStreamingHandler(ws)
    async with supervisor.attach(handler, tts_provider=my_tts) as sup:

        @handler.on_start
        async def _(event):
            sup.bind_call(event.start.call_id)

        # Wherever your agent runs an LLM and decides to speak:
        async def on_customer_turn(customer_text: str):
            sup.note_customer_turn(customer_text)
            # consume_override() returns Mirror's one-shot system note
            # if the previous turn was an intervention — inject it as an
            # extra system message so your agent re-orients correctly.
            override = await sup.consume_override()
            agent_text, tool_calls = await my_agent.run(
                customer_text, history=sup.history, system_note=override
            )

            # Mirror reviews; if a tool call is gated, block + intervene.
            verdict = await sup.review_turn(
                customer_text=customer_text,
                primary_text=agent_text,
                tool_calls=tool_calls,
            )
            if verdict.should_intervene:
                await sup.intervene(verdict)
                return

            # Happy path.
            await execute_tool_calls(tool_calls)
            await sup.speak(agent_text)

        await handler.start()
```

That's it. Everything else — pattern matching, demo scaffolding, value
calculators — is **not** in the library and is not needed.

## Tune the threshold before going live

```bash
python -m plivo_mirror.replay my_recorded_calls.json \
  --policies policies.txt \
  --threshold-sweep 0.5,0.6,0.7,0.8
```

Outputs per-threshold intervention rate so you can pick the sweet spot
before any customer hears Mirror in production.

## Streaming agents (OpenAI Realtime, Gemini Live, etc.)

Set `MirrorConfig.streaming_mode=True` and feed deltas as they arrive:

```python
async for delta in primary_llm_stream:
    audio = await tts.encode(delta)
    await sup._tts.send_media(audio)  # or your own TTS pipeline
    verdict = await sup.review_stream_delta(
        customer_text=user_text, delta=delta, tool_calls=[]
    )
    if verdict and verdict.should_intervene:
        await sup.intervene(verdict)
        break
```

Mirror fires at the first sentence boundary — well before the full
agent response is on the wire.

## Pre-tool-call gate

Irreversible tools (`place_order`, `charge_card`, `book_flights`,
`cancel_subscription`, `send_email`, `process_payment`,
`transfer_funds`, `send_sms`) are gated by default. Customize:

```python
MirrorConfig(
    ...,
    irreversible_tools=["charge_card", "send_legal_notice", "ship_package"],
)
```

Before executing any tool, ask the gate:

```python
verdict = await sup.gate_tool_call(
    customer_text=customer_text, intents=tool_intents,
)
if verdict.should_intervene:
    await sup.intervene(verdict)   # block + ask for confirmation
else:
    await my_tool_executor.run(tool_intents)
```
