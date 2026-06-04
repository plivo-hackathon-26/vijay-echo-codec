"""Hook B gated hold: assertiveness gate, inline judge, correction loop.

All offline — the judge is a fake; no model, no network, no keys.
"""

import asyncio
import time

from plivo_mirror_v5.deployables.intervention import (
    HANDOFF_FALLBACK,
    HELD_FALLBACK,
    CorrectionRetryLoop,
    JudgedPreTTSGate,
)
from plivo_mirror_v5.engine import (
    AssertivenessGate,
    Engine,
    EngineConfig,
    SessionState,
)

from helpers import REFERENCE

WRONG_PRICE_CLAIMS = [{"claim_id": "c1", "claim_type": "price",
                       "spoken_value": "$59.99",
                       "ref": "reference.plan.turbo.price_per_month"}]


class FakeJudge:
    """Scriptable TurnJudge: pops verdicts in order; records every call."""

    def __init__(self, verdicts=None, *, delay_s: float = 0.0,
                 error: Exception | None = None) -> None:
        self.verdicts = list(verdicts or [])
        self.delay_s = delay_s
        self.error = error
        self.calls: list[tuple[list[dict], int]] = []

    def judge_turn(self, turns, agent_turn_index):
        self.calls.append((turns, agent_turn_index))
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        if self.verdicts:
            return self.verdicts.pop(0)
        return {"violation": False, "category": None, "reason": ""}


def make_gate(judge=None, **config_kwargs):
    engine = Engine(EngineConfig(mode="intervene", **config_kwargs),
                    reference=REFERENCE)
    return JudgedPreTTSGate(engine, judge or FakeJudge()), SessionState("call-b")


# -- assertiveness gate --------------------------------------------------------

def test_gate_chitchat_is_not_assertive():
    gate = AssertivenessGate()
    for text in (
        "You're welcome! Anything else I can help with?",
        "Sure, let me look into that.",
        "Could you tell me which plan you're on?",
    ):
        assert not gate.check(text, []).assertive, text


def test_gate_assertive_triggers():
    gate = AssertivenessGate()
    cases = {
        "The Turbo plan is $59.99 a month.": "numberish",
        "I'll waive the cancellation fee for you.": "commitment_language",
        "Done — I've cancelled your service.": "completion_language",
        "We offer 24/7 phone support.": "capability_assertion",
    }
    for text, reason in cases.items():
        result = gate.check(text, [])
        assert result.assertive and reason in result.reasons, (text, result.reasons)


def test_gate_claims_alone_make_assertive():
    gate = AssertivenessGate()
    assert gate.check("It costs less than the other one.",
                      WRONG_PRICE_CLAIMS).assertive


# -- JudgedPreTTSGate ----------------------------------------------------------

async def test_non_assertive_turn_releases_without_judge():
    judge = FakeJudge()
    gate, state = make_gate(judge)
    decision = await gate.gate("You're welcome! Anything else?", [], state)
    assert decision.release and not decision.assertive
    assert judge.calls == []          # ~0 ms path: the judge never ran


async def test_l2_hold_never_waits_on_judge():
    judge = FakeJudge()
    gate, state = make_gate(judge)
    decision = await gate.gate("The Turbo plan is $59.99 a month.",
                               WRONG_PRICE_CLAIMS, state)
    assert not decision.release and decision.held_by == "L2"
    assert judge.calls == []          # deterministic hit short-circuits


async def test_judge_holds_assertive_violation():
    judge = FakeJudge([{"violation": True, "category": "promo_hallucination",
                        "reason": "No such promo exists in the facts."}])
    gate, state = make_gate(judge)
    gate.note_turn("user", "Any deals right now?")
    decision = await gate.gate("We have a buy-one-get-one-free promo today!",
                               [], state)
    assert not decision.release and decision.held_by == "JUDGE"
    assert decision.replacement_text == HELD_FALLBACK
    judge_verdict = decision.verdicts[-1]
    assert judge_verdict.detector == "JUDGE" and judge_verdict.severity == "high"
    # The judge saw the noted history plus the pending utterance.
    turns, idx = judge.calls[0]
    assert turns[idx]["text"].startswith("We have a buy-one")
    assert turns[0] == {"role": "user", "text": "Any deals right now?"}


async def test_judge_pass_releases():
    judge = FakeJudge([{"violation": False, "category": None, "reason": ""}])
    gate, state = make_gate(judge)
    decision = await gate.gate("The Turbo plan is $79.99 a month.",
                               [], state)
    assert decision.release and decision.assertive
    assert decision.judge_latency_ms is not None


async def test_judge_timeout_fails_open():
    judge = FakeJudge(delay_s=0.2)
    gate, state = make_gate(judge, inline_judge_timeout_s=0.05)
    decision = await gate.gate("The Turbo plan is $79.99 a month.", [], state)
    assert decision.release                       # fail-open: never block the call
    assert decision.judge_error == "TimeoutError"


async def test_judge_exception_fails_open():
    judge = FakeJudge(error=RuntimeError("boom"))
    gate, state = make_gate(judge)
    decision = await gate.gate("The Turbo plan is $79.99 a month.", [], state)
    assert decision.release and decision.judge_error == "RuntimeError"


async def test_history_window_respects_config():
    judge = FakeJudge()
    gate, state = make_gate(judge, inline_judge_history_turns=2)
    for i in range(5):
        gate.note_turn("user", f"turn {i}")
    await gate.gate("That's $79.99 total.", [], state)
    turns, idx = judge.calls[0]
    assert len(turns) == 3 and idx == 2           # 2 history + pending


# -- CorrectionRetryLoop -------------------------------------------------------

def make_loop(gate, replies, **kwargs):
    produced = []

    async def regenerate(packet: str, attempt: int) -> str:
        produced.append(packet)
        return replies.pop(0)

    loop = CorrectionRetryLoop(gate, regenerate, **kwargs)
    return loop, produced


async def test_loop_clean_turn_passes_through():
    gate, state = make_gate()
    loop, packets = make_loop(gate, [])
    outcome = await loop.run("Sure, let me check on that.", [], state)
    assert outcome.released and outcome.final_text == "Sure, let me check on that."
    assert outcome.filler_text is None and outcome.attempts == 0


async def test_loop_l2_hold_regenerates_and_releases():
    gate, state = make_gate()
    loop, packets = make_loop(gate, ["The Turbo plan is $79.99 a month."])
    outcome = await loop.run("The Turbo plan is $59.99 a month.",
                             WRONG_PRICE_CLAIMS, state)
    assert outcome.released and outcome.attempts == 1
    assert outcome.final_text == "The Turbo plan is $79.99 a month."
    assert outcome.filler_text == HELD_FALLBACK   # spoken while regenerating
    assert "79.99" in packets[0]                  # packet carries the truth
    assert "[CORRECTION:" in packets[0]


async def test_loop_pink_elephant_echo_counts_as_failed_attempt():
    gate, state = make_gate()
    loop, _ = make_loop(
        gate,
        ["I apologize — not $59.99 then.",            # echoes the flagged value
         "The Turbo plan is $79.99 a month."],
    )
    outcome = await loop.run("The Turbo plan is $59.99 a month.",
                             WRONG_PRICE_CLAIMS, state)
    assert outcome.released and outcome.attempts == 2
    assert "$59.99" not in outcome.final_text


async def test_loop_caps_retries_then_hands_off():
    judge = FakeJudge([{"violation": True, "category": "x", "reason": "r"}] * 9)
    gate, state = make_gate(judge)
    gate.note_turn("user", "Any promos?")
    bad = ["We also have 50% off everything today!",
           "And everything ships for $0.00 forever!"]
    loop, _ = make_loop(gate, bad, max_retries=2)
    outcome = await loop.run("We have a buy-one-get-one promo for $1!", [], state)
    assert not outcome.released
    assert outcome.final_text == HANDOFF_FALLBACK
    assert outcome.attempts == 2                   # capped


async def test_loop_latency_clean_turn_is_inline_safe():
    gate, state = make_gate()
    loop, _ = make_loop(gate, [])
    start = time.perf_counter()
    await loop.run("Happy to help with that.", [], state)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert elapsed_ms < 50.0                       # the L2 inline budget
