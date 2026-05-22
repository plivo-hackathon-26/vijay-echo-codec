"""Mirror evaluator — runs pattern rules, persists fires, sets flags.

Called synchronously after every customer turn is written to the turns
table, BEFORE the primary agent's run_turn fires. Phase 2 is observation
only; Phase 3 will read intervention_pending from mirror.state to alter
agent behavior.
"""

import logging

import db
from mirror import state
from mirror.patterns import ALL_RULES

log = logging.getLogger("mirror.evaluator")

_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


def _color_for(severity: str) -> str:
    if severity == "intervention":
        return _YELLOW
    return _CYAN


def _log_to_stdout(call_uuid: str, result: dict) -> None:
    color = _color_for(result["severity"])
    prefix = call_uuid[:8] if call_uuid else "????????"
    print(
        f"{color}⚠ MIRROR [{prefix}]: "
        f"{result['pattern_name']} ({result['severity']}) | "
        f"evidence={result['evidence']}{_RESET}",
        flush=True,
    )


def evaluate(
    call_uuid: str,
    recent_turns: list,
    current_user_turn: str,
    current_turn_id,
    agent_state: dict | None = None,
) -> list:
    """Run all rules; persist fires; set intervention flag if applicable.

    Returns the list of fired event dicts (possibly empty).
    """
    agent_state = agent_state or {}
    results: list = []

    for rule in ALL_RULES:
        try:
            fire = rule(recent_turns, current_user_turn, agent_state)
        except Exception:
            log.exception("rule %s raised; continuing", rule.__name__)
            continue
        if not fire:
            continue

        try:
            db.add_mirror_event(
                call_uuid=call_uuid,
                turn_id=current_turn_id,
                pattern_name=fire["pattern_name"],
                severity=fire["severity"],
                evidence_dict=fire["evidence"],
                intervention_needed=bool(fire["intervention_needed"]),
            )
        except Exception:
            log.exception("failed to persist mirror_event for %s", fire["pattern_name"])

        if fire["intervention_needed"]:
            state.set_intervention_pending(call_uuid, fire)

        _log_to_stdout(call_uuid, fire)
        results.append(fire)

    return results
