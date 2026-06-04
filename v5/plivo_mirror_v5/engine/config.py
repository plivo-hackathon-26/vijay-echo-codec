"""Engine configuration. One dataclass, plain defaults, no I/O at import.

``mode`` selects how the single observer routes verdicts — it never
changes detection behaviour:

- ``"shadow"``    — Deployable 1: telemetry only, ``action.taken="would_have"``.
- ``"intervene"`` — Deployable 2: verdicts at/above ``intervene_severity``
  trigger a correction / hold / handoff hook.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plivo_mirror_v5.engine.policy import PolicyPack

Mode = str  # "shadow" | "intervene"

# Default severity assigned to a firing L2 mismatch, by claim type.
_DEFAULT_SEVERITY_BY_CLAIM_TYPE = {
    "price": "high",
    "action": "high",       # speech-vs-action divergence
    "action_args": "high",  # tool args vs validated state
    "authorization": "high",  # tool fired without authorizing state fact
    "commitment": "high",   # unauthorized verbal commitment
    "disclosure": "med",    # required disclosure missing
    "persona": "med",       # persona drift / prompt leakage
    "policy": "med",
    "hours": "med",
    "fact": "med",
}


@dataclass
class EngineConfig:
    mode: Mode = "shadow"

    # Per-layer enable flags.
    enable_l1: bool = True
    enable_l2: bool = True
    enable_l3: bool = True

    # L1 — input integrity gate.
    asr_min_confidence: float = 0.6

    # L2 — severity of a firing deterministic mismatch, by claim type.
    severity_by_claim_type: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_SEVERITY_BY_CLAIM_TYPE)
    )
    default_severity: str = "med"

    # L3 — grounding NLI.
    l3_top_k: int = 3
    l3_contradicted_severity: str = "med"
    l3_unsupported_severity: str = "low"

    # Intervention routing (Deployable 2).
    intervene_severity: str = "high"

    # Latency budget: L2 is the only inline-safe detector; its overhead per
    # turn must stay under this (asserted in tests).
    l2_inline_budget_ms: float = 50.0

    # Data sources (paths to per-agent structured reference / prose KB).
    reference_path: str | None = None
    kb_path: str | None = None

    # L2 policy checks (arg bindings, authorization separation, commitments,
    # disclosures, persona). None → only the claims diff runs.
    policy: "PolicyPack | None" = None

    def severity_for(self, claim_type: str) -> str:
        return self.severity_by_claim_type.get(claim_type, self.default_severity)
