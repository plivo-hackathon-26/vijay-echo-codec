#!/usr/bin/env python
"""Simulate a LIVE voice call into the monitoring backend.

Streams a scripted Aurora-support call turn-by-turn (with realistic
pauses) through the SAME ``attach_mirror`` adapter a real LiveKit agent
uses — claims come from the live ``LexiconClaimExtractor`` (nothing is
hand-attached), tool executions arrive as ``function_tools_executed``
events, and telemetry flows over HTTP to the running backend. Watch the
dashboard while it runs: the call appears, turns stream in, flags pop.

    # backend + frontend already running, then:
    venv/bin/python v5/plivo_mirror_v5/deployables/monitoring/simulate_live_call.py
    venv/bin/python .../simulate_live_call.py --fast        # no pauses
    venv/bin/python .../simulate_live_call.py --url http://localhost:8500

Audio levels are SYNTHETIC (it's a simulator); a real adapter leaves them
unset until RMS taps land (see livekit_adapter TODO).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from plivo_mirror_v5.engine import ReferenceStore  # noqa: E402
from plivo_mirror_v5.integrations import attach_mirror  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "eval" / "fixtures"

# (role, transcript, asr_confidence, tools fired just before the turn)
SCRIPT: list[tuple[str, str, float | None, list[dict]]] = [
    ("user", "Hi, I'm thinking about upgrading. How much is the turbo plan?", 0.96, []),
    ("agent", "Great question! The turbo plan is $59.99 a month.", None, []),   # ← wrong (79.99)
    ("user", "Hmm okay. And what are your weekend support hours?", 0.93, []),
    ("agent", "We're available 9am-5pm on weekends.", None, []),                # ← correct
    ("user", "Actually I'd rather just cancel my current service.", 0.95, []),
    ("agent", "Done — I've cancelled your service effective today.", None, []),  # ← tool never fired
    ("user", "Wait, really? Also what's the refund window?", 0.91, []),
    ("agent", "You can get a full refund within 60 days.", None, []),           # ← wrong (30)
    ("user", "Fine. Please schedule a technician visit for Saturday.", 0.94, []),
    ("agent", "You're booked — I've scheduled the visit for Saturday morning.", None,
     [{"name": "schedule_visit", "arguments": json.dumps({"day": "saturday"}),
       "output": json.dumps({"visit_id": "vis-901"})}]),                        # ← tool fired: clean
]

ACTION_VERBS = {
    "cancel_service": ["cancelled", "canceled"],
    "schedule_visit": ["scheduled", "booked"],
}


class SimSession:
    """Session stand-in with the three events ``attach_mirror`` hooks."""

    def __init__(self) -> None:
        self._handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, payload=None) -> None:
        for handler in self._handlers.get(event, []):
            handler(payload)


def synthetic_levels(text: str, n: int = 24) -> list[float]:
    """Deterministic pseudo-waveform from the transcript (SIMULATED)."""
    digest = hashlib.sha256(text.encode()).digest()
    return [0.15 + (digest[i % len(digest)] / 255) * 0.8 for i in range(n)]


async def run(url: str, fast: bool) -> None:
    session = SimSession()
    call_id = f"live-sim-{int(time.time())}"
    observer = attach_mirror(
        session,
        room_id=call_id,
        reference=ReferenceStore.from_file(FIXTURES_DIR / "reference_aurora.json"),
        backend_url=url,
        agent_id="aurora-support",
        agent_version="1.1.0-live",
        mode="shadow",
        action_verbs=ACTION_VERBS,
    )
    print(f"live call {call_id} -> {url}   (open the dashboard now)")

    offset_ms = 0.0
    for role, text, asr_confidence, tools in SCRIPT:
        for tool in tools:
            session.emit("function_tools_executed", SimpleNamespace(
                zipped=lambda t=tool: [(
                    SimpleNamespace(name=t["name"], arguments=t["arguments"]),
                    SimpleNamespace(is_error=False, output=t["output"]),
                )],
            ))
        duration_ms = max(1200.0, len(text.split()) * 380.0)
        # The adapter stamps wall-clock offsets itself; the simulator wants a
        # scripted timeline, so it feeds items straight through the observer
        # bridge with explicit offsets + synthetic levels.
        from plivo_mirror_v5.integrations import ConversationItem  # noqa: PLC0415
        observer._on_item(ConversationItem(
            role=role, text=text, asr_confidence=asr_confidence,
            audio_offset_ms=offset_ms, audio_duration_ms=duration_ms,
            audio_levels=synthetic_levels(text),
        ))
        await observer.drain()
        flag = ""
        last = observer.results[-1]
        flags = [v for v in last.fired_verdicts if v.severity != "info"]
        if flags:
            flag = "  🚩 " + ", ".join(
                f"{v.detector}/{v.evidence.claim_type}: said "
                f"{v.evidence.spoken_value!r} truth {v.evidence.truth_value!r}"
                for v in flags)
        print(f"  [{offset_ms / 1000:5.1f}s] {role:>5}: {text}{flag}")
        offset_ms += duration_ms + 600.0
        if not fast:
            await asyncio.sleep(duration_ms / 1000 * 0.6 + 0.8)

    session.emit("close")
    print(f"call ended. open call {call_id} in the dashboard.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8500")
    parser.add_argument("--fast", action="store_true", help="no pauses")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.fast))
    return 0


if __name__ == "__main__":
    sys.exit(main())
