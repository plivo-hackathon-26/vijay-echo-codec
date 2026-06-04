#!/usr/bin/env python
"""plivo-mirror v5 eval harness.

Replays the bundled fixture calls (induced failures + organic) through the
engine and reports:

- catch rate        — recall on labeled failures, per layer
- false-alarm rate  — fired verdicts above ``info`` on clean agent turns
- latency           — p50/p90/p99 per layer, plus the L2 inline-budget check

Runs fully offline: no network, no API keys.

    venv/bin/python v5/eval/run_eval.py [--fixtures DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as a plain script without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plivo_mirror_v5.engine import (  # noqa: E402
    Engine,
    EngineConfig,
    KeywordKBRetriever,
    ReferenceStore,
    SessionState,
    TurnInput,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_calls(fixtures_dir: Path) -> list[dict]:
    calls = []
    for path in sorted(fixtures_dir.glob("call_*.json")):
        with open(path, encoding="utf-8") as f:
            calls.append(json.load(f))
    return calls


def replay_call(call: dict, fixtures_dir: Path) -> list[tuple[dict, object]]:
    """Run every turn of a fixture call through a fresh engine + state.
    Returns ``[(turn_fixture, TurnResult), ...]``."""
    reference = ReferenceStore.from_file(fixtures_dir / call["reference"])
    kb = KeywordKBRetriever.from_file(fixtures_dir / call["kb"]) if call.get("kb") else None
    engine = Engine(EngineConfig(), reference=reference, kb=kb)
    state = SessionState(call["call_id"])

    results = []
    for turn in call["turns"]:
        # Host-validated facts land in state before the turn is evaluated
        # (simulates the agent runtime writing tool-computed values).
        for key, value in (turn.get("state_updates") or {}).items():
            state.set_fact(key, value, source="host", turn_index=turn["turn_index"])
        turn_input = TurnInput(
            turn_id=f"{call['call_id']}-t{turn['turn_index']}",
            call_id=call["call_id"],
            turn_index=turn["turn_index"],
            role=turn["role"],
            transcript=turn["transcript"],
            asr_confidence=turn.get("asr_confidence"),
            claims=turn.get("claims") or [],
            tool_calls=turn.get("tool_calls") or [],
        )
        results.append((turn, engine.evaluate_turn(turn_input, state)))
    return results


def verdict_matches(expected: dict, verdict) -> bool:
    if verdict.detector != expected["detector"]:
        return False
    if "claim_id" in expected and verdict.claim_id != expected["claim_id"]:
        return False
    if "claim_type" in expected and (
        verdict.evidence is None
        or verdict.evidence.claim_type != expected["claim_type"]
    ):
        return False
    if "severity" in expected and verdict.severity != expected["severity"]:
        return False
    return True


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return ordered[idx]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES_DIR)
    args = parser.parse_args()

    calls = load_calls(args.fixtures)
    if not calls:
        print(f"no call_*.json fixtures found in {args.fixtures}", file=sys.stderr)
        return 2

    expected_total: dict[str, int] = {}
    caught_total: dict[str, int] = {}
    latencies: dict[str, list[float]] = {}
    false_alarms: list[tuple[str, object]] = []
    clean_agent_turns = 0
    total_turns = 0

    for call in calls:
        for turn, result in replay_call(call, args.fixtures):
            total_turns += 1
            expected = turn.get("expected_fired") or []
            fired = result.fired_verdicts
            matched_ids: set[str] = set()

            for exp in expected:
                det = exp["detector"]
                expected_total[det] = expected_total.get(det, 0) + 1
                hit = next(
                    (v for v in fired
                     if v.verdict_id not in matched_ids and verdict_matches(exp, v)),
                    None,
                )
                if hit is not None:
                    caught_total[det] = caught_total.get(det, 0) + 1
                    matched_ids.add(hit.verdict_id)

            # False alarms: unexpected firing verdicts above info severity.
            for v in fired:
                if v.verdict_id in matched_ids or v.severity == "info":
                    continue
                false_alarms.append((result.turn_id, v))

            if turn["role"] == "agent" and not expected:
                clean_agent_turns += 1

            for v in result.verdicts:
                latencies.setdefault(v.detector, []).append(v.latency_ms)

    # ---- report ----------------------------------------------------------
    print("== plivo-mirror v5 eval ==")
    print(f"fixtures: {len(calls)} calls, {total_turns} turns "
          f"({clean_agent_turns} clean agent turns)\n")

    print("catch rate (recall on labeled failures, per layer):")
    overall_exp = sum(expected_total.values())
    overall_caught = sum(caught_total.values())
    for det in sorted(expected_total):
        exp, caught = expected_total[det], caught_total.get(det, 0)
        print(f"  {det}: {caught}/{exp} ({caught / exp:6.1%})")
    print(f"  overall: {overall_caught}/{overall_exp} "
          f"({(overall_caught / overall_exp) if overall_exp else 0:6.1%})\n")

    fa_rate = len(false_alarms) / clean_agent_turns if clean_agent_turns else 0.0
    print(f"false-alarm rate: {len(false_alarms)}/{clean_agent_turns} "
          f"clean agent turns ({fa_rate:.1%})")
    for turn_id, v in false_alarms:
        ev = v.evidence
        print(f"  ! {turn_id} {v.detector} {v.severity} "
              f"{ev.claim_type if ev else '?'} spoken={ev.spoken_value if ev else '?'} "
              f"truth={ev.truth_value if ev else '?'}")
    print()

    print("latency per layer (ms):")
    for det in sorted(latencies):
        vals = latencies[det]
        print(f"  {det}: p50={percentile(vals, 50):.3f}  "
              f"p90={percentile(vals, 90):.3f}  p99={percentile(vals, 99):.3f}  "
              f"(n={len(vals)})")

    budget = EngineConfig().l2_inline_budget_ms
    l2_p90 = percentile(latencies.get("L2", []), 90)
    ok = l2_p90 < budget
    print(f"\nL2 inline budget ({budget:.0f}ms): "
          f"{'PASS' if ok else 'FAIL'} (p90={l2_p90:.3f}ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
