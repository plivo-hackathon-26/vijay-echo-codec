"""Tiered pre-gate heuristic tests.

Every branch of ``should_score`` must (a) fire when the signal is
present and (b) NOT fire when it's absent. Without this the tiered
gate either over-scores (defeats the cost win) or under-scores
(misses interventions Mirror is supposed to catch).
"""

from __future__ import annotations

import pytest

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import HistoryTurn, ToolCallIntent, TurnPayload
from plivo_mirror.scorer.pregate import should_score
from tests.unit.conftest import FakeLLM


def _cfg(**overrides) -> MirrorConfig:
    return MirrorConfig(
        llm=FakeLLM(),
        policies=["dummy policy"],
        **overrides,
    )


# ── master switch ────────────────────────────────────────────────────────

def test_master_switch_off_always_scores() -> None:
    cfg = _cfg(tiered_scoring_enabled=False)
    turn = TurnPayload(customer_text="hi", primary_text="hello")
    run, reason = should_score(turn, cfg)
    assert run is True
    assert reason == "tiered_off"


# ── heuristic 1: tool calls ─────────────────────────────────────────────

def test_tool_call_present_fires() -> None:
    cfg = _cfg()
    turn = TurnPayload(
        customer_text="I want a pizza",
        primary_text="Got it.",
        tool_calls=[ToolCallIntent(name="place_order", args={"items": []})],
    )
    run, reason = should_score(turn, cfg)
    assert run is True
    assert "tool_call" in reason


def test_tool_call_skipped_when_force_off_and_not_irreversible() -> None:
    cfg = _cfg(
        tiered_force_score_on_tool_call=False,
        irreversible_tools=["place_order"],
    )
    turn = TurnPayload(
        customer_text="I want a pizza",
        primary_text="Got it.",
        tool_calls=[ToolCallIntent(name="lookup_menu", args={})],
    )
    run, _ = should_score(turn, cfg)
    assert run is False


def test_irreversible_tool_always_fires() -> None:
    cfg = _cfg(
        tiered_force_score_on_tool_call=False,
        irreversible_tools=["place_order"],
    )
    turn = TurnPayload(
        customer_text="I want a pizza",
        primary_text="Got it.",
        tool_calls=[ToolCallIntent(name="place_order", args={})],
    )
    run, reason = should_score(turn, cfg)
    assert run is True
    assert "irreversible" in reason


# ── heuristic 2: high-stakes content ─────────────────────────────────────

@pytest.mark.parametrize(
    "primary_text",
    [
        "Total is $42.50.",
        "Your booking is for Friday.",
        "Pickup at 6 PM.",
        "That'll be 3 items.",
        "I see two orders on your account.",
        # Sensitive-intent keywords in the agent's response.
        "Sure, I'll process that refund right away.",
        "I'll cancel your subscription now.",
        "I'll transfer you to a supervisor.",
        "I'll charge your card now.",
    ],
)
def test_high_stakes_content_fires(primary_text: str) -> None:
    cfg = _cfg()
    turn = TurnPayload(customer_text="ok", primary_text=primary_text)
    run, reason = should_score(turn, cfg)
    assert run is True
    assert reason == "high_stakes_content"


@pytest.mark.parametrize(
    "customer_text",
    [
        "I want a refund for my last order",
        "Cancel my subscription please",
        "Where is my order?",
        "I want to dispute the charge",
    ],
)
def test_sensitive_intent_in_customer_text_fires(customer_text: str) -> None:
    """Regression test from the live call trace: a customer asking for
    a refund must trigger the scorer even when the agent's response
    text is innocuous (e.g. "Sure!"). Mirror needs to evaluate whether
    the deflection is correct, not skip the turn because the agent
    text was short."""
    cfg = _cfg()
    turn = TurnPayload(customer_text=customer_text, primary_text="Sure!")
    run, reason = should_score(turn, cfg)
    assert run is True
    assert reason == "high_stakes_content"


# ── heuristic 3: correction markers ──────────────────────────────────────

@pytest.mark.parametrize(
    "customer_text",
    [
        "Actually make it mushroom",
        "Wait, change that to Delhi",
        "No, scratch that",
        "Instead of pepperoni",
        "Just cheese only",
    ],
)
def test_correction_marker_fires(customer_text: str) -> None:
    cfg = _cfg()
    turn = TurnPayload(
        customer_text=customer_text,
        primary_text="ok",
    )
    run, reason = should_score(turn, cfg)
    assert run is True
    assert reason == "correction_marker"


# ── heuristic 4: adjacent disagreement ───────────────────────────────────

def test_adjacent_disagreement_fires() -> None:
    cfg = _cfg()
    # No correction markers and no high-stakes tokens — the ONLY thing
    # that should fire is the adjacent-disagreement heuristic.
    history = [
        HistoryTurn(role="customer", text="large pepperoni veggie supreme"),
    ]
    turn = TurnPayload(
        customer_text="mushroom margherita marinara",
        primary_text="thanks",
        history=history,
    )
    run, reason = should_score(turn, cfg)
    assert run is True
    assert reason == "adjacent_disagreement"


def test_short_utterances_dont_count_as_disagreement() -> None:
    cfg = _cfg()
    history = [HistoryTurn(role="customer", text="yes")]
    turn = TurnPayload(
        customer_text="okay",
        primary_text="thanks",
        history=history,
    )
    run, _ = should_score(turn, cfg)
    assert run is False


# ── heuristic 5: post-intervention verify ────────────────────────────────

def test_post_intervention_fires() -> None:
    cfg = _cfg()
    turn = TurnPayload(customer_text="yes", primary_text="ok")
    run, reason = should_score(turn, cfg, prev_intervention=True)
    assert run is True
    assert reason == "post_intervention_verify"


# ── safe skip ────────────────────────────────────────────────────────────

def test_clean_turn_skips() -> None:
    cfg = _cfg()
    turn = TurnPayload(
        customer_text="hi how are you",
        primary_text="I'm great, how can I help",
    )
    run, reason = should_score(turn, cfg)
    assert run is False
    assert reason == "skipped_safe"
