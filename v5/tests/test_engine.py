import time

from plivo_mirror_v5.engine import (
    Engine,
    EngineConfig,
    FakeKBRetriever,
    KBChunk,
    SessionState,
)
from plivo_mirror_v5.engine.layers.base import LayerContext

from helpers import REFERENCE, make_turn

KB = FakeKBRetriever([
    KBChunk(chunk_id="chunk_02",
            text="The Aurora home router supports wifi 6 and covers up to 2500 square feet.",
            score=0.9),
])


def make_engine(config=None, kb=KB):
    return Engine(config or EngineConfig(), reference=REFERENCE, kb=kb)


def test_mixed_turn_end_to_end():
    engine = make_engine()
    state = SessionState("call-t")
    turn = make_turn(
        transcript="The Turbo plan is $59.99 and the router covers 5000 square feet.",
        claims=[
            {"claim_id": "c1", "claim_type": "price", "spoken_value": "$59.99",
             "ref": "reference.plan.turbo.price_per_month"},
            {"claim_id": "c2", "claim_type": "fact", "spoken_value": None, "ref": None,
             "text": "The Aurora router covers up to 5000 square feet"},
        ],
    )
    result = engine.evaluate_turn(turn, state)

    assert result.state_snapshot_id.startswith("snap-call-t-")
    assert result.action is None  # engine never takes actions
    fired = {(v.detector, v.claim_id) for v in result.fired_verdicts}
    assert fired == {("L2", "c1"), ("L3", "c2")}
    assert result.max_severity() == "high"


def test_l3_skips_claim_l2_resolved():
    engine = make_engine()
    state = SessionState("call-t")
    turn = make_turn(claims=[
        {"claim_id": "c1", "claim_type": "price", "spoken_value": "$59.99",
         "ref": "reference.plan.turbo.price_per_month",
         "text": "The Turbo plan is $59.99"},
    ])
    result = engine.evaluate_turn(turn, state)
    assert [v.detector for v in result.verdicts] == ["L2"]


def test_tool_calls_committed_to_state_after_turn():
    engine = make_engine()
    state = SessionState("call-t")
    turn = make_turn(tool_calls=[{"name": "cancel_service", "result": {"ok": True}}],
                     turn_index=3)
    engine.evaluate_turn(turn, state)
    assert state.tool_log[0]["name"] == "cancel_service"

    # ... so a later "I cancelled it" claim diffs clean against the log.
    later = make_turn(turn_index=5, claims=[
        {"claim_id": "c2", "claim_type": "action", "spoken_value": "cancelled",
         "ref": "tool.cancel_service"},
    ])
    result = engine.evaluate_turn(later, state)
    assert result.fired_verdicts == []


def test_l1_gate_flows_into_same_call_later_turns():
    engine = make_engine()
    state = SessionState("call-t")
    engine.evaluate_turn(
        make_turn(role="user", transcript="garbled", asr_confidence=0.2, turn_index=0),
        state,
    )
    result = engine.evaluate_turn(
        make_turn(turn_index=1, claims=[
            {"claim_id": "c1", "claim_type": "price", "spoken_value": "$59.99",
             "ref": "reference.plan.turbo.price_per_month"},
        ]),
        state,
    )
    [v] = result.fired_verdicts
    assert v.severity == "info"  # downgraded, not silenced


def test_layer_enable_flags():
    config = EngineConfig(enable_l1=False, enable_l3=False)
    engine = make_engine(config=config)
    state = SessionState("call-t")
    turn = make_turn(role="user", transcript="x", asr_confidence=0.1, claims=[
        {"claim_id": "c1", "claim_type": "fact", "ref": None, "text": "prose claim"},
    ])
    result = engine.evaluate_turn(turn, state)
    assert result.verdicts == []
    assert state.untrusted_input is False  # L1 disabled → gate untouched


def test_verdicts_carry_layer_latency():
    engine = make_engine()
    state = SessionState("call-t")
    turn = make_turn(claims=[
        {"claim_id": "c1", "claim_type": "price", "spoken_value": "$79.99",
         "ref": "reference.plan.turbo.price_per_month"},
    ])
    [v] = engine.evaluate_turn(turn, state).verdicts
    assert v.latency_ms > 0.0


def test_l2_inline_latency_budget():
    """Direct L2 layer timing must stay well under the inline budget."""
    config = EngineConfig()
    engine = make_engine(config=config)
    state = SessionState("call-t")
    state.set_fact("order.total", 86.39)
    claims = [
        {"claim_id": f"c{i}", "claim_type": "price", "spoken_value": "$79.99",
         "ref": "reference.plan.turbo.price_per_month"}
        for i in range(10)
    ] + [
        {"claim_id": "cs", "claim_type": "price", "spoken_value": "$86.39",
         "ref": "session.order.total"},
        {"claim_id": "ca", "claim_type": "action", "spoken_value": "cancelled",
         "ref": "tool.cancel_service"},
    ]
    samples = []
    for i in range(50):
        turn = make_turn(turn_index=i, claims=claims)
        ctx = LayerContext(config=config, snapshot=state.snapshot(),
                           reference=REFERENCE, kb=None)
        start = time.perf_counter()
        engine.l2.check(turn, state, ctx)
        samples.append((time.perf_counter() - start) * 1000.0)
    samples.sort()
    p90 = samples[int(0.9 * (len(samples) - 1))]
    assert p90 < config.l2_inline_budget_ms, f"L2 p90 {p90:.2f}ms over budget"
