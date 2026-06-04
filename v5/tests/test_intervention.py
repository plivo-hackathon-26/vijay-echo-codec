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
    assert msg["content"].startswith("[CORRECTION:")
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
