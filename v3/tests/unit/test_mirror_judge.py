"""MirrorJudge orchestrator tests — tier short-circuiting + escalation."""

from __future__ import annotations

import pytest

from plivo_mirror import MirrorConfig, MirrorJudge
from plivo_mirror.context import (
    SupervisorContext,
    ToolCallIntent,
    TurnPayload,
    Verdict,
)
from plivo_mirror.scorer.tier1.base import Tier1Classifier, Tier1Result
from plivo_mirror.scorer.tier2.base import Tier2Judge, Tier2Result


CTX = SupervisorContext(call_uuid="test-call")


class _FakeLLM:
    """Minimal LLM stub so MirrorConfig validation passes."""

    async def structured_output(self, *a, **kw):
        return {}

    async def chat(self, *a, **kw):
        return ""


def _config(**overrides) -> MirrorConfig:
    return MirrorConfig(
        llm=_FakeLLM(),
        policies=["Latest preference wins."],
        intervention_threshold=overrides.pop("threshold", 0.7),
        **overrides,
    )


def _turn(**kw) -> TurnPayload:
    defaults = dict(customer_text="", primary_text="", tool_calls=[], history=[])
    defaults.update(kw)
    return TurnPayload(**defaults)


# ─── Fake tier 1 / tier 2 doubles ───────────────────────────────────────


class _FakeTier1(Tier1Classifier):
    def __init__(self, prob: float, confidence: str = "high"):
        self.prob = prob
        self.confidence_band = confidence
        self.calls = 0
        self.name = "fake_tier1"

    async def classify(self, turn, ctx):
        self.calls += 1
        return Tier1Result(
            violation_prob=self.prob,
            confidence=self.confidence_band,
            raw={"fake": True},
            latency_ms=42,
        )


class _FakeTier2(Tier2Judge):
    def __init__(self, score: float, intervene: bool):
        self.score = score
        self.intervene = intervene
        self.calls = 0
        self.name = "fake_tier2"

    async def judge(self, turn, ctx, tier1_violation_prob):
        self.calls += 1
        return Tier2Result(
            verdict=Verdict(
                score=self.score,
                reason="fake tier2",
                should_intervene=self.intervene,
                evidence={"fake_tier2": True},
            ),
            raw={"fake": True},
            latency_ms=120,
        )


# ─── tier 0 short-circuit ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tier0_short_circuit_on_tool_arg_violation():
    """Retraction marker + retracted item in tool args → Tier 0 fires;
    Tier 1 and Tier 2 must not be called."""
    fake_t1 = _FakeTier1(prob=0.5)
    fake_t2 = _FakeTier2(score=0.5, intervene=False)
    judge = MirrorJudge(config=_config(), tier1=fake_t1, tier2=fake_t2)

    verdict = await judge.score(
        _turn(
            customer_text="A club sandwich — no club, just a BLT",
            tool_calls=[
                ToolCallIntent(name="place_order", args={"items": ["club", "BLT"]})
            ],
        ),
        CTX,
    )

    assert verdict.should_intervene is True
    assert fake_t1.calls == 0
    assert fake_t2.calls == 0
    # Deciding tier should be tier0 (from one of the checks).
    assert verdict.evidence["deciding_tier"].startswith("tool_arg") or verdict.evidence["deciding_tier"] == "tier0"


# ─── tier 1 confident → no tier 2 call ──────────────────────────────────


@pytest.mark.asyncio
async def test_tier1_high_confidence_violation_short_circuits():
    fake_t1 = _FakeTier1(prob=0.95, confidence="high")
    fake_t2 = _FakeTier2(score=0.0, intervene=False)
    judge = MirrorJudge(config=_config(), tier1=fake_t1, tier2=fake_t2)

    verdict = await judge.score(
        _turn(customer_text="random ambiguous", primary_text="random reply"),
        CTX,
    )

    assert verdict.should_intervene is True
    assert verdict.score == pytest.approx(0.95)
    assert fake_t1.calls == 1
    assert fake_t2.calls == 0
    assert verdict.evidence["deciding_tier"] == "tier1"


@pytest.mark.asyncio
async def test_tier1_high_confidence_fine_short_circuits():
    fake_t1 = _FakeTier1(prob=0.05, confidence="high")
    fake_t2 = _FakeTier2(score=0.99, intervene=True)
    judge = MirrorJudge(config=_config(), tier1=fake_t1, tier2=fake_t2)

    verdict = await judge.score(
        _turn(customer_text="thanks", primary_text="you're welcome"),
        CTX,
    )

    assert verdict.should_intervene is False
    assert fake_t2.calls == 0


# ─── tier 1 uncertain → tier 2 fires ────────────────────────────────────


@pytest.mark.asyncio
async def test_tier1_uncertain_escalates_to_tier2():
    fake_t1 = _FakeTier1(prob=0.55, confidence="uncertain")
    fake_t2 = _FakeTier2(score=0.92, intervene=True)
    judge = MirrorJudge(config=_config(), tier1=fake_t1, tier2=fake_t2)

    verdict = await judge.score(
        _turn(customer_text="ambiguous request", primary_text="ambiguous reply"),
        CTX,
    )

    assert fake_t1.calls == 1
    assert fake_t2.calls == 1
    assert verdict.should_intervene is True
    assert verdict.score == pytest.approx(0.92)
    assert verdict.evidence["deciding_tier"] == "tier2"
    # Tier2 result should carry the tier1_prob for forensic purposes.
    assert verdict.evidence["tier1_prob"] == pytest.approx(0.55)


# ─── tier 2 unavailable: fall back to tier1 prob ────────────────────────


@pytest.mark.asyncio
async def test_tier2_missing_falls_back_to_tier1_prob():
    fake_t1 = _FakeTier1(prob=0.60, confidence="uncertain")
    judge = MirrorJudge(config=_config(), tier1=fake_t1, tier2=None)

    verdict = await judge.score(
        _turn(customer_text="x", primary_text="y"),
        CTX,
    )

    # Threshold=0.7, prob=0.60 → no intervention
    assert verdict.should_intervene is False
    assert verdict.score == pytest.approx(0.60)
    assert verdict.evidence["deciding_tier"] == "tier1_only"


# ─── no signal at all → no_intervention ─────────────────────────────────


@pytest.mark.asyncio
async def test_all_tiers_disabled_returns_no_intervention():
    judge = MirrorJudge(config=_config(), tier0_checks=[], tier1=None, tier2=None)
    verdict = await judge.score(
        _turn(customer_text="hi", primary_text="hi"),
        CTX,
    )
    assert verdict.should_intervene is False
    assert verdict.reason == "no_signal"


# ─── exception in tier 1 → falls through to tier 2 ──────────────────────


class _ExplodingTier1(Tier1Classifier):
    name = "exploding"

    async def classify(self, turn, ctx):
        raise RuntimeError("tier1 boom")


@pytest.mark.asyncio
async def test_tier1_exception_falls_through_to_tier2():
    fake_t2 = _FakeTier2(score=0.88, intervene=True)
    judge = MirrorJudge(config=_config(), tier1=_ExplodingTier1(), tier2=fake_t2)

    verdict = await judge.score(
        _turn(customer_text="x", primary_text="y"),
        CTX,
    )
    assert fake_t2.calls == 1
    assert verdict.should_intervene is True


# ─── exception in tier 2 → no-intervention (fail open) ──────────────────


class _ExplodingTier2(Tier2Judge):
    name = "exploding"

    async def judge(self, turn, ctx, tier1_violation_prob):
        raise RuntimeError("tier2 boom")


@pytest.mark.asyncio
async def test_tier2_exception_falls_open_with_tier1_prob():
    fake_t1 = _FakeTier1(prob=0.65, confidence="uncertain")
    judge = MirrorJudge(config=_config(), tier1=fake_t1, tier2=_ExplodingTier2())

    verdict = await judge.score(
        _turn(customer_text="x", primary_text="y"),
        CTX,
    )
    # Tier2 blew up → fall back to tier1_prob.
    assert verdict.score == pytest.approx(0.65)
    assert verdict.should_intervene is False


# ─── on_tier_complete callback fires ────────────────────────────────────


@pytest.mark.asyncio
async def test_on_tier_complete_callback_fires_for_each_tier():
    fake_t1 = _FakeTier1(prob=0.5, confidence="uncertain")
    fake_t2 = _FakeTier2(score=0.95, intervene=True)
    events = []

    def cb(tier, payload, latencies):
        events.append(tier)

    judge = MirrorJudge(
        config=_config(),
        tier1=fake_t1,
        tier2=fake_t2,
        on_tier_complete=cb,
    )
    await judge.score(_turn(customer_text="x", primary_text="y"), CTX)
    # tier0 (no hit), tier1 (uncertain), tier2 (fired)
    assert events == ["tier0", "tier1", "tier2"]
