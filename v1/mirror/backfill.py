"""Backfill failure_reports for calls that ended BEFORE Phase H existed.

Walks every `calls` row that has at least one mirror_events row with
intervention_needed=1 and no existing failure_reports row, then runs
the standard reporter for each.

Two entry points:
  - python -m mirror.backfill        (CLI, asyncio.run)
  - mirror.backfill.run_backfill()   (async, used by the dashboard endpoint)
"""

import asyncio
import logging
from typing import Iterable

import db
from mirror.reporter import generate_failure_report

log = logging.getLogger("mirror.backfill")


def _candidate_call_uuids(limit: int = 100) -> list[str]:
    """Calls that had intervention-grade events AND no existing report."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT m.call_uuid "
            "FROM mirror_events m "
            "WHERE m.intervention_needed = 1 "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM failure_reports f "
            "  WHERE f.call_uuid = m.call_uuid"
            ") "
            "ORDER BY m.id DESC "
            "LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [r["call_uuid"] for r in rows if r["call_uuid"]]


async def run_backfill(limit: int = 100) -> dict:
    """Generate reports for past calls that didn't get one. Returns a
    summary dict with counts."""
    candidates = _candidate_call_uuids(limit=limit)
    log.info("backfill candidates: %d calls", len(candidates))
    created = 0
    skipped = 0
    failed = 0
    for uuid in candidates:
        try:
            row = await generate_failure_report(uuid)
            if row is None:
                skipped += 1
            else:
                created += 1
        except Exception:
            log.exception("backfill failed for %s", uuid)
            failed += 1
    log.info(
        "backfill done: created=%d skipped=%d failed=%d",
        created,
        skipped,
        failed,
    )
    return {
        "candidates": len(candidates),
        "created": created,
        "skipped": skipped,
        "failed": failed,
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    import os
    from dotenv import load_dotenv

    load_dotenv()
    db.init_db()
    result = asyncio.run(run_backfill())
    print(result)
