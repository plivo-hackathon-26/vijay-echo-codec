"""Unit tests for the v0.3.0 public text filters."""

from __future__ import annotations

import pytest

from plivo_mirror.text import is_customer_voice, is_meta_description


# ─── is_customer_voice ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "I'd like cheese only, please.",
        "I would like a BLT",
        "I want a turkey sandwich",
        "I need a refund",
        "Can I get a club sandwich?",
        "Could I have fries with that",
        "May I order a salad",
        "Give me one veggie wrap",
        "I'm looking for a cheese sandwich",
    ],
)
def test_is_customer_voice_flags_customer_phrasing(text):
    assert is_customer_voice(text), f"expected customer-voice match: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "Got it — one cheese sandwich for you.",
        "Sure thing, the BLT is coming up",
        "Let me transfer you to a supervisor",
        "Your total comes to nine dollars",
        "Anything else?",
        "Sorry — could you say that again?",
    ],
)
def test_is_customer_voice_skips_agent_phrasing(text):
    assert not is_customer_voice(text), f"unexpected match: {text!r}"


def test_is_customer_voice_empty_input():
    assert not is_customer_voice("")
    assert not is_customer_voice(None)  # type: ignore[arg-type]
    assert not is_customer_voice("   ")


# ─── is_meta_description ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "The customer said their friend wants veggie, but they personally want cheese",
        "The customer wants cheese",
        "They said no club",
        "The customer asked for a refund",
        "Customer wants cheese",
        "The caller said to drop the club",
        "The user mentioned a refund",
    ],
)
def test_is_meta_description_flags_third_person_descriptions(text):
    assert is_meta_description(text), f"expected meta-description match: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "one cheese sandwich only",
        "BLT, no club sandwich",
        "cheese sandwich",
        "refund request",
        "veggie wrap with chips",
        "transfer to human supervisor",
    ],
)
def test_is_meta_description_skips_concrete_orders(text):
    assert not is_meta_description(text), f"unexpected match: {text!r}"


def test_is_meta_description_empty_input():
    assert not is_meta_description("")
    assert not is_meta_description(None)  # type: ignore[arg-type]
