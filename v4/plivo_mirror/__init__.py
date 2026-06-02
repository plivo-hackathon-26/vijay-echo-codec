"""plivo-mirror v4 — real-time dual-boundary policy firewall for LLM
voice agents.

Public surface (Phase 1): the locked contracts, the session state store,
the entity validators, and the policy compiler. Guards, the grounded
verifier, the runtime loop, and the LiveKit adapter land in later phases.
"""

from __future__ import annotations

from plivo_mirror.contracts import (
    Decision,
    Guard,
    HistoryTurn,
    Policy,
    ToolCallIntent,
    TurnContext,
    Verdict,
)
from plivo_mirror.authz.service import (
    AuthorizationService,
    AuthzDecision,
    Requirement,
    RuleBasedAuthorizationService,
    requires_entity,
)
from plivo_mirror.firewall import Firewall
from plivo_mirror.guards.action import ActionGuard, Validator
from plivo_mirror.guards.risk_spans import RiskSpan, SpanKind, tag_risk_spans
from plivo_mirror.guards.signal import (
    ConfidenceSignal,
    FixedConfidence,
    LogprobEntropySignal,
)
from plivo_mirror.guards.semantic import (
    NLICrossEncoderSignal,
    NoSemanticSignal,
    SemanticResult,
    SemanticSignal,
)
from plivo_mirror.guards.speech import SpeechGuard
from plivo_mirror.intervention.correction import (
    correction_for_spans,
    default_block_correction,
    reconfirm_correction,
)
from plivo_mirror.intervention.engine import (
    InterventionResult,
    run_intervention,
    template_corrected_reply,
)
from plivo_mirror.intervention.packet import (
    CorrectionPacket,
    assert_no_echo,
    build_packet,
)
from plivo_mirror.intervention.regenerate import LLMReplyGenerator, ReplyGenerator
from plivo_mirror.policy.compiler import compile_policies, compile_policy
from plivo_mirror.runtime.escalation import HandoffContext, build_handoff
from plivo_mirror.runtime.grounding import build_grounding_block
from plivo_mirror.runtime.intent_memory import IntentMemory
from plivo_mirror.runtime.loop import review_turn
from plivo_mirror.runtime.persona_guard import PersonaGuard, PersonaSignal
from plivo_mirror.state.entities import (
    EntityKind,
    ValidatedEntity,
    validate,
    validate_amount,
    validate_date,
    validate_item,
    validate_name,
)
from plivo_mirror.state.extract import (
    CaptureRule,
    EntityExtractor,
    RegexEntityExtractor,
)
from plivo_mirror.state.session import CommittedAction, SessionState, args_from_state
from plivo_mirror.verifier.base import GroundingEvidence, Verifier, VerifierResult
from plivo_mirror.verifier.llm_judge import LLMJudgeVerifier

__version__ = "0.4.0rc1"

__all__ = [
    "__version__",
    # contracts
    "Decision",
    "Guard",
    "HistoryTurn",
    "Policy",
    "ToolCallIntent",
    "TurnContext",
    "Verdict",
    # state
    "SessionState",
    "CommittedAction",
    "args_from_state",
    "EntityExtractor",
    "RegexEntityExtractor",
    "CaptureRule",
    "EntityKind",
    "ValidatedEntity",
    "validate",
    "validate_amount",
    "validate_date",
    "validate_item",
    "validate_name",
    # policy
    "compile_policies",
    "compile_policy",
    # intervention / regeneration (Phase B)
    "InterventionResult",
    "run_intervention",
    "template_corrected_reply",
    "CorrectionPacket",
    "build_packet",
    "assert_no_echo",
    "LLMReplyGenerator",
    "ReplyGenerator",
    # facade + runtime (Phase 4)
    "Firewall",
    "review_turn",
    "build_grounding_block",
    "IntentMemory",
    "PersonaGuard",
    "PersonaSignal",
    "HandoffContext",
    "build_handoff",
    # action guard + authz (Phase 3)
    "ActionGuard",
    "Validator",
    "AuthorizationService",
    "AuthzDecision",
    "Requirement",
    "RuleBasedAuthorizationService",
    "requires_entity",
    "reconfirm_correction",
    # speech guard (Phase 2)
    "SpeechGuard",
    "tag_risk_spans",
    "RiskSpan",
    "SpanKind",
    "ConfidenceSignal",
    "LogprobEntropySignal",
    "FixedConfidence",
    "SemanticSignal",
    "SemanticResult",
    "NLICrossEncoderSignal",
    "NoSemanticSignal",
    "correction_for_spans",
    "default_block_correction",
    # verifier (Phase 2)
    "Verifier",
    "VerifierResult",
    "GroundingEvidence",
    "LLMJudgeVerifier",
]
