"""plivo-mirror v4 measurement harness.

Drives labeled cases through the REAL firewall path
(``Firewall.review_turn`` → speech + action guards; ``intervene_stream``
for latency) — never a reimplementation. Two modes, both labeled in the
output:

  * ``deterministic`` — a perfect ORACLE verifier (returns the ground-truth
    label when consulted). Reproducible, runs in CI. Isolates the
    GATE/routing/structural numbers that don't depend on the LLM:
    verifier-hit (== lexicon-fire) rate, catch-with-perfect-verifier,
    missed-at-gate (the speech-recall ceiling), and deterministic-layer
    false positives.
  * ``live`` — the real configured model (``Firewall._build_client_from_env``).
    Required for end-to-end catch, false-intervention, and latency.
    Nondeterministic; the report stamps model + date.

HONESTY: catch rate is measured on INDUCED violations (not organic
traffic); false-intervention is measured on the GOLDEN good-call set. The
two are never conflated. If the live client can't be built, live numbers
are reported ABSENT with the reason — never fabricated.

CLI::

    python -m plivo_mirror.eval \\
        --induced ../v3/datasets/eval_v1.jsonl \\
        --golden  datasets/golden_v1.jsonl \\
        --policies ../v3/datasets/policies_v1.txt \\
        --mode deterministic            # or: --mode live --model gpt-5-mini
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any

from plivo_mirror.contracts import ToolCallIntent, TurnContext, Verdict
from plivo_mirror.firewall import Firewall, _build_client_from_env
from plivo_mirror.guards.risk_spans import tag_risk_spans
from plivo_mirror.verifier.base import GroundingEvidence, VerifierResult


# ─────────────────────────── cases ───────────────────────────────────


@dataclass
class Case:
    id: str
    category: str
    difficulty: str
    turns: list[dict[str, Any]]
    expected_intervene: bool
    violation_type: str = ""


def load_cases(path: str | Path) -> list[Case]:
    out: list[Case] = []
    for line in Path(path).read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        d = json.loads(s)
        out.append(
            Case(
                id=d["id"],
                category=d.get("category", ""),
                difficulty=d.get("difficulty", ""),
                turns=d["turns"],
                expected_intervene=bool(d["expected_intervene"]),
                violation_type=d.get("violation_type", ""),
            )
        )
    return out


def load_policies(path: str | Path | None) -> list[str]:
    if not path:
        return []
    lines = []
    for line in Path(path).read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            lines.append(s)
    return lines


def _build_context(case: Case, firewall: Firewall) -> TurnContext:
    agent_turn = case.turns[-1]
    planned_reply = agent_turn.get("text", "")
    tool_intents = [
        ToolCallIntent(
            name=tc["name"], args=tc.get("args", {}), irreversible=tc.get("irreversible", False)
        )
        for tc in agent_turn.get("tool_calls", [])
    ]
    customer_text = ""
    for t in reversed(case.turns[:-1]):
        if t.get("role") == "customer":
            customer_text = t.get("text", "")
            break
    # NOTE: SessionState is intentionally NOT populated with validated
    # entities — synthesizing them would require the NLU extractor that is
    # the customer's job, and would measure an idealized extractor rather
    # than v4. So action-boundary detection (arg-vs-state) is not exercised
    # here; see the report caveat.
    state = firewall.new_session(case.id)
    return TurnContext(
        state=state,
        planned_reply=planned_reply,
        tool_intents=tool_intents,
        customer_text=customer_text,
    )


# ─────────────────────────── verifiers ───────────────────────────────


class OracleVerifier:
    """Perfect verifier for deterministic mode: returns supported = NOT
    (this case is a labeled violation). Isolates gate/routing from the LLM."""

    def __init__(self) -> None:
        self.calls = 0
        self._is_violation = False

    def set_case(self, is_violation: bool) -> None:
        self._is_violation = is_violation

    async def verify(self, claim: str, evidence: GroundingEvidence) -> VerifierResult:
        self.calls += 1
        return VerifierResult(supported=not self._is_violation, reason="oracle")


class _CountingVerifier:
    """Wraps the real verifier to count calls (for verifier-hit rate)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "?")

    async def verify(self, claim: str, evidence: GroundingEvidence) -> VerifierResult:
        self.calls += 1
        return await self._inner.verify(claim, evidence)


# ─────────────────────────── per-case result ─────────────────────────


@dataclass
class CaseResult:
    case: Case
    fired: bool
    verifier_hit: bool
    risk_span: bool
    decision: str
    review_ms: float
    first_audio_ms: float | None = None
    corrected_ms: float | None = None


async def _run_case(
    case: Case, firewall: Firewall, verifier_counter: Any, *, mode: str
) -> CaseResult:
    ctx = _build_context(case, firewall)
    risk_span = bool(tag_risk_spans(ctx.planned_reply))

    verifier_counter.calls = 0
    if isinstance(verifier_counter, OracleVerifier):
        verifier_counter.set_case(case.expected_intervene)

    t0 = time.perf_counter()
    verdict: Verdict = await firewall.review_turn(ctx)
    review_ms = (time.perf_counter() - t0) * 1000.0

    fired = verdict.intervened
    verifier_hit = verifier_counter.calls > 0

    first_audio_ms: float | None = None
    corrected_ms: float | None = None
    if mode == "live":
        if fired:
            t1 = time.perf_counter()
            agen = firewall.intervene_stream(verdict, ctx)
            await agen.__anext__()  # first chunk = deflection filler
            first_audio_ms = review_ms + (time.perf_counter() - t1) * 1000.0
            async for _ in agen:  # drain to the grounded answer / escalation
                pass
            corrected_ms = review_ms + (time.perf_counter() - t1) * 1000.0
        else:
            # clean turn: the buffered reply is released right after review
            first_audio_ms = review_ms

    return CaseResult(
        case=case,
        fired=fired,
        verifier_hit=verifier_hit,
        risk_span=risk_span,
        decision=verdict.decision,
        review_ms=review_ms,
        first_audio_ms=first_audio_ms,
        corrected_ms=corrected_ms,
    )


# ─────────────────────────── evaluate ────────────────────────────────


def _pct(n: int, d: int) -> float:
    return (n / d) if d else 0.0


def _by_category(results: list[CaseResult]) -> dict[str, dict[str, Any]]:
    cats: dict[str, list[CaseResult]] = {}
    for r in results:
        cats.setdefault(r.case.category, []).append(r)
    out = {}
    for cat, rs in sorted(cats.items()):
        fired = sum(1 for r in rs if r.fired)
        out[cat] = {"n": len(rs), "fired": fired, "fire_rate": round(_pct(fired, len(rs)), 3)}
    return out


def _percentiles(values: list[float]) -> dict[str, float] | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    vals_sorted = sorted(vals)
    p50 = statistics.median(vals_sorted)
    # nearest-rank p95
    idx = max(0, min(len(vals_sorted) - 1, int(round(0.95 * len(vals_sorted) + 0.5)) - 1))
    return {"p50_ms": round(p50, 1), "p95_ms": round(vals_sorted[idx], 1), "n": len(vals_sorted)}


async def evaluate(
    *,
    induced_path: str,
    golden_path: str,
    policies_path: str | None,
    mode: str,
    model: str | None = None,
    run_date: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    policies = load_policies(policies_path)
    induced = load_cases(induced_path)
    golden = load_cases(golden_path)
    if limit:
        induced = induced[:limit]
        golden = golden[:limit]

    scorecard: dict[str, Any] = {
        "mode": mode,
        "model": None,
        "date": run_date or _date.today().isoformat(),
        "induced_source": induced_path,
        "golden_source": golden_path,
        "policies_source": policies_path,
    }

    # Build the firewall with the right verifier injected at construction
    # (the speech guard captures the verifier reference, so it must be set
    # at build time, not swapped after).
    if mode == "deterministic":
        verifier = OracleVerifier()
        firewall = Firewall(policies=policies, verifier=verifier, generator=None)
        scorecard["model"] = "ORACLE (perfect verifier; deterministic)"
    elif mode == "live":
        client = _build_client_from_env()
        if client is None:
            scorecard["model"] = None
            scorecard["live_status"] = "NOT RUN — no LLM client (set AZURE_OPENAI_* or OPENAI_API_KEY; openai installed)"
            return scorecard
        from plivo_mirror.intervention.regenerate import LLMReplyGenerator
        from plivo_mirror.verifier.llm_judge import LLMJudgeVerifier

        resolved_model = model or "gpt-4o-mini"
        verifier = _CountingVerifier(LLMJudgeVerifier(client, model=resolved_model))
        generator = LLMReplyGenerator(client, model=resolved_model)
        firewall = Firewall(policies=policies, verifier=verifier, generator=generator)
        scorecard["model"] = resolved_model
    else:
        raise ValueError(f"unknown mode {mode!r}")

    induced_results: list[CaseResult] = []
    for c in induced:
        induced_results.append(await _run_case(c, firewall, verifier, mode=mode))
    golden_results: list[CaseResult] = []
    for c in golden:
        golden_results.append(await _run_case(c, firewall, verifier, mode=mode))

    all_results = induced_results + golden_results

    # The induced file is a MIXED labeled set: violations paired with clean
    # near-misses. Split by label rather than assume the file is all-bad.
    violations = [r for r in induced_results if r.case.expected_intervene]
    induced_negatives = [r for r in induced_results if not r.case.expected_intervene]

    # ── catch rate on the induced VIOLATIONS (split) ──
    caught = [r for r in violations if r.fired]
    missed = [r for r in violations if not r.fired]
    missed_at_gate = [r for r in missed if not r.verifier_hit]
    missed_at_verifier = [r for r in missed if r.verifier_hit]
    scorecard["induced"] = {
        "label": "catch rate on INDUCED violations (NOT organic traffic)",
        "n": len(violations),
        "catch_rate": round(_pct(len(caught), len(violations)), 3),
        "caught": len(caught),
        "missed": len(missed),
        "missed_at_gate": len(missed_at_gate),
        "missed_at_gate_note": "no risk span tagged / no deterministic|action hit → verifier never consulted. THIS is the speech-recall (lexicon) ceiling.",
        "missed_at_verifier": len(missed_at_verifier),
        "missed_at_verifier_note": "flagged but judged supported (live only; 0 by construction under the oracle).",
        "by_category": _by_category(violations),
    }

    # ── false-intervention: headline on the GOLDEN set, secondary on the
    #    induced file's own near-miss negatives ──
    fp_golden = [r for r in golden_results if r.fired]
    fp_induced = [r for r in induced_negatives if r.fired]
    scorecard["golden"] = {
        "label": "false-intervention rate on the GOLDEN good-call set (headline)",
        "n": len(golden_results),
        "false_intervention_rate": round(_pct(len(fp_golden), len(golden_results)), 3),
        "fired": len(fp_golden),
        "fired_ids": [r.case.id for r in fp_golden],
        "by_category": _by_category(golden_results),
    }
    scorecard["induced_near_miss"] = {
        "label": "false-intervention on the induced file's clean near-misses (secondary)",
        "n": len(induced_negatives),
        "false_intervention_rate": round(_pct(len(fp_induced), len(induced_negatives)), 3),
        "fired": len(fp_induced),
        "fired_ids": [r.case.id for r in fp_induced],
    }

    # ── verifier-hit / lexicon-fire rate (across all cases) ──
    scorecard["verifier_hit_rate"] = round(
        _pct(sum(1 for r in all_results if r.verifier_hit), len(all_results)), 3
    )
    scorecard["lexicon_fire_rate"] = round(
        _pct(sum(1 for r in all_results if r.risk_span), len(all_results)), 3
    )
    scorecard["routing_note"] = (
        "confidence gate is inert today (no logprobs), so verifier-hit rate == lexicon-fire rate; "
        "any divergence is deterministic/action blocks short-circuiting before the verifier."
    )

    # ── latency (live only) ──
    if mode == "live":
        clean = [r for r in all_results if not r.fired]
        flagged = [r for r in all_results if r.fired]
        scorecard["latency"] = {
            "note": "verifier+regeneration latency; TTS adds ~250-500ms on top (not included).",
            "time_to_first_audio_clean": _percentiles([r.first_audio_ms for r in clean]),
            "time_to_first_audio_flagged": _percentiles([r.first_audio_ms for r in flagged]),
            "time_to_corrected_answer_flagged": _percentiles([r.corrected_ms for r in flagged]),
        }
    else:
        scorecard["latency"] = {"ABSENT": "latency requires live mode (real model round-trips)"}

    return scorecard


# ─────────────────────────── report ──────────────────────────────────


def format_report(sc: dict[str, Any]) -> str:
    L = []
    L.append("=" * 66)
    L.append(f"plivo-mirror v4 scorecard  [mode={sc['mode']}  model={sc.get('model')}  date={sc['date']}]")
    L.append("=" * 66)
    if sc.get("live_status"):
        L.append(f"LIVE: {sc['live_status']}")
        return "\n".join(L)
    ind = sc["induced"]
    gold = sc["golden"]
    L.append(f"\nINDUCED — {ind['label']}")
    L.append(f"  source: {sc['induced_source']}")
    L.append(f"  catch_rate = {ind['catch_rate']:.0%}  ({ind['caught']}/{ind['n']})")
    L.append(f"    missed_at_gate     = {ind['missed_at_gate']}  ← lexicon/recall ceiling")
    L.append(f"    missed_at_verifier = {ind['missed_at_verifier']}")
    L.append("  by category (fire rate):")
    for cat, d in ind["by_category"].items():
        L.append(f"    {cat:<26} {d['fired']}/{d['n']}  ({d['fire_rate']:.0%})")
    L.append(f"\nGOLDEN — {gold['label']}")
    L.append(f"  source: {sc['golden_source']}")
    L.append(f"  false_intervention_rate = {gold['false_intervention_rate']:.0%}  ({gold['fired']}/{gold['n']})")
    if gold["fired_ids"]:
        L.append(f"  fired (FPs): {', '.join(gold['fired_ids'])}")
    L.append("  by category (fire rate):")
    for cat, d in gold["by_category"].items():
        L.append(f"    {cat:<26} {d['fired']}/{d['n']}  ({d['fire_rate']:.0%})")
    nm = sc.get("induced_near_miss")
    if nm:
        L.append(f"\nINDUCED NEAR-MISS — {nm['label']}")
        L.append(f"  false_intervention_rate = {nm['false_intervention_rate']:.0%}  ({nm['fired']}/{nm['n']})")
        if nm["fired_ids"]:
            L.append(f"  fired (FPs): {', '.join(nm['fired_ids'])}")
    L.append(f"\nROUTING")
    L.append(f"  verifier_hit_rate = {sc['verifier_hit_rate']:.0%}   lexicon_fire_rate = {sc['lexicon_fire_rate']:.0%}")
    L.append(f"  {sc['routing_note']}")
    L.append(f"\nLATENCY")
    lat = sc["latency"]
    if "ABSENT" in lat:
        L.append(f"  ABSENT — {lat['ABSENT']}")
    else:
        L.append(f"  {lat['note']}")
        for k in ("time_to_first_audio_clean", "time_to_first_audio_flagged", "time_to_corrected_answer_flagged"):
            v = lat[k]
            L.append(f"    {k:<34} {v if v else 'n/a'}")
    L.append("=" * 66)
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="plivo-mirror v4 measurement harness")
    ap.add_argument("--induced", default="../v3/datasets/eval_v1.jsonl")
    ap.add_argument("--golden", default="datasets/golden_v1.jsonl")
    ap.add_argument("--policies", default="../v3/datasets/policies_v1.txt")
    ap.add_argument("--mode", choices=["deterministic", "live"], default="deterministic")
    ap.add_argument("--model", default=None)
    ap.add_argument("--date", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--json", action="store_true", help="emit raw scorecard JSON")
    args = ap.parse_args()

    sc = asyncio.run(
        evaluate(
            induced_path=args.induced,
            golden_path=args.golden,
            policies_path=args.policies,
            mode=args.mode,
            model=args.model,
            run_date=args.date,
            limit=args.limit,
        )
    )
    if args.json:
        print(json.dumps(sc, indent=2))
    else:
        print(format_report(sc))


if __name__ == "__main__":
    main()


__all__ = ["evaluate", "format_report", "load_cases", "Case"]
