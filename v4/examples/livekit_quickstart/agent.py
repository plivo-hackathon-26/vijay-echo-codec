"""End-to-end LiveKit example on plivo-mirror v4.

A supervised sandwich-ordering agent. The v4 firewall watches both
boundaries: it grounds the model in validated ``SessionState`` every
turn, corrects fabricated facts / unauthorized commitments before they're
voiced, and blocks tool calls that disagree with confirmed state or that
the caller isn't authorized to make.

The ~5-line integration:
    1. firewall = Firewall.from_env(policies=POLICIES)
    2. class SandwichAgent(SupervisedAgent):
    3.     def __init__(self): super().__init__(firewall=firewall, instructions=PROMPT)
    4.     @function_tool place_order(self)   # reads items from STATE, not args
    5. session.start(agent=SandwichAgent())

Run:
    pip install "plivo-mirror[livekit,openai]"
    # set LIVEKIT_*, DEEPGRAM_API_KEY, ELEVENLABS_API_KEY, and either
    # AZURE_OPENAI_* or OPENAI_API_KEY (for the grounded verifier)
    python agent.py dev
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentSession, JobContext, function_tool
from livekit.plugins import deepgram, elevenlabs, openai, silero

from plivo_mirror import Firewall, NLICrossEncoderSignal
from plivo_mirror.adapters.livekit import SupervisedAgent
from plivo_mirror.contracts import Verdict
from plivo_mirror.state.entities import validate_item

load_dotenv()

# HuggingFace reads HF_TOKEN; accept the HF_API_KEY spelling too so the NLI
# model download authenticates regardless of which name is in .env.
if os.environ.get("HF_API_KEY") and not os.environ.get("HF_TOKEN"):
    os.environ["HF_TOKEN"] = os.environ["HF_API_KEY"]


def _agent_llm() -> "openai.LLM":
    """Resolve the agent's LLM the same way the firewall resolves the
    verifier (single-LLM principle): native Azure vars if present, else an
    OpenAI-compatible endpoint via ``OPENAI_BASE_URL`` / ``OPENAI_API_URL``
    (e.g. Azure's ``/openai/v1`` surface). Keeps agent + verifier on one
    model/endpoint and works across both credential styles."""
    if os.environ.get("AZURE_OPENAI_API_KEY"):
        return openai.LLM.with_azure()
    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_URL")
    return openai.LLM(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        base_url=base_url or None,
        api_key=os.environ.get("OPENAI_API_KEY"),
    )

SYSTEM_PROMPT = """You are SandwichBot, a friendly sandwich-shop voice
agent. Take the order, confirm it, and place it with the place_order tool.
Be concise. Never quote a price or promise a discount you weren't given."""

POLICIES = [
    "Never place an order containing items the customer asked to remove.",
    "If the customer corrects themselves, the final order reflects ONLY the "
    "corrected version.",
    "Never invent menu items, prices, or promotions.",
    "FORBID: full refund",  # demo: a hard deterministic block
]

MENU = {"turkey sub", "veggie wrap", "italian sub", "meatball sub", "club sandwich"}


def _require_confirmed_order(intent, state) -> "Verdict | None":
    """Action-guard gate: block ``place_order`` unless the order is real —
    validated items exist in state AND an intent was confirmed. Stops the
    agent from 'placing' an empty cart or committing off a price question
    (the irreversible-action defense; business rule lives in CODE)."""
    if not state.entity_value("items") or not state.confirmed_intent:
        return Verdict.block(
            reason="place_order with no confirmed items in state",
            policy_id="unconfirmed_order",
        )
    return None


# One firewall per process. The grounded verifier ("last model for
# intervening") is auto-wired from env; pass verifier=... or model=... to
# configure it explicitly. The semantic tier (NLI) routes lexically-invisible
# contradictions to the verifier; the action validator gates irreversible
# placement.
_nli = NLICrossEncoderSignal()
firewall = Firewall.from_env(
    policies=POLICIES,
    semantic_signal=_nli,
    validators={"place_order": [_require_confirmed_order]},
)

# Pre-load the NLI model at startup (~10s) so the FIRST live turn doesn't
# stall on a cold model load. No-op if the optional dependency is missing.
_nli.contradicts("warm", "up")


class SandwichAgent(SupervisedAgent):
    def __init__(self) -> None:
        super().__init__(firewall=firewall, instructions=SYSTEM_PROMPT)

    async def extract_state(self, customer_text: str) -> None:
        """Validate any on-menu items out of the caller's utterance and
        write them to state OUTSIDE the model's context. (Production would
        use a real NLU extractor; this keeps the example self-contained.)"""
        found: list[str] = []
        lowered = customer_text.lower()
        for item in MENU:
            if item in lowered and validate_item(item, catalog=MENU):
                found.append(item)
        if found:
            from plivo_mirror.state.entities import ValidatedEntity

            self.state.set_entity("items", ValidatedEntity("item", found, customer_text))
            self.state.confirm_intent(", ".join(found))

    @function_tool
    async def place_order(self) -> dict:
        """Place the order. Zero-argument: reads the validated items from
        SessionState via the executor helper, never from the model."""
        from plivo_mirror.state import args_from_state

        args = args_from_state(self.state, ["items"])
        self.state.log_committed_action("place_order", args)  # clears intent memory
        return {"placed": True, **args}


async def entrypoint(ctx: JobContext) -> None:
    # ElevenLabs plugin reads ELEVEN_API_KEY; accept the ELEVENLABS_API_KEY
    # spelling too and pass it explicitly so either env name works.
    el_key = os.environ.get("ELEVEN_API_KEY") or os.environ.get("ELEVENLABS_API_KEY")
    el_kw = {"api_key": el_key} if el_key else {}
    if os.environ.get("ELEVENLABS_VOICE_ID"):
        el_kw["voice_id"] = os.environ["ELEVENLABS_VOICE_ID"]
    session = AgentSession(
        vad=silero.VAD.load(),
        stt=deepgram.STT(),
        llm=_agent_llm(),
        tts=elevenlabs.TTS(**el_kw),
    )
    await session.start(agent=SandwichAgent(), room=ctx.room)


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
