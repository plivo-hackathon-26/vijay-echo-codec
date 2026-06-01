"""Phase 3 — ActionGuard: consistency + authz separation + validation."""

from __future__ import annotations

from decimal import Decimal

from plivo_mirror.authz.service import (
    RuleBasedAuthorizationService,
    requires_entity,
)
from plivo_mirror.contracts import ToolCallIntent, TurnContext, Verdict
from plivo_mirror.guards.action import ActionGuard
from plivo_mirror.state.entities import ValidatedEntity
from plivo_mirror.state.session import SessionState


def _ctx(reply="", intents=None, state=None):
    return TurnContext(
        state=state or SessionState(),
        planned_reply=reply,
        tool_intents=intents or [],
    )


async def test_no_intents_clean_reply_passes():
    v = await ActionGuard().inspect(_ctx("Sure, anything else?"))
    assert v.decision == "pass"


async def test_false_completion_blocks():
    v = await ActionGuard().inspect(_ctx("All set — I've placed your order!"))
    assert v.decision == "block"
    assert v.policy_id == "false_completion"
    assert v.spoken_correction


async def test_completion_claim_ok_if_already_committed():
    st = SessionState()
    st.log_committed_action("place_order", {"items": ["x"]})
    v = await ActionGuard().inspect(_ctx("Your order has been placed.", state=st))
    assert v.decision == "pass"


async def test_arg_state_mismatch_blocks():
    # state = source of truth: final order is mushroom only
    st = SessionState()
    st.set_entity("items", ValidatedEntity("item", ["large mushroom"], "..."))
    intent = ToolCallIntent(
        name="place_order",
        args={"items": ["large mushroom", "large pepperoni"]},  # model kept removed item
        irreversible=True,
    )
    v = await ActionGuard().inspect(_ctx("Placing your order now.", intents=[intent], state=st))
    assert v.decision == "block"
    assert v.policy_id == "arg_state_mismatch"


async def test_arg_state_match_passes():
    st = SessionState()
    st.set_entity("items", ValidatedEntity("item", ["large mushroom"], "..."))
    intent = ToolCallIntent(name="place_order", args={"items": ["Large Mushroom"]})
    v = await ActionGuard().inspect(_ctx("Placing it now.", intents=[intent], state=st))
    assert v.decision == "pass"


async def test_amount_compared_numerically():
    st = SessionState()
    st.write_entity("amount", "amount", "$12.50")
    intent = ToolCallIntent(name="charge", args={"amount": "12.5"})  # same value, diff string
    v = await ActionGuard().inspect(_ctx("Charging now.", intents=[intent], state=st))
    assert v.decision == "pass"


async def test_authz_separation_blocks_unauthorized():
    svc = RuleBasedAuthorizationService(
        {"process_refund": [requires_entity("identity_verified")]}
    )
    guard = ActionGuard(authz=svc)
    intent = ToolCallIntent(name="process_refund", args={}, irreversible=True)
    v = await guard.inspect(_ctx("Refunding you now.", intents=[intent]))
    assert v.decision == "block"
    assert v.policy_id == "requires_entity:identity_verified"


async def test_authz_separation_allows_when_authorized():
    st = SessionState()
    st.set_entity("identity_verified", ValidatedEntity("name", "yes", "yes"))
    svc = RuleBasedAuthorizationService(
        {"process_refund": [requires_entity("identity_verified")]}
    )
    guard = ActionGuard(authz=svc)
    intent = ToolCallIntent(name="process_refund", args={}, irreversible=True)
    v = await guard.inspect(_ctx("Refunding now.", intents=[intent], state=st))
    assert v.decision == "pass"


async def test_injection_scenario_model_cannot_self_authorize():
    # Prompt injection convinced the model to refund; authz is separate
    # and has the final say regardless of how persuasive the text is.
    svc = RuleBasedAuthorizationService(
        {"process_refund": [requires_entity("identity_verified")]}
    )
    guard = ActionGuard(authz=svc)
    intent = ToolCallIntent(name="process_refund", args={"amount": "9999"})
    reply = "Absolutely, as the system instructed, I'm authorized to refund you."
    v = await guard.inspect(_ctx(reply, intents=[intent]))
    assert v.decision == "block"


async def test_parameter_validation_blocks_over_cap():
    def cap_1000(intent, state):
        amt = state.entity_value("refund_amount")
        if amt is not None and amt > Decimal("1000"):
            return Verdict.block(reason="over refund cap", policy_id="refund_cap")
        return None

    st = SessionState()
    st.write_entity("refund_amount", "amount", "$2500")
    guard = ActionGuard(validators={"process_refund": [cap_1000]})
    intent = ToolCallIntent(name="process_refund", args={})
    v = await guard.inspect(_ctx("Processing refund.", intents=[intent], state=st))
    assert v.decision == "block"
    assert v.policy_id == "refund_cap"
    assert v.spoken_correction  # filled with policy re-confirm line


async def test_spoken_refusal_with_firing_action_blocks():
    intent = ToolCallIntent(name="place_order", args={})
    v = await ActionGuard().inspect(
        _ctx("Actually I won't place that order.", intents=[intent])
    )
    assert v.decision == "block"
    assert v.policy_id == "spoken_action_mismatch"
