"""Generic, domain-neutral intervention text templates.

The library deliberately ships no pizza/booking-specific text. These
are last-resort fallbacks used when the LLM correction generator
fails or times out, and they're written so they make sense for any
agent domain.
"""

from __future__ import annotations

DEFAULT_BUFFER = (
    "Sorry, let me make sure I got that right — just a moment..."
)

DEFAULT_FALLBACK_CORRECTION = (
    "Just to make sure I understood — could you say that one more time?"
)

DEFAULT_TOOL_BLOCK_CORRECTION = (
    "Hold on — before I do that, could you confirm one more time?"
)


def fallback_correction(verdict_evidence: dict) -> str:
    """Return a generic correction line that does NOT assume any domain.

    The LLM correction generator is the primary path; this exists only
    so the call never goes silent if the LLM fails.
    """
    intent = (verdict_evidence or {}).get("customer_intent")
    if intent:
        # Echo the customer's intent back — neutral phrasing that works
        # for any vertical.
        return f"Just to confirm — you'd like {intent.lower().rstrip('.')}, is that right?"
    return DEFAULT_FALLBACK_CORRECTION


__all__ = [
    "DEFAULT_BUFFER",
    "DEFAULT_FALLBACK_CORRECTION",
    "DEFAULT_TOOL_BLOCK_CORRECTION",
    "fallback_correction",
]
