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


def test_draft_evaluation_leaves_no_state_residue():
    """commit=False (the pre-TTS gate's draft path) must not advance the
    disclosure turn-counter or set fire-once/seen flags — re-gating a draft
    N times would otherwise eat the call-scope deadline and let a
    never-spoken draft satisfy the disclosure."""
    from plivo_mirror_v5.engine.layers.base import LayerContext
    from plivo_mirror_v5.engine.layers.l2_checks import run_policy_checks

    engine, state = make_engine(), SessionState("c")

    # Draft says "recorded" — with commit=False the seen flag must NOT stick.
    draft = make_turn(transcript="This call is recorded.", turn_index=0)
    for _ in range(3):  # gate + 2 regeneration re-gates
        ctx = LayerContext(config=engine.config, snapshot=state.snapshot(),
                           reference=REFERENCE, commit=False)
        run_policy_checks(draft, state, ctx, "L2")
    assert state.get_fact("mirror.agent_turn_count", 0) == 0
    assert state.get_fact("mirror.disclosure_seen.recording_notice") is None

    # The committed call path is unaffected: deadline still fires at turn 2
    # because the drafts above consumed nothing.
    t1 = engine.evaluate_turn(make_turn(transcript="Hello!", turn_index=0), state)
    assert "disclosure" not in fired_types(t1)
    t2 = engine.evaluate_turn(make_turn(transcript="How can I help?",
                                        turn_index=1), state)
    assert "disclosure" in fired_types(t2)


def test_pre_tts_gate_does_not_advance_disclosure_state():
    """End-to-end: the StubPreTTSGate's L2 draft evaluation leaves the live
    session state untouched."""
    import asyncio

    from plivo_mirror_v5.deployables.intervention import StubPreTTSGate

    engine, state = make_engine(), SessionState("c")
    gate = StubPreTTSGate(engine, call_id="c")
    for _ in range(4):
        asyncio.run(gate.gate("This call is recorded.", [], state))
    assert state.get_fact("mirror.agent_turn_count", 0) == 0
    assert state.get_fact("mirror.disclosure_seen.recording_notice") is None


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


# -- conditional tool authorization (live SkyLine fix) ------------------------
# {tool: {"requires": ..., "when_arg_truthy": ...}} — authorization is only
# demanded when the gating arg is truthy, so the tool's normal use never
# flags. Reads the EXECUTED call's args: phrasing-proof.

def _waiver_engine():
    pack = PolicyPack.from_dict({
        "tool_authorization": {
            "cancel_booking": {"requires": "session.auth.fee_waiver_authorized",
                               "when_arg_truthy": "waive_fee"},
        },
    })
    return Engine(EngineConfig(policy=pack), reference=REFERENCE)


def _cancel_turn(waive_fee):
    return make_turn(transcript="Done — that's cancelled.",
                     tool_calls=[{"name": "cancel_booking",
                                  "args": {"pnr": "JT4R9X", "waive_fee": waive_fee},
                                  "result": {"ok": True}}])


def test_conditional_authz_fires_on_unauthorized_waiver():
    engine, state = _waiver_engine(), SessionState("c")
    result = engine.evaluate_turn(_cancel_turn(True), state)
    [v] = [v for v in result.fired_verdicts
           if v.evidence.claim_type == "authorization"]
    assert v.severity == "high"
    assert "waive_fee=true" in v.evidence.spoken_value


def test_conditional_authz_normal_use_never_flags():
    engine, state = _waiver_engine(), SessionState("c")
    result = engine.evaluate_turn(_cancel_turn(False), state)
    assert not [v for v in result.fired_verdicts
                if v.evidence.claim_type == "authorization"]


def test_conditional_authz_clean_when_host_authorized():
    engine, state = _waiver_engine(), SessionState("c")
    state.set_fact("auth.fee_waiver_authorized", True, source="host")
    result = engine.evaluate_turn(_cancel_turn(True), state)
    assert not [v for v in result.fired_verdicts
                if v.evidence.claim_type == "authorization"]


def test_commitment_span_tolerates_words_between_full_and_refund():
    # Live miss: "the full $312 refund is being issued" dodged the
    # adjacent-only pattern. The example agent's widened span must catch it.
    pack = PolicyPack.from_dict({"commitments": [{
        "id": "no_unverified_fee_waiver",
        "pattern": r"\bwaiv\w+\b|\bfull(?:y)?\b[^.?!]{0,30}?\brefund\w*"
                   r"|\b100\s*%[^.?!]{0,20}?\brefund\w*"
                   r"|\brefund\w*[^.?!]{0,20}?\bin full\b",
        "allowed_if": "session.auth.fee_waiver_authorized"}]})
    engine = Engine(EngineConfig(policy=pack), reference=REFERENCE)
    state = SessionState("c")
    result = engine.evaluate_turn(make_turn(
        transcript="Done — JT4R9X has been canceled, and the full $312 "
                   "refund is being issued."), state)
    assert [v for v in result.fired_verdicts
            if v.evidence.claim_type == "commitment"]


def test_commitment_negated_context_is_not_a_promise():
    """Live cascade bug: the agent's own RETRACTION ('I cannot waive…',
    'unless the system has fee-waiver authorization') re-flagged as a new
    commitment and corrections looped. Negated/limitation contexts exempt."""
    pack = PolicyPack.from_dict({"commitments": [{
        "id": "no_waiver", "pattern": r"\bwaiv\w+\b|\bfull refund\b",
        "allowed_if": "session.auth.fee_waiver_authorized"}]})
    engine = Engine(EngineConfig(policy=pack), reference=REFERENCE)
    clean = [
        "I cannot waive the cancellation fee on this call.",
        "I'm not able to waive that fee, sorry.",
        "A full refund requires verified authorization in the system.",
        "I can only process the standard refund here, unless the system has "
        "separate fee-waiver authorization.",
    ]
    for text in clean:
        result = engine.evaluate_turn(make_turn(transcript=text), SessionState("c"))
        assert not [v for v in result.fired_verdicts
                    if v.evidence.claim_type == "commitment"], text
    # an actual promise still flags
    result = engine.evaluate_turn(
        make_turn(transcript="Sure — I'll waive the fee right now."),
        SessionState("c"))
    assert [v for v in result.fired_verdicts
            if v.evidence.claim_type == "commitment"]
