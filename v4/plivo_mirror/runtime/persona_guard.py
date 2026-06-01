"""Session / persona guard — keep the agent on-persona over a long call.

Tracks conversation length and caller tone. Signals when to re-inject a
system-prompt summary (drift defense) and when to escalate to a human
(tone breakdown or excessive length). All thresholds are code, not prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NEGATIVE_WORDS = (
    "angry",
    "ridiculous",
    "terrible",
    "useless",
    "frustrated",
    "frustrating",
    "unacceptable",
    "supervisor",
    "manager",
    "complaint",
    "lawyer",
    "sue",
    "awful",
    "horrible",
    "worst",
    "never again",
    "cancel my account",
)
_NEGATIVE_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _NEGATIVE_WORDS) + r")\b", re.I
)


@dataclass
class PersonaSignal:
    reinject: bool = False
    reinject_text: str = ""
    escalate: bool = False
    reason: str = ""


class PersonaGuard:
    def __init__(
        self,
        *,
        system_summary: str = "",
        reinject_every: int = 6,
        escalate_after: int = 20,
        negative_tone_threshold: int = 3,
    ) -> None:
        self._summary = system_summary
        self._reinject_every = max(0, reinject_every)
        self._escalate_after = max(0, escalate_after)
        self._neg_threshold = max(1, negative_tone_threshold)
        self._turns = 0
        self._negativity = 0

    def observe_turn(
        self, *, customer_text: str = "", agent_text: str = ""
    ) -> PersonaSignal:
        """Record one exchange and return what the runtime should do."""
        self._turns += 1
        hits = len(_NEGATIVE_RE.findall(customer_text or ""))
        self._negativity += hits

        reinject = bool(
            self._summary
            and self._reinject_every
            and self._turns % self._reinject_every == 0
        )
        escalate = False
        reason = ""
        if self._negativity >= self._neg_threshold:
            escalate, reason = True, "caller tone breakdown"
        elif self._escalate_after and self._turns >= self._escalate_after:
            escalate, reason = True, "conversation length exceeded threshold"

        return PersonaSignal(
            reinject=reinject,
            reinject_text=self._summary if reinject else "",
            escalate=escalate,
            reason=reason,
        )

    @property
    def turns(self) -> int:
        return self._turns


__all__ = ["PersonaGuard", "PersonaSignal"]
