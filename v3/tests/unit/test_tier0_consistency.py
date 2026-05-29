"""Unit tests — Tier 0 number / quantity consistency checks."""

from __future__ import annotations

from plivo_mirror.context import (
    SupervisorContext,
    ToolCallIntent,
    TurnPayload,
)
from plivo_mirror.scorer.tier0.consistency import (
    NumberConsistencyCheck,
    QuantityConsistencyCheck,
    _extract_ints,
    _extract_money,
    _ints_in_tool_args,
)


CTX = SupervisorContext(call_uuid="test-call")


def _turn(**kwargs) -> TurnPayload:
    defaults = dict(customer_text="", primary_text="", tool_calls=[], history=[])
    defaults.update(kwargs)
    return TurnPayload(**defaults)


# ─── _extract_ints / _extract_money helpers ──────────────────────────────


def test_extract_ints_digits_and_words():
    assert _extract_ints("I want 3 sandwiches and two sodas") == {3, 2}


def test_extract_ints_handles_empty():
    assert _extract_ints("") == set()


def test_extract_money_currency_symbol():
    assert _extract_money("That'll be $42.50") == {42.50}


def test_extract_money_suffix_words():
    assert _extract_money("Refund 30 dollars please") == {30.0}


def test_ints_in_tool_args_walks_nested():
    assert _ints_in_tool_args({"quantity": 3, "extras": {"count": 7}}) == {3, 7}


def test_ints_in_tool_args_pulls_from_strings():
    assert _ints_in_tool_args({"note": "charge $42"}) == {42}


# ─── NumberConsistencyCheck ──────────────────────────────────────────────


def test_no_money_in_either_side_passes():
    check = NumberConsistencyCheck()
    result = check.evaluate(
        _turn(customer_text="I want a refund", primary_text="Sure, transferring you"),
        CTX,
    )
    assert result.verdict is None


def test_matching_money_passes():
    check = NumberConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="Refund the $42 charge",
            primary_text="Got it, refunding $42 now",
        ),
        CTX,
    )
    assert result.verdict is None


def test_fabricated_amount_fires():
    """Customer said $42; agent confirmed $24 (transposition)."""
    check = NumberConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="Refund the $42 charge",
            primary_text="Got it, refunding $24 now",
        ),
        CTX,
    )
    assert result.verdict is not None
    assert result.verdict.should_intervene is True
    assert 24.0 in result.verdict.evidence["agent_money"]


def test_rounding_within_1pct_does_not_fire():
    """$9.99 vs $10.00 is just rounding — don't false-positive."""
    check = NumberConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="The $9.99 item",
            primary_text="That'll be $10 even",
        ),
        CTX,
    )
    # $10 vs $9.99 is ~0.1% off — within tolerance, doesn't fire.
    # If this fails, the tolerance heuristic in NumberConsistencyCheck
    # needs revisiting. The test documents intentional behaviour.
    assert result.verdict is None


# ─── QuantityConsistencyCheck ────────────────────────────────────────────


def test_quantity_match_passes():
    check = QuantityConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="Three burgers please",
            tool_calls=[
                ToolCallIntent(name="place_order", args={"quantity": 3})
            ],
        ),
        CTX,
    )
    assert result.verdict is None


def test_quantity_mismatch_fires():
    """Customer said three, tool args carry five."""
    check = QuantityConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="Three burgers please",
            tool_calls=[
                ToolCallIntent(name="place_order", args={"quantity": 5})
            ],
        ),
        CTX,
    )
    assert result.verdict is not None
    assert result.verdict.should_intervene is True
    assert result.verdict.blocked_tool == "place_order"


def test_no_quantity_in_customer_passes():
    """No integers in customer text — nothing to compare."""
    check = QuantityConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="A burger please",
            tool_calls=[
                ToolCallIntent(name="place_order", args={"quantity": 3})
            ],
        ),
        CTX,
    )
    assert result.verdict is None


def test_large_integer_in_tool_args_does_not_fire():
    """Customer said 'three', tool args carry order_id=12345 — order_id
    isn't a quantity, should be ignored."""
    check = QuantityConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="Three burgers please",
            tool_calls=[
                ToolCallIntent(
                    name="place_order",
                    args={"order_id": 12345, "quantity": 3},
                )
            ],
        ),
        CTX,
    )
    # 12345 is filtered out (only 0 < n <= 99 counts as plausible
    # quantity). The quantity field matches → no fire.
    assert result.verdict is None
