"""Tool-gate tests.

Validates:
  - ``is_gated`` always returns True for irreversible tools, regardless
    of ``tool_gate_enabled``.
  - ``is_gated`` respects the global switch for non-irreversible tools.
  - ``review`` correctly parses the LLM verdict.
  - ``review`` fails open on LLM error.
"""

from __future__ import annotations

from typing import Any

import pytest

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import SupervisorContext, ToolCallIntent
from plivo_mirror.scorer.tool_gate import ToolGate
from tests.unit.conftest import FakeLLM


def _gate(
    responder=None,
    *,
    tool_gate_enabled: bool = True,
    irreversible: list[str] | None = None,
) -> ToolGate:
    llm = FakeLLM(responder=responder)
    cfg = MirrorConfig(
        llm=llm,
        policies=["dummy"],
        tool_gate_enabled=tool_gate_enabled,
        irreversible_tools=irreversible
        if irreversible is not None
        else ["place_order", "charge_card"],
    )
    return ToolGate(cfg)


def test_is_gated_irreversible_always_true() -> None:
    gate = _gate(tool_gate_enabled=False)
    assert gate.is_gated("place_order") is True
    assert gate.is_gated("charge_card") is True


def test_is_gated_respects_global_switch() -> None:
    gate_on = _gate(tool_gate_enabled=True)
    gate_off = _gate(tool_gate_enabled=False)
    assert gate_on.is_gated("lookup_menu") is True
    assert gate_off.is_gated("lookup_menu") is False


@pytest.mark.asyncio
async def test_review_no_gated_tools_short_circuits() -> None:
    llm_called: list[Any] = []

    def responder(s: str, u: str | None) -> dict[str, Any]:
        llm_called.append((s, u))
        return {"score": 1.0, "should_intervene": True}

    gate = _gate(responder=responder, tool_gate_enabled=False)
    ctx = SupervisorContext(call_uuid="t")
    intents = [ToolCallIntent(name="lookup_menu", args={})]
    v = await gate.review(intents, "what's on the menu", [], ctx)

    assert v.should_intervene is False
    assert v.reason == "no_gated_tools"
    assert llm_called == []  # LLM not invoked


@pytest.mark.asyncio
async def test_review_blocks_on_high_score() -> None:
    gate = _gate(
        responder=lambda s, u: {
            "score": 0.92,
            "reason": "wrong items",
            "should_intervene": True,
            "blocked_tool": "place_order",
            "suggested_correction": "confirm the order",
        }
    )
    ctx = SupervisorContext(call_uuid="t")
    intents = [
        ToolCallIntent(name="place_order", args={"items": ["pepperoni", "mushroom"]})
    ]
    v = await gate.review(intents, "mushroom only", [], ctx)
    assert v.should_intervene is True
    assert v.blocked_tool == "place_order"
    assert "confirm" in v.suggested_correction.lower()


@pytest.mark.asyncio
async def test_review_fails_open_on_llm_error() -> None:
    def boom(s: str, u: str | None) -> dict[str, Any]:
        raise RuntimeError("simulated LLM outage")

    gate = _gate(responder=boom)
    ctx = SupervisorContext(call_uuid="t")
    intents = [ToolCallIntent(name="place_order", args={})]
    v = await gate.review(intents, "", [], ctx)
    assert v.should_intervene is False
    assert v.reason == "tool_gate_error"
