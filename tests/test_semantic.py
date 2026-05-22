"""mirror.semantic — LLM-based response reviewer.

We monkey-patch the openai client so these tests don't need network.
"""

import json
from unittest.mock import patch

import pytest

from mirror import semantic


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


def _fake_openai_returning(content: str):
    """Build a fake AsyncOpenAI client whose .chat.completions.create
    returns the given JSON string as the assistant message."""

    class _FakeCompletions:
        async def create(self, **kwargs):
            return _FakeResponse(content)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    return _FakeClient()


async def test_short_response_no_tools_skips_llm():
    """Heuristic shortcut: a short response with no tool calls is
    a happy-path turn — we should NOT pay an LLM call to check it."""

    def fail_if_called():
        raise AssertionError("LLM should not be called for short happy-path turns")

    with patch.object(semantic, "_openai", side_effect=fail_if_called):
        verdict = await semantic.review_response(
            customer_text="hi",
            primary_response_text="Hi there!",
            tool_calls=[],
            history=[],
        )
    assert verdict["intervention_needed"] is False
    assert verdict["pattern_name"] == "semantic_mismatch"


async def test_intervention_flagged_when_llm_says_so():
    fake = _fake_openai_returning(json.dumps({
        "needs_intervention": True,
        "reason": "primary captured pepperoni after customer said 'no pepperoni'",
        "what_customer_wants": "mushroom pizza only",
        "suggested_correction": "Just to confirm — mushroom only, no pepperoni — right?",
    }))
    with patch.object(semantic, "_openai", return_value=fake):
        verdict = await semantic.review_response(
            customer_text="Large pepperoni, actually mushroom only, no pepperoni",
            primary_response_text="Got it — pepperoni and mushroom coming up.",
            tool_calls=[{
                "name": "place_order",
                "args": {"items": ["large pepperoni", "mushroom"]},
                "result": {"order_id": "ORD-x"},
            }],
            history=[],
        )
    assert verdict["intervention_needed"] is True
    assert verdict["pattern_name"] == "semantic_mismatch"
    assert verdict["strategy"] == "self_correct"
    assert "mushroom" in verdict["evidence"]["what_customer_wants"]
    assert verdict["evidence"]["reason"]


async def test_no_intervention_when_llm_approves():
    fake = _fake_openai_returning(json.dumps({
        "needs_intervention": False,
        "reason": "single-item happy path, plan matches intent",
        "what_customer_wants": "",
        "suggested_correction": "",
    }))
    with patch.object(semantic, "_openai", return_value=fake):
        verdict = await semantic.review_response(
            customer_text="I'd like a large cheese pizza please",
            primary_response_text="Got it — large cheese coming right up. Anything else?",
            tool_calls=[{
                "name": "place_order",
                "args": {"items": ["large cheese"]},
                "result": {"order_id": "ORD-y"},
            }],
            history=[],
        )
    assert verdict["intervention_needed"] is False


async def test_llm_failure_defaults_to_no_intervention():
    """A Mirror outage must not silently degrade the agent."""

    def boom():
        raise RuntimeError("LLM down")

    with patch.object(semantic, "_openai", side_effect=boom):
        verdict = await semantic.review_response(
            customer_text="Large pepperoni, actually mushroom only",
            primary_response_text="Got it — pepperoni and mushroom.",
            tool_calls=[{
                "name": "place_order",
                "args": {"items": ["pepperoni", "mushroom"]},
                "result": {"order_id": "ORD-z"},
            }],
            history=[],
        )
    # On LLM error we deliberately don't intervene — better to let
    # the primary's response through than block the entire call.
    assert verdict["intervention_needed"] is False


async def test_non_json_response_defaults_to_no_intervention():
    fake = _fake_openai_returning("this is not json at all")
    with patch.object(semantic, "_openai", return_value=fake):
        verdict = await semantic.review_response(
            customer_text="cheese please",
            primary_response_text="Got it — cheese pizza coming up.",
            tool_calls=[{
                "name": "place_order",
                "args": {"items": ["cheese"]},
                "result": {"order_id": "ORD-q"},
            }],
            history=[],
        )
    assert verdict["intervention_needed"] is False


def test_summarize_history_caps_window():
    history = [{"role": "customer", "text": f"turn {i}"} for i in range(20)]
    summary = semantic._summarize_history(history, max_turns=6)
    # Should only show the last 6 turns
    assert "turn 14" in summary
    assert "turn 19" in summary
    assert "turn 0" not in summary
