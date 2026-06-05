"""Wellspring Family Clinic — live LiveKit agent (good healthcare agent).

Prompt/facts/policies live in config.py (shared with the scripted demo).
Run with a real mic:

    cd v5/examples/wellspring_clinic_agent
    ../../../venv/bin/python agent.py console     # local mic/speaker
    #  (or `agent.py dev` against your LiveKit Cloud project)

Register it first in the dashboard (or `run_demo.py` does it for you).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentSession, JobContext, function_tool
from livekit.plugins import deepgram, elevenlabs, openai, silero

from plivo_mirror_v5.engine import EngineConfig, PolicyPack
from plivo_mirror_v5.integrations import attach_mirror

import config  # noqa: E402 — sibling module (run from this dir)

load_dotenv(Path(__file__).resolve().parents[3] / ".env")
logger = logging.getLogger("wellspring-agent")

# Mock clinic data the tools read from (the live agent's "system of record").
_APPOINTMENTS: dict[str, dict] = {}
_REFILLS = {"lisinopril": "active", "amoxicillin": "expired"}


class WellspringAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=config.SYSTEM_PROMPT)

    # >>> mirror pre-TTS gate (no-op passthrough unless intervene is on) <<<
    async def llm_node(self, chat_ctx, tools, model_settings):
        gate = getattr(self, "_mirror_pre_tts", None)

        def default(ctx):
            return Agent.default.llm_node(self, ctx, tools, model_settings)

        if gate is None:
            async for chunk in default(chat_ctx):
                yield chunk
            return
        async for out in gate.gate_stream(chat_ctx, default):
            yield out
    # >>> end mirror pre-TTS gate

    @function_tool
    async def book_appointment(self, patient_name: str, when: str,
                               visit_type: str = "established") -> dict:
        """Book an appointment after the caller confirmed name, DOB, and time.

        Args:
            patient_name: the patient's full name.
            when: the agreed day and time.
            visit_type: "new" or "established".
        """
        _APPOINTMENTS[patient_name] = {"when": when, "type": visit_type}
        logger.info("🔧 book_appointment %s @ %s", patient_name, when)
        return {"booked": True, "patient": patient_name, "when": when}

    @function_tool
    async def submit_refill(self, medication: str) -> dict:
        """Submit a prescription refill request for provider review.

        Args:
            medication: the medication name the caller asked to refill.
        """
        status = _REFILLS.get(medication.lower(), "unknown")
        logger.info("🔧 submit_refill %s (status=%s)", medication, status)
        return {"submitted": True, "medication": medication, "status": status}

    @function_tool
    async def get_refill_status(self, medication: str) -> dict:
        """Look up the status of a prescription on file.

        Args:
            medication: the medication name to check.
        """
        return {"medication": medication,
                "status": _REFILLS.get(medication.lower(), "not on file")}


def _agent_llm() -> "openai.LLM":
    if os.environ.get("AZURE_OPENAI_API_KEY"):
        return openai.LLM.with_azure()
    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_URL")
    return openai.LLM(model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                      base_url=base_url or None,
                      api_key=os.environ.get("OPENAI_API_KEY"))


def prewarm(proc: agents.JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    el_key = os.environ.get("ELEVEN_API_KEY") or os.environ.get("ELEVENLABS_API_KEY")
    el_kw = {"api_key": el_key} if el_key else {}
    session = AgentSession(
        vad=ctx.proc.userdata.get("vad") or silero.VAD.load(),
        stt=deepgram.STT(model="nova-2", language="en"),
        llm=_agent_llm(),
        tts=elevenlabs.TTS(**el_kw),
    )

    await ctx.connect()

    room_id = ctx.room.name
    if room_id == "console":
        import time as _time  # noqa: PLC0415
        room_id = f"console-{int(_time.time())}"
    clinic = WellspringAgent()
    observer = attach_mirror(
        session,
        room_id=room_id,
        backend_url=os.environ.get("MIRROR_BACKEND_URL", "http://localhost:8500"),
        agent_id=config.AGENT_ID,
        agent_version=config.AGENT_VERSION,
        agent=clinic,
        config=EngineConfig(policy=PolicyPack.from_dict(config.POLICY_DICT)),
        action_verbs=config.ACTION_VERBS,
        room=ctx.room,
    )
    ctx.add_shutdown_callback(lambda: observer.close())
    logger.info("plivo-mirror v5 attached (mode=%s) — call %s",
                observer.mode, room_id)

    await session.start(agent=clinic, room=ctx.room)
    await session.generate_reply(
        instructions="Greet the caller warmly as Wellspring Family Clinic and "
        "ask how you can help. One sentence.")


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
