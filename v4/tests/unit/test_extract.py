"""Phase 5a.5 — generic entity extractor + reference-fact grounding.

Locks the deterministic behavior the state-grounding fix depends on: the
extractor only writes VALIDATED values, ``known_facts`` round-trips through
the session, and both reach the grounded verifier as evidence.
"""

from __future__ import annotations

from decimal import Decimal

from plivo_mirror.state.extract import CaptureRule, RegexEntityExtractor
from plivo_mirror.state.session import SessionState


# ── extractor: writes only validated values ──────────────────────────────


def test_extracts_money_amount():
    st = SessionState()
    RegexEntityExtractor().extract("I'd like to put down $24.50 today", st)
    ent = st.get_entity("amount")
    assert ent is not None and ent.value == Decimal("24.50")


def test_extracts_dollars_word_form():
    st = SessionState()
    RegexEntityExtractor().extract("make it 12 dollars please", st)
    assert st.entity_value("amount") == Decimal("12")


def test_extracts_iso_date():
    st = SessionState()
    RegexEntityExtractor().extract("can you deliver on 2026-06-15?", st)
    ent = st.get_entity("date")
    assert ent is not None and ent.value.isoformat() == "2026-06-15"


def test_no_match_leaves_state_untouched():
    st = SessionState()
    RegexEntityExtractor().extract("just a plain sentence with no values", st)
    assert st.entities == {}


def test_empty_text_is_noop():
    st = SessionState()
    RegexEntityExtractor().extract("", st)
    RegexEntityExtractor().extract("   ", st)
    assert st.entities == {}


def test_invalid_value_is_dropped_not_written():
    # A custom rule that captures a non-amount string under an amount key:
    # the validator rejects it, so nothing is written (never guesses).
    import re

    rule = CaptureRule(key="amount", kind="amount", pattern=re.compile(r"price=(\w+)"), group=1)
    st = SessionState()
    RegexEntityExtractor((rule,)).extract("price=abc", st)
    assert st.get_entity("amount") is None


# ── known_facts: code-owned reference data ────────────────────────────────


def test_known_facts_seed_and_snapshot():
    st = SessionState(known_facts={"wings_per_order": "6", "open_until": "9 PM"})
    assert st.known_facts == {"wings_per_order": "6", "open_until": "9 PM"}
    st.add_known_fact("sizes", "small, medium, large")
    assert st.known_facts["sizes"] == "small, medium, large"
    # snapshot is a copy — mutating it must not affect state
    st.known_facts["sizes"] = "tampered"
    assert st.known_facts["sizes"] == "small, medium, large"


def test_default_session_has_empty_known_facts():
    assert SessionState().known_facts == {}


# ── grounding wiring: facts reach the verifier as evidence ────────────────


async def test_known_facts_flow_into_grounding_evidence():
    from plivo_mirror.contracts import TurnContext
    from plivo_mirror.guards.speech import SpeechGuard
    from plivo_mirror.verifier.base import GroundingEvidence, VerifierResult

    captured: dict[str, GroundingEvidence] = {}

    class _CaptureVerifier:
        async def verify(self, claim, evidence):
            captured["ev"] = evidence
            return VerifierResult(supported=True, reason="captured")

    st = SessionState(known_facts={"open_until": "9 PM"})
    guard = SpeechGuard(_CaptureVerifier())
    # a reply with a risky numeric span so the verifier is actually reached
    ctx = TurnContext(state=st, planned_reply="We're open until 9 tonight.", customer_text="open late?")
    await guard.inspect(ctx)
    ev = captured.get("ev")
    assert ev is not None
    assert "open_until: 9 PM" in ev.retrieved_facts
