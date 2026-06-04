"""v5 core data types — everything that flows through the engine is one of
these. Pure data; no behaviour beyond small construction helpers.

Claim convention (the dicts in ``TurnInput.claims``)
----------------------------------------------------
Claims are extracted upstream (by the integration's claim extractor, or
supplied directly by eval fixtures). Each claim dict uses these keys:

- ``claim_id``     stable id within the turn (``"c1"``, ``"c2"``, …)
- ``claim_type``   ``"price" | "policy" | "hours" | "action" | "fact" |
                   "correction" | …``
- ``spoken_value`` the value the speaker asserted (str / number / None)
- ``ref``          the structured referent, if any. Namespaced:
                   ``session.<dotted.path>`` — runtime per-call fact,
                   ``reference.<dotted.key>`` — static structured data,
                   ``tool.<tool_name>``      — claimed action vs tool log.
                   ``None`` / missing → free-form prose; L3 jurisdiction.
- ``text``         the raw spoken span (optional; L3 uses it as the NLI
                   hypothesis when present)

A user-turn claim of type ``"correction"`` is a readback correction: L1
writes ``spoken_value`` into session state at ``ref``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

# Severity scale, weakest → strongest. Used for ordering comparisons.
SEVERITIES = ("info", "low", "med", "high")


def severity_at_least(severity: str, floor: str) -> bool:
    """True when ``severity`` is at or above ``floor`` on the scale."""
    return SEVERITIES.index(severity) >= SEVERITIES.index(floor)


def new_verdict_id() -> str:
    """Stable uuid for a verdict; the labeling loop keys on this later."""
    return f"v-{uuid.uuid4().hex[:12]}"


@dataclass
class Evidence:
    """The explainable payload behind a verdict — the product
    differentiator. The monitoring frontend renders this verbatim."""

    claim_type: str                 # "price" | "policy" | "hours" | "action" | "fact" | ...
    spoken_value: str | None
    truth_value: str | None
    source: str | None              # "session.order.total" | "reference.menu#A12" | "kb#chunk_77"
    extra: dict = field(default_factory=dict)


@dataclass
class Verdict:
    """One detector's decision about one claim (or, for L1, one turn)."""

    verdict_id: str                 # stable uuid; used later by the labeling loop
    detector: str                   # "L1" | "L2" | "L3"
    fired: bool
    severity: str                   # "info" | "low" | "med" | "high"
    latency_ms: float
    evidence: Evidence | None = None
    suppressed_by: list[str] = field(default_factory=list)

    @property
    def claim_id(self) -> str | None:
        """The claim this verdict is about, if claim-scoped."""
        if self.evidence is None:
            return None
        return self.evidence.extra.get("claim_id")


@dataclass
class Action:
    """What was (or would have been) done about a turn's verdicts."""

    taken: str                      # "none" | "alert" | "correct" | "hold" | "handoff" | "would_have"
    hook: str | None = None        # "A" | "B"
    correction_text: str | None = None


@dataclass
class TurnInput:
    """One conversation turn as the engine sees it."""

    turn_id: str
    call_id: str
    turn_index: int
    role: str                       # "user" | "agent"
    transcript: str
    asr_confidence: float | None = None
    claims: list[dict] = field(default_factory=list)   # extracted intent/claims L2/L3 operate on
    tool_calls: list[dict] = field(default_factory=list)  # {name, args, result, t_result}


@dataclass
class TurnResult:
    """The engine's full output for one turn. The deployables route this —
    the engine itself never emits telemetry or takes actions."""

    turn_id: str
    call_id: str
    turn_index: int
    role: str
    transcript: str
    asr_confidence: float | None
    state_snapshot_id: str          # which state version L2 diffed against (diff-timing audit)
    verdicts: list[Verdict]
    action: Action | None = None

    @property
    def fired_verdicts(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.fired and not v.suppressed_by]

    def max_severity(self) -> str | None:
        """Highest severity among fired, unsuppressed verdicts."""
        fired = self.fired_verdicts
        if not fired:
            return None
        return max(fired, key=lambda v: SEVERITIES.index(v.severity)).severity
