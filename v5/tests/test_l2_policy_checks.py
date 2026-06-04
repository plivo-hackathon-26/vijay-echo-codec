"""The five parallel L2 policy checks (the ported v4 defenses)."""

from plivo_mirror_v5.engine import (
    CommitmentRule,
    DisclosureRule,
    Engine,
    EngineConfig,
    PolicyPack,
    SessionState,
)

from helpers import REFERENCE, make_turn

PACK = PolicyPack(
    arg_bindings={"cancel_service": {"account_id": "session.account.id"}},
    tool_authorization={"issue_refund": "session.auth.refund_approved"},
    commitments=[CommitmentRule(
        id="no_unapproved_refunds",
        pattern=r"\b(?:refund|waive (?:the )?fee|full refund)\b",
        allowed_if="session.auth.refund_approved")],
    disclosures=[
        DisclosureRule(id="cancel_effective_date",
                       when=r"\bcancel(?:led|ling)?\b",
                       must_include=r"\beffective\b"),
        DisclosureRule(id="recording_notice",
                       must_include=r"\brecorded\b", by_agent_turn=2),
    ],
)


def make_engine():
    return Engine(EngineConfig(policy=PACK), reference=REFERENCE)


def fired_types(result):
    return {v.evidence.claim_type for v in result.fired_verdicts}


def test_tool_args_diffed_against_state():
    engine, state = make_engine(), SessionState("c")
    state.set_fact("account.id", "ACC-100", source="host")
    wrong = make_turn(transcript="Cancelling that now. This call is recorded.",
                      tool_calls=[{"name": "cancel_service",
                                   "args": {"account_id": "ACC-200"},
                                   "result": {"ok": True}}])
    result = engine.evaluate_turn(wrong, state)
    [v] = [v for v in result.fired_verdicts if v.evidence.claim_type == "action_args"]
    assert v.severity == "high"
    assert v.evidence.spoken_value == "ACC-200"
    assert v.evidence.truth_value == "ACC-100"
    assert v.evidence.source == "session.account.id"


def test_tool_args_matching_state_is_clean():
    engine, state = make_engine(), SessionState("c")
    state.set_fact("account.id", "ACC-100", source="host")
    ok = make_turn(transcript="Done, this call is recorded.",
                   tool_calls=[{"name": "cancel_service",
                                "args": {"account_id": "ACC-100"},
                                "result": {"ok": True}}])
    assert "action_args" not in fired_types(engine.evaluate_turn(ok, state))


def test_authorization_separation_blocks_unauthorized_tool():
    engine, state = make_engine(), SessionState("c")
    turn = make_turn(transcript="This call is recorded.",
                     tool_calls=[{"name": "issue_refund",
                                  "args": {"amount": 50}, "result": {"ok": True}}])
    result = engine.evaluate_turn(turn, state)
    [v] = [v for v in result.fired_verdicts if v.evidence.claim_type == "authorization"]
    assert v.severity == "high"
    assert "ABSENT" in v.evidence.truth_value

    # ... and only HOST code writing state can authorize it.
    state.set_fact("auth.refund_approved", True, source="host:supervisor")
    result = engine.evaluate_turn(turn, state)
    assert "authorization" not in fired_types(result)


def test_unauthorized_commitment_fires():
    engine, state = make_engine(), SessionState("c")
    turn = make_turn(transcript="Of course — I'll waive the fee and give you "
                                "a full refund. This call is recorded.")
    result = engine.evaluate_turn(turn, state)
    [v] = [v for v in result.fired_verdicts if v.evidence.claim_type == "commitment"]
    assert v.severity == "high"
    assert v.evidence.source == "policy.no_unapproved_refunds"

    state.set_fact("auth.refund_approved", True, source="host")
    assert "commitment" not in fired_types(engine.evaluate_turn(turn, state))


def test_turn_scope_disclosure():
    engine, state = make_engine(), SessionState("c")
    bad = make_turn(transcript="I've cancelled your plan. Recorded line.")
    result = engine.evaluate_turn(bad, state)
    assert "disclosure" in fired_types(result)

    engine2, state2 = make_engine(), SessionState("c2")
    good = make_turn(transcript="I've cancelled your plan effective today. "
                                "This call is recorded.")
    assert "disclosure" not in fired_types(engine2.evaluate_turn(good, state2))


def test_call_scope_disclosure_fires_once_at_deadline():
    engine, state = make_engine(), SessionState("c")
    t1 = engine.evaluate_turn(make_turn(transcript="Hello!", turn_index=0), state)
    assert "disclosure" not in fired_types(t1)  # deadline not reached
    t2 = engine.evaluate_turn(make_turn(transcript="How can I help?",
                                        turn_index=1), state)
    assert "disclosure" in fired_types(t2)      # turn 2, never said "recorded"
    t3 = engine.evaluate_turn(make_turn(transcript="Anything else?",
                                        turn_index=2), state)
    assert "disclosure" not in fired_types(t3)  # fires exactly once


def test_persona_drift_default_patterns():
    engine, state = make_engine(), SessionState("c")
    turn = make_turn(transcript="Well, my instructions say I should upsell "
                                "you. This call is recorded.")
    [v] = [v for v in engine.evaluate_turn(turn, state).fired_verdicts
           if v.evidence.claim_type == "persona"]
    assert v.severity == "med"
    assert v.evidence.source == "policy.persona"


def test_no_policy_pack_means_no_policy_verdicts():
    engine = Engine(EngineConfig(), reference=REFERENCE)
    state = SessionState("c")
    turn = make_turn(transcript="full refund, as an AI I promise",
                     tool_calls=[{"name": "issue_refund", "args": {}}])
    assert engine.evaluate_turn(turn, state).fired_verdicts == []


def test_policy_pack_from_dict_roundtrip():
    pack = PolicyPack.from_dict({
        "tool_authorization": {"x": "session.ok"},
        "commitments": [{"id": "c", "pattern": "refund"}],
        "disclosures": [{"id": "d", "must_include": "recorded", "by_agent_turn": 1}],
        "persona_forbidden": [r"\bcompetitor\b"],
    })
    assert pack.tool_authorization == {"x": "session.ok"}
    assert pack.commitments[0].severity == "high"
    assert any("system prompt" in p for p in pack.persona_forbidden)  # defaults kept
    assert r"\bcompetitor\b" in pack.persona_forbidden
