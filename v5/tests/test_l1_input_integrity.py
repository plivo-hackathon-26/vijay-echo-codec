from plivo_mirror_v5.engine import SessionState
from plivo_mirror_v5.engine.layers import InputIntegrityLayer

from helpers import make_ctx, make_turn

L1 = InputIntegrityLayer()


def test_low_asr_confidence_marks_untrusted():
    state, ctx = make_ctx()
    turn = make_turn(role="user", transcript="garbled", asr_confidence=0.3)
    verdicts = L1.check(turn, state, ctx)

    assert state.untrusted_input is True
    [v] = verdicts
    assert (v.detector, v.fired, v.severity) == ("L1", True, "info")
    assert v.evidence.claim_type == "untrusted_input"
    assert v.evidence.source == "asr_confidence"


def test_confident_turn_clears_gate():
    state, ctx = make_ctx()
    state.mark_input_trust(False)
    turn = make_turn(role="user", transcript="the basic plan please", asr_confidence=0.95)
    assert L1.check(turn, state, ctx) == []
    assert state.untrusted_input is False


def test_missing_confidence_is_trusted():
    state, ctx = make_ctx()
    turn = make_turn(role="user", transcript="hello")
    assert L1.check(turn, state, ctx) == []
    assert state.untrusted_input is False


def test_readback_correction_written_to_state():
    state = SessionState("call-t")
    state.set_fact("caller.address", "42 Helm Street")
    state, ctx = make_ctx(state=state)
    turn = make_turn(
        role="user",
        transcript="No, I said 42 Elm Street.",
        asr_confidence=0.94,
        claims=[{
            "claim_id": "u1",
            "claim_type": "correction",
            "ref": "session.caller.address",
            "spoken_value": "42 Elm Street",
        }],
    )
    [v] = L1.check(turn, state, ctx)
    assert state.get_fact("caller.address") == "42 Elm Street"
    assert v.evidence.claim_type == "correction"
    assert v.evidence.spoken_value == "42 Elm Street"
    assert v.evidence.truth_value == "42 Helm Street"  # the value it replaced
    assert v.severity == "info"


def test_correction_phrase_without_claim_is_flagged():
    state, ctx = make_ctx()
    turn = make_turn(role="user", transcript="No, I said the turbo plan",
                     asr_confidence=0.9)
    [v] = L1.check(turn, state, ctx)
    assert v.evidence.claim_type == "correction_phrase"
    assert v.severity == "info"


def test_ignores_agent_turns():
    state, ctx = make_ctx()
    turn = make_turn(role="agent", transcript="anything", asr_confidence=0.1)
    assert L1.check(turn, state, ctx) == []
