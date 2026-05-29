"""Tier 0 protocol + result type.

Every Tier 0 check is a pure function from (turn, ctx) → Tier0Result.
The MirrorJudge orchestrator runs them sequentially; the first one that
returns a non-None verdict short-circuits the rest.

Order matters: cheapest + most precise checks go first. The default
orchestrator order is documented in MirrorJudge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from plivo_mirror.context import SupervisorContext, TurnPayload, Verdict


@dataclass
class Tier0Result:
    """Result of a single Tier 0 check.

    Attributes:
        verdict: Hard verdict if this check fired. None means the check
            had nothing to say — the orchestrator continues to the
            next check (or to Tier 1).
        check_name: Identifier of the check, useful for telemetry.
        evidence: Free-form dict explaining what the check matched on.
    """

    verdict: Verdict | None
    check_name: str
    evidence: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Tier0Check(Protocol):
    """Synchronous deterministic check. No I/O permitted."""

    name: str

    def evaluate(
        self, turn: TurnPayload, ctx: SupervisorContext
    ) -> Tier0Result: ...


__all__ = ["Tier0Check", "Tier0Result"]
