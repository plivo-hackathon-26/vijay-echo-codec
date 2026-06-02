"""Phase B — regeneration loop (speech guard re-verifies; LLM mocked).

Proves: structured violation → templated-from-state reply that re-verifies
clean; open violation → regenerated reply that re-verifies clean;
non-converging → escalation after the retry cap; packet never echoes the
flagged span; regeneration uses a system/developer channel + the real
customer turn (never a fabricated one)."""

from __future__ import annotations

import asyncio
import json

import pytest

from plivo_mirror.contracts import TurnContext, Verdict
from plivo_mirror.guards.speech import SpeechGuard
from plivo_mirror.intervention.engine import (
    ESCALATION_LINE,
    run_intervention,
    stream_intervention,
)
from plivo_mirror.intervention.packet import assert_no_echo, build_packet
from plivo_mirror.intervention.regenerate import LLMReplyGenerator
from plivo_mirror.state.session import SessionState
from plivo_mirror.verifier.base import GroundingEvidence, VerifierResult


class FakeVerifier:
    def __init__(self, result):
        self.result = result

    async def verify(self, claim, evidence: GroundingEvidence) -> VerifierResult:
        return self.result


class ScriptedGenerator:
    """Returns scripted replies in order; records the packets it saw."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.packets = []
        self.customer_texts = []

    async def regenerate(self, *, packet, customer_text):
        self.packets.append(packet)
        self.customer_texts.append(customer_text)
        return self._replies.pop(0) if self._replies else ""


def _ctx(state, reply, customer="I'd like to order."):
    return TurnContext(state=state, planned_reply=reply, customer_text=customer)


# ── structured violation → template from state (no LLM) ────────────────


async def test_structured_violation_templated_from_state_reverifies_clean():
    st = SessionState()
    st.confirm_intent("one veggie wrap")
    guard = SpeechGuard(FakeVerifier(VerifierResult(supported=True)))
    verdict = Verdict.correct(reason="fabricated price", span="$19.99", spoken_correction="One sec.")
    result = await run_intervention(
        verdict=verdict, context=_ctx(st, "That's $19.99."), speech_guard=guard, generator=None
    )
    assert result.escalated is False
    assert result.filler == "One sec."  # deflection spoken first
    assert result.answer == "Got it — one veggie wrap. Anything else?"  # from state, no LLM
    assert "19.99" not in result.answer  # pink-elephant


# ── open violation → regenerate via main LLM, re-verify clean ──────────


async def test_open_violation_regenerated_reply_reverifies_clean():
    st = SessionState()  # no confirmed intent ⇒ open path
    guard = SpeechGuard(FakeVerifier(VerifierResult(supported=True)))
    gen = ScriptedGenerator(["Let me confirm that with the kitchen and get right back to you."])
    verdict = Verdict.correct(reason="price fabrication", span="$19.99", spoken_correction="One moment.")
    result = await run_intervention(
        verdict=verdict, context=_ctx(st, "That's $19.99."), speech_guard=guard, generator=gen
    )
    assert result.escalated is False
    assert result.answer.startswith("Let me confirm")
    assert result.attempts == 1
    assert len(gen.packets) == 1  # regenerated once, re-verified clean


async def test_regeneration_retries_then_succeeds():
    st = SessionState()
    # first regen still risky+unsupported, second is clean
    guard = SpeechGuard(FakeVerifier(VerifierResult(supported=False)))

    class TwoStep:
        def __init__(self):
            self.n = 0

        async def regenerate(self, *, packet, customer_text):
            self.n += 1
            return "It's $5 off." if self.n == 1 else "I'll check and confirm shortly."

    gen = TwoStep()
    verdict = Verdict.correct(reason="x", span="$19.99", spoken_correction="One sec.")
    result = await run_intervention(
        verdict=verdict, context=_ctx(st, "?"), speech_guard=guard, generator=gen, max_retries=2
    )
    # second attempt has no risky span ⇒ speech guard passes regardless of verifier
    assert result.escalated is False
    assert result.answer.startswith("I'll check")
    assert result.attempts == 2


# ── non-convergence → escalation after the cap ─────────────────────────


async def test_non_converging_escalates_after_cap():
    st = SessionState(call_id="c9")
    guard = SpeechGuard(FakeVerifier(VerifierResult(supported=False)))
    # every regen keeps a risky price span the verifier rejects
    gen = ScriptedGenerator(["It's $5 off.", "Actually $6 off.", "$7 off!"])
    verdict = Verdict.correct(reason="x", span="$19.99", spoken_correction="One sec.")
    result = await run_intervention(
        verdict=verdict, context=_ctx(st, "?"), speech_guard=guard, generator=gen, max_retries=2
    )
    assert result.escalated is True
    assert result.handoff is not None
    assert result.handoff.call_id == "c9"
    assert result.attempts == 2


async def test_open_violation_no_generator_escalates():
    st = SessionState()
    guard = SpeechGuard(FakeVerifier(VerifierResult(supported=True)))
    verdict = Verdict.correct(reason="x", span="$19.99", spoken_correction="One sec.")
    result = await run_intervention(
        verdict=verdict, context=_ctx(st, "?"), speech_guard=guard, generator=None
    )
    assert result.escalated is True


# ── packet: pink-elephant + channel shape ──────────────────────────────


def test_packet_never_echoes_flagged_span():
    st = SessionState()
    st.confirm_intent("one veggie wrap")
    verdict = Verdict.correct(reason="said $19.99 which is wrong", span="$19.99", spoken_correction="")
    packet = build_packet(verdict, st)
    dev = packet.as_developer_message()
    assert "19.99" not in dev  # pink-elephant: wrong value absent
    assert "CONFIRMED FACTS" in dev or "confirmed" in dev.lower()
    # explicit guard
    with pytest.raises(ValueError):
        assert_no_echo("the price is $19.99", "$19.99")


def test_packet_does_not_crash_when_span_substring_of_correct_fact():
    # REGRESSION (Phase 5a.5): a short flagged span (a digit) can be a
    # SUBSTRING of a CORRECT value that legitimately belongs in the packet.
    # Here the agent wrongly said the total was "9 dollars" (span "9") but the
    # correct confirmed total is "$19" — "9" is a substring of "$19". The old
    # code asserted no-echo against the whole message and crashed regeneration
    # on exactly this collision. The packet must now build WITHOUT raising;
    # the pink-elephant guarantee is enforced on the spoken answer instead.
    st = SessionState()
    st.confirm_intent("an order totaling $19")  # correct value contains "9"
    verdict = Verdict.correct(
        reason="said the total was 9 dollars which is wrong",
        span="9",
        spoken_correction="",
    )
    packet = build_packet(verdict, st)
    dev = packet.as_developer_message()  # must NOT raise (old code did)
    assert "$19" in dev  # correct intent is carried through


class _Completions:
    def __init__(self, content):
        self._content = content
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": self._content})})]})


class FakeClient:
    def __init__(self, content):
        self.chat = type("Chat", (), {"completions": _Completions(content)})()


async def test_regenerator_uses_system_channel_and_real_customer_turn():
    client = FakeClient("Sure, let me sort that out.")
    gen = LLMReplyGenerator(client, model="gpt-5-mini")
    st = SessionState()
    st.confirm_intent("one veggie wrap")
    packet = build_packet(Verdict.correct(reason="x", span="$19.99", spoken_correction=""), st)
    await gen.regenerate(packet=packet, customer_text="How much is it?")
    msgs = client.chat.completions.last_kwargs["messages"]
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user"]  # packet is system; NOT a fake customer turn
    assert "CORRECTION REQUIRED" in msgs[0]["content"]
    assert msgs[1]["content"] == "How much is it?"  # the REAL customer utterance
    assert gen.model == "gpt-5-mini"  # single-LLM: same model as the agent


def test_firewall_from_env_builds_generator_on_same_client():
    from plivo_mirror.firewall import Firewall

    fw = Firewall.from_env(policies=[], model="gpt-5-mini", client=FakeClient("{}"))
    assert fw.generator is not None
    assert fw.generator.model == "gpt-5-mini"  # regeneration reuses the main model
    assert fw.verifier.model == "gpt-5-mini"


# ── FIX 1: deflection filler is streamed BEFORE regeneration runs ──────


async def test_filler_streamed_before_regeneration_is_awaited():
    events = []

    class SlowGen:
        async def regenerate(self, *, packet, customer_text):
            events.append("regen_started")
            await asyncio.sleep(0.01)  # deliberately slow
            events.append("regen_done")
            return "I'll check and confirm that for you shortly."  # no risky span

    st = SessionState()  # no confirmed intent ⇒ open/regenerate path
    guard = SpeechGuard(FakeVerifier(VerifierResult(supported=True)))
    verdict = Verdict.correct(reason="x", span="$19.99", spoken_correction="One moment.")
    agen = stream_intervention(
        verdict=verdict,
        context=_ctx(st, "That's $19.99.", "How much?"),
        speech_guard=guard,
        generator=SlowGen(),
    )

    first = await agen.__anext__()
    assert first == "One moment."  # the deflection filler
    assert events == []  # regeneration has NOT started yet — filler is already on the wire

    rest = [chunk async for chunk in agen]
    assert events == ["regen_started", "regen_done"]  # regen ran only after the filler
    assert rest == ["I'll check and confirm that for you shortly."]


async def test_stream_escalation_yields_filler_then_escalation_line_and_handoff():
    captured = []
    gen = ScriptedGenerator(["It's $5 off.", "Actually $6 off.", "$7 off!"])
    guard = SpeechGuard(FakeVerifier(VerifierResult(supported=False)))
    verdict = Verdict.correct(reason="x", span="$19.99", spoken_correction="One sec.")
    st = SessionState(call_id="c1")
    chunks = [
        c
        async for c in stream_intervention(
            verdict=verdict,
            context=_ctx(st, "?", "?"),
            speech_guard=guard,
            generator=gen,
            on_escalate=captured.append,
        )
    ]
    assert chunks[0] == "One sec."  # filler first
    assert chunks[-1] == ESCALATION_LINE  # escalation after non-convergence
    assert len(captured) == 1 and captured[0].call_id == "c1"  # warm-handoff context delivered
