"""The detection engine — the shared core both deployables run.

Three layers in strict precedence + arbitration:

- L1 input integrity — a gate, not a detector
- L2 deterministic diff — PRIMARY: claim vs session state / reference / tool log
- L3 claim-grounding NLI — SECONDARY: prose claims vs the unstructured KB

Deterministic wins: L3 fires only on claims outside L2 jurisdiction.
"""

from plivo_mirror_v5.engine.config import EngineConfig
from plivo_mirror_v5.engine.engine import Engine
from plivo_mirror_v5.engine.kb_retriever import (
    FakeKBRetriever,
    KBChunk,
    KBRetriever,
    KeywordKBRetriever,
)
from plivo_mirror_v5.engine.reference import ReferenceStore
from plivo_mirror_v5.engine.session_state import SessionState, StateSnapshot
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
    "Engine",
    "EngineConfig",
    "Evidence",
    "FakeKBRetriever",
    "KBChunk",
    "KBRetriever",
    "KeywordKBRetriever",
    "ReferenceStore",
    "SessionState",
    "StateSnapshot",
    "TurnInput",
    "TurnResult",
    "Verdict",
    "severity_at_least",
]
