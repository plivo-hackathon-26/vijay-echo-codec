"""Phase 1 — contracts: Verdict constructors, Policy.run, TurnContext."""

from __future__ import annotations

from plivo_mirror.contracts import Policy, ToolCallIntent, TurnContext, Verdict
from plivo_mirror.state.session import SessionState


def test_verdict_pass_does_not_intervene():
    v = Verdict.pass_("looks fine")
    assert v.decision == "pass"
    assert v.intervened is False
    assert v.reason == "looks fine"


def test_verdict_correct_carries_spoken_correction():
    v = Verdict.correct(
        reason="fabricated price",
        spoken_correction="Let me double-check that price for you.",
        policy_id="no_price_invention",
        span="$9.99",
    )
    assert v.decision == "correct"
    assert v.intervened is True
    assert v.spoken_correction.startswith("Let me")
    assert v.policy_id == "no_price_invention"
    assert v.span == "$9.99"


def test_verdict_block_intervenes():
    v = Verdict.block(reason="refund must transfer", policy_id="refund_xfer")
    assert v.decision == "block"
    assert v.intervened is True


def test_policy_run_with_no_check_is_verifier_only():
    p = Policy(id="p1", text="Be polite.", check=None)
    ctx = TurnContext(state=SessionState(), planned_reply="hello")
    assert p.run(ctx) is None


def test_policy_run_with_check_returns_verdict():
    def check(ctx: TurnContext):
        return Verdict.block(reason="hit", policy_id="p1") if "bad" in ctx.planned_reply else None

    p = Policy(id="p1", text="No bad.", check=check)
    assert p.run(TurnContext(state=SessionState(), planned_reply="all good")) is None
    hit = p.run(TurnContext(state=SessionState(), planned_reply="this is bad"))
    assert hit is not None and hit.decision == "block"


def test_tool_call_intent_defaults():
    tc = ToolCallIntent(name="place_order")
    assert tc.args == {}
    assert tc.irreversible is False
    assert tc.tool_call_id is None
