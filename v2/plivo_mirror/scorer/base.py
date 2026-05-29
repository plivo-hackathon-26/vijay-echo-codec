"""Scorer protocol — the detection layer.

v1 ships a single LLMScorer implementation. The protocol exists so
test suites can substitute a deterministic scorer and so v2 / users
can plug in alternatives.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from plivo_mirror.context import SupervisorContext, TurnPayload, Verdict


@runtime_checkable
class Scorer(Protocol):
    async def score(self, turn: TurnPayload, ctx: SupervisorContext) -> Verdict: ...


__all__ = ["Scorer"]
