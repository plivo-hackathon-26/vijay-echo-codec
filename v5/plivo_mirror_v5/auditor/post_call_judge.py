"""Layer-4 — post-call LLM-judge auditor. INTERFACE + STUB ONLY in v5.

The LLM-judge is strictly OFFLINE: a post-call recall backstop (catch what
L2/L3 missed) and a labeling source for the eval loop. It is NEVER in the
inline path — the voice latency budget and the false-alarm budget both
forbid it.

# TODO: full implementation (post-v5) — grounded-entailment judge over the
# stored call timeline + evidence, emitting label suggestions keyed by
# verdict_id for the labeling loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class AuditFinding:
    """A judge-proposed miss or mislabel, keyed to stored telemetry."""

    call_id: str
    turn_id: str
    kind: str                      # "missed_failure" | "false_alarm" | "label"
    rationale: str
    verdict_id: str | None = None  # set when the finding re-labels a verdict
    extra: dict = field(default_factory=dict)


@runtime_checkable
class PostCallJudge(Protocol):
    """Audits one completed call (as returned by ``CallStore.get_call``)."""

    def audit_call(self, call: dict) -> list[AuditFinding]: ...


class StubPostCallJudge:
    """v5 placeholder: audits nothing, returns nothing. Exists so the
    auditor wiring point is real and typed before the model lands."""

    def audit_call(self, call: dict) -> list[AuditFinding]:
        return []
