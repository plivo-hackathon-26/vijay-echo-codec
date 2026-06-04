#!/usr/bin/env python
"""Replay eval fixture calls through the SHADOW observer into the
monitoring backend — the Phase-2 end-to-end demo.

    # write straight into a SQLite store the backend will serve:
    venv/bin/python v5/plivo_mirror_v5/deployables/monitoring/replay_fixture.py
    MIRROR_DB=v5/mirror_monitoring.db venv/bin/python -m uvicorn \
        plivo_mirror_v5.deployables.monitoring.backend.app:app --port 8500

    # or POST into a running backend:
    venv/bin/python .../replay_fixture.py --url http://localhost:8500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from plivo_mirror_v5.deployables.monitoring.backend.store import CallStore  # noqa: E402
from plivo_mirror_v5.engine import (  # noqa: E402
    Engine,
    EngineConfig,
    ReferenceStore,
)
from plivo_mirror_v5.integrations import (  # noqa: E402
    ConversationItem,
    FakeSession,
    MirrorObserver,
)
from plivo_mirror_v5.telemetry import HTTPSink, TelemetryEmitter  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "eval" / "fixtures"
# Simulated audio timeline for the "replay at offset" link: 4s per turn.
TURN_AUDIO_MS = 4000


async def replay_call(call: dict, sink, fixtures_dir: Path) -> None:
    reference = ReferenceStore.from_file(fixtures_dir / call["reference"])
    engine = Engine(EngineConfig(mode="shadow"), reference=reference)
    emitter = TelemetryEmitter(sink)
    observer = MirrorObserver(
        engine, emitter,
        agent_id=call.get("agent_id", "unknown"),
        agent_version=call.get("agent_version", "unknown"),
    )
    session = FakeSession(room_id=call["call_id"])
    observer.attach(session)

    for turn in call["turns"]:
        for key, value in (turn.get("state_updates") or {}).items():
            observer.state.set_fact(key, value, source="host",
                                    turn_index=turn["turn_index"])
        session.add_item(ConversationItem(
            role=turn["role"],
            text=turn["transcript"],
            asr_confidence=turn.get("asr_confidence"),
            claims=turn.get("claims") or [],
            tool_calls=turn.get("tool_calls") or [],
            audio_offset_ms=turn["turn_index"] * TURN_AUDIO_MS,
        ))
    await observer.drain()
    observer.close()
    flagged = sum(1 for r in observer.results for v in r.fired_verdicts
                  if v.severity != "info")
    print(f"  {call['call_id']}: {len(observer.results)} turns, "
          f"{flagged} flagged verdicts")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES_DIR)
    parser.add_argument("--db", default="v5/mirror_monitoring.db",
                        help="SQLite file to write into (local-exporter path)")
    parser.add_argument("--url", default=None,
                        help="POST to a running backend instead of writing the db")
    args = parser.parse_args()

    sink = HTTPSink(args.url) if args.url else CallStore(args.db)
    target = args.url or args.db
    print(f"replaying fixtures from {args.fixtures} -> {target}")
    for path in sorted(args.fixtures.glob("call_*.json")):
        with open(path, encoding="utf-8") as f:
            await replay_call(json.load(f), sink, args.fixtures)
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
