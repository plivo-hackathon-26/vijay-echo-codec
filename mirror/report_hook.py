"""Non-invasive hook: trigger a post-call failure report when a call ends.

Phase 5 already monkey-patched db.end_call once (for final_outcome
computation in dashboard.mirror_toggle). We can't edit that patch, but
we CAN wrap whatever the current db.end_call is at the moment install_hook
runs and chain a fire-and-forget background task onto it.

Result: regardless of how many layers of wrapping db.end_call has, this
hook runs LAST and schedules report generation.

The task is fire-and-forget — we don't await it. The call hangup path
should not be slowed by an LLM round-trip.
"""

import asyncio
import logging

import db as _db
from mirror.reporter import generate_failure_report

log = logging.getLogger("mirror.report_hook")

_installed = False


def install_hook() -> None:
    """Idempotently chain a background report-generation task onto whatever
    db.end_call currently resolves to at install time.

    Safe to call multiple times — re-running is a no-op.
    Safe to call before any event loop exists — falls through silently if
    asyncio.get_event_loop() finds none (e.g. test context).
    """
    global _installed
    if _installed:
        return

    base_end_call = _db.end_call

    def patched_end_call(call_uuid, status="completed", *args, **kwargs):
        result = base_end_call(call_uuid, status, *args, **kwargs)
        if call_uuid:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop (e.g. test/synchronous context) — skip silently.
                return result
            try:
                loop.create_task(generate_failure_report(call_uuid))
                log.info(
                    "scheduled failure_report generation for %s",
                    call_uuid[:8] if call_uuid else "????????",
                )
            except Exception:
                log.exception("failed to schedule failure_report for %s", call_uuid)
        return result

    _db.end_call = patched_end_call
    _installed = True
    log.info("report_hook installed (db.end_call wrapped)")
