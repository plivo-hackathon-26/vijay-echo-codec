"""mirror.evaluator wiring tests — cooldown skip + event id propagation."""

from mirror import evaluator, state


def test_cooldown_skips_all_rules():
    call = "test-evaluator-cooldown"
    state.cleanup_call(call)
    state.set_cooldown(call, 30.0)
    results = evaluator.evaluate(
        call_uuid=call,
        recent_turns=[],
        current_user_turn="Large pepperoni, actually mushroom only, no pepperoni",
        current_turn_id=1,
    )
    assert results == []
    state.cleanup_call(call)


def test_evaluator_sets_pending_and_event_id():
    call = "test-evaluator-event-id"
    state.cleanup_call(call)
    results = evaluator.evaluate(
        call_uuid=call,
        recent_turns=[],
        current_user_turn="Large pepperoni, actually mushroom only, no pepperoni",
        current_turn_id=42,
    )
    assert len(results) == 1
    fired = results[0]
    assert fired["pattern_name"] == "contradiction"
    assert fired["mirror_event_id"] is not None
    pending = state.get_intervention_pending(call)
    assert pending is not None
    assert pending["strategy"] == "self_correct"
    assert pending["evidence"]["likely_kept_items"] == ["mushroom"]
    state.cleanup_call(call)


def test_evaluator_does_not_fire_on_happy_path():
    call = "test-evaluator-happy"
    state.cleanup_call(call)
    results = evaluator.evaluate(
        call_uuid=call,
        recent_turns=[],
        current_user_turn="I'd like a large cheese pizza",
        current_turn_id=1,
    )
    assert results == []
    assert state.get_intervention_pending(call) is None
    state.cleanup_call(call)


def test_evaluator_missing_tool_path():
    call = "test-evaluator-handoff"
    state.cleanup_call(call)
    results = evaluator.evaluate(
        call_uuid=call,
        recent_turns=[],
        current_user_turn="Can you check my last order?",
        current_turn_id=7,
    )
    assert len(results) == 1
    pending = state.get_intervention_pending(call)
    assert pending["strategy"] == "handoff"
    state.cleanup_call(call)
