"""plivo-mirror eval harness — score Mirror against a LABELED dataset.

Unlike ``plivo_mirror.replay`` (which prints which turns *would* fire at
each threshold for a single transcript), this module runs Mirror over a
dataset of **independent, ground-truth-labeled cases** and computes a
scorecard: confusion matrix, precision / recall / F1, the
false-intervention rate, latency percentiles, and an approximate cost.

It tests *Mirror's judgment*, not a live agent. Each case carries a
pre-written agent reply (good or bad) and a label saying whether Mirror
*should* have intervened. No STT/TTS/agent-loop is involved — the only
LLM that runs is Mirror's own judge, which is the thing under test.

Dataset format — JSONL, one self-contained case per line::

    {"id": "correction_ignored_01",
     "category": "correction_ignored",
     "difficulty": "medium",
     "turns": [
       {"role": "customer", "text": "Large pepperoni — actually mushroom, no pepperoni."},
       {"role": "agent", "text": "Sure, one large pepperoni and one mushroom.",
        "tool_calls": [{"name": "place_order",
                        "args": {"items": ["large pepperoni", "large mushroom"]}}]}
     ],
     "expected_intervene": true,
     "violation_type": "correction_ignored",
     "reference_correction": "Got it — one large mushroom pizza, no pepperoni."}

The LAST turn must be the agent turn under test. Earlier turns are the
conversation history.

Usage::

    python -m plivo_mirror.eval datasets/eval_v1.jsonl \\
        --policies datasets/policies_v1.txt \\
        --model gpt-5.4-mini --threshold 0.7 --out scorecard

    # validate the dataset (coverage + balance) without spending a cent:
    python -m plivo_mirror.eval datasets/eval_v1.jsonl --validate

    # prove the loop on the first 10 cases, then scale:
    python -m plivo_mirror.eval datasets/eval_v1.jsonl \\
        --policies datasets/policies_v1.txt --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import (
    HistoryTurn,
    SupervisorContext,
    ToolCallIntent,
    TurnPayload,
)
from plivo_mirror.scorer.llm import LLMScorer
from plivo_mirror.scorer.mirror_judge import _default_tier0_checks
from plivo_mirror.scorer.pregate import should_score
from plivo_mirror.scorer.tier0.base import Tier0Check
from plivo_mirror.scorer.tool_gate import ToolGate

# ─────────────────────────── data model ──────────────────────────────


@dataclass
class Case:
    id: str
    category: str
    difficulty: str
    turns: list[dict[str, Any]]
    expected_intervene: bool
    violation_type: str
    reference_correction: str

    @property
    def final_agent_turn(self) -> dict[str, Any]:
        return self.turns[-1]


@dataclass
class CaseResult:
    case: Case
    scored: bool
    score: float
    predicted_intervene: bool
    reason: str
    correction: str
    blocked_tool: str | None
    latency_ms: float
    in_tokens: int
    out_tokens: int

    @property
    def outcome(self) -> str:
        """TP / FP / TN / FN."""
        exp, pred = self.case.expected_intervene, self.predicted_intervene
        if exp and pred:
            return "TP"
        if not exp and pred:
            return "FP"  # false intervention — the metric that matters most
        if not exp and not pred:
            return "TN"
        return "FN"  # missed catch


# ─────────────────────────── dataset I/O ─────────────────────────────


_REQUIRED = ("id", "turns", "expected_intervene")


def load_dataset(path: Path) -> list[Case]:
    cases: list[Case] = []
    seen_ids: set[str] = set()
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path}:{lineno}: invalid JSON — {e}")
        for k in _REQUIRED:
            if k not in obj:
                raise SystemExit(f"{path}:{lineno}: case missing required field {k!r}")
        cid = str(obj["id"])
        if cid in seen_ids:
            raise SystemExit(f"{path}:{lineno}: duplicate case id {cid!r}")
        seen_ids.add(cid)
        turns = obj["turns"]
        if not turns or turns[-1].get("role") != "agent":
            raise SystemExit(
                f"{path}:{lineno} ({cid}): last turn must be the agent turn under test"
            )
        cases.append(
            Case(
                id=cid,
                category=str(obj.get("category", "uncategorized")),
                difficulty=str(obj.get("difficulty", "n/a")),
                turns=turns,
                expected_intervene=bool(obj["expected_intervene"]),
                violation_type=str(obj.get("violation_type", "")),
                reference_correction=str(obj.get("reference_correction", "")),
            )
        )
    if not cases:
        raise SystemExit(f"{path}: no cases found")
    return cases


def load_policies(path: Path) -> list[str]:
    lines = [l.strip() for l in path.read_text().splitlines()]
    return [l for l in lines if l and not l.startswith("#")]


def _payload_from_case(case: Case) -> TurnPayload:
    history: list[HistoryTurn] = []
    customer_text = ""
    for turn in case.turns[:-1]:
        role = turn.get("role")
        text = turn.get("text") or ""
        if role in ("customer", "agent"):
            history.append(HistoryTurn(role=role, text=text))
        if role == "customer":
            customer_text = text

    agent = case.final_agent_turn
    tool_calls = [
        ToolCallIntent(
            name=tc.get("name", ""),
            args=tc.get("args") or {},
            irreversible=bool(tc.get("irreversible")),
        )
        for tc in (agent.get("tool_calls") or [])
    ]
    return TurnPayload(
        customer_text=customer_text,
        primary_text=agent.get("text") or "",
        tool_calls=tool_calls,
        history=history,
    )


# ─────────────────────────── config ──────────────────────────────────


def build_config(
    policies: list[str],
    *,
    threshold: float,
    model: str,
    base_url: str | None,
    respect_pregate: bool,
    tool_gate: bool,
) -> MirrorConfig:
    from plivo_mirror.llm.openai import OpenAIClient

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required to run the eval")

    return MirrorConfig(
        llm=OpenAIClient(api_key=api_key, model=model, base_url=base_url),
        policies=policies,
        intervention_threshold=threshold,
        # A benchmark must judge EVERY labeled case. By default we bypass
        # the cheap pre-gate so the scorecard reflects the judge's true
        # discrimination; --respect-pregate restores production behaviour.
        tiered_scoring_enabled=respect_pregate,
        tool_gate_enabled=tool_gate,
    )


# ─────────────────────────── scoring ─────────────────────────────────


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


async def score_case(
    case: Case,
    scorer: LLMScorer,
    tool_gate: ToolGate | None,
    config: MirrorConfig,
    tier0_checks: list[Tier0Check] | None = None,
) -> CaseResult:
    payload = _payload_from_case(case)
    ctx = SupervisorContext(call_uuid=f"eval:{case.id}")

    # Tier 0 (deterministic) runs first, exactly like MirrorJudge — the
    # first check that fires short-circuits before any LLM call.
    if tier0_checks:
        t0 = time.perf_counter()
        for chk in tier0_checks:
            try:
                res = chk.evaluate(payload, ctx)
            except Exception:
                continue
            if res.verdict is not None and res.verdict.should_intervene:
                v = res.verdict
                return CaseResult(
                    case=case, scored=True, score=v.score,
                    predicted_intervene=True,
                    reason=f"[tier0:{res.check_name}] {v.reason}",
                    correction=v.spoken_correction(),
                    blocked_tool=v.blocked_tool,
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                    in_tokens=0, out_tokens=0,
                )

    run, _reason = should_score(payload, config, prev_intervention=False)
    if not run:
        # Pregate skipped → production would not intervene here.
        return CaseResult(
            case=case, scored=False, score=0.0, predicted_intervene=False,
            reason="pregate-skipped", correction="", blocked_tool=None,
            latency_ms=0.0, in_tokens=0, out_tokens=0,
        )

    # Best-effort input-token estimate from the real judge prompt.
    in_tokens = 0
    try:
        in_tokens = _approx_tokens(scorer._format_prompt(payload))  # type: ignore[attr-defined]
    except Exception:
        pass

    t0 = time.perf_counter()
    verdict = await scorer.score(payload, ctx)
    if (
        tool_gate is not None
        and not verdict.should_intervene
        and payload.tool_calls
        and any(tool_gate.is_gated(tc.name) for tc in payload.tool_calls)
    ):
        tg = await tool_gate.review(
            payload.tool_calls, payload.customer_text, payload.history, ctx
        )
        if tg.should_intervene:
            verdict = tg
    latency_ms = (time.perf_counter() - t0) * 1000.0

    out_tokens = _approx_tokens(verdict.reason + verdict.suggested_correction) + 20
    return CaseResult(
        case=case,
        scored=True,
        score=verdict.score,
        predicted_intervene=verdict.should_intervene,
        reason=verdict.reason,
        correction=verdict.spoken_correction() if verdict.should_intervene else "",
        blocked_tool=verdict.blocked_tool,
        latency_ms=latency_ms,
        in_tokens=in_tokens,
        out_tokens=out_tokens,
    )


# ─────────────────────────── metrics ─────────────────────────────────


@dataclass
class Scorecard:
    model: str
    threshold: float
    n: int
    counts: dict[str, int]  # TP/FP/TN/FN
    precision: float
    recall: float
    f1: float
    false_intervention_rate: float
    accuracy: float
    latency_ms: dict[str, float]  # p50/p95/p99/mean
    est_cost_usd: float
    by_category: dict[str, dict[str, int]]
    false_positives: list[CaseResult] = field(default_factory=list)
    false_negatives: list[CaseResult] = field(default_factory=list)


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return s[idx]


def compute_scorecard(
    results: list[CaseResult], *, model: str, threshold: float,
    price_in: float, price_out: float,
) -> Scorecard:
    counts = Counter(r.outcome for r in results)
    tp, fp, tn, fn = counts["TP"], counts["FP"], counts["TN"], counts["FN"]

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    fir = fp / (fp + tn) if (fp + tn) else 0.0
    accuracy = (tp + tn) / len(results) if results else 0.0

    lat = [r.latency_ms for r in results if r.scored]
    latency = {
        "p50": _pct(lat, 0.50), "p95": _pct(lat, 0.95),
        "p99": _pct(lat, 0.99),
        "mean": (sum(lat) / len(lat)) if lat else 0.0,
    }

    in_tok = sum(r.in_tokens for r in results)
    out_tok = sum(r.out_tokens for r in results)
    est_cost = (in_tok / 1_000_000) * price_in + (out_tok / 1_000_000) * price_out

    by_cat: dict[str, dict[str, int]] = defaultdict(lambda: Counter())
    for r in results:
        by_cat[r.case.category][r.outcome] += 1

    return Scorecard(
        model=model, threshold=threshold, n=len(results),
        counts={"TP": tp, "FP": fp, "TN": tn, "FN": fn},
        precision=precision, recall=recall, f1=f1,
        false_intervention_rate=fir, accuracy=accuracy,
        latency_ms=latency, est_cost_usd=est_cost,
        by_category={k: dict(v) for k, v in by_cat.items()},
        false_positives=[r for r in results if r.outcome == "FP"],
        false_negatives=[r for r in results if r.outcome == "FN"],
    )


# ─────────────────────────── rendering ───────────────────────────────


def render_markdown(sc: Scorecard) -> str:
    c = sc.counts
    L = [
        "# Mirror eval scorecard",
        "",
        f"- **Model (judge):** `{sc.model}`",
        f"- **Threshold:** {sc.threshold:.2f}",
        f"- **Cases:** {sc.n}",
        "",
        "## Headline",
        "",
        f"- **False-intervention rate:** {sc.false_intervention_rate:.1%}  "
        f"_(FP / clean cases — the number that matters most for voice)_",
        f"- **Recall (catch rate):** {sc.recall:.1%}",
        f"- **Precision:** {sc.precision:.1%}",
        f"- **F1:** {sc.f1:.3f}    **Accuracy:** {sc.accuracy:.1%}",
        "",
        "## Confusion matrix",
        "",
        "| | predicted intervene | predicted allow |",
        "|---|---|---|",
        f"| **should intervene** | {c['TP']} (TP) | {c['FN']} (FN — missed) |",
        f"| **should allow** | {c['FP']} (FP — false alarm) | {c['TN']} (TN) |",
        "",
        "## Latency & cost",
        "",
        f"- Scoring latency: p50 {sc.latency_ms['p50']:.0f} ms · "
        f"p95 {sc.latency_ms['p95']:.0f} ms · p99 {sc.latency_ms['p99']:.0f} ms · "
        f"mean {sc.latency_ms['mean']:.0f} ms",
        f"- Estimated cost: ${sc.est_cost_usd:.4f} total "
        f"_(≈ estimate from char-based token counts, not metered)_",
        "",
        "## Per-category breakdown",
        "",
        "| category | TP | FN | FP | TN |",
        "|---|---|---|---|---|",
    ]
    for cat in sorted(sc.by_category):
        d = sc.by_category[cat]
        L.append(
            f"| {cat} | {d.get('TP',0)} | {d.get('FN',0)} | "
            f"{d.get('FP',0)} | {d.get('TN',0)} |"
        )
    if sc.false_positives:
        L += ["", "## ❗ False interventions (Mirror corrected a correct reply)", ""]
        for r in sc.false_positives:
            L.append(f"- `{r.case.id}` (score {r.score:.2f}) — {r.reason}")
    if sc.false_negatives:
        L += ["", "## ❗ Missed catches (Mirror let a real error through)", ""]
        for r in sc.false_negatives:
            L.append(
                f"- `{r.case.id}` [{r.case.violation_type}] (score {r.score:.2f}) — {r.reason}"
            )
    return "\n".join(L) + "\n"


def scorecard_to_dict(sc: Scorecard) -> dict[str, Any]:
    return {
        "model": sc.model, "threshold": sc.threshold, "n": sc.n,
        "counts": sc.counts, "precision": sc.precision, "recall": sc.recall,
        "f1": sc.f1, "false_intervention_rate": sc.false_intervention_rate,
        "accuracy": sc.accuracy, "latency_ms": sc.latency_ms,
        "est_cost_usd": sc.est_cost_usd, "by_category": sc.by_category,
        "false_positives": [r.case.id for r in sc.false_positives],
        "false_negatives": [r.case.id for r in sc.false_negatives],
    }


# ─────────────────────────── validate mode ───────────────────────────


def print_coverage(cases: list[Case]) -> None:
    n = len(cases)
    viol = sum(1 for c in cases if c.expected_intervene)
    clean = n - viol
    by_cat = Counter(c.category for c in cases)
    by_diff = Counter(c.difficulty for c in cases)
    print(f"\n  Dataset coverage — {n} cases")
    print(f"  {'─' * 50}")
    print(f"  violation (should-intervene): {viol}")
    print(f"  clean    (should-allow)     : {clean}")
    bal = (min(viol, clean) / max(viol, clean)) if max(viol, clean) else 0
    print(f"  balance ratio               : {bal:.2f}  (1.0 = perfectly balanced)")
    print(f"\n  by category ({len(by_cat)} categories):")
    for cat, k in sorted(by_cat.items()):
        print(f"    {cat:<32} {k}")
    print(f"\n  by difficulty:")
    for d, k in sorted(by_diff.items()):
        print(f"    {d:<32} {k}")
    print()


# ─────────────────────────── main ────────────────────────────────────


async def _async_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="plivo-mirror-eval",
        description="Run Mirror over a labeled dataset and emit a scorecard.",
    )
    p.add_argument("dataset", type=Path, help="JSONL dataset of labeled cases")
    p.add_argument("--policies", type=Path, help="Policies file (one per line)")
    p.add_argument("--threshold", type=float, default=0.7)
    p.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    p.add_argument("--base-url", default=os.getenv("OPENAI_API_URL"))
    p.add_argument("--limit", type=int, help="Only run the first N cases")
    p.add_argument("--respect-pregate", action="store_true",
                   help="Use production pre-gate (skip cheap turns) instead of scoring every case")
    p.add_argument("--no-tool-gate", action="store_true", help="Disable the tool-gate layer")
    p.add_argument("--tier0", action="store_true",
                   help="Run the deterministic Tier-0 checks before the LLM judge (matches MirrorJudge)")
    p.add_argument("--tier0-only", type=str, default=None,
                   help="Run only the named Tier-0 checks (comma-separated check names), e.g. arithmetic_consistency")
    p.add_argument("--validate", action="store_true",
                   help="Only validate the dataset (coverage + balance); no LLM calls")
    p.add_argument("--out", type=str, help="Write <out>.json + <out>.md scorecards")
    p.add_argument("--price-in", type=float, default=0.15,
                   help="USD per 1M input tokens (default gpt-4o-mini)")
    p.add_argument("--price-out", type=float, default=0.60,
                   help="USD per 1M output tokens (default gpt-4o-mini)")
    args = p.parse_args(argv)

    if not args.dataset.exists():
        print(f"dataset not found: {args.dataset}", file=sys.stderr)
        return 1
    cases = load_dataset(args.dataset)
    if args.limit:
        cases = cases[: args.limit]

    print_coverage(cases)
    if args.validate:
        print("  ✓ dataset valid (use without --validate to score it).\n")
        return 0

    if not args.policies or not args.policies.exists():
        print("must supply --policies <file> to score (or use --validate)", file=sys.stderr)
        return 1
    policies = load_policies(args.policies)

    config = build_config(
        policies, threshold=args.threshold, model=args.model,
        base_url=args.base_url, respect_pregate=args.respect_pregate,
        tool_gate=not args.no_tool_gate,
    )
    scorer = LLMScorer(config)
    tool_gate = None if args.no_tool_gate else ToolGate(config)
    if args.tier0_only:
        names = {n.strip() for n in args.tier0_only.split(",") if n.strip()}
        tier0_checks = [c for c in _default_tier0_checks() if c.name in names]
    elif args.tier0:
        tier0_checks = _default_tier0_checks()
    else:
        tier0_checks = None

    engine = (
        f"tier0[{','.join(c.name for c in tier0_checks)}] + LLM judge"
        if tier0_checks else "LLM judge"
    )
    print(f"  Scoring {len(cases)} cases through {engine} `{args.model}` ...")
    results: list[CaseResult] = []
    for i, case in enumerate(cases, 1):
        r = await score_case(case, scorer, tool_gate, config, tier0_checks)
        flag = {"TP": "✓", "TN": "✓", "FP": "✗ FALSE-ALARM", "FN": "✗ MISSED"}[r.outcome]
        print(f"    [{i:>3}/{len(cases)}] {case.id:<34} score={r.score:.2f} {flag}")
        results.append(r)

    sc = compute_scorecard(
        results, model=args.model, threshold=args.threshold,
        price_in=args.price_in, price_out=args.price_out,
    )
    md = render_markdown(sc)
    print("\n" + md)

    if args.out:
        Path(f"{args.out}.json").write_text(json.dumps(scorecard_to_dict(sc), indent=2))
        Path(f"{args.out}.md").write_text(md)
        print(f"  wrote {args.out}.json + {args.out}.md")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_async_main()))


if __name__ == "__main__":
    main()
