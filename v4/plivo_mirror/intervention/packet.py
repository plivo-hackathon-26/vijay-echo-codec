"""Grounded correction packet.

Built from ``{state, violation, policy}`` and framed around the CORRECT
facts + the rule. **Pink-elephant guarantee:** the packet must never
restate the flagged (wrong) value — repeating it both risks re-voicing it
and primes the model to say it again. ``assert_no_echo`` enforces this.

The packet is delivered to the main LLM as a SYSTEM/developer message
(see ``regenerate.py``) — never as a synthesized customer turn.
"""

from __future__ import annotations

from dataclasses import dataclass

from plivo_mirror.contracts import Verdict
from plivo_mirror.state.session import SessionState


def echoes(text: str, span: str) -> bool:
    """True if ``text`` contains the flagged span (case-insensitive)."""
    s = (span or "").strip().lower()
    return bool(s) and s in (text or "").lower()


def assert_no_echo(text: str, span: str) -> None:
    """Pink-elephant guard: raise if ``text`` restates the flagged span."""
    if echoes(text, span):
        raise ValueError(f"correction packet/answer echoed the flagged span {span!r}")


@dataclass
class CorrectionPacket:
    violation_reason: str
    policy_id: str | None
    policy_text: str | None
    flagged_span: str
    facts: dict[str, str]
    confirmed_intent: str | None = None

    def as_developer_message(self) -> str:
        """Render the packet for the main LLM as a developer/system
        instruction — built around the CORRECT facts + the rule, with the
        flagged value deliberately ABSENT (pink-elephant)."""
        facts = "\n".join(f"  - {k}: {v}" for k, v in self.facts.items()) or "  (none)"
        if self.policy_text:
            rule = f"policy [{self.policy_id}]: {self.policy_text}"
        elif self.policy_id:
            rule = f"policy [{self.policy_id}]"
        else:
            rule = "the stated policies"
        intent = (
            f"\nThe caller actually wants: {self.confirmed_intent}."
            if self.confirmed_intent
            else ""
        )
        msg = (
            "CORRECTION REQUIRED (internal — never read aloud). Your previous "
            "reply was not grounded and has been withheld. Produce a corrected "
            "reply that:\n"
            f"  - is supported ONLY by these CONFIRMED FACTS:\n{facts}\n"
            f"  - complies with {rule}\n"
            "  - does NOT repeat or reference the withheld claim."
            f"{intent}\n"
            "Reply in the agent's normal voice, concise."
        )
        # NOTE: the pink-elephant guarantee is enforced on the SPOKEN ANSWER
        # (``engine._reverify`` rejects + regenerates if the answer echoes the
        # span) — NOT on this internal developer message. We deliberately do
        # NOT ``assert_no_echo`` here: the message is built only from the
        # CORRECT facts/intent, but the flagged span can be a short token
        # (a digit, "$24") that legitimately appears inside a correct fact
        # value — asserting against it crashed regeneration on live turns.
        return msg


def build_packet(verdict: Verdict, state: SessionState) -> CorrectionPacket:
    """Assemble a correction packet from the violating verdict + state.
    The violation *reason* is intentionally NOT placed into the developer
    message (it may contain the flagged value); only correct facts + the
    rule are."""
    policy_text = None
    if verdict.policy_id:
        for p in state.compiled_policies:
            if p.id == verdict.policy_id:
                policy_text = p.text
                break
    return CorrectionPacket(
        violation_reason=verdict.reason,
        policy_id=verdict.policy_id,
        policy_text=policy_text,
        flagged_span=verdict.span,
        facts={k: str(e.value) for k, e in state.entities.items()},
        confirmed_intent=state.confirmed_intent,
    )


__all__ = ["CorrectionPacket", "build_packet", "assert_no_echo", "echoes"]
