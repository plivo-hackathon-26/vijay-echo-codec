"""Unit tests — Tier 1 HuggingFaceClassifier with mocked HTTP."""

from __future__ import annotations

import pytest

from plivo_mirror.context import SupervisorContext, TurnPayload
from plivo_mirror.scorer.tier1.huggingface import (
    DEFAULT_LABELS,
    HuggingFaceClassifier,
    _build_premise,
    _format_args,
)


CTX = SupervisorContext(call_uuid="test-call")


def _turn(*, customer_text: str = "", primary_text: str = "", tool_calls=None, history=None) -> TurnPayload:
    return TurnPayload(
        customer_text=customer_text,
        primary_text=primary_text,
        tool_calls=tool_calls or [],
        history=history or [],
    )


def _hf_response(violation_prob: float) -> dict:
    """Mock HF zero-shot response with the canonical shape."""
    fine = 1.0 - violation_prob
    return {
        "sequence": "...",
        "labels": list(DEFAULT_LABELS),
        "scores": [violation_prob, fine],
    }


# ─── helpers ────────────────────────────────────────────────────────────


def test_build_premise_includes_customer_and_agent_text():
    text = _build_premise(
        _turn(customer_text="hello", primary_text="hi there"),
    )
    assert "hello" in text
    assert "hi there" in text


def test_format_args_truncates_long_values():
    long_value = "x" * 500
    out = _format_args({"big": long_value})
    assert len(out) <= 120


# ─── classifier behaviour ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_returns_high_confidence_violation(httpx_mock):
    httpx_mock.add_response(json=_hf_response(0.95))

    classifier = HuggingFaceClassifier(api_key="hf_test")
    result = await classifier.classify(
        _turn(customer_text="I want a refund", primary_text="No problem, refund coming"),
        CTX,
    )
    await classifier.aclose()

    assert result.violation_prob == pytest.approx(0.95)
    assert result.confidence == "high"
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_classify_returns_high_confidence_fine(httpx_mock):
    httpx_mock.add_response(json=_hf_response(0.05))

    classifier = HuggingFaceClassifier(api_key="hf_test")
    result = await classifier.classify(
        _turn(customer_text="thanks", primary_text="you're welcome"),
        CTX,
    )
    await classifier.aclose()

    assert result.violation_prob == pytest.approx(0.05)
    assert result.confidence == "high"


@pytest.mark.asyncio
async def test_classify_uncertain_band_escalates(httpx_mock):
    httpx_mock.add_response(json=_hf_response(0.55))

    classifier = HuggingFaceClassifier(api_key="hf_test")
    result = await classifier.classify(
        _turn(customer_text="ambiguous", primary_text="maybe"),
        CTX,
    )
    await classifier.aclose()

    assert 0.20 < result.violation_prob < 0.85
    assert result.confidence == "uncertain"


@pytest.mark.asyncio
async def test_classify_handles_http_error_gracefully(httpx_mock):
    httpx_mock.add_response(status_code=500, text="boom")

    classifier = HuggingFaceClassifier(api_key="hf_test", timeout_s=1.0)
    result = await classifier.classify(
        _turn(customer_text="x", primary_text="y"),
        CTX,
    )
    await classifier.aclose()

    # Failure → fail-open: prob=0.5, confidence=uncertain so the
    # orchestrator escalates to Tier 2 (or no-op).
    assert result.violation_prob == pytest.approx(0.5)
    assert result.confidence == "uncertain"
    assert "error" in result.raw


@pytest.mark.asyncio
async def test_classify_handles_malformed_json(httpx_mock):
    """HF sometimes returns a list-wrapped dict on serverless."""
    httpx_mock.add_response(json=[_hf_response(0.9)])

    classifier = HuggingFaceClassifier(api_key="hf_test")
    result = await classifier.classify(
        _turn(customer_text="x", primary_text="y"),
        CTX,
    )
    await classifier.aclose()

    assert result.violation_prob == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_classify_uses_dedicated_endpoint_url(httpx_mock):
    """When endpoint_url is set, requests go there instead of the
    default api-inference URL."""
    httpx_mock.add_response(
        url="https://my-endpoint.cloud/v1",
        json=_hf_response(0.1),
    )

    classifier = HuggingFaceClassifier(
        api_key="hf_test",
        endpoint_url="https://my-endpoint.cloud/v1",
    )
    result = await classifier.classify(
        _turn(customer_text="x", primary_text="y"),
        CTX,
    )
    await classifier.aclose()
    assert result.violation_prob == pytest.approx(0.1)


def test_invalid_label_count_rejected():
    with pytest.raises(ValueError):
        HuggingFaceClassifier(
            api_key="hf_test", candidate_labels=["only_one_label"]
        )


def test_invalid_threshold_order_rejected():
    with pytest.raises(ValueError):
        HuggingFaceClassifier(
            api_key="hf_test",
            high_confidence_low=0.8,
            high_confidence_high=0.2,
        )
