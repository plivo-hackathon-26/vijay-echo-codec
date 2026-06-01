"""FIX 2 — lock the honest router behavior.

Today the confidence signal is inactive (no logprobs in the LiveKit path),
so the speech-guard router is risk-span (lexicon) driven ONLY. These tests
pin that down — and confirm the confidence-gate code path is intact behind
it (so a real signal activates it with no code change), matching the
FUTURE-capability framing in CLAUDE.md."""

from __future__ import annotations

from plivo_mirror.contracts import TurnContext
from plivo_mirror.guards.signal import FixedConfidence, LogprobEntropySignal
from plivo_mirror.guards.speech import SpeechGuard
from plivo_mirror.state.session import SessionState
from plivo_mirror.verifier.base import GroundingEvidence, VerifierResult


class CountingVerifier:
    def __init__(self):
        self.calls = 0

    async def verify(self, claim, evidence: GroundingEvidence) -> VerifierResult:
        self.calls += 1
        return VerifierResult(supported=True)


def test_turncontext_logprobs_defaults_none():
    # the adapter never sets this, so the signal always sees None
    assert TurnContext(state=SessionState()).logprobs is None


async def test_routing_is_risk_span_only_today():
    # default LogprobEntropySignal + no logprobs ⇒ confidence 0.0 ⇒ the
    # gate never passes ⇒ any risky span escalates to the verifier.
    vf = CountingVerifier()
    guard = SpeechGuard(vf, signal=LogprobEntropySignal())
    await guard.inspect(TurnContext(state=SessionState(), planned_reply="That'll be $12.50."))
    assert vf.calls == 1  # escalated; the confidence gate did NOT short-circuit


async def test_no_risky_span_still_skips_verifier():
    # risk-span gating, not confidence, is what spares clean turns
    vf = CountingVerifier()
    guard = SpeechGuard(vf, signal=LogprobEntropySignal())
    await guard.inspect(TurnContext(state=SessionState(), planned_reply="Sure, what would you like?"))
    assert vf.calls == 0


async def test_confidence_gate_path_intact_when_a_real_signal_exists():
    # supplying a real signal (here a stub) activates the gate with NO code
    # change — proving the path is intact, just unused in production today.
    vf = CountingVerifier()
    guard = SpeechGuard(vf, signal=FixedConfidence(0.95), confidence_threshold=0.6)
    await guard.inspect(TurnContext(state=SessionState(), planned_reply="That'll be $12.50."))
    assert vf.calls == 0  # high confidence short-circuits the risky span
