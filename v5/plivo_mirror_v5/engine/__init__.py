"""The detection engine — the shared core both deployables run.

Two deterministic layers + arbitration; everything beyond structured
truth belongs to the grounded LLM judge (inline via Hook B's gate,
post-call via the auditor):

- L1 input integrity — a gate, not a detector
- L2 deterministic diff — the µs floor: claim vs session state /
  reference / tool log

Deterministic wins: arbitration suppresses any same-claim verdict from a
weaker detector.
"""

from plivo_mirror_v5.engine.config import EngineConfig
from plivo_mirror_v5.engine.engine import Engine
from plivo_mirror_v5.engine.gate import AssertivenessGate, GateResult
from plivo_mirror_v5.engine.policy import CommitmentRule, DisclosureRule, PolicyPack
from plivo_mirror_v5.engine.reference import ReferenceStore
from plivo_mirror_v5.engine.session_state import SessionState, StateSnapshot
from plivo_mirror_v5.engine.tool_gate import ToolDecision, ToolGate
from plivo_mirror_v5.engine.verdict import (
    Action,
    Evidence,
    TurnInput,
    TurnResult,
    Verdict,
    severity_at_least,
)

__all__ = [
    "Action",
    "AssertivenessGate",
    "CommitmentRule",
    "GateResult",
    "DisclosureRule",
    "Engine",
    "EngineConfig",
    "Evidence",
    "PolicyPack",
    "ReferenceStore",
    "SessionState",
    "StateSnapshot",
    "ToolDecision",
    "ToolGate",
    "TurnInput",
    "TurnResult",
    "Verdict",
    "severity_at_least",
]
