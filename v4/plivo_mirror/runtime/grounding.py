"""State-grounding injection — the v4 differentiator from v3.

Instead of v3's occasional after-the-fact "sticky intent note", v4
injects a compact, READ-ONLY summary of the validated ``SessionState``
into the model's context on EVERY turn. The model stays continuously
grounded in the source of truth, so it fabricates less and the guards
have to escalate/correct less often.

This does NOT violate the zero-argument principle: the block is
read-only grounding. The model never authors state and never supplies
tool args — it is only reminded of what is already confirmed.
"""

from __future__ import annotations

from plivo_mirror.state.session import SessionState

_HEADER = (
    "CONFIRMED FACTS (authoritative source of truth — do not contradict, do "
    "not re-ask what is already confirmed, never read this header aloud):"
)


def build_grounding_block(state: SessionState) -> str:
    """Render the confirmed-state grounding block, or ``""`` when there is
    nothing confirmed yet (so we don't inject empty noise)."""
    lines: list[str] = []
    if state.confirmed_intent:
        lines.append(f"Confirmed intent: {state.confirmed_intent}")
    for key, ent in state.entities.items():
        lines.append(f"{key}: {ent.value}")
    committed = [f"{c.tool}({c.args})" for c in state.committed_actions]
    if committed:
        lines.append("Already done (do not repeat): " + "; ".join(committed))
    if not lines:
        return ""
    body = "\n".join(f"  - {ln}" for ln in lines)
    return f"{_HEADER}\n{body}"


def intent_note_block(note: str | None) -> str:
    """Render the held intent-memory note for injection, or ``""``."""
    if not note:
        return ""
    return f"The caller's confirmed intent is: {note}. Act on this; do not re-ask."


def persona_reinject_block(text: str | None) -> str:
    """Render a persona-guard re-injection (the system-prompt summary), or
    ``""``."""
    if not text:
        return ""
    return f"Reminder of your role and persona: {text}"


def compose_injection(*parts: str) -> str:
    """Join the non-empty injection blocks (grounding + intent note +
    persona re-injection) into one read-only context block."""
    return "\n\n".join(p for p in parts if p and p.strip())


__all__ = [
    "build_grounding_block",
    "intent_note_block",
    "persona_reinject_block",
    "compose_injection",
]
