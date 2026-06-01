"""Unit tests — Tier 0 tool-argument consistency check.

The check fires only on EXPLICIT, high-confidence retractions:
  * explicit negation of a named item — "no club", "without onions"
  * explicit replacement naming the dropped item — "instead of the fries"
  * "scratch that / cancel that" undoing the item named just before it

Ambiguous markers ("actually", bare "just") and anaphoric / multi-turn
corrections deliberately defer to Tier 1/2 so this 0.98-confidence Tier 0
check keeps its near-zero false-positive rate.
"""

from __future__ import annotations

from plivo_mirror.context import (
    HistoryTurn,
    SupervisorContext,
    ToolCallIntent,
    TurnPayload,
)
from plivo_mirror.scorer.tier0.tool_arg_check import (
    ToolArgConsistencyCheck,
    _retracted_items,
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
        _turn(customer_text="a club sandwich, no club, just a BLT"),
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


def test_explicit_negation_fires():
    """Customer explicitly negates the club; the tool call still has it."""
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="A club sandwich — no club, just a BLT please",
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
    assert "club" in result.verdict.evidence["violating_tokens"]


def test_no_violation_when_tool_args_drop_retracted_item():
    """Agent correctly handled the retraction — no violation."""
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="A club sandwich — no club, just a BLT please",
            tool_calls=[
                ToolCallIntent(name="place_order", args={"items": ["BLT"]})
            ],
        ),
        CTX,
    )
    assert result.verdict is None


def test_instead_of_fires():
    """'instead of the garlic bread' names garlic bread as dropped."""
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="Instead of the garlic bread, just give me a Coke.",
            tool_calls=[
                ToolCallIntent(
                    name="place_order", args={"items": ["garlic bread", "coke"]}
                )
            ],
        ),
        CTX,
    )
    assert result.verdict is not None
    assert result.verdict.should_intervene is True
    violating = result.verdict.evidence["violating_tokens"]
    assert "garlic" in violating or "bread" in violating


def test_scratch_that_fires():
    """'scratch that' retracts the item named immediately before it."""
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="A margherita please. Scratch that — a veggie supreme.",
            tool_calls=[
                ToolCallIntent(
                    name="place_order", args={"items": ["margherita", "veggie supreme"]}
                )
            ],
        ),
        CTX,
    )
    assert result.verdict is not None
    assert "margherita" in result.verdict.evidence["violating_tokens"]


def test_scratch_that_handled():
    """Agent dropped the scratched margherita — no violation."""
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="A margherita please. Scratch that — a veggie supreme.",
            tool_calls=[
                ToolCallIntent(name="place_order", args={"items": ["veggie supreme"]})
            ],
        ),
        CTX,
    )
    assert result.verdict is None


def test_ambiguous_actually_defers_to_tier2():
    """Bare 'actually' is too ambiguous for a 0.98 Tier 0 verdict — it
    could introduce a new thought rather than retract the prior item — so
    the check defers and lets the LLM judge decide."""
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="A club sandwich, actually just a BLT please",
            tool_calls=[
                ToolCallIntent(
                    name="place_order", args={"items": ["club sandwich", "BLT"]}
                )
            ],
        ),
        CTX,
    )
    assert result.verdict is None


def test_carried_over_modifier_not_flagged():
    """The classic false positive: a size modifier carried onto the
    replacement item must not be scored as retracted."""
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="Large pepperoni — make it mushroom, no pepperoni.",
            tool_calls=[
                ToolCallIntent(name="place_order", args={"items": ["large mushroom"]})
            ],
        ),
        CTX,
    )
    # "large" carried onto the mushroom; only "pepperoni" was retracted,
    # and the agent dropped it. No violation.
    assert result.verdict is None


def test_agent_recorded_exclusion_not_flagged():
    """Customer says "no olives"; agent correctly records the exclusion.
    The excluded token appearing in the args (in an exclusion field, or as
    a "no olives" modifier string) is the exclusion being honoured, NOT a
    kept retracted item."""
    check = ToolArgConsistencyCheck()
    for args in (
        {"items": ["veggie supreme"], "exclude": ["olives"]},
        {"items": ["veggie supreme"], "modifiers": ["no olives"]},
        {"items": ["veggie supreme no olives"]},
    ):
        result = check.evaluate(
            _turn(
                customer_text="A veggie supreme but no olives, I'm allergic.",
                tool_calls=[ToolCallIntent(name="place_order", args=args)],
            ),
            CTX,
        )
        assert result.verdict is None, args


def test_agent_added_negated_item_fires():
    """Customer says "no olives"; agent puts olives under an inclusion
    field ("add") — that IS keeping a negated item, so it must fire."""
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="A veggie supreme but no olives, I'm allergic.",
            tool_calls=[
                ToolCallIntent(
                    name="place_order",
                    args={"items": ["veggie supreme"], "add": ["olives"]},
                )
            ],
        ),
        CTX,
    )
    assert result.verdict is not None
    assert "olives" in result.verdict.evidence["violating_tokens"]


def test_dict_arg_with_nested_lists():
    """Tool args may have list values under various keys — we walk them all."""
    check = ToolArgConsistencyCheck()
    result = check.evaluate(
        _turn(
            customer_text="Pepperoni pizza — no pepperoni, just mushroom only",
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


def test_retracted_items_signals():
    """Direct coverage of the three high-confidence signals."""
    assert "pepperoni" in _retracted_items("mushroom, no pepperoni")
    assert _retracted_items("instead of the garlic bread, a coke") >= {"garlic", "bread"}
    assert "margherita" in _retracted_items("a margherita. scratch that, a veggie")
    # Ambiguous / anaphoric markers yield nothing at Tier 0.
    assert _retracted_items("a club sandwich, actually a BLT") == set()
    assert _retracted_items("change that first pizza to a mushroom") == set()
