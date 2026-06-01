"""Runtime — per-turn control loop and the cross-cutting concerns
(state grounding, intent memory, persona guard, escalation)."""

from __future__ import annotations

from plivo_mirror.runtime.escalation import HandoffContext, build_handoff
from plivo_mirror.runtime.grounding import (
    build_grounding_block,
    compose_injection,
    intent_note_block,
    persona_reinject_block,
)
from plivo_mirror.runtime.intent_memory import IntentMemory
from plivo_mirror.runtime.loop import review_turn
from plivo_mirror.runtime.persona_guard import PersonaGuard, PersonaSignal

__all__ = [
    "review_turn",
    "build_grounding_block",
    "intent_note_block",
    "persona_reinject_block",
    "compose_injection",
    "IntentMemory",
    "PersonaGuard",
    "PersonaSignal",
    "HandoffContext",
    "build_handoff",
]
