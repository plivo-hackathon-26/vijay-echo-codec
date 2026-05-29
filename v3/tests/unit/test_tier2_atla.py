"""Unit tests — Tier 2 AtlaSeleneJudge with mocked HTTP."""

from __future__ import annotations

import pytest

from plivo_mirror.context import SupervisorContext, TurnPayload
from plivo_mirror.scorer.tier2.atla import AtlaSeleneJudge, _coerce_score


CTX = SupervisorContext(call_uuid="test-call")


def _turn(**kwargs) -> TurnPayload:
    defaults = dict(customer_text="", primary_text="", tool_calls=[], history=[])
    defaults.update(kwargs)
    return TurnPayload(**defaults)


# ─── _coerce_score helper ──────────────────────────────────────────────


def test_coerce_likert_to_probability():
    # 5 = clearly fine
    assert _coerce_score(5, 0.5) == pytest.approx(0.05)
    # 1 = clear violation
    assert _coerce_score(1, 0.5) == pytest.approx(0.97)
    # 3 = uncertain
    assert _coerce_score(3, 0.5) == pytest.approx(0.50)


def test_coerce_passthrough_probabilities():
    assert _coerce_score(0.42, 0.5) == pytest.approx(0.42)


def test_coerce_falls_back_on_garbage():
    assert _coerce_score("not a number", 0.123) == pytest.approx(0.123)
    assert _coerce_score(None, 0.7) == pytest.approx(0.7)


def test_coerce_unknown_int_falls_back():
    """Likert is 1-5; anything outside returns the fallback."""
    assert _coerce_score(7, 0.4) == pytest.approx(0.4)


# ─── judge behaviour ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_judge_clear_violation_returns_intervention(httpx_mock):
    httpx_mock.add_response(
        json={
            "score": 1,
            "critique": "agent confirmed a refund the operator does not allow",
        }
    )

    judge = AtlaSeleneJudge(
        api_key="atla_test",
        policies=["Never confirm a refund — always transfer to a human."],
    )
    result = await judge.judge(
        _turn(
            customer_text="I want a refund",
            primary_text="Sure, your refund is on its way",
        ),
        CTX,
        tier1_violation_prob=0.6,
    )
    await judge.aclose()

    assert result.verdict.should_intervene is True
    assert result.verdict.score >= 0.7
    assert "refund" in result.verdict.reason.lower()


@pytest.mark.asyncio
async def test_judge_clearly_fine_returns_no_intervention(httpx_mock):
    httpx_mock.add_response(
        json={"score": 5, "critique": "agent followed policy"}
    )

    judge = AtlaSeleneJudge(api_key="atla_test")
    result = await judge.judge(
        _turn(customer_text="hi", primary_text="hi there"),
        CTX,
        tier1_violation_prob=0.1,
    )
    await judge.aclose()

    assert result.verdict.should_intervene is False
    assert result.verdict.score < 0.7


@pytest.mark.asyncio
async def test_judge_fails_open_on_500(httpx_mock):
    httpx_mock.add_response(status_code=500, text="atla on fire")

    judge = AtlaSeleneJudge(api_key="atla_test", timeout_s=1.0)
    result = await judge.judge(
        _turn(customer_text="x", primary_text="y"),
        CTX,
        tier1_violation_prob=0.95,
    )
    await judge.aclose()

    # Fail-open: never raises, returns no-intervention with the error.
    assert result.verdict.should_intervene is False
    assert "error" in result.raw


@pytest.mark.asyncio
async def test_judge_handles_non_dict_response(httpx_mock):
    """Atla sometimes returns a plain string on weird inputs."""
    httpx_mock.add_response(json="not a dict")

    judge = AtlaSeleneJudge(api_key="atla_test")
    result = await judge.judge(
        _turn(customer_text="x", primary_text="y"),
        CTX,
        tier1_violation_prob=0.6,
    )
    await judge.aclose()

    # Falls back to tier1_prob with explanatory reason.
    assert result.verdict.score == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_judge_drops_instruction_format_correction(httpx_mock):
    """If Atla returns instruction-shaped 'suggested_correction' text,
    text_guards.sanitise should strip it so the orchestrator falls back
    to the generator's clean prompt."""
    httpx_mock.add_response(
        json={
            "score": 2,
            "critique": "agent skipped read-back",
            "suggested_correction": "Tell the customer: 'I'll confirm.'",
        }
    )

    judge = AtlaSeleneJudge(api_key="atla_test")
    result = await judge.judge(
        _turn(customer_text="confirm please", primary_text="placing order now"),
        CTX,
        tier1_violation_prob=0.6,
    )
    await judge.aclose()

    # Instruction-format text gets dropped → empty string.
    assert result.verdict.suggested_correction == ""
    # The verdict still intervenes.
    assert result.verdict.should_intervene is True


@pytest.mark.asyncio
async def test_judge_hits_custom_base_url(httpx_mock):
    httpx_mock.add_response(
        url="https://my-atla-proxy.local/v1/evaluation",
        json={"score": 5, "critique": "fine"},
    )
    judge = AtlaSeleneJudge(
        api_key="atla_test", base_url="https://my-atla-proxy.local/v1"
    )
    result = await judge.judge(
        _turn(customer_text="x", primary_text="y"),
        CTX,
        tier1_violation_prob=0.1,
    )
    await judge.aclose()
    assert result.verdict.score == pytest.approx(0.05)
