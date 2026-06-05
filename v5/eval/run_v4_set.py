#!/usr/bin/env python
"""Run the v4 eval sets (v3/datasets/eval_v*.jsonl + v4 golden) through v5.

Per case, the turns replay through the REAL v5 path: LLM claim extraction
(constrained to the Crave-Plivo reference keys / tool names — it never sees
truth values), then the engine (L1→L2→L3 + arbitration) on the final agent
turn. Separately the OFFLINE L4 judge audits the same turn grounded in
facts + policies. Reported:

- inline (L1-L3) catch / false-intervention — the real-time, µs-latency layer
- judge (L4) and combined (inline ∪ judge) — the post-call recall backstop
- per-category breakdown + L2 latency, vs the v4 scorecard baselines

Truth split mirrors v5's architecture: numeric facts (price_*, wings) →
ReferenceStore (L2); prose facts (hours, menu, policies) ground the judge.
LLM outputs are cached in eval/.v4set_cache.json — pass --no-cache (or
delete the file) to re-run fresh.

    venv/bin/python v5/eval/run_v4_set.py [--limit N] [--workers 8]
                                          [--no-judge] [--no-cache]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from plivo_mirror_v5.auditor import LLMPostCallJudge  # noqa: E402
from plivo_mirror_v5.engine import (  # noqa: E402
    AssertivenessGate,
    Engine,
    EngineConfig,
    ReferenceStore,
    SessionState,
    TurnInput,
)
from plivo_mirror_v5.engine.claims import LLMClaimExtractor  # noqa: E402
from plivo_mirror_v5.llm_client import ChatClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EVAL_SETS = {
    "eval_v1": ROOT / "v3" / "datasets" / "eval_v1.jsonl",
    "eval_v2": ROOT / "v3" / "datasets" / "eval_v2.jsonl",
    "golden_v1": ROOT / "v4" / "datasets" / "golden_v1.jsonl",
}
FACTS_PATH = ROOT / "v4" / "datasets" / "facts_v1.json"
POLICIES_PATH = ROOT / "v3" / "datasets" / "policies_v1.txt"
CACHE_PATH = Path(__file__).parent / ".v4set_cache.json"

TOOLS = ["place_order", "charge_card", "check_order_status",
         "cancel_order", "process_refund", "transfer_call"]


# ── data loading ─────────────────────────────────────────────────────────

def load_cases(path: Path, source: str) -> list[dict]:
    cases = []
    for line in path.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            case = json.loads(s)
            case["_source"] = source
            cases.append(case)
    return cases


def load_truth() -> tuple[ReferenceStore, dict, list[str]]:
    facts = {k: str(v) for k, v in json.loads(FACTS_PATH.read_text()).items()
             if not k.startswith("_")}
    # v5 split: crisp numeric facts → deterministic ReferenceStore (L2);
    # prose facts ground the judge (facts dict goes to it verbatim).
    numeric = {}
    for key, value in facts.items():
        if key.startswith("price_") or key in ("wings_per_order",):
            numeric[key] = value

    policies = [line.strip() for line in POLICIES_PATH.read_text().splitlines()
                if line.strip() and not line.startswith("#")]
    return ReferenceStore(numeric), facts, policies


# ── per-case evaluation ─────────────────────────────────────────────────────

class CachedChat:
    """ChatClient wrapper with a JSON disk cache keyed by (kind, case, text).
    ``bypass_reads=True`` (--no-cache) forces fresh LLM calls — results are
    still written, so the cache ends up refreshed rather than stale."""

    def __init__(self, client: ChatClient, cache_path: Path,
                 bypass_reads: bool = False) -> None:
        self.client = client
        self.cache_path = cache_path
        self.cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        self.bypass_reads = bypass_reads
        self.lock = threading.Lock()
        self.calls = 0

    def complete_json_cached(self, key: str, system: str, user: str) -> dict:
        if not self.bypass_reads:
            with self.lock:
                if key in self.cache:
                    return self.cache[key]
        result = self.client.complete_json(system, user)
        with self.lock:
            self.calls += 1
            self.cache[key] = result
            self.cache_path.write_text(json.dumps(self.cache))
        return result


class KeyedClient:
    """Adapter giving extractor/judge a complete_json bound to a cache key."""

    def __init__(self, cached: CachedChat, key: str) -> None:
        self.cached, self.key = cached, key

    def complete_json(self, system: str, user: str) -> dict:
        return self.cached.complete_json_cached(self.key, system, user)


def eval_case(case: dict, reference: ReferenceStore, facts, policies,
              cached: CachedChat, run_judge: bool) -> dict:
    state = SessionState(case["id"])
    engine = Engine(EngineConfig(), reference=reference)
    last_agent_result, last_agent_index = None, None
    last_agent_text, last_agent_claims = "", []
    l2_ms = 0.0

    for i, turn in enumerate(case["turns"]):
        role = "agent" if turn["role"] == "agent" else "user"
        claims: list[dict] = []
        if role == "agent":
            extractor = LLMClaimExtractor(
                reference, client=KeyedClient(cached, f"extract::{case['id']}::{i}"),
                tools=TOOLS)
            claims = extractor.extract_from_text(turn["text"])
        tool_calls = [{"name": tc["name"], "args": tc.get("args", {}),
                       "result": {"ok": True}}
                      for tc in (turn.get("tool_calls") or [])]
        result = engine.evaluate_turn(TurnInput(
            turn_id=f"{case['id']}-t{i}", call_id=case["id"], turn_index=i,
            role=role, transcript=turn["text"], claims=claims,
            tool_calls=tool_calls,
        ), state)
        if role == "agent":
            last_agent_result, last_agent_index = result, i
            last_agent_text, last_agent_claims = turn["text"], claims
            l2_ms = max((v.latency_ms for v in result.verdicts
                         if v.detector == "L2"), default=l2_ms)

    from plivo_mirror_v5.engine.verdict import severity_at_least

    inline_fired = [v for v in (last_agent_result.fired_verdicts
                                if last_agent_result else [])
                    if v.severity != "info"]
    # Two operating points:
    # - "low":  every non-info flag (what the monitoring dashboard shows)
    # - "med":  the INTERVENTION threshold.
    inline_low = bool(inline_fired)
    inline_med = any(severity_at_least(v.severity, "med") for v in inline_fired)

    judge_violation = None
    if run_judge:
        # Cache key carries a judge-prompt version: bump it whenever
        # _JUDGE_SYSTEM changes so stale verdicts never masquerade as results.
        judge = LLMPostCallJudge(
            KeyedClient(cached, f"judge.v4::{case['id']}::{last_agent_index}"),
            facts=facts, policies=policies)
        judge_violation = judge.judge_turn(case["turns"], last_agent_index)["violation"]

    # Hook B gated hold: would this final agent turn have paid the inline
    # judge? Mirrors JudgedPreTTSGate exactly — L2-style inline verdicts
    # short-circuit; otherwise the assertiveness gate decides whether the
    # (same grounded) judge runs BEFORE TTS instead of post-call.
    gate_result = AssertivenessGate().check(last_agent_text, last_agent_claims)
    inline_gated = inline_med or (gate_result.assertive and bool(judge_violation))

    return {
        "id": case["id"], "source": case["_source"],
        "category": case.get("category", ""),
        "expected": bool(case["expected_intervene"]),
        "inline": inline_med,
        "inline_low": inline_low,
        "inline_detectors": sorted({v.detector for v in inline_fired
                                    if severity_at_least(v.severity, "med")}),
        "judge": judge_violation,
        "combined": inline_med or bool(judge_violation),
        "assertive": gate_result.assertive,
        "gate_reasons": gate_result.reasons,
        "inline_gated": inline_gated,
        "l2_ms": l2_ms,
    }


# ── scoring ──────────────────────────────────────────────────────────────────

def rate(hits: int, n: int) -> str:
    return f"{hits}/{n} ({hits / n:5.1%})" if n else "n/a"


def score(rows: list[dict], field_name: str) -> dict:
    violations = [r for r in rows if r["expected"]]
    cleans = [r for r in rows if not r["expected"]]
    caught = sum(1 for r in violations if r[field_name])
    false_pos = sum(1 for r in cleans if r[field_name])
    return {"caught": caught, "violations": len(violations),
            "false_pos": false_pos, "cleans": len(cleans)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--no-cache", action="store_true",
                        help="bypass cache READS (fresh LLM calls for every "
                             "case); results are still written back, so the "
                             "cache is refreshed, not stale")
    parser.add_argument("--latency-probe", type=int, default=0, metavar="N",
                        help="time N FRESH inline-judge calls (no cache) on "
                             "assertive turns to measure real held-turn latency")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    reference, facts, policies = load_truth()
    cached = CachedChat(ChatClient(), CACHE_PATH, bypass_reads=args.no_cache)
    run_judge = not args.no_judge

    cases = []
    for source, path in EVAL_SETS.items():
        cases += load_cases(path, source)
    if args.limit:
        cases = cases[:args.limit]
    print(f"{len(cases)} cases | model={cached.client.model} | "
          f"judge={'on' if run_judge else 'off'} | cache={len(cached.cache)} entries")

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(eval_case, c, reference, facts, policies,
                               cached, run_judge) for c in cases]
        for i, fut in enumerate(futures, 1):
            rows.append(fut.result())
            if i % 25 == 0:
                print(f"  ... {i}/{len(cases)}")

    # ---- report ------------------------------------------------------------
    print(f"\n== v5 on the v4 eval sets ==  ({cached.calls} live LLM calls)")
    for source in EVAL_SETS:
        subset = [r for r in rows if r["source"] == source]
        if not subset:
            continue
        print(f"\n[{source}]  ({len(subset)} cases)")
        for label, field_name in (("inline flags (low+) ", "inline_low"),
                                  ("inline intervene med+", "inline"),
                                  ("judge L4             ", "judge"),
                                  ("combined (med+|judge)", "combined"),
                                  ("HOOK-B gated hold    ", "inline_gated")):
            if field_name in ("judge", "inline_gated") and not run_judge:
                continue
            s = score(subset, field_name)
            print(f"  {label}: catch {rate(s['caught'], s['violations'])}   "
                  f"false-intervention {rate(s['false_pos'], s['cleans'])}")

    print("\nper-category catch (violations only, all sets):")
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        if r["expected"]:
            by_cat.setdefault(r["category"], []).append(r)
    for cat in sorted(by_cat):
        sub = by_cat[cat]
        inline_n = sum(1 for r in sub if r["inline"])
        comb_n = sum(1 for r in sub if r["combined"])
        dets = sorted({d for r in sub for d in r["inline_detectors"]})
        print(f"  {cat:34s} inline {inline_n}/{len(sub)}  "
              f"combined {comb_n}/{len(sub)}  {','.join(dets)}")

    # Gate economics: which turns would pay judge latency under gated hold.
    if run_judge:
        viol = [r for r in rows if r["expected"]]
        clean = [r for r in rows if not r["expected"]]
        print("\nHook B gate hit-rate (final agent turn assertive → pays the judge):")
        print(f"  violations: {rate(sum(r['assertive'] for r in viol), len(viol))}"
              f"   clean turns: {rate(sum(r['assertive'] for r in clean), len(clean))}")
        exempt = [r for r in viol if not r["assertive"] and r["judge"]]
        if exempt:
            print(f"  ⚠ judge-catchable violations the gate would EXEMPT: "
                  f"{[r['id'] for r in exempt]}")

    l2_samples = sorted(r["l2_ms"] for r in rows if r["l2_ms"] > 0)
    if l2_samples:
        p = lambda q: l2_samples[min(len(l2_samples) - 1, int(q * (len(l2_samples) - 1)))]  # noqa: E731
        print(f"\nL2 inline latency: p50={p(.5):.3f}ms p95={p(.95):.3f}ms "
              f"(n={len(l2_samples)})")

    # Real judge latency on assertive turns: FRESH calls, sequential, no cache
    # — this is the wait an assertive turn pays before TTS under gated hold.
    if args.latency_probe and run_judge:
        import time as _time
        sample = [c for c in cases
                  if any(r["id"] == c["id"] and r["assertive"] for r in rows)]
        sample = sample[: args.latency_probe]
        judge = LLMPostCallJudge(ChatClient(), facts=facts, policies=policies)
        waits = []
        for c in sample:
            last_idx = max(i for i, t in enumerate(c["turns"])
                           if t["role"] == "agent")
            t0 = _time.perf_counter()
            judge.judge_turn(c["turns"], last_idx)
            waits.append((_time.perf_counter() - t0) * 1000.0)
        waits.sort()
        q = lambda f: waits[min(len(waits) - 1, int(f * (len(waits) - 1)))]  # noqa: E731
        print(f"\ninline-judge latency, FRESH calls (n={len(waits)}): "
              f"p50={q(.5):.0f}ms p95={q(.95):.0f}ms max={waits[-1]:.0f}ms"
              f"  ← added before TTS on ASSERTIVE turns only")

    print("\nv4 baselines (v4/scorecard_eval_v2_live.json): "
          "catch 35.4% on eval_v2 violations, false-intervention 9.5% on golden_v1")

    out = Path(__file__).parent / "scorecard_v4set.json"
    out.write_text(json.dumps({"rows": rows}, indent=1))
    print(f"rows written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
