from plivo_mirror_v5.engine import SessionState
from plivo_mirror_v5.engine.layers import DeterministicDiffLayer, values_match

from helpers import make_ctx, make_turn

L2 = DeterministicDiffLayer()


def _claim(claim_id="c1", claim_type="price", spoken="$59.99",
           ref="reference.plan.turbo.price_per_month"):
    return {"claim_id": claim_id, "claim_type": claim_type,
            "spoken_value": spoken, "ref": ref}


def test_values_match_semantics():
    assert values_match("$79.99", 79.99)
    assert values_match("60", 60)
    assert values_match(" 9am-5pm ", "9AM-5PM")
    assert not values_match("$59.99", 79.99)
    assert not values_match("9am-6pm", "9am-5pm")  # no first-number shortcut
    assert not values_match("60 days", 30)


def test_wrong_reference_price_fires_high_with_evidence():
    state, ctx = make_ctx()
    [v] = L2.check(make_turn(claims=[_claim()]), state, ctx)
    assert (v.detector, v.fired, v.severity) == ("L2", True, "high")
    ev = v.evidence
    assert ev.claim_type == "price"
    assert ev.spoken_value == "$59.99"
    assert ev.truth_value == "79.99"
    assert ev.source == "reference.plan.turbo.price_per_month"
    assert "c1" in ctx.l2_claim_ids


def test_correct_claim_emits_non_firing_verdict():
    state, ctx = make_ctx()
    [v] = L2.check(make_turn(claims=[_claim(spoken="$79.99")]), state, ctx)
    assert (v.fired, v.severity) == (False, "info")


def test_session_state_claim_diffs_against_snapshot():
    state = SessionState("call-t")
    state.set_fact("order.total", 86.39)
    state, ctx = make_ctx(state=state)
    claim = _claim(ref="session.order.total", spoken="$96.39")
    [v] = L2.check(make_turn(claims=[claim]), state, ctx)
    assert v.fired and v.severity == "high"
    assert v.evidence.truth_value == "86.39"
    assert v.evidence.source == "session.order.total"


def test_claimed_but_unfired_action_fires():
    state, ctx = make_ctx()
    claim = {"claim_id": "c2", "claim_type": "action",
             "spoken_value": "cancelled", "ref": "tool.cancel_service"}
    [v] = L2.check(make_turn(claims=[claim]), state, ctx)
    assert v.fired and v.severity == "high"
    assert v.evidence.truth_value == "not_fired"


def test_action_fired_same_turn_is_clean():
    state, ctx = make_ctx()
    claim = {"claim_id": "c2", "claim_type": "action",
             "spoken_value": "cancelled", "ref": "tool.cancel_service"}
    turn = make_turn(claims=[claim],
                     tool_calls=[{"name": "cancel_service", "result": {"ok": True}}])
    [v] = L2.check(turn, state, ctx)
    assert not v.fired
    assert v.evidence.truth_value == "fired"


def test_action_fired_earlier_in_call_is_clean():
    state = SessionState("call-t")
    state.record_tool_call({"name": "cancel_service", "result": {"ok": True}}, turn_index=3)
    state, ctx = make_ctx(state=state)
    claim = {"claim_id": "c2", "claim_type": "action",
             "spoken_value": "cancelled", "ref": "tool.cancel_service"}
    [v] = L2.check(make_turn(claims=[claim]), state, ctx)
    assert not v.fired


def test_errored_tool_counts_as_failed():
    state, ctx = make_ctx()
    claim = {"claim_id": "c2", "claim_type": "action",
             "spoken_value": "cancelled", "ref": "tool.cancel_service"}
    turn = make_turn(claims=[claim],
                     tool_calls=[{"name": "cancel_service",
                                  "result": {"error": "timeout"}}])
    [v] = L2.check(turn, state, ctx)
    assert v.fired
    assert v.evidence.truth_value == "failed"


def test_unresolvable_referent_is_outside_jurisdiction():
    state, ctx = make_ctx()
    claim = _claim(ref="reference.plan.mega.price_per_month")
    assert L2.check(make_turn(claims=[claim]), state, ctx) == []
    assert ctx.l2_claim_ids == set()  # claim falls through to L3


def test_untrusted_input_downgrades_to_info():
    state = SessionState("call-t")
    state.mark_input_trust(False)
    state, ctx = make_ctx(state=state)
    [v] = L2.check(make_turn(claims=[_claim()]), state, ctx)
    assert v.fired
    assert v.severity == "info"
    assert v.evidence.extra["untrusted_input"] is True


def test_ignores_user_turns_and_proseclaims():
    state, ctx = make_ctx()
    assert L2.check(make_turn(role="user", claims=[_claim()]), state, ctx) == []
    prose = {"claim_id": "c9", "claim_type": "fact", "spoken_value": None, "ref": None}
    assert L2.check(make_turn(claims=[prose]), state, ctx) == []
