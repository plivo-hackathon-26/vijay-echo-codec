"""Integration-style tests for the public Supervisor / CallSupervisor surface.

The other unit tests cover scorer, pregate, tool-gate, etc. in isolation.
This file exercises the composition — the path a real customer's code
takes — using a fake LLM and a fake TTS sink.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from plivo_mirror import (
    MirrorConfig,
    Supervisor,
    ToolCallIntent,
    Verdict,
)
from plivo_mirror.reports.sinks.memory import InMemoryReportSink


# ─────────────────── fakes ───────────────────────────────────────────────


class _FakeLLM:
    """Deterministic LLM stand-in with overridable verdict + chat reply."""

    def __init__(
        self,
        *,
        verdict_score: float = 0.0,
        verdict_reason: str = "ok",
        suggested_correction: str = "",
        blocked_tool: str = "",
        chat_reply: str = "Just to confirm — that's right?",
        # Optional override: a callable that returns the full verdict dict.
        responder=None,
        report_dict: dict | None = None,
    ):
        self.verdict_score = verdict_score
        self.verdict_reason = verdict_reason
        self.suggested_correction = suggested_correction
        self.blocked_tool = blocked_tool
        self.chat_reply = chat_reply
        self.responder = responder
        self.report_dict = report_dict
        self.structured_calls = 0
        self.chat_calls = 0

    async def structured_output(self, system_prompt, user_prompt=None, *, timeout_s=None):
        self.structured_calls += 1
        if self.responder is not None:
            return self.responder(system_prompt, user_prompt)
        # Detect whether this is the report generator call (it asks for
        # pattern_name + root_cause); if so return the report shape.
        if "pattern_name" in system_prompt and self.report_dict is not None:
            return dict(self.report_dict)
        return {
            "score": self.verdict_score,
            "reason": self.verdict_reason,
            "should_intervene": self.verdict_score >= 0.7,
            "suggested_correction": self.suggested_correction,
            "blocked_tool": self.blocked_tool,
            "evidence": {
                "customer_intent": "x",
                "violation_summary": "y",
            },
        }

    async def chat(self, system_prompt, user_prompt=None, *, timeout_s=None):
        self.chat_calls += 1
        return self.chat_reply


class _FakeTTSSink:
    """Records every speak/clear/checkpoint and emits checkpoint events."""

    def __init__(self):
        self.spoken: list[str] = []
        self.cleared = 0
        self.checkpoints_sent: list[str] = []
        self.precomputed = 0
        self.played_precomputed: list[str] = []

    async def clear_audio(self) -> None:
        self.cleared += 1

    async def speak(self, text: str, *, checkpoint: str | None = None) -> None:
        self.spoken.append(text)
        if checkpoint:
            self.checkpoints_sent.append(checkpoint)

    async def precompute(self, text: str) -> bytes | None:
        self.precomputed += 1
        return b"audio:" + text.encode()

    async def play_precomputed(self, audio: bytes, *, checkpoint: str | None = None) -> None:
        self.played_precomputed.append(audio.decode(errors="replace"))

    async def wait_checkpoint(self, name: str, *, timeout_s: float = 10.0) -> bool:
        return True


def _build_supervisor(
    llm: _FakeLLM,
    *,
    threshold: float = 0.7,
    cooldown_s: float = 10.0,
    tool_gate: bool = True,
    report_sink: Any | None = None,
) -> Supervisor:
    return Supervisor(
        MirrorConfig(
            llm=llm,
            policies=[
                "Latest preference wins on contradiction.",
                "Never confirm a refund — transfer to a human supervisor.",
            ],
            intervention_threshold=threshold,
            cooldown_s=cooldown_s,
            tool_gate_enabled=tool_gate,
            semantic_review_timeout_s=2.0,
        ),
        report_sink=report_sink,
    )


# ─────────────────── tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_and_speak_happy_path_uses_precomputed_audio():
    """When the scorer says OK, the precomputed TTS bytes should play
    and nothing should go through the intervention orchestrator."""
    llm = _FakeLLM(verdict_score=0.0)
    sup = _build_supervisor(llm)
    sink = _FakeTTSSink()

    async with sup.attach(tts_sink=sink) as call:
        call.bind_call("call-happy")
        # Push a high-stakes signal so the pregate forces the scorer to run.
        call.note_customer_turn("Actually wait, change that")
        outcome = await call.review_and_speak(
            customer_text="Actually wait, change that to a BLT",
            primary_text="Got it, one BLT coming up.",
        )

    assert outcome.intervened is False
    assert outcome.spoken_text == "Got it, one BLT coming up."
    # Precompute path must have been used (parallel TTS + scorer).
    assert sink.precomputed == 1
    assert sink.played_precomputed == ["audio:Got it, one BLT coming up."]
    # Intervention path must NOT have run.
    assert sink.cleared == 0
    assert sink.checkpoints_sent == []


@pytest.mark.asyncio
async def test_review_and_speak_intervenes_when_scorer_flags():
    """High-score verdict triggers the full intervention sequence
    (clear → buffer → correction)."""
    llm = _FakeLLM(
        verdict_score=0.95,
        verdict_reason="retracted item still in order",
        suggested_correction="Just to confirm — that was a BLT only, right?",
    )
    sup = _build_supervisor(llm)
    sink = _FakeTTSSink()

    async with sup.attach(tts_sink=sink) as call:
        call.bind_call("call-intervene")
        call.note_customer_turn("Club sandwich, actually a BLT")
        outcome = await call.review_and_speak(
            customer_text="Club sandwich, actually a BLT",
            primary_text="Got it, one club sandwich and one BLT.",
        )

    assert outcome.intervened is True
    assert outcome.verdict.should_intervene is True
    assert outcome.verdict.score >= 0.7
    # Orchestrator must have flushed and emitted both checkpoints.
    assert sink.cleared >= 1
    assert "mirror_buffer" in sink.checkpoints_sent
    assert "mirror_done" in sink.checkpoints_sent
    # The agent's planned text must NOT have been spoken.
    assert "Got it, one club sandwich and one BLT." not in sink.spoken
    # And the precomputed bytes were thrown away (no play_precomputed call).
    assert sink.played_precomputed == []
    # Suggested correction was spoken.
    assert any("BLT only" in s for s in sink.spoken)


@pytest.mark.asyncio
async def test_cooldown_short_circuits_next_turn():
    """After an intervention sets cooldown, the next turn skips even
    the pregate→scorer pipeline and returns in_cooldown."""
    llm = _FakeLLM(verdict_score=0.9, suggested_correction="confirm please?")
    sup = _build_supervisor(llm, cooldown_s=5.0)
    sink = _FakeTTSSink()

    async with sup.attach(tts_sink=sink) as call:
        call.bind_call("call-cooldown")
        call.note_customer_turn("Actually change that")
        # Turn 1 — intervenes, sets cooldown.
        first = await call.review_and_speak(
            customer_text="Actually change that to a BLT",
            primary_text="Got it, club and BLT.",
        )
        assert first.intervened is True
        scorer_calls_after_first = llm.structured_calls

        # Turn 2 — even with a triggering customer text, cooldown wins.
        call.note_customer_turn("Actually cancel everything")
        second = await call.review_and_speak(
            customer_text="Actually cancel everything",
            primary_text="Sure, I'll cancel.",
        )
        assert second.intervened is False
        assert second.verdict.reason == "in_cooldown"
        # No new scorer LLM call should have happened on turn 2.
        assert llm.structured_calls == scorer_calls_after_first


@pytest.mark.asyncio
async def test_gate_tool_call_blocks_irreversible_tool():
    """Tool-gate returns intervene; CallSupervisor surfaces the verdict
    so the customer's agent can skip the tool."""
    llm = _FakeLLM(
        verdict_score=0.99,
        verdict_reason="tool args don't match customer intent",
        blocked_tool="place_order",
        suggested_correction="Hold on — was that a BLT only?",
    )
    sup = _build_supervisor(llm)
    sink = _FakeTTSSink()

    async with sup.attach(tts_sink=sink) as call:
        call.bind_call("call-gate")
        call.note_customer_turn("Club sandwich, actually a BLT")
        verdict = await call.gate_tool_call(
            customer_text="Club sandwich, actually a BLT",
            intents=[
                ToolCallIntent(
                    name="place_order",
                    args={"items": ["club sandwich", "BLT"]},
                    irreversible=True,
                ),
            ],
        )

    assert verdict.should_intervene is True
    assert verdict.blocked_tool == "place_order"
    assert "BLT only" in verdict.suggested_correction


@pytest.mark.asyncio
async def test_run_supervised_loop_blocks_on_tool_gate():
    """The supervised-loop helper must NOT execute the tool when the
    tool-gate says intervene. Instead it returns blocked=True with the
    correction text already spoken."""

    class _FakeOpenAITool:
        def __init__(self, name, args_json, tc_id):
            self.id = tc_id
            self.type = "function"
            self.function = type("F", (), {"name": name, "arguments": args_json})()

    class _FakeOpenAIMessage:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class _FakeOpenAIChoice:
        def __init__(self, msg):
            self.message = msg

    class _FakeOpenAIResp:
        def __init__(self, msg):
            self.choices = [_FakeOpenAIChoice(msg)]

    class _FakeCompletions:
        def __init__(self, msg):
            self._msg = msg
            self.calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            return _FakeOpenAIResp(self._msg)

    class _FakeChat:
        def __init__(self, msg):
            self.completions = _FakeCompletions(msg)

    class _FakeOpenAIClient:
        def __init__(self, msg):
            self.chat = _FakeChat(msg)

    # The LLM picks a place_order on the first turn; tool-gate blocks it.
    agent_msg = _FakeOpenAIMessage(
        content=None,
        tool_calls=[
            _FakeOpenAITool(
                "place_order",
                '{"items": ["club sandwich", "BLT"]}',
                "tc_1",
            )
        ],
    )
    openai_client = _FakeOpenAIClient(agent_msg)

    # Mirror's scorer/tool-gate verdict says intervene.
    llm = _FakeLLM(
        verdict_score=0.99,
        suggested_correction="Got it — that's a BLT only, right?",
        blocked_tool="place_order",
    )
    sup = _build_supervisor(llm)
    sink = _FakeTTSSink()
    executor_calls = []

    def _exec_place_order(args):
        executor_calls.append(args)
        return {"status": "placed", "order_id": "BAD"}

    async with sup.attach(tts_sink=sink) as call:
        call.bind_call("call-loop")
        call.note_customer_turn("Club sandwich, actually a BLT")
        result = await call.run_supervised_loop(
            llm_client=openai_client,
            model="gpt-fake",
            system_prompt="You take sandwich orders.",
            tool_specs=[
                {
                    "type": "function",
                    "function": {"name": "place_order", "parameters": {}},
                }
            ],
            tool_executors={"place_order": _exec_place_order},
            customer_text="Club sandwich, actually a BLT",
            irreversible=("place_order",),
        )

    assert result.blocked is True
    assert result.block_verdict is not None
    assert result.block_verdict.blocked_tool == "place_order"
    # Tool MUST NOT have fired.
    assert executor_calls == []
    # The intervention orchestrator spoke the correction.
    assert any("BLT only" in s for s in sink.spoken)


@pytest.mark.asyncio
async def test_aclose_triggers_report_generation_when_sink_wired():
    """When a report_sink is on the Supervisor and at least one turn
    intervened, aclose() should fire the ReportGenerator and persist
    a FailureReport."""
    llm = _FakeLLM(
        verdict_score=0.92,
        verdict_reason="retracted item still in order",
        suggested_correction="Just to confirm — BLT only, right?",
        report_dict={
            "pattern_name": "retracted_item",
            "severity": "high",
            "summary": "Agent captured retracted item in order.",
            "root_cause": "System prompt instructs the agent to capture every mentioned item regardless of correction markers.",
            "proposed_fix_text": "Change SYSTEM_PROMPT so the LATEST preference wins on contradiction.",
            "proposed_file": "agent.py",
            "suggested_diff": "- capture every item\n+ latest preference wins",
            "confidence": 0.9,
        },
    )
    sink = InMemoryReportSink()
    sup = _build_supervisor(llm, report_sink=sink)
    tts = _FakeTTSSink()

    async with sup.attach(tts_sink=tts) as call:
        call.bind_call("call-report")
        call.note_customer_turn("Club, actually BLT")
        await call.review_and_speak(
            customer_text="Club, actually BLT",
            primary_text="Got it, one club and one BLT.",
        )

    rows = await sink.list()
    assert len(rows) == 1
    report = rows[0]
    assert report.pattern_name == "retracted_item"
    assert report.severity == "high"
    assert report.proposed_file == "agent.py"
    assert 0.0 <= report.confidence <= 1.0


@pytest.mark.asyncio
async def test_streaming_scorer_resets_between_turns_without_explicit_flush():
    """After a streaming verdict lands, the next stream on the same
    CallSupervisor must start fresh — the caller should NOT need to
    call flush_stream() to clear state."""
    llm = _FakeLLM(verdict_score=0.0)
    sup = _build_supervisor(llm)
    sink = _FakeTTSSink()

    async with sup.attach(tts_sink=sink) as call:
        call.bind_call("call-stream")
        # First stream — feed enough text to cross the boundary.
        v1 = None
        for delta in ["Hello, ", "I'll process that refund for you ", "right away. ", "Anything else?"]:
            v = await call.review_stream_delta(
                customer_text="I want a refund",
                delta=delta,
            )
            if v is not None:
                v1 = v
                break
        assert v1 is not None, "first stream should fire a verdict"

        # Second stream — feed brand-new text. If the StreamingScorer
        # didn't reset, this would return None forever.
        v2 = None
        for delta in ["Sure, that ", "comes to nineteen dollars. ", "Anything else?"]:
            v = await call.review_stream_delta(
                customer_text="ok",
                delta=delta,
            )
            if v is not None:
                v2 = v
                break
        assert v2 is not None, "streaming scorer must auto-reset between turns"


@pytest.mark.asyncio
async def test_review_only_api_never_speaks():
    """Supervisor.review (detection-only) must not touch any TTS — it
    returns a Verdict and that's it."""
    llm = _FakeLLM(verdict_score=0.95, suggested_correction="confirm BLT?")
    sup = _build_supervisor(llm)

    verdict = await sup.review(
        customer_text="Actually change that to a BLT",
        primary_text="Got it, club and BLT.",
    )
    assert verdict.should_intervene is True
    # No CallSupervisor was attached → no sink → no speak ever happened.
    # The contract is that review() is a pure function. Nothing to assert
    # beyond returning the verdict, but make sure it didn't crash.
    assert isinstance(verdict, Verdict)
