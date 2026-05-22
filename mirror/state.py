"""In-memory per-call state for Mirror.

Holds the intervention_pending flag that Phase 3 will read before the
primary agent generates its response. Pure in-memory — durable Mirror
findings live in the mirror_events SQLite table.
"""

import threading
from collections import defaultdict

_lock = threading.Lock()
_state: dict = defaultdict(lambda: {"intervention_pending": None})


def set_intervention_pending(call_uuid: str, pattern_result: dict) -> None:
    with _lock:
        _state[call_uuid]["intervention_pending"] = pattern_result


def get_intervention_pending(call_uuid: str):
    with _lock:
        return _state[call_uuid].get("intervention_pending")


def clear_intervention_pending(call_uuid: str) -> None:
    with _lock:
        _state[call_uuid]["intervention_pending"] = None


def cleanup_call(call_uuid: str) -> None:
    with _lock:
        _state.pop(call_uuid, None)
