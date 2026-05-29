"""Test the scorer across 3 distinct domains using identical library code.

Proves that ``plivo_mirror`` is genuinely domain-agnostic: the only
thing that changes between pizza / flight / refund tests is the
``policies`` list in MirrorConfig.
"""

from __future__ import annotations

from typing import Any

import pytest

from plivo_mirror.context import (
    HistoryTurn,
    SupervisorContext,
    ToolCallIntent,
    TurnPayload,
)
from plivo_mirror.scorer.llm import LLMScorer
from tests.unit.conftest import FakeLLM


# ─────────────────────────── domain configs ──────────────────────────────


PIZZA_POLICIES = [
    "Never include an item the customer retracted (using 'actually' / 'no' / 'instead').",
    "Read the order back to the customer before calling place_order.",
    "Do not promise delivery times.",
]

FLIGHT_POLICIES = [
    "Never book a destination the customer corrected away from.",
    "Always confirm the final destination + date before calling book_flights.",
    "Do not book a flight without both a destination and a departure date.",
]

REFUND_POLICIES = [
    "Never confirm a refund. Transfer to a human supervisor instead.",
    "Do not promise refund timelines.",
    "Always read back the order ID before any account action.",
]


# ─────────────────────────── verdict factories ───────────────────────────


def verdict_ok() -> dict[str, Any]:
    return {
        "score": 0.05,
        "reason": "clean response",
        "should_intervene": False,
    }


def verdict_intervene(reason: str, intent: str = "") -> dict[str, Any]:
    return {
        "score": 0.92,
        "reason": reason,
        "should_intervene": True,
        "suggested_correction": "Just to confirm — that's right?",
        "evidence": {
            "customer_intent": intent,
            "violation_summary": reason,
        },
    }


# ─────────────────────────── shared scoring helper ───────────────────────


async def _score_with(
    responder, policies: list[str], turn: TurnPayload
) -> Any:
    from plivo_mirror.config import MirrorConfig

    llm = FakeLLM(responder=responder)
    cfg = MirrorConfig(llm=llm, policies=policies, intervention_threshold=0.7)
    scorer = LLMScorer(cfg)
    ctx = SupervisorContext(call_uuid="t1")
    return await scorer.score(turn, ctx)


# ─────────────────────────── PIZZA fixtures ──────────────────────────────


async def _run_domain(
    name: str,
    policies: list[str],
    fixtures: dict[str, tuple[TurnPayload, dict, bool]],
) -> None:
    """Run a domain's fixture matrix.

    Each fixture is (turn, fake_verdict, expected_should_intervene).
    """
    for fixture_name, (turn, fake_verdict, expected) in fixtures.items():
        v = await _score_with(lambda s, u: fake_verdict, policies, turn)
        assert v.should_intervene is expected, (
            f"[{name}/{fixture_name}] expected should_intervene={expected}, "
            f"got score={v.score} reason={v.reason!r}"
        )


@pytest.mark.asyncio
async def test_pizza_domain() -> None:
    fixtures = {
        "clean": (
            TurnPayload(
                customer_text="I'd like a large pepperoni pizza please.",
                primary_text="Got it — one large pepperoni pizza coming up.",
                tool_calls=[],
                history=[],
            ),
            verdict_ok(),
            False,
        ),
        "retracted_item": (
            TurnPayload(
                customer_text="Large pepperoni, actually mushroom only.",
                primary_text="Got it, one large pepperoni and one mushroom.",
                tool_calls=[
                    ToolCallIntent(
                        name="place_order",
                        args={"items": ["large pepperoni", "mushroom"]},
                    )
                ],
                history=[],
            ),
            verdict_intervene("retracted item still in order", "large mushroom pizza"),
            True,
        ),
        "third_party": (
            TurnPayload(
                customer_text="My wife wants pepperoni but I'd like mushroom.",
                primary_text="Sure — one pepperoni and one mushroom.",
                tool_calls=[
                    ToolCallIntent(
                        name="place_order",
                        args={"items": ["pepperoni", "mushroom"]},
                    )
                ],
                history=[],
            ),
            verdict_intervene("third-party preference in order", "mushroom only"),
            True,
        ),
    }
    await _run_domain("pizza", PIZZA_POLICIES, fixtures)


@pytest.mark.asyncio
async def test_flight_domain() -> None:
    fixtures = {
        "clean": (
            TurnPayload(
                customer_text="Book me a flight to Mumbai on Friday.",
                primary_text="Got it — one flight to Mumbai on Friday.",
                tool_calls=[
                    ToolCallIntent(
                        name="book_flights",
                        args={
                            "flights": [
                                {"destination": "Mumbai", "departure_date": "Friday"}
                            ]
                        },
                    )
                ],
                history=[],
            ),
            verdict_ok(),
            False,
        ),
        "retracted_destination": (
            TurnPayload(
                customer_text="Mumbai Friday, actually Delhi Saturday.",
                primary_text="Booking Mumbai Friday and Delhi Saturday.",
                tool_calls=[
                    ToolCallIntent(
                        name="book_flights",
                        args={
                            "flights": [
                                {"destination": "Mumbai", "departure_date": "Friday"},
                                {"destination": "Delhi", "departure_date": "Saturday"},
                            ]
                        },
                    )
                ],
                history=[],
            ),
            verdict_intervene(
                "retracted destination still in booking", "Delhi on Saturday"
            ),
            True,
        ),
        "missing_date": (
            TurnPayload(
                customer_text="Book Goa.",
                primary_text="Booking Goa.",
                tool_calls=[
                    ToolCallIntent(
                        name="book_flights",
                        args={
                            "flights": [
                                {"destination": "Goa", "departure_date": "unknown"}
                            ]
                        },
                    )
                ],
                history=[],
            ),
            verdict_intervene("departure_date missing", "Goa on a specific date"),
            True,
        ),
    }
    await _run_domain("flight", FLIGHT_POLICIES, fixtures)


@pytest.mark.asyncio
async def test_refund_domain() -> None:
    fixtures = {
        "clean": (
            TurnPayload(
                customer_text="Just checking on order ORD-12345 status.",
                primary_text="Let me look that up for you.",
                tool_calls=[],
                history=[],
            ),
            verdict_ok(),
            False,
        ),
        "policy_violation_refund": (
            TurnPayload(
                customer_text="I want a refund for order ORD-12345.",
                primary_text="Sure, I'll process that refund right away.",
                tool_calls=[
                    ToolCallIntent(
                        name="process_refund", args={"order_id": "ORD-12345"}
                    )
                ],
                history=[],
            ),
            verdict_intervene(
                "policy 1: refunds must transfer to human",
                "speak with a human about refund",
            ),
            True,
        ),
        "missing_order_id": (
            TurnPayload(
                customer_text="Cancel my last order.",
                primary_text="Cancelling your last order now.",
                tool_calls=[
                    ToolCallIntent(
                        name="cancel_order", args={"order_id": "unknown"}
                    )
                ],
                history=[],
            ),
            verdict_intervene(
                "policy 3: order ID not read back",
                "confirm the order ID first",
            ),
            True,
        ),
    }
    await _run_domain("refund", REFUND_POLICIES, fixtures)
