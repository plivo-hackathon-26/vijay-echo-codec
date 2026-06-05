"""Intervene-mode auto-wiring: ToolGate wraps policy-named tools BEFORE
execution; the pre-TTS gate auto-routes through the default llm_node.

These tests exercise the REAL livekit-agents surface (the wrap is
SDK-coupled by nature) — the module skips cleanly where the ``livekit``
extra is not installed. The degrade-gracefully tests at the bottom need
no livekit at all.
"""

import asyncio

import pytest

from plivo_mirror_v5.engine import PolicyPack, SessionState, ToolGate
from plivo_mirror_v5.integrations.livekit_adapter import (
    _autowrap_llm_node,
    _autowrap_tool_gate,
)

lk_agents = pytest.importorskip("livekit.agents")
Agent = lk_agents.Agent
function_tool = lk_agents.function_tool

POLICY = PolicyPack.from_dict({
    "tool_authorization": {
        "cancel_booking": {"requires": "session.auth.fee_waiver_authorized",
                           "when_arg_truthy": "waive_fee"},
    },
})


class BookingAgent(Agent):
    def __init__(self):
        super().__init__(instructions="x")
        self.executed = []

    @function_tool
    async def cancel_booking(self, pnr: str, waive_fee: bool = False) -> dict:
        """Cancel a booking.

        Args:
            pnr: the booking reference.
            waive_fee: waive the 20% fee.
        """
        self.executed.append((pnr, waive_fee))
        return {"cancelled": True, "pnr": pnr, "fee_waived": waive_fee}

    @function_tool
    async def get_booking(self, pnr: str) -> dict:
        """Look up a booking.

        Args:
            pnr: the booking reference.
        """
        return {"found": True, "pnr": pnr}


def wire(agent, state):
    gate = ToolGate(POLICY)
    wrapped = _autowrap_tool_gate(agent, gate, state)
    return wrapped


def tool_named(agent, name):
    return next(t for t in agent.tools if t.info.name == name)


def test_unauthorized_irreversible_action_is_blocked_before_execution():
    agent, state = BookingAgent(), SessionState("c")
    assert wire(agent, state) == ["cancel_booking"]
    result = asyncio.run(
        tool_named(agent, "cancel_booking")(pnr="JT4R9X", waive_fee=True))
    assert "error" in result               # blocked → tool log records a failure
    assert result["blocked_by"] == "authz:cancel_booking"
    assert result["say"]                   # safe refusal the model can voice
    assert agent.executed == []            # the side effect NEVER ran


def test_authorized_call_passes_through_unchanged():
    agent, state = BookingAgent(), SessionState("c")
    wire(agent, state)
    state.set_fact("auth.fee_waiver_authorized", True, source="host")
    result = asyncio.run(
        tool_named(agent, "cancel_booking")(pnr="JT4R9X", waive_fee=True))
    assert result == {"cancelled": True, "pnr": "JT4R9X", "fee_waived": True}
    assert agent.executed == [("JT4R9X", True)]


def test_normal_use_of_a_conditionally_guarded_tool_never_blocks():
    agent, state = BookingAgent(), SessionState("c")
    wire(agent, state)
    result = asyncio.run(
        tool_named(agent, "cancel_booking")(pnr="JT4R9X"))  # waive_fee=False
    assert result["cancelled"] is True


def test_unguarded_tools_are_left_untouched():
    agent, state = BookingAgent(), SessionState("c")
    before = tool_named(agent, "get_booking")
    wire(agent, state)
    assert tool_named(agent, "get_booking") is before


def test_wrapped_tool_keeps_identical_llm_schema():
    from livekit.agents.llm import utils as llm_utils

    agent, state = BookingAgent(), SessionState("c")
    schema_before = llm_utils.build_legacy_openai_schema(
        tool_named(agent, "cancel_booking"), internally_tagged=True)
    wire(agent, state)
    schema_after = llm_utils.build_legacy_openai_schema(
        tool_named(agent, "cancel_booking"), internally_tagged=True)
    assert schema_after == schema_before


def test_llm_node_autowrap_skips_host_override():
    class CustomNodeAgent(Agent):
        def __init__(self):
            super().__init__(instructions="x")

        async def llm_node(self, chat_ctx, tools, model_settings):  # manual
            yield "custom"

    agent = CustomNodeAgent()
    assert _autowrap_llm_node(agent) is False  # host wiring stays canonical


def test_llm_node_autowrap_installs_on_default_agent():
    agent = BookingAgent()
    assert _autowrap_llm_node(agent) is True
    assert "llm_node" in vars(agent)
    assert _autowrap_llm_node(agent) is False  # idempotent: second call no-ops


def test_llm_node_autowrap_passthrough_without_gate():
    """No _mirror_pre_tts on the agent → the wrapped node streams the
    default output untouched (zero-cost passthrough)."""
    agent = BookingAgent()
    _autowrap_llm_node(agent)

    async def fake_default(self, ctx, tools, model_settings):
        for chunk in ("hel", "lo"):
            yield chunk

    # Point Agent.default.llm_node at a fake stream for this test.
    original = Agent.default.llm_node
    Agent.default.llm_node = fake_default
    try:
        async def collect():
            return [c async for c in agent.llm_node("ctx", [], None)]
        assert asyncio.run(collect()) == ["hel", "lo"]
    finally:
        Agent.default.llm_node = original


# ── degrade-gracefully (no livekit required) ────────────────────────────────


def test_autowrap_degrades_on_unrecognized_agent_shape():
    """A duck-typed agent with no .tools surface → no wrap, no raise from
    the guarded call site (attach_mirror catches; here we assert the
    helper's behavior on the empty surface)."""
    class Bare:
        tools = []

        async def update_tools(self, tools):
            raise AssertionError("must not be called when nothing wrapped")

    gate = ToolGate(POLICY)
    assert _autowrap_tool_gate(Bare(), gate, SessionState("c")) == []


def test_manual_gate_pattern_still_works():
    """The documented fallback: host calls gate.check() at the top of the
    tool body — independent of any auto-wiring."""
    gate, state = ToolGate(POLICY), SessionState("c")
    decision = gate.check("cancel_booking", {"pnr": "X", "waive_fee": True}, state)
    assert not decision.allow
    state.set_fact("auth.fee_waiver_authorized", True, source="host")
    assert gate.check("cancel_booking", {"pnr": "X", "waive_fee": True}, state).allow
