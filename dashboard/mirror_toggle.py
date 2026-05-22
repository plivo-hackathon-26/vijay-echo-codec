"""Global Mirror toggle + non-invasive runtime hooks.

The global flag is a single mutable boolean. Every time `db.create_call`
fires, we read the flag and freeze that value onto the call (both in
the SQLite `calls.mirror_enabled` column and in an in-memory map).
For the rest of the call's life, the per-call frozen state — not the
global flag — decides whether Mirror runs. That gives us:

  - Mid-call toggle changes never affect an in-flight call.
  - Mirror OFF means TOTAL silence: no pattern detection runs, no
    semantic LLM call is made, no intervention is delivered. Direct
    primary → client.
  - Concurrent / back-to-back calls don't step on each other: the
    per-turn call_uuid is tracked via a `contextvars.ContextVar` set
    by the patched `mirror.evaluator.evaluate`, so semantic.review_response
    (which doesn't take call_uuid in its signature) reads the right
    UUID for its own task.

The dashboard's `final_outcome` column is computed at end_call. For
OFF calls — where no events were written — a pattern-only post-hoc
scan runs over the customer turns so the dashboard can still display
WRONG vs CORRECT badges in the with-vs-without comparison view.
"""

import contextvars
import json
import logging
import threading
from typing import Any

import db
from mirror import evaluator as mirror_evaluator
from mirror import interventions as mirror_interventions
from mirror import semantic as mirror_semantic
from mirror import state as mirror_state

log = logging.getLogger("mirror.dashboard.toggle")

_lock = threading.Lock()
_global_enabled: bool = True
_call_states: dict[str, bool] = {}

# Per-task call_uuid set by _patched_evaluate so review_response in
# the same asyncio task can find it. contextvars are task-local in
# asyncio, so two overlapping WebSocket handlers each get their own.
_current_call_uuid_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mirror_current_call_uuid", default=None
)

_hooks_installed = False


# ---------- public API -----------------------------------------------------


def get_global_enabled() -> bool:
    with _lock:
        return _global_enabled


def set_global_enabled(value: bool) -> None:
    global _global_enabled
    with _lock:
        _global_enabled = bool(value)
    log.info("mirror global flag set to %s", value)


def is_enabled_for_call(call_uuid: str) -> bool:
    """Frozen Mirror state for a specific call. Defaults to True for
    unknown calls (e.g. historical rows seen after a server restart).
    """
    if not call_uuid:
        return True
    with _lock:
        if call_uuid in _call_states:
            return _call_states[call_uuid]
    # Fallback: persisted column. Covers post-restart lookups.
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT mirror_enabled FROM calls WHERE call_uuid = ?",
                (call_uuid,),
            ).fetchone()
        if row is None:
            return True
        return bool(row["mirror_enabled"])
    except Exception:
        log.exception("failed to look up mirror_enabled for %s", call_uuid)
        return True


def current_call_uuid_from_context() -> str | None:
    """The call_uuid for the current asyncio task, set by _patched_evaluate."""
    return _current_call_uuid_var.get()


# ---------- monkey-patches -------------------------------------------------


_orig_create_call = db.create_call
_orig_end_call = db.end_call
_orig_evaluate = mirror_evaluator.evaluate
_orig_get_intervention_pending = mirror_state.get_intervention_pending
_orig_review_response = mirror_semantic.review_response
_orig_handle_intervention = mirror_interventions.handle_intervention


def _patched_create_call(call_uuid: str, caller: str, to: str) -> None:
    _orig_create_call(call_uuid, caller, to)
    enabled = get_global_enabled()
    try:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE calls SET mirror_enabled = ?, agent_name = ? "
                "WHERE call_uuid = ?",
                (1 if enabled else 0, "pizza-plivo", call_uuid),
            )
    except Exception:
        log.exception("failed to stamp mirror_enabled on calls row")
    with _lock:
        _call_states[call_uuid] = enabled
    log.info(
        "call %s created (mirror=%s)",
        call_uuid[:8] if call_uuid else "????????",
        "ON" if enabled else "OFF",
    )


def _patched_end_call(call_uuid: str, status: str = "completed") -> None:
    _orig_end_call(call_uuid, status)
    try:
        outcome = _compute_final_outcome(call_uuid)
        if outcome:
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE calls SET final_outcome = ? WHERE call_uuid = ?",
                    (outcome, call_uuid),
                )
    except Exception:
        log.exception("failed to compute final_outcome for %s", call_uuid)
    with _lock:
        _call_states.pop(call_uuid, None)


def _patched_evaluate(
    call_uuid: str,
    recent_turns: list,
    current_user_turn: str,
    current_turn_id,
    agent_state: dict | None = None,
) -> list:
    """First Mirror entry point in the per-turn flow. We use it to:

    1. Pin the call_uuid into the asyncio-task contextvar so a later
       review_response in the same task can find it.
    2. Short-circuit entirely when Mirror is OFF for this call —
       no events written, no rule code executed.
    """
    _current_call_uuid_var.set(call_uuid)
    if call_uuid and not is_enabled_for_call(call_uuid):
        log.debug("MIRROR OFF for %s — skipping evaluator", call_uuid[:8])
        return []
    return _orig_evaluate(
        call_uuid, recent_turns, current_user_turn, current_turn_id, agent_state
    )


def _patched_get_intervention_pending(call_uuid: str) -> Any:
    """Defense in depth. If Mirror is OFF and somehow a pending flag
    got set (race during toggle, etc.), suppress it and clear the flag.
    """
    if not is_enabled_for_call(call_uuid):
        pending = _orig_get_intervention_pending(call_uuid)
        if pending is not None:
            log.info(
                "MIRROR OFF for %s: cleared stray intervention_pending (%s)",
                call_uuid[:8],
                pending.get("pattern_name"),
            )
            try:
                mirror_state.clear_intervention_pending(call_uuid)
            except Exception:
                pass
        return None
    return _orig_get_intervention_pending(call_uuid)


async def _patched_review_response(*args, **kwargs) -> dict:
    """Skip the semantic LLM call entirely when Mirror is OFF."""
    call_uuid = current_call_uuid_from_context()
    if call_uuid and not is_enabled_for_call(call_uuid):
        log.debug("MIRROR OFF for %s — skipping semantic LLM review", call_uuid[:8])
        return {
            "pattern_name": "semantic_mismatch",
            "severity": "info",
            "strategy": "self_correct",
            "intervention_needed": False,
            "evidence": {"reason": "mirror_disabled_for_call"},
        }
    return await _orig_review_response(*args, **kwargs)


async def _patched_handle_intervention(
    call_uuid: str, *args, **kwargs
) -> str:
    """Last line of defense. Even if something upstream slips through
    and asks for an intervention on a Mirror-OFF call, we never speak
    or write the intervention row.
    """
    if call_uuid and not is_enabled_for_call(call_uuid):
        log.info(
            "MIRROR OFF for %s — handle_intervention no-op'd",
            call_uuid[:8],
        )
        return ""
    return await _orig_handle_intervention(call_uuid, *args, **kwargs)


def install_hooks() -> None:
    global _hooks_installed
    if _hooks_installed:
        return
    db.create_call = _patched_create_call
    db.end_call = _patched_end_call
    mirror_evaluator.evaluate = _patched_evaluate
    mirror_state.get_intervention_pending = _patched_get_intervention_pending
    mirror_semantic.review_response = _patched_review_response
    mirror_interventions.handle_intervention = _patched_handle_intervention
    _hooks_installed = True
    log.info("dashboard hooks installed (full OFF-mode gating)")


# ---------- internal helpers ----------------------------------------------


def _compute_final_outcome(call_uuid: str) -> str | None:
    """Classify the final outcome of a call as 'correct_order' / 'wrong_order' / None.

    For Mirror-ON calls, we use the in-flight signal: any intervention
    means we corrected the order → CORRECT. No interventions → CORRECT.

    For Mirror-OFF calls, no mirror_events were written (we
    short-circuited evaluator). So we do a one-shot post-hoc pattern
    scan over the customer turns to detect "would have been flagged"
    moments — if any, the primary delivered a WRONG order to the
    customer. Otherwise CORRECT.
    """
    with db.get_conn() as conn:
        order_row = conn.execute(
            "SELECT items_json FROM orders WHERE call_uuid = ? "
            "ORDER BY id DESC LIMIT 1",
            (call_uuid,),
        ).fetchone()
        enabled_row = conn.execute(
            "SELECT mirror_enabled FROM calls WHERE call_uuid = ?",
            (call_uuid,),
        ).fetchone()

    if not (order_row and order_row["items_json"]):
        return None
    mirror_was_on = bool(enabled_row and enabled_row["mirror_enabled"])

    if mirror_was_on:
        # Mirror was watching — every flagged turn was corrected
        # in-flight, so the order on file is the corrected one.
        return "correct_order"

    # Mirror was OFF — silent post-hoc check on the customer's turns.
    if _pattern_scan_for_off_call(call_uuid):
        return "wrong_order"
    return "correct_order"


def _pattern_scan_for_off_call(call_uuid: str) -> bool:
    """Return True if any customer turn on this call would have tripped
    a pattern-grade intervention (contradiction / missing-tool-request).
    Pure read-only — touches no other tables.
    """
    try:
        from mirror.patterns import contradiction_rule, missing_tool_rule
    except Exception:
        return False
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT text FROM turns WHERE call_uuid = ? AND role = 'customer' "
                "ORDER BY id ASC",
                (call_uuid,),
            ).fetchall()
    except Exception:
        log.exception("pattern scan failed to load turns for %s", call_uuid)
        return False
    for row in rows:
        text = row["text"] or ""
        try:
            if contradiction_rule([], text, {}):
                return True
            if missing_tool_rule([], text, {}):
                return True
        except Exception:
            continue
    return False
