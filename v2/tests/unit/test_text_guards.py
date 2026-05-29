"""text_guards — drop instruction-format LLM output before TTS speaks it."""

from __future__ import annotations

import pytest

from plivo_mirror._internal.text_guards import (
    looks_like_instruction,
    sanitise_suggested_correction,
)


@pytest.mark.parametrize(
    "text",
    [
        "Please confirm the order as chicken sandwich only before placing it.",
        "Before placing anything, read the order back to the customer: 'Just to confirm...'",
        "Tell the customer: 'I can help with that, but I'll need to transfer you.'",
        "Say to the caller: 'Got it.'",
        "Read the order back to the customer.",
        "The agent should ask the customer to confirm.",
        "You should say 'Just to confirm — is that right?'",
        "You should transfer them to a human supervisor.",
        "If a refund is involved, transfer to a human.",
        "Before charging the card, confirm with the customer.",
        "First, confirm. Then proceed.",
        "Then if any refund issue is involved, transfer to a supervisor.",
    ],
)
def test_instruction_format_flagged(text: str) -> None:
    assert looks_like_instruction(text), f"should be flagged: {text!r}"
    assert sanitise_suggested_correction(text) == ""


@pytest.mark.parametrize(
    "text",
    [
        "Just to confirm — you'd like a chicken sandwich only, is that right?",
        "Got it — one mushroom pizza, anything else?",
        "I'll need to transfer you to a human supervisor for refunds.",
        "Sure, one large cheese pizza coming up.",
        "Sorry, could you say that again?",
        "Hey, welcome to Burger Plivo! What can I get started for you?",
        "Your total comes to $19.00, thank you!",
    ],
)
def test_clean_speech_passes_through(text: str) -> None:
    assert not looks_like_instruction(text), f"should NOT be flagged: {text!r}"
    assert sanitise_suggested_correction(text) == text.strip()


def test_empty_returns_empty() -> None:
    assert sanitise_suggested_correction("") == ""
    assert sanitise_suggested_correction("   ") == ""
    assert looks_like_instruction("") is False
