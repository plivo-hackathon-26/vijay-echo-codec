"""Phase 4 — runtime: control loop, grounding, intent memory, persona,
escalation."""

from __future__ import annotations

from plivo_mirror.contracts import TurnContext, Verdict
from plivo_mirror.runtime.escalation import build_handoff
from plivo_mirror.runtime.grounding import build_grounding_block
from plivo_mirror.runtime.intent_memory import IntentMemory
from plivo_mirror.runtime.loop import review_turn
from plivo_mirror.runtime.persona_guard import PersonaGuard
from plivo_mirror.state.entities import ValidatedEntity
from plivo_mirror.state.session import SessionState


# ── control loop ──────────────────────────────────────────────────────


class _Guard:
    def __init__(self, verdict):
        self.verdict = verdict
        self.called = False

    async def inspect(self, ctx):
        self.called = True
        return self.verdict


async def test_loop_speech_intervention_short_circuits_action():
    speech = _Guard(Verdict.correct(reason="x", spoken_correction="hold on"))
    action = _Guard(Verdict.pass_())
    v = await review_turn(speech, action, TurnContext(state=SessionState()))
    assert v.decision == "correct"
    assert action.called is False  # speech intervened ⇒ action never runs


async def test_loop_action_runs_when_speech_passes():
    speech = _Guard(Verdict.pass_())
    action = _Guard(Verdict.block(reason="bad tool"))
    v = await review_turn(speech, action, TurnContext(state=SessionState()))
    assert v.decision == "block"
    assert action.called is True


async def test_loop_pass_when_both_pass():
    v = await review_turn(_Guard(Verdict.pass_()), _Guard(Verdict.pass_()), TurnContext(state=SessionState()))
    assert v.decision == "pass"


# ── grounding ─────────────────────────────────────────────────────────


def test_grounding_empty_when_nothing_confirmed():
    assert build_grounding_block(SessionState()) == ""


def test_grounding_includes_state():
    st = SessionState()
    st.confirm_intent("one veggie wrap")
    st.set_entity("items", ValidatedEntity("item", ["veggie wrap"], "..."))
    st.log_committed_action("place_order", {"items": ["veggie wrap"]})
    block = build_grounding_block(st)
    assert "one veggie wrap" in block
    assert "veggie wrap" in block
    assert "Already done" in block
    assert "never read this header aloud" in block  # safety guard present


# ── intent memory ─────────────────────────────────────────────────────


def test_intent_memory_decays_and_clears():
    m = IntentMemory()
    m.hold("mushroom only", turns=2)
    assert m.consume() == "mushroom only"
    assert m.active == "mushroom only"
    assert m.consume() == "mushroom only"
    assert m.active is None  # decayed to zero
    assert m.consume() is None


def test_intent_memory_clear_on_commit():
    m = IntentMemory()
    m.hold("x", turns=3)
    m.clear()
    assert m.consume() is None


# ── persona guard ─────────────────────────────────────────────────────


def test_persona_reinjects_on_interval():
    pg = PersonaGuard(system_summary="You are Bob.", reinject_every=2, escalate_after=0)
    assert pg.observe_turn().reinject is False
    s = pg.observe_turn()
    assert s.reinject is True and s.reinject_text == "You are Bob."


def test_persona_escalates_on_tone():
    pg = PersonaGuard(reinject_every=0, escalate_after=0, negative_tone_threshold=2)
    pg.observe_turn(customer_text="this is ridiculous")
    s = pg.observe_turn(customer_text="absolutely terrible, get me a manager")
    assert s.escalate is True
    assert "tone" in s.reason


def test_persona_escalates_on_length():
    pg = PersonaGuard(reinject_every=0, escalate_after=3)
    pg.observe_turn()
    pg.observe_turn()
    s = pg.observe_turn()
    assert s.escalate is True
    assert "length" in s.reason


# ── escalation handoff ────────────────────────────────────────────────


def test_handoff_built_from_state():
    st = SessionState(call_id="call-1")
    st.confirm_intent("refund $25")
    st.write_entity("refund_amount", "amount", "$25")
    h = build_handoff(st, "caller requested human", transcript_summary="upset re: refund")
    assert h.call_id == "call-1"
    assert h.confirmed_intent == "refund $25"
    assert "refund_amount" in h.entities
    briefing = h.as_briefing()
    assert "caller requested human" in briefing
    assert "refund $25" in briefing
