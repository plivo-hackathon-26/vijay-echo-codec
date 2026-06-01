"""Phase 4 — Firewall facade end-to-end (verifier mocked)."""

from __future__ import annotations

from plivo_mirror.contracts import ToolCallIntent, TurnContext
from plivo_mirror.firewall import Firewall
from plivo_mirror.state.entities import ValidatedEntity
from plivo_mirror.verifier.base import GroundingEvidence, VerifierResult


class FakeVerifier:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def verify(self, claim, evidence: GroundingEvidence) -> VerifierResult:
        self.calls += 1
        return self.result


def test_factories_share_compiled_policies():
    fw = Firewall(policies=["Never invent a price."])
    st = fw.new_session("c1")
    assert st.call_id == "c1"
    assert st.compiled_policies[0].text == "Never invent a price."
    assert fw.new_persona_guard() is not fw.new_persona_guard()


async def test_clean_turn_passes_no_verifier_call():
    vf = FakeVerifier(VerifierResult(supported=False))
    fw = Firewall(policies=[], verifier=vf)
    st = fw.new_session()
    v = await fw.review_turn(TurnContext(state=st, planned_reply="Sure, what else?"))
    assert v.decision == "pass"
    assert vf.calls == 0  # zero-cost clean path


async def test_speech_boundary_corrects_fabrication():
    vf = FakeVerifier(VerifierResult(supported=False, policy_id="no_price"))
    fw = Firewall(policies=["Never invent a price."], verifier=vf)
    st = fw.new_session()
    v = await fw.review_turn(TurnContext(state=st, planned_reply="That'll be $19.99."))
    assert v.decision == "correct"
    assert vf.calls == 1


async def test_action_boundary_blocks_wrong_args():
    # speech passes (no risky span); action guard catches arg/state mismatch
    fw = Firewall(policies=[])
    st = fw.new_session()
    st.set_entity("items", ValidatedEntity("item", ["turkey sub"], "..."))
    ctx = TurnContext(
        state=st,
        planned_reply="Placing your order.",
        tool_intents=[ToolCallIntent(name="place_order", args={"items": ["turkey sub", "italian sub"]})],
    )
    v = await fw.review_turn(ctx)
    assert v.decision == "block"
    assert v.policy_id == "arg_state_mismatch"
