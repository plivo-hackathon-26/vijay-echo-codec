"""End-to-end LiveKit example using plivo-mirror ≥ 0.3.0.

What this shows
---------------
A complete supervised LiveKit voice agent in <80 lines of code. The
agent takes sandwich orders and places them via a tool. Mirror
watches every LLM turn, catches contradictions / wrong-voice
corrections / missing-tool-calls, speaks the correction itself, and
injects a sticky intent note so the LLM doesn't ask the customer to
repeat themselves on the next turn.

Compare this to the v0.2.0 hand-rolled integration (~1000 LOC) — the
adapter does the heavy lifting now.

Running it
----------
1. ``pip install "plivo-mirror[livekit,openai] ~= 0.3.0"``
2. Set env vars in ``.env`` or your shell:

   - ``LIVEKIT_URL``, ``LIVEKIT_API_KEY``, ``LIVEKIT_API_SECRET``
   - ``DEEPGRAM_API_KEY`` (STT)
   - ``ELEVENLABS_API_KEY`` (TTS)
   - One of:
       * ``AZURE_OPENAI_API_KEY`` + ``AZURE_OPENAI_ENDPOINT`` +
         ``AZURE_OPENAI_DEPLOYMENT`` — recommended
       * ``OPENAI_API_KEY``
       * ``HF_API_KEY``
3. ``python agent.py dev``
"""

from __future__ import annotations

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentSession, JobContext, function_tool
from livekit.plugins import deepgram, elevenlabs, openai, silero

from plivo_mirror import Supervisor
from plivo_mirror.adapters.livekit import SupervisedAgent

load_dotenv()


SYSTEM_PROMPT = """You are SandwichBot, a friendly sandwich-shop voice
agent. Take the customer's order. Confirm it back to them. Use the
``place_order`` tool when they finish ordering. Be concise."""


POLICIES = [
    "Never place an order containing items the customer asked to remove.",
    "If the customer corrects themselves (e.g. 'actually...', 'no wait, ...'), "
    "the final state of the order must reflect ONLY the corrected version.",
    "Never invent items the customer did not request.",
    "If unsure, repeat back the full order in plain English before placing it.",
]


# One Supervisor per process — created at import time, reused across calls.
supervisor = Supervisor.from_env(policies=POLICIES)


class SandwichAgent(SupervisedAgent):
    def __init__(self) -> None:
        super().__init__(supervisor=supervisor, instructions=SYSTEM_PROMPT)

    @function_tool
    async def place_order(self, items: list[str]) -> dict:
        """Submit the customer's final sandwich order to the kitchen."""
        # Real impl would POST to the POS system — for the example we
        # just echo back what was placed.
        return {"placed": True, "items": items}


async def entrypoint(ctx: JobContext) -> None:
    session = AgentSession(
        vad=silero.VAD.load(),
        stt=deepgram.STT(),
        llm=openai.LLM.with_azure(),  # or openai.LLM() for vanilla OpenAI
        tts=elevenlabs.TTS(),
    )
    await session.start(agent=SandwichAgent(), room=ctx.room)


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
