#!/usr/bin/env python
"""Register a demo agent and drive its scripted calls into the dashboard.

Deterministic and key-free: it replays each config's DEMO_CALLS through the
REAL v5 engine (same L1/L2 + policy pack a live call uses) and POSTs the
telemetry to the backend, so the dashboard shows genuine receipts. The only
thing it stands in for is speech→claims extraction + the LLM judge (which a
live mic call does); here claims/tool_calls are attached in the config so
the run is reproducible.

    # backend already running (local or behind the ngrok URL):
    venv/bin/python v5/examples/run_demo.py --agent wellspring
    venv/bin/python v5/examples/run_demo.py --agent northwind
    venv/bin/python v5/examples/run_demo.py --agent both --url http://localhost:8500
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import urllib.request
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent
sys.path.insert(0, str(EXAMPLES.parents[0]))          # v5/  (the package)
sys.path.insert(0, str(EXAMPLES))                      # examples/ (configs)

from plivo_mirror_v5.engine import (  # noqa: E402
    Engine,
    EngineConfig,
    PolicyPack,
    ReferenceStore,
)
from plivo_mirror_v5.integrations import (  # noqa: E402
    ConversationItem,
    FakeSession,
    MirrorObserver,
)
from plivo_mirror_v5.telemetry import HTTPSink, TelemetryEmitter  # noqa: E402

CONFIGS = {
    "wellspring": "wellspring_clinic_agent.config",
    "northwind": "northwind_bank_agent.config",
}


def register(url: str, cfg) -> None:
    body = json.dumps({
        "agent_id": cfg.AGENT_ID,
        "name": cfg.AGENT_NAME,
        "system_prompt": cfg.SYSTEM_PROMPT,
        "facts": cfg.FACTS,
        "policies": cfg.POLICIES_TEXT,
    }).encode()
    req = urllib.request.Request(url.rstrip("/") + "/agents", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        ok = json.load(resp)
    print(f"registered {ok['agent_id']} ({ok['mode']} mode)")


async def drive(url: str, cfg) -> None:
    reference = ReferenceStore(cfg.FACTS)
    policy = PolicyPack.from_dict(cfg.POLICY_DICT)
    sink = HTTPSink(url)
    for call in cfg.DEMO_CALLS:
        engine = Engine(EngineConfig(mode="shadow", policy=policy),
                        reference=reference)
        observer = MirrorObserver(engine, TelemetryEmitter(sink),
                                  agent_id=cfg.AGENT_ID,
                                  agent_version=cfg.AGENT_VERSION)
        room_id = f"{cfg.AGENT_ID}-{call['id']}"
        observer.attach(FakeSession(room_id=room_id))
        offset = 0.0
        for i, turn in enumerate(call["turns"]):
            if turn["role"] == "_state":            # host writes validated facts
                for key, value in turn["set"].items():
                    observer.state.set_fact(key, value, source="host",
                                            turn_index=i)
                continue
            observer._on_item(ConversationItem(
                role=turn["role"], text=turn["text"],
                claims=turn.get("claims", []),
                tool_calls=turn.get("tool_calls", []),
                asr_confidence=0.96 if turn["role"] == "user" else None,
                audio_offset_ms=offset, audio_duration_ms=4000))
            offset += 4000
        await observer.drain()
        observer.close()
        flags = [v for r in observer.results for v in r.fired_verdicts
                 if v.severity != "info"]
        tag = "CLEAN" if not flags else (
            "FLAGGED " + ",".join(sorted({
                f"{v.detector}/{v.evidence.claim_type}" for v in flags
                if v.evidence})))
        print(f"  {room_id:36s} {len(call['turns'])} turns  {tag}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=[*CONFIGS, "both"], default="both")
    parser.add_argument("--url", default="http://localhost:8500")
    args = parser.parse_args()

    targets = list(CONFIGS) if args.agent == "both" else [args.agent]
    for name in targets:
        cfg = importlib.import_module(CONFIGS[name])
        print(f"\n=== {cfg.AGENT_NAME} → {args.url} ===")
        register(args.url, cfg)
        asyncio.run(drive(args.url, cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
