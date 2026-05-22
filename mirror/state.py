"""In-memory per-call state for Mirror.

Holds:
- intervention_pending: the pattern_result dict that the consumer side
  of Mirror (voice/stream.py + mirror/interventions.py) should act on
  before the primary agent generates its response.
- cooldown_until: monotonic deadline before which all rules are skipped.
  Used to prevent re-firing on the customer's immediate confirmation
  turn after an intervention.

Durable Mirror findings live in the mirror_events / interventions
SQLite tables — this module is process-local only.
"""

import threading
import time
from collections import defaultdict


def _new_entry() -> dict:
    return {
        "intervention_pending": None,
        "cooldown_until": 0.0,
        "post_correction_override": None,
    }


_lock = threading.Lock()
_state: dict = defaultdict(_new_entry)


def set_intervention_pending(call_uuid: str, pattern_result: dict) -> None:
    with _lock:
        _state[call_uuid]["intervention_pending"] = pattern_result


def get_intervention_pending(call_uuid: str):
    with _lock:
        return _state[call_uuid].get("intervention_pending")


def clear_intervention_pending(call_uuid: str) -> None:
    with _lock:
        _state[call_uuid]["intervention_pending"] = None


def set_cooldown(call_uuid: str, seconds: float) -> None:
    with _lock:
        _state[call_uuid]["cooldown_until"] = time.monotonic() + float(seconds)


def is_in_cooldown(call_uuid: str) -> bool:
    with _lock:
        return _state[call_uuid].get("cooldown_until", 0.0) > time.monotonic()


def set_post_correction_override(call_uuid: str, note: str) -> None:
    """One-shot system note to inject into the next primary-agent turn.

    Used after a self-correct intervention to neutralize the rigged
    item-capture rule so the agent doesn't re-extract items from the
    contradictory history turn.
    """
    with _lock:
        _state[call_uuid]["post_correction_override"] = note


def get_post_correction_override(call_uuid: str):
    with _lock:
        return _state[call_uuid].get("post_correction_override")


def clear_post_correction_override(call_uuid: str) -> None:
    with _lock:
        _state[call_uuid]["post_correction_override"] = None


def cleanup_call(call_uuid: str) -> None:
    with _lock:
        _state.pop(call_uuid, None)
