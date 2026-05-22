"""Per-call agent dispatch — non-invasive overlay.

voice/stream.py imports run_turn / run_correction_turn from
agent.primary (pizza). That binding is what we patch — we replace
voice.stream's local references with dispatcher functions that look
at the call's agent_name in the DB and route to the right primary.

Mid-call switches are impossible by design: agent_name is frozen at
create_call time (in mirror_toggle._patched_create_call). Switching
the active agent in the dashboard affects only NEW calls.

The greeting that Plivo speaks at call answer (`prompts.GREETING`)
is also per-agent. We monkey-patch the `GREETING` references in
main.py and voice/stream.py whenever the active agent changes so the
next call's XML answer + transcript seed both use the right text.
"""

import logging

import db
import voice.stream as voice_stream
from agent import primary as pizza_primary
from agents.travel import primary as travel_primary
from agents.travel.prompts import GREETING_TRAVEL
from prompts import GREETING as PIZZA_GREETING

log = logging.getLogger("mirror.dashboard.agent_router")

_hooks_installed = False

# Original references — captured before patching, used as fallback
# for pizza-plivo and for any unknown agent_name.
_orig_run_turn = pizza_primary.run_turn
_orig_run_correction_turn = pizza_primary.run_correction_turn


# ---------- registry ----------------------------------------------------


PRIMARIES = {
    "pizza-plivo": {
        "run_turn": _orig_run_turn,
        "run_correction_turn": _orig_run_correction_turn,
        "greeting": PIZZA_GREETING,
    },
    "travel-plivo": {
        "run_turn": travel_primary.run_turn,
        "run_correction_turn": travel_primary.run_correction_turn,
        "greeting": GREETING_TRAVEL,
    },
}


def known_agents() -> list[str]:
    return list(PRIMARIES.keys())


def greeting_for(agent_name: str) -> str:
    return PRIMARIES.get(agent_name, PRIMARIES["pizza-plivo"])["greeting"]


# ---------- lookup ------------------------------------------------------


def _agent_for_call(call_uuid: str) -> str:
    """Read the persisted agent_name from the calls table."""
    if not call_uuid:
        return "pizza-plivo"
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(agent_name, 'pizza-plivo') AS a "
                "FROM calls WHERE call_uuid = ?",
                (call_uuid,),
            ).fetchone()
        return row["a"] if row else "pizza-plivo"
    except Exception:
        log.exception("agent lookup failed; defaulting to pizza-plivo")
        return "pizza-plivo"


# ---------- dispatchers -------------------------------------------------


async def _dispatched_run_turn(
    call_uuid: str,
    transcript_history,
    extra_system_note=None,
    return_details: bool = False,
):
    agent_name = _agent_for_call(call_uuid)
    impl = PRIMARIES.get(agent_name, PRIMARIES["pizza-plivo"])["run_turn"]
    return await impl(
        call_uuid,
        transcript_history,
        extra_system_note=extra_system_note,
        return_details=return_details,
    )


async def _dispatched_run_correction_turn(
    call_uuid: str,
    transcript_history,
    mirror_evidence,
):
    agent_name = _agent_for_call(call_uuid)
    impl = PRIMARIES.get(agent_name, PRIMARIES["pizza-plivo"])["run_correction_turn"]
    return await impl(call_uuid, transcript_history, mirror_evidence)


# ---------- greeting patching -------------------------------------------


def set_active_greeting(agent_name: str) -> None:
    """Update main.GREETING and voice.stream.GREETING for the NEXT call.

    Both modules bound GREETING at import time via `from prompts import
    GREETING`, so the symbol lives in each module's namespace. We replace
    it; the next /voice/answer + the next WebSocket open both pick it up.
    """
    greeting = greeting_for(agent_name)
    try:
        import main
        main.GREETING = greeting
    except Exception:
        log.exception("could not patch main.GREETING")
    try:
        voice_stream.GREETING = greeting
    except Exception:
        log.exception("could not patch voice.stream.GREETING")
    log.info("greeting switched to agent=%s", agent_name)


# ---------- install -----------------------------------------------------


def install_hooks() -> None:
    global _hooks_installed
    if _hooks_installed:
        return
    voice_stream.run_turn = _dispatched_run_turn
    voice_stream.run_correction_turn = _dispatched_run_correction_turn
    _hooks_installed = True
    log.info("agent router hooks installed (multi-agent dispatch)")
