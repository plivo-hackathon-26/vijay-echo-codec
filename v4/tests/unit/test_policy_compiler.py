"""Phase 1 — policy compiler: ids, directive checks, verifier-only."""

from __future__ import annotations

from plivo_mirror.contracts import TurnContext
from plivo_mirror.policy.compiler import compile_policies, compile_policy
from plivo_mirror.state.session import SessionState


def _ctx(reply: str) -> TurnContext:
    return TurnContext(state=SessionState(), planned_reply=reply)


def test_plain_english_is_verifier_only():
    p = compile_policy("Never invent a price the menu does not list.", "no_price")
    assert p.check is None
    assert p.run(_ctx("That'll be nine ninety-nine.")) is None


def test_forbid_directive_blocks_on_phrase():
    p = compile_policy("FORBID: full refund", "no_full_refund")
    assert p.check is not None
    assert p.run(_ctx("Sure, I'll process a full refund now.")).decision == "block"
    assert p.run(_ctx("Let me transfer you to a specialist.")) is None


def test_require_directive_blocks_when_absent():
    p = compile_policy("REQUIRE: this call may be recorded", "rec_disclosure")
    # disclosure present -> ok
    assert p.run(_ctx("Hi, this call may be recorded. How can I help?")) is None
    # disclosure missing on a non-empty reply -> block
    v = p.run(_ctx("Hi, how can I help?"))
    assert v is not None and v.decision == "block"
    # empty reply -> not due yet
    assert p.run(_ctx("   ")) is None


def test_compile_policies_assigns_unique_ids():
    pols = compile_policies(
        [
            "Never invent a price.",
            "Never invent a price.",  # collision -> suffixed
            "FORBID: full refund",
        ]
    )
    ids = [p.id for p in pols]
    assert len(ids) == len(set(ids)), f"ids not unique: {ids}"
    assert pols[2].text == "FORBID: full refund"
    assert pols[2].check is not None


def test_compile_policies_preserves_text():
    pols = compile_policies(["Always read the order back before placing it."])
    assert pols[0].text == "Always read the order back before placing it."
    assert pols[0].check is None
