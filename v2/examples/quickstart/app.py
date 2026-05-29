"""Minimal runnable quickstart for plivo-mirror.

Run with:
    OPENAI_API_KEY=... uvicorn examples.quickstart.app:app --port 8000

Then point a Plivo Stream XML at wss://<host>/stream and call the
attached number.

This file is intentionally tiny — it shows the WHOLE Mirror integration
in under 80 lines including imports and docstrings.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, WebSocket

from plivo_mirror import MirrorConfig, Supervisor, ToolCallIntent
from plivo_mirror.llm.openai import OpenAIClient


# ─── 1. Construct ONE Supervisor per process. ─────────────────────────────

supervisor = Supervisor(
    MirrorConfig(
        llm=OpenAIClient(
            api_key=os.environ["OPENAI_API_KEY"],
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            base_url=os.getenv("OPENAI_API_URL"),
        ),
        policies=[
            "Never confirm a refund — transfer the caller to a human supervisor.",
            "Always read the customer's order back before calling place_order.",
            "Do not promise delivery times.",
            "If the customer retracts an item ('actually', 'no', 'instead'), "
            "the retracted item is NOT part of their order.",
        ],
        intervention_threshold=0.7,
        cooldown_s=10,
    )
)


# ─── 2. Your existing voice agent — Mirror is transparent to it. ──────────


async def my_agent_run(customer_text: str, history, system_note: str | None) -> tuple[str, list[ToolCallIntent]]:
    """Replace this with your real agent. The signature is what
    Supervisor.consume_override returns."""
    # Pretend the agent decided what to say + which tools to call.
    return ("Got it, one large cheese pizza coming up.", [
        ToolCallIntent(name="place_order", args={"items": ["large cheese"]})
    ])


async def my_tts(text: str) -> bytes:
    """Replace with your TTS provider — ElevenLabs, Deepgram, Cartesia, etc.
    Must return mulaw 8kHz bytes for Plivo's audio stream."""
    raise NotImplementedError("Wire up your TTS provider here")


async def execute_tools(intents: list[ToolCallIntent]) -> None:
    """Fire the tool calls Mirror allowed through."""
    for tc in intents:
        print(f"executing {tc.name} with {tc.args}")


# ─── 3. The FastAPI WebSocket. ────────────────────────────────────────────


app = FastAPI()


@app.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    # In a real app you'd use plivo-stream's PlivoFastAPIStreamingHandler
    # here. For the quickstart we use the raw-WS adapter so this file is
    # runnable without the plivo-stream dep.
    from plivo_mirror.plivo.raw_ws import RawPlivoWSAdapter

    await ws.accept()
    handler = RawPlivoWSAdapter(ws)

    async with supervisor.attach(handler, tts_provider=my_tts) as sup:
        # Drive the agent loop yourself. This block is the "your code
        # already exists" part — Mirror just wraps it.
        async for customer_text in customer_turns(ws, handler):
            sup.note_customer_turn(customer_text)
            override = await sup.consume_override()
            agent_text, tool_calls = await my_agent_run(
                customer_text, sup.history, override
            )

            verdict = await sup.review_turn(
                customer_text=customer_text,
                primary_text=agent_text,
                tool_calls=tool_calls,
            )
            if verdict.should_intervene:
                # Mirror handles speaking the correction.
                await sup.intervene(verdict)
                continue

            await execute_tools(tool_calls)
            await sup.speak(agent_text)


async def customer_turns(ws: WebSocket, handler: Any):
    """Bring-your-own STT loop. Yields each customer utterance as text.

    This is where you'd integrate Deepgram / Plivo's STT / Whisper /
    whatever you use. Out of Mirror's scope on purpose.
    """
    raise NotImplementedError("Wire up your STT loop here and yield text")
    yield  # pragma: no cover
