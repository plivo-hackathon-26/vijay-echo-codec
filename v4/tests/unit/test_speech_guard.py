"""Phase 2 — SpeechGuard router (verifier mocked; no live LLM)."""

from __future__ import annotations

from plivo_mirror.contracts import TurnContext
from plivo_mirror.guards.signal import FixedConfidence
from plivo_mirror.guards.speech import SpeechGuard
from plivo_mirror.policy.compiler import compile_policies
from plivo_mirror.state.session import SessionState
from plivo_mirror.verifier.base import GroundingEvidence, VerifierResult


class FakeVerifier:
    """Records calls + returns a scripted result."""

    def __init__(self, result: VerifierResult):
        self.result = result
        self.calls: list[tuple[str, GroundingEvidence]] = []

    async def verify(self, claim: str, evidence: GroundingEvidence) -> VerifierResult:
        self.calls.append((claim, evidence))
        return self.result


def _ctx(reply, *, policies=None, facts=None):
    st = SessionState(policies=policies or [])
    for k, ent in (facts or {}).items():
        st.set_entity(k, ent)
    return TurnContext(state=st, planned_reply=reply)


async def test_clean_turn_passes_without_calling_verifier():
    vf = FakeVerifier(VerifierResult(supported=False))
    guard = SpeechGuard(vf)
    v = await guard.inspect(_ctx("Sure, what can I get started for you?"))
    assert v.decision == "pass"
    assert vf.calls == []  # zero-cost path: verifier never touched


async def test_confident_risky_span_passes_without_verifier():
    vf = FakeVerifier(VerifierResult(supported=False))
    guard = SpeechGuard(vf, signal=FixedConfidence(0.95), confidence_threshold=0.6)
    v = await guard.inspect(_ctx("That'll be $12.50."))
    assert v.decision == "pass"
    assert vf.calls == []  # high confidence ⇒ no escalation


async def test_uncertain_risky_span_escalates_and_corrects_when_unsupported():
    vf = FakeVerifier(VerifierResult(supported=False, policy_id="no_price", reason="not in facts"))
    guard = SpeechGuard(vf)  # default signal ⇒ confidence 0.0 ⇒ escalate
    v = await guard.inspect(_ctx("That'll be $12.50."))
    assert len(vf.calls) == 1
    assert v.decision == "correct"
    assert v.policy_id == "no_price"
    assert v.spoken_correction  # agent-voice line present
    assert "12" not in v.spoken_correction  # doesn't repeat the bad figure


async def test_uncertain_risky_span_passes_when_supported():
    vf = FakeVerifier(VerifierResult(supported=True))
    guard = SpeechGuard(vf)
    v = await guard.inspect(_ctx("That'll be $12.50."))
    assert len(vf.calls) == 1
    assert v.decision == "pass"


async def test_deterministic_block_short_circuits_verifier():
    vf = FakeVerifier(VerifierResult(supported=True))
    pols = compile_policies(["FORBID: full refund"])
    guard = SpeechGuard(vf)
    v = await guard.inspect(_ctx("Sure, a full refund is on its way.", policies=pols))
    assert v.decision == "block"
    assert v.policy_id is not None
    assert v.spoken_correction  # filled with default block correction
    assert vf.calls == []  # deterministic hit ⇒ verifier never runs


async def test_verifier_error_fails_open():
    class Boom:
        async def verify(self, claim, evidence):
            raise RuntimeError("nope")

    guard = SpeechGuard(Boom())
    v = await guard.inspect(_ctx("That'll be $12.50."))
    assert v.decision == "pass"
    assert v.reason == "verifier_error"


async def test_no_verifier_fails_open_on_risky_span():
    guard = SpeechGuard(None)
    v = await guard.inspect(_ctx("That'll be $12.50."))
    assert v.decision == "pass"
