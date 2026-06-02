"""Agent-voice correction builder.

When the speech guard suppresses a risky span, it substitutes a grounded,
agent-voice line the TTS can speak directly. These are deliberately
generic and domain-agnostic — they buy a beat to re-ground without
asserting any new fact or commitment.
"""

from __future__ import annotations

from plivo_mirror.guards.risk_spans import RiskSpan

_BY_KIND = {
    "commitment": (
        "I want to make sure I get this exactly right — let me confirm that "
        "before I commit to anything."
    ),
    "price": "Let me double-check that figure before I give you a number.",
    "number": "Let me double-check that figure before I give you a number.",
    "percent": "Let me double-check that figure before I give you a number.",
    "name": "Let me just confirm those details before I continue.",
    "semantic": (
        "Let me make sure I've got your request exactly right before I "
        "confirm that."
    ),
}

_DEFAULT = "Let me make sure I have that right before I say more."


def correction_for_spans(spans: list[RiskSpan]) -> str:
    """Pick an agent-voice correction by the highest-liability flagged
    span kind (commitment > price/number/percent > name > semantic)."""
    kinds = {s.kind for s in spans}
    for kind in ("commitment", "price", "number", "percent", "name", "semantic"):
        if kind in kinds:
            return _BY_KIND[kind]
    return _DEFAULT


def default_block_correction() -> str:
    """Agent-voice line for a hard deterministic block (forbidden phrase /
    missing required disclosure) when the policy didn't supply one."""
    return (
        "I'm not able to confirm that here — let me make sure we handle it "
        "the right way for you."
    )


# Agent-voice lines for an action-boundary block, by why it was blocked.
_RECONFIRM = {
    "mismatch": (
        "Before I do that — let me make sure I have your request exactly "
        "right. Could you confirm that for me?"
    ),
    "authz": (
        "I'm not able to authorize that on this call — let me confirm the "
        "details or get the right person to help you."
    ),
    "policy": (
        "I want to be sure that's within what I'm able to do — let me "
        "double-check before I go ahead."
    ),
    "incomplete": "Let me actually take care of that for you right now — one moment.",
}


def reconfirm_correction(kind: str = "mismatch") -> str:
    """Agent-voice correction for an action-guard block. ``kind`` is one of
    ``mismatch`` / ``authz`` / ``policy`` / ``incomplete``."""
    return _RECONFIRM.get(kind, _RECONFIRM["mismatch"])


__all__ = [
    "correction_for_spans",
    "default_block_correction",
    "reconfirm_correction",
]
