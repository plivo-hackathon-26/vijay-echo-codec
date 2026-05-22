"""mirror.state round-trip tests."""

import time

from mirror import state


def test_intervention_pending_round_trip():
    call = "test-state-pending"
    state.cleanup_call(call)
    assert state.get_intervention_pending(call) is None
    state.set_intervention_pending(call, {"pattern_name": "contradiction"})
    assert state.get_intervention_pending(call) == {"pattern_name": "contradiction"}
    state.clear_intervention_pending(call)
    assert state.get_intervention_pending(call) is None
    state.cleanup_call(call)


def test_cooldown_round_trip():
    call = "test-state-cooldown"
    state.cleanup_call(call)
    assert state.is_in_cooldown(call) is False
    state.set_cooldown(call, 0.3)
    assert state.is_in_cooldown(call) is True
    time.sleep(0.4)
    assert state.is_in_cooldown(call) is False
    state.cleanup_call(call)


def test_post_correction_override_round_trip():
    call = "test-state-override"
    state.cleanup_call(call)
    assert state.get_post_correction_override(call) is None
    state.set_post_correction_override(call, "DO THIS")
    assert state.get_post_correction_override(call) == "DO THIS"
    state.clear_post_correction_override(call)
    assert state.get_post_correction_override(call) is None
    state.cleanup_call(call)


def test_cleanup_clears_everything():
    call = "test-state-cleanup"
    state.set_intervention_pending(call, {"x": 1})
    state.set_cooldown(call, 30.0)
    state.set_post_correction_override(call, "note")
    state.cleanup_call(call)
    assert state.get_intervention_pending(call) is None
    assert state.is_in_cooldown(call) is False
    assert state.get_post_correction_override(call) is None
