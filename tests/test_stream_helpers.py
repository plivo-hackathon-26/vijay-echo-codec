"""Tests for stream.py helpers that are pure logic."""

from voice.stream import _is_wrapping_up


def test_wrapup_detected_after_thanks_so_much():
    history = [
        {"role": "customer", "text": "Yes that's right"},
        {"role": "agent", "text": "Got it — your total is $15.00. Thanks so much, and have a great day!"},
    ]
    assert _is_wrapping_up(history) is True


def test_wrapup_detected_after_thanks_for_calling():
    history = [
        {"role": "agent", "text": "Perfect — your total is $22. Thanks for calling Pizza Plivo!"},
    ]
    assert _is_wrapping_up(history) is True


def test_not_wrapup_during_active_turn():
    history = [
        {"role": "agent", "text": "Hi there — what pizza can I get for you?"},
    ]
    assert _is_wrapping_up(history) is False


def test_not_wrapup_during_correction():
    history = [
        {"role": "agent", "text": "Just to confirm — you'd like a mushroom pizza, no pepperoni — is that right?"},
    ]
    assert _is_wrapping_up(history) is False


def test_only_inspects_last_agent_turn():
    # An earlier goodbye doesn't count if the agent has spoken since.
    history = [
        {"role": "agent", "text": "Thanks for calling."},
        {"role": "customer", "text": "Wait, one more thing"},
        {"role": "agent", "text": "Sure, what else?"},
    ]
    assert _is_wrapping_up(history) is False


def test_empty_history_returns_false():
    assert _is_wrapping_up([]) is False


def test_history_with_no_agent_turns_returns_false():
    assert _is_wrapping_up([{"role": "customer", "text": "hello"}]) is False
