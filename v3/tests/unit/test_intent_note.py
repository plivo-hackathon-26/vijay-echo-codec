"""Unit tests for v0.3.0 CallSupervisor sticky intent-note API.

The intent note exists to remind the primary LLM what the customer
actually wanted, on the turn immediately after Mirror intervened. It
must:

  • Survive ``turns`` consecutive ``consume_intent_note()`` calls, then
    auto-clear.
  • Auto-clear when a tool fires (``note_committed``) — the order has
    been placed, no further reminder needed.
  • Be force-clearable via ``clear_intent_note()``.
  • Expose a non-consuming peek via ``pending_intent_note``.
"""

from __future__ import annotations

import pytest

from plivo_mirror import MirrorConfig
from plivo_mirror.supervisor import CallSupervisor


class _FakeTTSSink:
    async def speak(self, text: str) -> None:
        pass

    async def aclose(self) -> None:
        pass


class _NoOpLLM:
    """Inert LLM stub — MirrorConfig requires `llm=` but none of these
    bookkeeping tests trigger an LLM call."""

    async def chat(self, *args, **kwargs):
        return ""

    async def structured_output(self, *args, **kwargs):
        return {}


@pytest.fixture
def call_sup() -> CallSupervisor:
    """Minimal CallSupervisor with no real scorer / gate / orchestrator —
    we only exercise the intent-note bookkeeping, none of the rest."""
    config = MirrorConfig(llm=_NoOpLLM(), policies=["dummy"])
    cs = CallSupervisor(
        config=config,
        scorer=None,
        tool_gate=None,
        orchestrator=None,
        state=None,
        tts=_FakeTTSSink(),
    )
    cs.bind_call("call-test-1234")
    return cs


def test_intent_note_initially_none(call_sup):
    assert call_sup.pending_intent_note is None
    assert call_sup.consume_intent_note() is None


def test_intent_note_persists_for_default_three_turns(call_sup):
    call_sup.set_intent_note("[mirror context] customer wants cheese only")
    # turn 1
    assert call_sup.consume_intent_note() == "[mirror context] customer wants cheese only"
    # turn 2
    assert call_sup.consume_intent_note() == "[mirror context] customer wants cheese only"
    # turn 3
    assert call_sup.consume_intent_note() == "[mirror context] customer wants cheese only"
    # turn 4 — note should have decayed
    assert call_sup.consume_intent_note() is None
    assert call_sup.pending_intent_note is None


def test_intent_note_custom_turn_count(call_sup):
    call_sup.set_intent_note("note", turns=1)
    assert call_sup.consume_intent_note() == "note"
    assert call_sup.consume_intent_note() is None


def test_intent_note_peek_does_not_consume(call_sup):
    call_sup.set_intent_note("peek-me", turns=2)
    # Peek 100 times — turn counter should not move.
    for _ in range(100):
        assert call_sup.pending_intent_note == "peek-me"
    # Real consume cycle still gets exactly two pulls.
    assert call_sup.consume_intent_note() == "peek-me"
    assert call_sup.consume_intent_note() == "peek-me"
    assert call_sup.consume_intent_note() is None


def test_intent_note_clear_force_zeroes(call_sup):
    call_sup.set_intent_note("active", turns=5)
    call_sup.clear_intent_note()
    assert call_sup.pending_intent_note is None
    assert call_sup.consume_intent_note() is None


def test_intent_note_auto_clears_on_tool_commit(call_sup):
    call_sup.set_intent_note("about-to-be-superseded")
    assert call_sup.pending_intent_note == "about-to-be-superseded"

    call_sup.note_committed("place_order", '{"items":["cheese"]}', {"ok": True})

    # tool just fired, intent note is no longer relevant
    assert call_sup.pending_intent_note is None
    assert call_sup.consume_intent_note() is None


def test_intent_note_set_replaces_previous(call_sup):
    call_sup.set_intent_note("first", turns=3)
    call_sup.set_intent_note("second", turns=1)
    assert call_sup.pending_intent_note == "second"
    assert call_sup.consume_intent_note() == "second"
    assert call_sup.consume_intent_note() is None


def test_note_committed_without_intent_note_is_noop(call_sup):
    # Should not raise even if no intent note was set.
    call_sup.note_committed("place_order", '{}', {"ok": True})
    assert call_sup.pending_intent_note is None
