#!/usr/bin/env python
"""Seed the monitoring backend with a small FLEET of varied calls so the
fleet view (KPIs, trend, categories, version compare, systemic patterns)
has something real to show.

What it plants — deliberately story-shaped:
- a SYSTEMIC wrong price: agent v1.0.1 quotes $59.99 for the Turbo plan
  (truth $79.99) across several calls → one fact-pattern with receipts;
- two false completions ("cancelled" with no tool call) on v1.0.0;
- a spread of clean calls on both versions so rates are meaningful.

    # into a SQLite file the backend serves:
    venv/bin/python v5/plivo_mirror_v5/deployables/monitoring/seed_fleet.py
    MIRROR_DB=v5/mirror_fleet.db venv/bin/python -m uvicorn \
        plivo_mirror_v5.deployables.monitoring.backend.app:app --port 8500

    # or POST into a running backend:
    venv/bin/python .../seed_fleet.py --url http://localhost:8500
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from plivo_mirror_v5.deployables.monitoring.backend.store import CallStore  # noqa: E402
from plivo_mirror_v5.engine import Engine, EngineConfig, ReferenceStore  # noqa: E402
from plivo_mirror_v5.integrations import (  # noqa: E402
    ConversationItem,
    FakeSession,
    MirrorObserver,
)
from plivo_mirror_v5.telemetry import HTTPSink, TelemetryEmitter  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "eval" / "fixtures"

U = "user"
A = "agent"

PRICE_CLAIM_BAD = [{"claim_id": "c1", "claim_type": "price", "spoken_value": "$59.99",
                    "ref": "reference.plan.turbo.price_per_month",
                    "text": "The Turbo plan is $59.99 a month"}]
PRICE_CLAIM_OK = [{"claim_id": "c1", "claim_type": "price", "spoken_value": "$79.99",
                   "ref": "reference.plan.turbo.price_per_month",
                   "text": "The Turbo plan is $79.99 a month"}]
CANCEL_CLAIM = [{"claim_id": "c1", "claim_type": "action", "spoken_value": "cancelled",
                 "ref": "tool.cancel_service",
                 "text": "I've cancelled your service"}]

# (call_id, agent_version, [(role, text, claims), ...])
CALLS = [
    # — the systemic wrong price: three calls on v1.0.1, same wrong value —
    ("fleet-price-01", "1.0.1", [
        (U, "Hi, how much is the Turbo plan?", None),
        (A, "The Turbo plan is $59.99 a month.", PRICE_CLAIM_BAD),
        (U, "Great, thanks!", None),
        (A, "Anything else I can help with?", None)]),
    ("fleet-price-02", "1.0.1", [
        (U, "What does Turbo cost monthly?", None),
        (A, "The Turbo plan is $59.99 a month.", PRICE_CLAIM_BAD),
        (U, "Okay, I'll think about it.", None),
        (A, "Of course — happy to help anytime.", None)]),
    ("fleet-price-03", "1.0.1", [
        (U, "Turbo plan price please?", None),
        (A, "The Turbo plan is $59.99 a month.", PRICE_CLAIM_BAD),
        (U, "Thanks.", None),
        (A, "You're welcome!", None)]),
    # — false completions on v1.0.0 —
    ("fleet-cancel-01", "1.0.0", [
        (U, "Please cancel my service.", None),
        (A, "Done — I've cancelled your service.", CANCEL_CLAIM)]),
    ("fleet-cancel-02", "1.0.0", [
        (U, "Cancel my subscription now please.", None),
        (A, "All set, I've cancelled your service effective today.", CANCEL_CLAIM)]),
    # — clean calls on both versions —
    ("fleet-clean-01", "1.0.0", [
        (U, "How much is Turbo?", None),
        (A, "The Turbo plan is $79.99 a month.", PRICE_CLAIM_OK)]),
    ("fleet-clean-02", "1.0.0", [
        (U, "What are your weekend hours?", None),
        (A, "Let me check that for you — one moment.", None)]),
    ("fleet-clean-03", "1.0.1", [
        (U, "How much is Turbo?", None),
        (A, "The Turbo plan is $79.99 a month.", PRICE_CLAIM_OK)]),
]


async def seed(sink) -> None:
    reference = ReferenceStore.from_file(FIXTURES_DIR / "reference_aurora.json")
    for call_id, version, turns in CALLS:
        engine = Engine(EngineConfig(mode="shadow"), reference=reference)
        observer = MirrorObserver(engine, TelemetryEmitter(sink),
                                  agent_id="aurora-support",
                                  agent_version=version)
        session = FakeSession(room_id=call_id)
        observer.attach(session)
        for i, (role, text, claims) in enumerate(turns):
            session.add_item(ConversationItem(
                role=role, text=text, claims=claims or [],
                asr_confidence=0.95 if role == U else None,
                audio_offset_ms=i * 4000, audio_duration_ms=4000))
        await observer.drain()
        observer.close()
        flags = sum(1 for r in observer.results for v in r.fired_verdicts
                    if v.severity != "info")
        print(f"  {call_id}  v{version}  {len(turns)} turns  "
              f"{flags or 'no'} flag(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None,
                        help="POST into a running backend instead of SQLite")
    parser.add_argument("--db", default="v5/mirror_fleet.db")
    args = parser.parse_args()

    sink = HTTPSink(args.url) if args.url else CallStore(args.db)
    print(f"seeding fleet → {args.url or args.db}")
    asyncio.run(seed(sink))
    if not args.url:
        print(f"\nserve it:\n  MIRROR_DB={args.db} venv/bin/python -m uvicorn "
              "plivo_mirror_v5.deployables.monitoring.backend.app:app --port 8500")
    return 0


if __name__ == "__main__":
    sys.exit(main())
