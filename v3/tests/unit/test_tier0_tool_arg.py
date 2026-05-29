"""Unit tests — Tier 0 tool-argument consistency check."""

from __future__ import annotations

import pytest

from plivo_mirror.context import (
    HistoryTurn,
    SupervisorContext,
    ToolCallIntent,
    TurnPayload,
)
from plivo_mirror.scorer.tier0.tool_arg_check import (
    ToolArgConsistencyCheck,
    _split_on_retraction,
)


CTX = SupervisorContext(call_uuid="test-call")


def _turn(
    *,
    customer_text: str,
    tool_calls: list[ToolCallIntent] | None = None,
    primary_text: str = "",
    history: list[HistoryTurn] | None = None,
) -> TurnPayload:
    return TurnPayload(
        customer_text=customer_text,
        primary_text=primary_text,
        tool_calls=tool_calls or [],
        history=history or [],
    )


def test_no_tool_calls_passes_through():
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(customer_text="actually just a BLT, no club"),
        CTX,
    )
    assert result.verdict is None


def test_no_retraction_marker_passes_through():
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="I'd like a club sandwich please",
            tool_calls=[
                ToolCallIntent(name="place_order", args={"items": ["club sandwich"]})
            ],
        ),
        CTX,
    )
    assert result.verdict is None


def test_classic_retraction_fires():
    """Customer retracts the club; agent's tool call still includes it."""
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="A club sandwich, actually just a BLT please",
            tool_calls=[
                ToolCallIntent(
                    name="place_order",
                    args={"items": ["club sandwich", "BLT"]},
                    irreversible=True,
                )
            ],
        ),
        CTX,
    )
    assert result.verdict is not None
    assert result.verdict.should_intervene is True
    assert result.verdict.score >= 0.9
    assert result.verdict.blocked_tool == "place_order"
    # Evidence should mention which token was retracted.
    assert "club" in result.verdict.evidence["violating_tokens"]


def test_no_violation_when_tool_args_drop_retracted_item():
    """Agent correctly handled the retraction — no violation."""
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="A club sandwich, actually just a BLT please",
            tool_calls=[
                ToolCallIntent(name="place_order", args={"items": ["BLT"]})
            ],
        ),
        CTX,
    )
    assert result.verdict is None


def test_multiple_retraction_markers_uses_latest():
    """Customer changes their mind twice — only the final preference
    counts, anything before the *latest* marker is retracted."""
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="Cheeseburger, actually a veggie burger, no wait, just a salad",
            tool_calls=[
                ToolCallIntent(name="place_order", args={"items": ["veggie burger"]})
            ],
        ),
        CTX,
    )
    # "veggie burger" was retracted by "no wait" → tool args wrong.
    assert result.verdict is not None
    assert result.verdict.should_intervene is True


def test_split_on_retraction_finds_latest_marker():
    before, after = _split_on_retraction(
        "Cheeseburger, actually a veggie burger, instead a chicken sandwich"
    )
    # The latest marker is "instead"
    assert "instead" in before.lower()
    assert "chicken" in after.lower()


def test_split_returns_none_without_marker():
    # No retraction marker anywhere — should return None.
    assert _split_on_retraction("I would like a BLT please") is None


def test_dict_arg_with_nested_lists():
    """Tool args may have list values under various keys — we walk them all."""
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="Pepperoni pizza, actually just mushroom only",
            tool_calls=[
                ToolCallIntent(
                    name="place_order",
                    args={"main_items": ["pepperoni"], "sides": ["mushroom"]},
                )
            ],
        ),
        CTX,
    )
    assert result.verdict is not None
    assert "pepperoni" in result.verdict.evidence["violating_tokens"]
