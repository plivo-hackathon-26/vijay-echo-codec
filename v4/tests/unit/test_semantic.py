"""Phase 5b — semantic recall tier (the NLI lever).

Proves the ROUTER behavior with a MOCK signal (no torch in CI): a lexically
clean reply that a semantic signal flags as contradicting the customer is
routed to the verifier and corrected; the verifier still has the final say;
customer_text reaches the evidence; and the real NLI impl degrades gracefully
when the optional dependency is absent.
"""

from __future__ import annotations

from plivo_mirror.contracts import TurnContext, Verdict
from plivo_mirror.guards.risk_spans import tag_risk_spans
from plivo_mirror.guards.semantic import (
    NLICrossEncoderSignal,
    NoSemanticSignal,
    SemanticResult,
)
from plivo_mirror.guards.speech import SpeechGuard
from plivo_mirror.state.session import SessionState
from plivo_mirror.verifier.base import GroundingEvidence, VerifierResult


class _FakeVerifier:
    def __init__(self, result, sink=None):
        self.result = result
        self.sink = sink
        self.calls = 0

    async def verify(self, claim, evidence: GroundingEvidence) -> VerifierResult:
        self.calls += 1
        if self.sink is not None:
            self.sink["ev"] = evidence
        return self.result


class _FlagSemantic:
    """Mock semantic signal: always reports a contradiction."""

    def contradicts(self, customer_text, reply, *, state=None) -> SemanticResult:
        return SemanticResult(contradiction=True, score=0.9, premise=customer_text, hypothesis=reply)


# the canonical lexically-invisible violation: ignored negation
_CUSTOMER = "No onions please."
_REPLY = "Sure, one pizza with extra onions coming up."


def test_reply_has_no_lexical_span():
    # precondition for the whole tier: the lexicon does NOT flag this reply
    assert tag_risk_spans(_REPLY) == []


async def test_semantic_flag_routes_clean_reply_to_verifier_and_corrects():
    verifier = _FakeVerifier(VerifierResult(supported=False, reason="ignored negation"))
    guard = SpeechGuard(verifier, semantic_signal=_FlagSemantic())
    ctx = TurnContext(state=SessionState(), planned_reply=_REPLY, customer_text=_CUSTOMER)
    v = await guard.inspect(ctx)
    assert verifier.calls == 1                 # the semantic tier routed it
    assert v.decision == "correct"
    assert v.span == _REPLY                     # synthetic whole-reply span


async def test_verifier_still_overrules_a_semantic_false_positive():
    # NLI is recall-only: if the verifier says supported, no intervention.
    verifier = _FakeVerifier(VerifierResult(supported=True))
    guard = SpeechGuard(verifier, semantic_signal=_FlagSemantic())
    ctx = TurnContext(state=SessionState(), planned_reply=_REPLY, customer_text=_CUSTOMER)
    v = await guard.inspect(ctx)
    assert verifier.calls == 1
    assert v.decision == "pass"


async def test_no_semantic_signal_keeps_zero_latency_pass():
    verifier = _FakeVerifier(VerifierResult(supported=False))
    # default: no semantic tier → clean reply passes at the gate, verifier unused
    guard = SpeechGuard(verifier)
    ctx = TurnContext(state=SessionState(), planned_reply=_REPLY, customer_text=_CUSTOMER)
    v = await guard.inspect(ctx)
    assert verifier.calls == 0
    assert v.decision == "pass" and v.reason == "no_risk_span"


async def test_null_signal_never_fires():
    verifier = _FakeVerifier(VerifierResult(supported=False))
    guard = SpeechGuard(verifier, semantic_signal=NoSemanticSignal())
    ctx = TurnContext(state=SessionState(), planned_reply=_REPLY, customer_text=_CUSTOMER)
    v = await guard.inspect(ctx)
    assert verifier.calls == 0
    assert v.decision == "pass"


async def test_noncommittal_replies_skip_the_semantic_tier():
    # live-call finding: NLI must NOT run on refusals/deferrals/questions —
    # they commit nothing, so an off-topic customer turn shouldn't trip a
    # spurious contradiction. A flagging signal + these replies => still pass,
    # and the verifier is never consulted.
    flag = _FlagSemantic()
    for reply in (
        "I can't quote prices here.",          # refusal
        "I don't have the full menu list.",    # refusal/deferral
        "Let me check on that for you.",        # deferral
        "What would you like to order?",        # pure question
    ):
        verifier = _FakeVerifier(VerifierResult(supported=False))
        guard = SpeechGuard(verifier, semantic_signal=flag)
        ctx = TurnContext(state=SessionState(), planned_reply=reply, customer_text="are you good at jogging?")
        v = await guard.inspect(ctx)
        assert v.decision == "pass", f"should pass: {reply!r}"
        assert verifier.calls == 0, f"verifier should not be consulted for: {reply!r}"


async def test_committal_reply_still_routes_to_semantic_tier():
    # a genuine affirmative confirmation must still be checked
    verifier = _FakeVerifier(VerifierResult(supported=False, reason="ignored negation"))
    guard = SpeechGuard(verifier, semantic_signal=_FlagSemantic())
    ctx = TurnContext(state=SessionState(), planned_reply="Sure, one pizza with extra onions.", customer_text="no onions")
    v = await guard.inspect(ctx)
    assert verifier.calls == 1 and v.decision == "correct"


async def test_customer_text_reaches_grounding_evidence():
    sink: dict = {}
    verifier = _FakeVerifier(VerifierResult(supported=True), sink=sink)
    guard = SpeechGuard(verifier, semantic_signal=_FlagSemantic())
    ctx = TurnContext(state=SessionState(), planned_reply=_REPLY, customer_text=_CUSTOMER)
    await guard.inspect(ctx)
    assert sink["ev"].customer_text == _CUSTOMER


def test_nli_signal_degrades_gracefully_without_dependency():
    # In CI torch/transformers are not installed: the signal must fail open
    # (never fire, never raise) and mark itself unavailable after one try.
    sig = NLICrossEncoderSignal("definitely-not-a-real-model")
    res = sig.contradicts("no onions", "one pizza with extra onions")
    assert res.contradiction is False
    assert sig._unavailable is True


def test_nli_signal_empty_inputs_short_circuit():
    sig = NLICrossEncoderSignal()
    assert sig.contradicts("", "something").contradiction is False
    assert sig.contradicts("something", "").contradiction is False
