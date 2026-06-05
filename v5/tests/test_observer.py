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


def make_observer(mode="shadow", intervention_handler=None, shadow_judge=None,
                  config=None):
    engine = Engine(config or EngineConfig(mode=mode), reference=REFERENCE)
    sink = InMemorySink()
    observer = MirrorObserver(
        engine, TelemetryEmitter(sink),
        agent_id="aurora", agent_version="1.0.0",
        intervention_handler=intervention_handler,
        shadow_judge=shadow_judge,
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
    # Make the engine artificially slow.
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


async def test_overlapping_items_evaluate_in_dispatch_order():
    """Evaluations mutate shared SessionState — they are serialized in
    dispatch order even when an earlier turn is SLOW. Without the eval
    lock a fast turn could overtake a slow one (turn order, tool-log
    order and the L1 gate all depend on ordering)."""
    observer, sink = make_observer()
    real_evaluate = observer.engine.evaluate_turn

    def staggered_evaluate(turn, state):
        if turn.turn_index == 0:
            time.sleep(0.1)  # first turn is the slow one
        return real_evaluate(turn, state)

    observer.engine.evaluate_turn = staggered_evaluate
    session = FakeSession()
    observer.attach(session)
    # Dispatch both back-to-back; no drain in between.
    session.add_item(WRONG_PRICE_ITEM)
    session.add_item(CLEAN_ITEM)
    await observer.drain()

    turns = sink.of_type(S.REC_TURN)
    assert [t[S.ATTR_TURN_INDEX] for t in turns] == [0, 1]
    assert [r.turn_index for r in observer.results] == [0, 1]
    # The slow first item is the wrong-price one — it must still be first.
    assert turns[0][S.ATTR_TRANSCRIPT].startswith("The Turbo plan is $59.99")


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


class FakeShadowJudge:
    """Scriptable TurnJudge: flags when the audited turn contains a trigger."""

    def __init__(self, trigger="$59.99", delay=0.0, error=False):
        self.trigger, self.delay, self.error = trigger, delay, error
        self.calls = 0

    def judge_turn(self, turns, agent_turn_index):
        self.calls += 1
        if self.error:
            raise ConnectionError("judge down")
        if self.delay:
            time.sleep(self.delay)
        text = turns[agent_turn_index]["text"]
        if self.trigger in text:
            return {"violation": True, "category": "fabricated_fact",
                    "reason": f"contains {self.trigger}"}
        return {"violation": False, "category": None, "reason": "clean"}


# An assertive factual turn whose claim has NO structured referent — L2
# can't touch it; only the judge can (the shadow-mode recall seam).
UNGROUNDED_WRONG_ITEM = ConversationItem(
    role="agent",
    text="The Turbo plan is $59.99 a month.",  # numberish → assertive
)
NON_ASSERTIVE_ITEM = ConversationItem(role="agent", text="Sure, happy to help!")


async def test_shadow_judge_flags_what_l2_cannot_see():
    judge = FakeShadowJudge()
    observer, sink = make_observer(shadow_judge=judge)
    session = FakeSession()
    observer.attach(session)
    session.add_item(UNGROUNDED_WRONG_ITEM)
    await observer.drain()

    [verdict] = [v for v in sink.of_type(S.REC_VERDICT)
                 if v[S.ATTR_DETECTOR] == "JUDGE"]
    assert verdict[S.ATTR_FIRED] is True
    assert verdict[S.ATTR_EVIDENCE]["source"] == "shadow_judge"
    [action] = sink.of_type(S.REC_ACTION)
    assert action[S.ATTR_ACTION_TAKEN] == "would_have"  # flag-only in shadow


async def test_shadow_judge_skips_non_assertive_turns():
    judge = FakeShadowJudge()
    observer, sink = make_observer(shadow_judge=judge)
    session = FakeSession()
    observer.attach(session)
    session.add_item(NON_ASSERTIVE_ITEM)
    await observer.drain()
    assert judge.calls == 0
    [action] = sink.of_type(S.REC_ACTION)
    assert action[S.ATTR_ACTION_TAKEN] == "none"


async def test_shadow_judge_skips_turns_l2_already_flagged():
    judge = FakeShadowJudge()
    observer, sink = make_observer(shadow_judge=judge)
    session = FakeSession()
    observer.attach(session)
    session.add_item(WRONG_PRICE_ITEM)  # L2 fires high on the claim diff
    await observer.drain()
    assert judge.calls == 0  # deterministic already flags: don't pay the judge
    [action] = sink.of_type(S.REC_ACTION)
    assert action[S.ATTR_ACTION_TAKEN] == "would_have"


async def test_shadow_judge_fails_open_on_error_and_timeout():
    for judge in (FakeShadowJudge(error=True),
                  FakeShadowJudge(delay=0.2)):
        config = EngineConfig(mode="shadow", inline_judge_timeout_s=0.05)
        observer, sink = make_observer(shadow_judge=judge, config=config)
        session = FakeSession()
        observer.attach(session)
        session.add_item(UNGROUNDED_WRONG_ITEM)
        await observer.drain()
        assert judge.calls == 1
        # fail-open: no JUDGE verdict, turn still traced, no crash
        assert [v for v in sink.of_type(S.REC_VERDICT)
                if v[S.ATTR_DETECTOR] == "JUDGE"] == []
        assert len(sink.of_type(S.REC_TURN)) == 1


async def test_shadow_judge_clean_verdict_adds_nothing():
    judge = FakeShadowJudge(trigger="NEVER-PRESENT")
    observer, sink = make_observer(shadow_judge=judge)
    session = FakeSession()
    observer.attach(session)
    session.add_item(UNGROUNDED_WRONG_ITEM)
    await observer.drain()
    assert judge.calls == 1
    assert sink.of_type(S.REC_VERDICT) == []
    [action] = sink.of_type(S.REC_ACTION)
    assert action[S.ATTR_ACTION_TAKEN] == "none"


def test_unknown_mode_rejected():
    engine = Engine(EngineConfig(), reference=REFERENCE)
    with pytest.raises(ValueError):
        MirrorObserver(engine, TelemetryEmitter(InMemorySink()), mode="both")
