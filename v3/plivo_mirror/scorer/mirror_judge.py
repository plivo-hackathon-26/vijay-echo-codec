"""MirrorJudge — the three-tier scoring orchestrator.

Replaces the v0.1.0 single-LLM scorer with a tiered ensemble:

    Tier 0 (deterministic, ~5-20μs)
      • Tool-arg consistency
      • Number / quantity consistency
      • Policy keyword tripwires
      • Contradiction-marker check
              │
              ▼  no hard verdict
    Tier 1 (NLI classifier, ~30-200ms via HF Inference API)
      • DeBERTa-v3-large-zeroshot-v2.0
      • Calibrated violation probability + confidence band
              │
              ▼  uncertain
    Tier 2 (LLM judge, ~200-400ms via Atla Selene API)
      • Atla Selene 8B fine-tuned judge
      • Full structured Verdict with critique

Most turns return a verdict from Tier 0 or Tier 1; Tier 2 only fires
on the ~5-10% of turns the classifier flagged as uncertain. Result:
median latency ~35ms, p99 ~400ms, ~10× cheaper than always calling
GPT-5-mini.

MirrorJudge implements the same ``score()`` API as the v0.1.0 LLMScorer,
so it slots into MirrorConfig.scorer without changing the supervisor
pipeline.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import SupervisorContext, TurnPayload, Verdict
from plivo_mirror.scorer.tier0.arithmetic import ArithmeticConsistencyCheck
from plivo_mirror.scorer.tier0.base import Tier0Check, Tier0Result
from plivo_mirror.scorer.tier0.consistency import (
    NumberConsistencyCheck,
    QuantityConsistencyCheck,
)
from plivo_mirror.scorer.tier0.contradiction import ContradictionMarkerCheck
from plivo_mirror.scorer.tier0.policy_tripwires import PolicyTripwireCheck
from plivo_mirror.scorer.tier0.tool_arg_check import ToolArgConsistencyCheck
from plivo_mirror.scorer.tier1.base import Tier1Classifier, Tier1Result
from plivo_mirror.scorer.tier2.base import Tier2Judge, Tier2Result

log = logging.getLogger("plivo_mirror.scorer.mirror_judge")


def _default_tier0_checks() -> list[Tier0Check]:
    """The default order: cheapest+most-precise first, broadest last."""
    return [
        ToolArgConsistencyCheck(),       # zero false positives
        ContradictionMarkerCheck(),      # high precision
        ArithmeticConsistencyCheck(),    # recomputes totals/change, narrow
        QuantityConsistencyCheck(),      # tight precondition
        NumberConsistencyCheck(),        # money-only, narrow
        PolicyTripwireCheck(),           # if-then rules
    ]


@dataclass
class TierLatency:
    """Per-tier wall-clock breakdown for one ``score()`` call."""

    tier0_us: int = 0
    tier1_ms: int = 0
    tier2_ms: int = 0


@dataclass
class MirrorJudge:
    """Three-tier scorer. Drop-in replacement for v0.1.0's LLMScorer.

    Args:
        config: The MirrorConfig (used to read intervention_threshold).
        tier0_checks: Ordered list of deterministic checks. The first
            one whose ``evaluate()`` returns a verdict short-circuits.
            Set to ``[]`` to disable Tier 0 entirely.
        tier1: Async NLI classifier (default: HuggingFaceClassifier).
            ``None`` skips Tier 1 entirely → goes straight to Tier 2.
        tier2: Async judge LLM (default: AtlaSeleneJudge). ``None``
            skips Tier 2 → uncertain Tier 1 verdicts pass through with
            their probability as Verdict.score.
        on_tier_complete: Optional callback receiving (tier_name,
            verdict_or_none, latency). For metrics / telemetry hooks.
        skip_when_no_customer_text: When True (default), ``score()``
            short-circuits to no-intervention on turns where the
            customer hasn't said anything (typically the agent's
            opening greeting). Avoids wasting Tier 2 budget on
            reviews that have nothing to score against.
    """

    config: MirrorConfig
    tier0_checks: list[Tier0Check] = field(default_factory=_default_tier0_checks)
    tier1: Tier1Classifier | None = None
    tier2: Tier2Judge | None = None
    on_tier_complete: Any | None = None
    skip_when_no_customer_text: bool = True

    name: str = "mirror_judge"

    async def score(
        self, turn: TurnPayload, ctx: SupervisorContext
    ) -> Verdict:
        """Run the three-tier pipeline and return a Verdict.

        Never raises — every tier handles its own errors and falls
        through to the next (or to a no-intervention verdict if all
        tiers are skipped).
        """
        latencies = TierLatency()

        # Empty-customer-text short-circuit. The agent's opening greeting
        # has no customer utterance yet; reviewing it wastes a Tier 2
        # call and the judge tends to produce confused "the customer
        # said nothing" corrections. Disable per-call by setting
        # ``skip_when_no_customer_text=False`` on construction.
        if self.skip_when_no_customer_text and not (turn.customer_text or "").strip():
            return self._stamp(
                Verdict.no_intervention("no_customer_text"),
                latencies,
                "skipped_empty_customer",
            )

        # ── Tier 0 ──────────────────────────────────────────────────
        t0_start = time.perf_counter()
        for check in self.tier0_checks:
            try:
                result = check.evaluate(turn, ctx)
            except Exception:
                log.exception("tier0 check %r raised — skipping", check.name)
                continue
            if result.verdict is not None:
                latencies.tier0_us = int((time.perf_counter() - t0_start) * 1_000_000)
                self._notify("tier0", result.verdict, latencies)
                return self._stamp(result.verdict, latencies, check.name)
        latencies.tier0_us = int((time.perf_counter() - t0_start) * 1_000_000)
        self._notify("tier0", None, latencies)

        # ── Tier 1 ──────────────────────────────────────────────────
        tier1_prob: float | None = None
        if self.tier1 is not None:
            try:
                t1_result: Tier1Result = await self.tier1.classify(turn, ctx)
                latencies.tier1_ms = t1_result.latency_ms
                tier1_prob = t1_result.violation_prob
                self._notify("tier1", t1_result, latencies)
                if t1_result.confidence == "high":
                    should = t1_result.violation_prob >= self.config.intervention_threshold
                    verdict = Verdict(
                        score=t1_result.violation_prob,
                        reason=(
                            "tier1 classifier confident "
                            f"({t1_result.violation_prob:.2f})"
                        ),
                        should_intervene=should,
                        suggested_correction="",
                        should_report=should,
                        evidence={
                            "tier": "tier1",
                            "tier1_prob": t1_result.violation_prob,
                            "tier1_confidence": t1_result.confidence,
                            "classifier": getattr(self.tier1, "name", "tier1"),
                        },
                    )
                    return self._stamp(verdict, latencies, "tier1")
            except Exception:
                log.exception("tier1 classifier failed — falling through")

        # ── Tier 2 ──────────────────────────────────────────────────
        if self.tier2 is not None:
            try:
                t2_result: Tier2Result = await self.tier2.judge(
                    turn, ctx, tier1_violation_prob=tier1_prob or 0.5
                )
                latencies.tier2_ms = t2_result.latency_ms
                self._notify("tier2", t2_result, latencies)
                verdict = t2_result.verdict
                # Stamp tier evidence + tier1 context so post-call
                # reports see the full chain.
                if verdict.evidence is None or not isinstance(verdict.evidence, dict):
                    verdict.evidence = {}
                verdict.evidence.setdefault("tier1_prob", tier1_prob)
                verdict.evidence.setdefault("tier", "tier2")
                return self._stamp(verdict, latencies, "tier2")
            except Exception:
                log.exception("tier2 judge failed — using tier1 prob if available")

        # ── Fallback: emit tier1 prob as-is (or no-intervention) ────
        if tier1_prob is not None:
            should = tier1_prob >= self.config.intervention_threshold
            verdict = Verdict(
                score=tier1_prob,
                reason=f"tier1-only (tier2 unavailable, prob={tier1_prob:.2f})",
                should_intervene=should,
                should_report=should,
                evidence={
                    "tier": "tier1_only_fallback",
                    "tier1_prob": tier1_prob,
                },
            )
            return self._stamp(verdict, latencies, "tier1_only")

        return self._stamp(
            Verdict.no_intervention("no_signal"),
            latencies,
            "no_tier_signal",
        )

    # ─────────────────────── helpers ─────────────────────────────────

    def _stamp(
        self, verdict: Verdict, latencies: TierLatency, deciding_tier: str
    ) -> Verdict:
        """Attach latency + tier metadata to the Verdict for telemetry."""
        if verdict.evidence is None or not isinstance(verdict.evidence, dict):
            verdict.evidence = {}
        verdict.evidence.setdefault("deciding_tier", deciding_tier)
        verdict.evidence.setdefault(
            "latency",
            {
                "tier0_us": latencies.tier0_us,
                "tier1_ms": latencies.tier1_ms,
                "tier2_ms": latencies.tier2_ms,
            },
        )
        return verdict

    def _notify(self, tier_name: str, payload: Any, latencies: TierLatency) -> None:
        cb = self.on_tier_complete
        if cb is None:
            return
        try:
            cb(tier_name, payload, latencies)
        except Exception:
            log.debug("on_tier_complete callback raised — ignored", exc_info=True)


__all__ = ["MirrorJudge", "TierLatency"]
