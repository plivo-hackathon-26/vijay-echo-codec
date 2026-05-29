"""Wire the plivo_mirror Supervisor for this voice agent.

One Supervisor per process — instantiated at import time, attached
per-call inside main.py's WebSocket handler.

Policies are plain English. Edit this file to change what Mirror
considers a violation; no library code changes needed.
"""

from __future__ import annotations

import os

from dotenv import find_dotenv, load_dotenv

# Make sure .env is loaded even if this module is imported before main.py
# runs its load_dotenv() (e.g. by tests or the replay CLI).
load_dotenv(find_dotenv())

from plivo_mirror import MirrorConfig, Supervisor  # noqa: E402
from plivo_mirror.llm.openai import OpenAIClient  # noqa: E402


# Policies the supervisor enforces on every agent turn.
# Each policy is a complete English sentence; order doesn't matter
# (Mirror's scorer reads them as a numbered list).
POLICIES = [
    "Never confirm a refund — always transfer the caller to a human supervisor instead.",
    "Always read the customer's order back to them before calling place_order.",
    "If the customer changes their mind, the LATEST stated preference wins. The retracted item or destination is NOT part of the order.",
    "Treat third-party preferences ('my wife wants X', 'my friend ordered Y') as context only — the caller's order is what THEY personally said they want.",
    "Do not promise specific delivery times. Refer the caller to the kitchen estimate if asked.",
]


def build_supervisor() -> Supervisor:
    """Construct the singleton Supervisor for this agent."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY must be set")

    llm = OpenAIClient(
        api_key=api_key,
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        base_url=os.getenv("OPENAI_API_URL"),
    )

    threshold = float(os.getenv("PLIVO_MIRROR_THRESHOLD", "0.7"))
    cooldown = float(os.getenv("PLIVO_MIRROR_COOLDOWN_S", "10"))
    timeout = float(os.getenv("PLIVO_MIRROR_TIMEOUT_S", "4.0"))

    return Supervisor(MirrorConfig(
        llm=llm,
        policies=POLICIES,
        intervention_threshold=threshold,
        cooldown_s=cooldown,
        semantic_review_timeout_s=timeout,
        tiered_scoring_enabled=True,
        tool_gate_enabled=True,
        irreversible_tools=[
            "place_order",
            "charge_card",
            "process_refund",
            "cancel_order",
        ],
        buffer_text="Sorry, let me make sure I got that right — just a moment...",
    ))


# Singleton — imported by main.py once at startup.
supervisor: Supervisor = build_supervisor()


__all__ = ["supervisor", "build_supervisor", "POLICIES"]
