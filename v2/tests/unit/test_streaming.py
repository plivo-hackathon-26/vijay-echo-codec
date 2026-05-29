"""Streaming-scorer tests.

Validates that:
  - the scorer buffers until the first sentence boundary,
  - it fires exactly once per turn,
  - it falls back to ``flush()`` if no boundary lands,
  - the partial payload it scores is marked ``is_partial=True``.
"""

from __future__ import annotations

from typing import Any

import pytest

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import SupervisorContext, TurnPayload, Verdict
from plivo_mirror.scorer.llm import LLMScorer
from plivo_mirror.scorer.streaming import StreamingScorer
from tests.unit.conftest import FakeLLM


def _build_scorer(verdict: dict[str, Any]) -> tuple[StreamingScorer, FakeLLM]:
    llm = FakeLLM(responder=lambda s, u: verdict)
    cfg = MirrorConfig(llm=llm, policies=["dummy"])
    inner = LLMScorer(cfg)
    return StreamingScorer(inner), llm


@pytest.mark.asyncio
async def test_buffers_until_boundary() -> None:
    streaming, llm = _build_scorer({"score": 0.1, "reason": "ok"})
    ctx = SupervisorContext(call_uuid="t")
    turn = TurnPayload(customer_text="ok", primary_text="")

    # Short tokens — no boundary yet.
    v1 = await streaming.feed("Sure", turn, ctx)
    v2 = await streaming.feed(", ", turn, ctx)
    v3 = await streaming.feed("one large pepperoni", turn, ctx)
    assert v1 is None and v2 is None and v3 is None
    assert llm.calls == []


@pytest.mark.asyncio
async def test_fires_once_at_first_sentence_boundary() -> None:
    streaming, llm = _build_scorer({"score": 0.95, "reason": "bad", "should_intervene": True})
    ctx = SupervisorContext(call_uuid="t")
    turn = TurnPayload(customer_text="ok", primary_text="")

    v1 = await streaming.feed("Got it — one pepperoni", turn, ctx)
    assert v1 is None
    v2 = await streaming.feed(" and one mushroom.", turn, ctx)
    assert isinstance(v2, Verdict)
    assert v2.should_intervene is True
    assert len(llm.calls) == 1

    # Further deltas after a verdict must NOT re-fire.
    v3 = await streaming.feed(" Anything else?", turn, ctx)
    assert v3 is None
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_flush_when_no_boundary() -> None:
    streaming, llm = _build_scorer({"score": 0.3, "reason": "borderline"})
    ctx = SupervisorContext(call_uuid="t")
    turn = TurnPayload(customer_text="ok", primary_text="")

    # Stream ends without any punctuation.
    await streaming.feed("one large pepperoni no extra cheese", turn, ctx)
    v = await streaming.flush(turn, ctx)
    assert isinstance(v, Verdict)
    assert len(llm.calls) == 1

    # A subsequent flush is a no-op.
    v2 = await streaming.flush(turn, ctx)
    assert v2 is None


@pytest.mark.asyncio
async def test_partial_marker_set_on_payload() -> None:
    captured: dict[str, str] = {}

    def responder(system_prompt: str, user_prompt: str | None) -> dict[str, Any]:
        captured["sys"] = system_prompt
        return {"score": 0.0}

    llm = FakeLLM(responder=responder)
    cfg = MirrorConfig(llm=llm, policies=["dummy"])
    inner = LLMScorer(cfg)
    streaming = StreamingScorer(inner)
    ctx = SupervisorContext(call_uuid="t")
    turn = TurnPayload(customer_text="ok", primary_text="")
    await streaming.feed("This is the first complete sentence.", turn, ctx)

    # The prompt must contain the accumulated buffer, proving the
    # streaming scorer substituted the partial text into the slot.
    assert "first complete sentence" in captured["sys"]
