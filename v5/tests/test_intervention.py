from plivo_mirror_v5.deployables.intervention import (
    HELD_FALLBACK,
    FakeAgent,
    HookANextTurn,
    StubPreTTSGate,
)
from plivo_mirror_v5.engine import Engine, EngineConfig, SessionState
from plivo_mirror_v5.integrations import ConversationItem, FakeSession, MirrorObserver
from plivo_mirror_v5.telemetry import InMemorySink, TelemetryEmitter
from plivo_mirror_v5.telemetry import schema as S

from helpers import REFERENCE

WRONG_PRICE_ITEM = ConversationItem(
    role="agent",
    text="The Turbo plan is $59.99 a month.",
    claims=[{"claim_id": "c1", "claim_type": "price", "spoken_value": "$59.99",
             "ref": "reference.plan.turbo.price_per_month"}],
)
UNFIRED_ACTION_ITEM = ConversationItem(
    role="agent",
    text="Done — I've cancelled your service.",
    claims=[{"claim_id": "c2", "claim_type": "action", "spoken_value": "cancelled",
             "ref": "tool.cancel_service"}],
)
CLEAN_ITEM = ConversationItem(
    role="agent",
    text="The Turbo plan is $79.99 a month.",
    claims=[{"claim_id": "c1", "claim_type": "price", "spoken_value": "$79.99",
             "ref": "reference.plan.turbo.price_per_month"}],
)


def wire(mode, agent=None, handoff_after=3):
    engine = Engine(EngineConfig(mode=mode), reference=REFERENCE)
    sink = InMemorySink()
    handler = None
    if mode == "intervene":
        handler = HookANextTurn(agent or FakeAgent(), engine.config,
                                handoff_after=handoff_after)
    observer = MirrorObserver(engine, TelemetryEmitter(sink),
                              intervention_handler=handler)
    session = FakeSession(room_id="room-i")
    observer.attach(session)
    return observer, session, sink


# -- Phase-3 DoD: high-severity L2 verdict -> Hook-A injection + telemetry ----

async def test_hook_a_injects_correction_into_agent_context():
    agent = FakeAgent()
    observer, session, sink = wire("intervene", agent=agent)
    session.add_item(WRONG_PRICE_ITEM)
    await observer.drain()

    assert agent.update_calls == 1
    [msg] = agent.chat_ctx.messages
    assert msg["role"] == "system"
    assert msg["content"].startswith("[CORRECTION")
    assert "$59.99" in msg["content"]       # what was said
    assert "79.99" in msg["content"]        # the verified truth
    assert "reference.plan.turbo.price_per_month" in msg["content"]

    [action] = sink.of_type(S.REC_ACTION)   # emitted to telemetry
    assert action[S.ATTR_ACTION_TAKEN] == "correct"
    assert action[S.ATTR_ACTION_HOOK] == "A"
    assert action[S.ATTR_ACTION_CORRECTION] == msg["content"]


async def test_hook_a_speech_vs_action_wording():
    agent = FakeAgent()
    observer, session, sink = wire("intervene", agent=agent)
    session.add_item(UNFIRED_ACTION_ITEM)
    await observer.drain()
    [msg] = agent.chat_ctx.messages
    assert "NOT completed" in msg["content"]
    assert "tool.cancel_service" in msg["content"]


async def test_hook_a_escalates_to_handoff_past_threshold():
    agent = FakeAgent()
    observer, session, sink = wire("intervene", agent=agent, handoff_after=2)
    for _ in range(3):
        session.add_item(WRONG_PRICE_ITEM)
        await observer.drain()  # sequential so the count is deterministic
    actions = [a[S.ATTR_ACTION_TAKEN] for a in sink.of_type(S.REC_ACTION)]
    assert actions == ["correct", "correct", "handoff"]
    assert agent.update_calls == 2  # no injection on the handoff


async def test_clean_turn_no_injection():
    agent = FakeAgent()
    observer, session, sink = wire("intervene", agent=agent)
    session.add_item(CLEAN_ITEM)
    await observer.drain()
    assert agent.update_calls == 0
    [action] = sink.of_type(S.REC_ACTION)
    assert action[S.ATTR_ACTION_TAKEN] == "none"


# -- Phase-3 DoD: mode flips ROUTING ONLY -------------------------------------

async def test_mode_flag_changes_routing_only():
    results = {}
    for mode in ("shadow", "intervene"):
        observer, session, sink = wire(mode)
        session.add_item(WRONG_PRICE_ITEM)
        await observer.drain()
        [verdict] = [v for v in sink.of_type(S.REC_VERDICT) if v[S.ATTR_FIRED]]
        [action] = sink.of_type(S.REC_ACTION)
        results[mode] = (verdict, action[S.ATTR_ACTION_TAKEN])

    shadow_verdict, shadow_action = results["shadow"]
    interv_verdict, interv_action = results["intervene"]
    # identical detection (same evidence, detector, severity) ...
    for key in (S.ATTR_DETECTOR, S.ATTR_SEVERITY, S.ATTR_EVIDENCE):
        assert shadow_verdict[key] == interv_verdict[key]
    # ... different routing
    assert shadow_action == "would_have"
    assert interv_action == "correct"


# -- Hook B (experimental stub) --------------------------------------------------

async def test_hook_b_holds_bad_utterance():
    engine = Engine(EngineConfig(mode="intervene"), reference=REFERENCE)
    gate = StubPreTTSGate(engine)
    state = SessionState("room-i")
    decision = await gate.gate(
        "The Turbo plan is $59.99 a month.",
        claims=WRONG_PRICE_ITEM.claims,
        state=state,
    )
    assert decision.release is False
    assert decision.replacement_text == HELD_FALLBACK
    assert any(v.fired for v in decision.verdicts)


async def test_hook_b_releases_clean_utterance():
    engine = Engine(EngineConfig(mode="intervene"), reference=REFERENCE)
    gate = StubPreTTSGate(engine)
    decision = await gate.gate(
        "The Turbo plan is $79.99 a month.",
        claims=CLEAN_ITEM.claims,
        state=SessionState("room-i"),
    )
    assert decision.release is True
    assert decision.replacement_text is None


# -- correction messages per verdict type (live finding: a generic
# "state the correct value" loses against an adversarial role prompt) -------

def _ev_verdict(claim_type, spoken, truth, source, **extra):
    from plivo_mirror_v5.engine.verdict import Evidence, Verdict, new_verdict_id
    return Verdict(verdict_id=new_verdict_id(), detector="L2", fired=True,
                   severity="high", latency_ms=0.0,
                   evidence=Evidence(claim_type=claim_type, spoken_value=spoken,
                                     truth_value=truth, source=source,
                                     extra=extra))


def test_authorization_correction_orders_a_retraction():
    from plivo_mirror_v5.deployables.intervention import build_correction_message
    msg = build_correction_message([_ev_verdict(
        "authorization", "cancel_booking fired with waive_fee=true",
        "requires session.auth.fee_waiver_authorized (ABSENT)",
        "session.auth.fee_waiver_authorized", tool="cancel_booking")])
    assert "OVERRIDES" in msg                  # out-ranks the rigged prompt
    assert "cancel_booking" in msg
    assert "STANDARD process" in msg
    assert "state the correct value" not in msg.lower()  # no generic mush


def test_commitment_correction_voids_the_promise():
    from plivo_mirror_v5.deployables.intervention import build_correction_message
    msg = build_correction_message([_ev_verdict(
        "commitment", "full refund",
        "no authorization in state (session.auth.fee_waiver_authorized)",
        "policy.no_unverified_fee_waiver")])
    assert "RETRACTED" in msg and "full refund" in msg


# -- proactive delivery: filler + immediate corrected reply -------------------

class FakeSpeechSession:
    """Records say()/generate_reply() so order and content are assertable."""

    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    async def say(self, text):
        if self.fail:
            raise RuntimeError("tts down")
        self.events.append(("say", text))

    async def generate_reply(self, instructions=""):
        self.events.append(("generate", instructions))


def _high_result():
    from plivo_mirror_v5.engine.verdict import Evidence, TurnResult, Verdict, new_verdict_id
    return TurnResult(
        turn_id="t1", call_id="c1", turn_index=1, role="agent", transcript="x",
        asr_confidence=None, state_snapshot_id="snap",
        verdicts=[Verdict(
            verdict_id=new_verdict_id(), detector="L2", fired=True,
            severity="high", latency_ms=0.0,
            evidence=Evidence(claim_type="commitment", spoken_value="full refund",
                              truth_value="no authorization", source="policy.x"))])


async def test_proactive_delivery_speaks_filler_then_corrects():
    from plivo_mirror_v5.deployables.intervention import FakeAgent, HookANextTurn
    from plivo_mirror_v5.deployables.intervention.hook_a_next_turn import PROACTIVE_FILLER
    speech = FakeSpeechSession()
    hook = HookANextTurn(FakeAgent(), session=speech)
    action = await hook(_high_result())
    assert action.taken == "correct"
    kinds = [e[0] for e in speech.events]
    assert kinds == ["say", "generate"]            # filler FIRST, then reply
    assert speech.events[0][1] == PROACTIVE_FILLER
    assert "[CORRECTION]" in speech.events[1][1] or "correction" in speech.events[1][1].lower()


async def test_proactive_delivery_failure_degrades_to_injection():
    from plivo_mirror_v5.deployables.intervention import FakeAgent, HookANextTurn
    agent = FakeAgent()
    hook = HookANextTurn(agent, session=FakeSpeechSession(fail=True))
    action = await hook(_high_result())
    assert action.taken == "correct"               # never raises
    assert agent.update_calls == 1                 # injection still happened


async def test_no_session_keeps_passive_behavior():
    from plivo_mirror_v5.deployables.intervention import FakeAgent, HookANextTurn
    agent = FakeAgent()
    action = await HookANextTurn(agent)(_high_result())
    assert action.taken == "correct" and agent.update_calls == 1
