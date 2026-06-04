"""Shared test fixtures/builders."""

from plivo_mirror_v5.engine import (
    EngineConfig,
    ReferenceStore,
    SessionState,
    TurnInput,
)
from plivo_mirror_v5.engine.layers.base import LayerContext

REFERENCE = ReferenceStore(
    {
        "plan": {
            "basic": {"price_per_month": 49.99},
            "turbo": {"price_per_month": 79.99},
        },
        "policy": {"refund_window_days": 30},
        "hours": {"weekend": "9am-5pm"},
    }
)


def make_turn(role="agent", transcript="", claims=None, tool_calls=None,
              asr_confidence=None, turn_index=0, call_id="call-t"):
    return TurnInput(
        turn_id=f"{call_id}-t{turn_index}",
        call_id=call_id,
        turn_index=turn_index,
        role=role,
        transcript=transcript,
        asr_confidence=asr_confidence,
        claims=claims or [],
        tool_calls=tool_calls or [],
    )


def make_ctx(state=None, config=None, reference=REFERENCE):
    state = state or SessionState("call-t")
    return state, LayerContext(
        config=config or EngineConfig(),
        snapshot=state.snapshot(),
        reference=reference,
    )
