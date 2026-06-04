import asyncio
import time

import pytest

from plivo_mirror_v5.engine import Engine, EngineConfig
from plivo_mirror_v5.integrations import ConversationItem, FakeSession, MirrorObserver
from plivo_mirror_v5.telemetry import InMemorySink, TelemetryEmitter
from plivo_mirror_v5.telemetry import schema as S

from helpers import REFERENCE

WRONG_PRICE_ITEM = ConversationItem(
    role="agent",
    text="The Turbo plan is $59.99 a month.",
    claims=[{"claim_id": "c1", "claim_type": "price", "spoken_value": "$59.99",
             "ref": "reference.plan.turbo.price_per_month"}],
    audio_offset_ms=4000,
)
CLEAN_ITEM = ConversationItem(
    role="agent",
    text="The Turbo plan is $79.99 a month.",
    claims=[{"claim_id": "c1", "claim_type": "price", "spoken_value": "$79.99",
             "ref": "reference.plan.turbo.price_per_month"}],
)


def make_observer(mode="shadow", intervention_handler=None):
    engine = Engine(EngineConfig(mode=mode), reference=REFERENCE)
    sink = InMemorySink()
    observer = MirrorObserver(
        engine, TelemetryEmitter(sink),
        agent_id="aurora", agent_version="1.0.0",
        intervention_handler=intervention_handler,
    )
    return observer, sink


async def test_shadow_mode_emits_would_have():
    observer, sink = make_observer()
    session = FakeSession(room_id="room-77")
    observer.attach(session)
    assert observer.call_id == "room-77"  # call_id == LiveKit room id

    session.add_item(ConversationItem(role="user", text="how much is turbo?",
                                      asr_confidence=0.95))
    session.add_item(WRONG_PRICE_ITEM)
    await observer.drain()
    observer.close()

    [start] = sink.of_type(S.REC_CALL_START)
    assert start[S.ATTR_CALL_ID] == "room-77"
    turns = sink.of_type(S.REC_TURN)
    assert len(turns) == 2  # user turn also traced (L1 reads it)
    assert turns[1][S.ATTR_AUDIO_OFFSET_MS] == 4000
    actions = sink.of_type(S.REC_ACTION)
    assert [a[S.ATTR_ACTION_TAKEN] for a in actions] == ["none", "would_have"]
    assert len(sink.of_type(S.REC_CALL_END)) == 1


async def test_clean_turn_takes_no_action():
    observer, sink = make_observer()
    session = FakeSession()
    observer.attach(session)
    session.add_item(CLEAN_ITEM)
    await observer.drain()
    [action] = sink.of_type(S.REC_ACTION)
    assert action[S.ATTR_ACTION_TAKEN] == "none"


async def test_l1_gate_carries_across_items():
    observer, sink = make_observer()
    session = FakeSession()
    observer.attach(session)
    session.add_item(ConversationItem(role="user", text="garbled",
                                      asr_confidence=0.2))
    await observer.drain()  # order matters for the gate
    session.add_item(WRONG_PRICE_ITEM)
    await observer.drain()
    # downgraded to info by the gate -> below intervene threshold
    [_, action] = sink.of_type(S.REC_ACTION)
    assert action[S.ATTR_ACTION_TAKEN] == "none"
    [_, agent_turn_verdict] = sink.of_type(S.REC_VERDICT)
    assert agent_turn_verdict[S.ATTR_SEVERITY] == "info"


async def test_evaluation_never_blocks_the_call_loop():
    observer, sink = make_observer()
    # Make the engine artificially slow (a slow L3 model, say).
    real_evaluate = observer.engine.evaluate_turn

    def slow_evaluate(turn, state):
        time.sleep(0.15)
        return real_evaluate(turn, state)

    observer.engine.evaluate_turn = slow_evaluate
    session = FakeSession()
    observer.attach(session)

    start = time.perf_counter()
    session.add_item(WRONG_PRICE_ITEM)
    dispatch_ms = (time.perf_counter() - start) * 1000
    assert dispatch_ms < 20, f"add_item blocked the loop for {dispatch_ms:.1f}ms"
    assert sink.of_type(S.REC_TURN) == []  # not evaluated yet

    await observer.drain()
    assert len(sink.of_type(S.REC_TURN)) == 1  # ... but it does arrive


async def test_intervene_mode_routes_to_handler():
    handled = []

    async def handler(result):
        handled.append(result)
        from plivo_mirror_v5.engine.verdict import Action
        return Action(taken="correct", hook="A", correction_text="[CORRECTION: ...]")

    observer, sink = make_observer(mode="intervene", intervention_handler=handler)
    session = FakeSession()
    observer.attach(session)
    session.add_item(WRONG_PRICE_ITEM)
    await observer.drain()

    assert len(handled) == 1
    [action] = sink.of_type(S.REC_ACTION)
    assert action[S.ATTR_ACTION_TAKEN] == "correct"
    assert action[S.ATTR_ACTION_HOOK] == "A"


async def test_intervene_mode_without_handler_degrades_to_alert():
    observer, sink = make_observer(mode="intervene")
    session = FakeSession()
    observer.attach(session)
    session.add_item(WRONG_PRICE_ITEM)
    await observer.drain()
    [action] = sink.of_type(S.REC_ACTION)
    assert action[S.ATTR_ACTION_TAKEN] == "alert"


def test_unknown_mode_rejected():
    engine = Engine(EngineConfig(), reference=REFERENCE)
    with pytest.raises(ValueError):
        MirrorObserver(engine, TelemetryEmitter(InMemorySink()), mode="both")
