"""Escalation — warm handoff with context delivery enforced in CODE.

When the runtime escalates, the context that accompanies the transfer is
assembled deterministically from ``SessionState`` (never from the model,
which could omit or fabricate). A human (or the next system) always
receives the confirmed intent, validated entities, and what was already
done — so the caller never has to start over.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from plivo_mirror.state.session import SessionState


@dataclass
class HandoffContext:
    call_id: str
    reason: str
    confirmed_intent: str | None = None
    entities: dict[str, str] = field(default_factory=dict)
    committed_actions: list[str] = field(default_factory=list)
    transcript_summary: str = ""

    def as_briefing(self) -> str:
        """A short human-readable briefing for the receiving agent."""
        lines = [f"Transfer reason: {self.reason}", f"Call: {self.call_id}"]
        if self.confirmed_intent:
            lines.append(f"Caller wants: {self.confirmed_intent}")
        if self.entities:
            lines.append(
                "Confirmed details: "
                + ", ".join(f"{k}={v}" for k, v in self.entities.items())
            )
        if self.committed_actions:
            lines.append("Already done: " + "; ".join(self.committed_actions))
        if self.transcript_summary:
            lines.append(f"Summary: {self.transcript_summary}")
        return "\n".join(lines)


def build_handoff(
    state: SessionState, reason: str, *, transcript_summary: str = ""
) -> HandoffContext:
    """Assemble the warm-handoff context from state. Always populated from
    the source of truth — this is the code-enforced part."""
    return HandoffContext(
        call_id=state.call_id,
        reason=reason,
        confirmed_intent=state.confirmed_intent,
        entities={k: str(e.value) for k, e in state.entities.items()},
        committed_actions=[f"{c.tool}({c.args})" for c in state.committed_actions],
        transcript_summary=transcript_summary,
    )


__all__ = ["HandoffContext", "build_handoff"]
