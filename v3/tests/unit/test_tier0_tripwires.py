"""Unit tests — Tier 0 policy tripwires + contradiction marker check."""

from __future__ import annotations

import re

from plivo_mirror.context import SupervisorContext, TurnPayload
from plivo_mirror.scorer.tier0.contradiction import ContradictionMarkerCheck
from plivo_mirror.scorer.tier0.policy_tripwires import (
    DEFAULT_TRIPWIRES,
    PolicyTripwireCheck,
    Tripwire,
)


CTX = SupervisorContext(call_uuid="test-call")


def _turn(*, customer_text: str = "", primary_text: str = "") -> TurnPayload:
    return TurnPayload(
        customer_text=customer_text,
        primary_text=primary_text,
        tool_calls=[],
        history=[],
    )


# ─── Tripwires ──────────────────────────────────────────────────────────


def test_refund_without_handoff_fires():
    check = PolicyTripwireCheck(tripwires=DEFAULT_TRIPWIRES)
    result = check.evaluate(
        _turn(
            customer_text="I want a refund for my last order",
            primary_text="Sure, the refund will arrive in 5-7 days",
        ),
        CTX,
    )
    assert result.verdict is not None
    assert result.verdict.should_intervene is True
    assert result.evidence["tripwire"] == "refund_must_transfer"


def test_refund_with_transfer_passes():
    check = PolicyTripwireCheck(tripwires=DEFAULT_TRIPWIRES)
    result = check.evaluate(
        _turn(
            customer_text="I want a refund please",
            primary_text="Let me transfer you to a human supervisor",
        ),
        CTX,
    )
    assert result.verdict is None


def test_cancel_subscription_without_confirm_fires():
    check = PolicyTripwireCheck(tripwires=DEFAULT_TRIPWIRES)
    result = check.evaluate(
        _turn(
            customer_text="Cancel my subscription please",
            primary_text="Done, your subscription is now cancelled",
        ),
        CTX,
    )
    assert result.verdict is not None
    assert result.evidence["tripwire"] == "cancel_subscription_must_confirm_or_transfer"


def test_cancel_subscription_with_confirm_passes():
    check = PolicyTripwireCheck(tripwires=DEFAULT_TRIPWIRES)
    result = check.evaluate(
        _turn(
            customer_text="Cancel my subscription",
            primary_text="Are you sure you want to cancel? This is permanent.",
        ),
        CTX,
    )
    assert result.verdict is None


def test_dispute_charge_without_handoff_fires():
    check = PolicyTripwireCheck(tripwires=DEFAULT_TRIPWIRES)
    result = check.evaluate(
        _turn(
            customer_text="I want to dispute the charge from last week",
            primary_text="Sure, I've reversed it for you",
        ),
        CTX,
    )
    assert result.verdict is not None


def test_custom_tripwire_can_be_added():
    custom = Tripwire(
        name="never_promise_delivery_time",
        customer_pattern=re.compile(r"\bwhen will it (?:arrive|come)\b", re.I),
        required_in_response=re.compile(
            r"\b(estimate|approximately|usually|kitchen)\b", re.I
        ),
        reason="agent promised a specific delivery time without 'estimate' qualifier",
    )
    check = PolicyTripwireCheck(tripwires=[custom])
    result = check.evaluate(
        _turn(
            customer_text="When will it arrive?",
            primary_text="It will be there in exactly 30 minutes",
        ),
        CTX,
    )
    assert result.verdict is not None
    assert result.evidence["tripwire"] == "never_promise_delivery_time"


def test_empty_tripwire_list_never_fires():
    check = PolicyTripwireCheck(tripwires=[])
    result = check.evaluate(
        _turn(
            customer_text="refund refund refund",
            primary_text="I'll process the refund now",
        ),
        CTX,
    )
    assert result.verdict is None


def test_default_tripwires_includes_three_canonical():
    names = {t.name for t in DEFAULT_TRIPWIRES}
    assert "refund_must_transfer" in names
    assert "cancel_subscription_must_confirm_or_transfer" in names
    assert "dispute_charge_must_transfer" in names


def test_tripwires_off_by_default():
    """A generic safety net must not assume a customer's business rules.
    With no tripwires supplied the check never fires, even on 'refund'."""
    check = PolicyTripwireCheck()
    result = check.evaluate(
        _turn(
            customer_text="I want a refund for my last order",
            primary_text="Sure, the refund will arrive in 5-7 days",
        ),
        CTX,
    )
    assert result.verdict is None
    assert check.tripwires == []


# ─── ContradictionMarkerCheck ───────────────────────────────────────────


def test_contradiction_marker_fires_when_agent_repeats_retracted_token():
    check = ContradictionMarkerCheck()
    result = check.evaluate(
        _turn(
            customer_text="A large pepperoni, actually just mushroom only",
            primary_text="Got it — one large pepperoni and one mushroom!",
        ),
        CTX,
    )
    assert result.verdict is not None
    assert result.verdict.should_intervene is True
    assert "pepperoni" in result.verdict.evidence["agent_repeated_tokens"]


def test_contradiction_marker_no_marker_passes():
    check = ContradictionMarkerCheck()
    result = check.evaluate(
        _turn(
            customer_text="I want a pepperoni pizza",
            primary_text="Sure, one pepperoni pizza coming up",
        ),
        CTX,
    )
    assert result.verdict is None


def test_contradiction_marker_agent_does_not_repeat_passes():
    """Customer retracted; agent correctly only said the new item.
    No retracted token in the agent response → no fire."""
    check = ContradictionMarkerCheck()
    result = check.evaluate(
        _turn(
            customer_text="A pepperoni, actually just mushroom only",
            primary_text="Got it, one mushroom pizza",
        ),
        CTX,
    )
    assert result.verdict is None


def test_contradiction_marker_only_partial_repeat_skips_to_tier1():
    """Agent mentions the retracted token but NOT the new one — could be
    a benign acknowledgement. We defer to Tier 1 by returning None."""
    check = ContradictionMarkerCheck()
    result = check.evaluate(
        _turn(
            customer_text="Pepperoni, actually mushroom only",
            primary_text="Pepperoni you said?",
        ),
        CTX,
    )
    assert result.verdict is None
