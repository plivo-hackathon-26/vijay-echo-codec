"""Intent memory — hold the caller's real intent for a few turns after an
intervention, so the agent doesn't lose it; auto-clear on commit.

In v4 this rides on top of the continuous state-grounding injection: the
held intent is added to the grounding block for the next few turns, then
clears itself (or is cleared the moment the relevant action commits).
"""

from __future__ import annotations


class IntentMemory:
    def __init__(self) -> None:
        self._note: str | None = None
        self._turns_remaining: int = 0

    def hold(self, intent: str, *, turns: int = 3) -> None:
        """Remember ``intent`` for the next ``turns`` turns."""
        self._note = intent or None
        self._turns_remaining = max(0, turns) if intent else 0

    def consume(self) -> str | None:
        """Return the held intent and decay its TTL by one turn. Returns
        ``None`` when nothing is held."""
        if not self._note:
            return None
        note = self._note
        self._turns_remaining -= 1
        if self._turns_remaining <= 0:
            self._note = None
        return note

    @property
    def active(self) -> str | None:
        return self._note

    def clear(self) -> None:
        """Force-clear — call this the moment the relevant action commits."""
        self._note = None
        self._turns_remaining = 0


__all__ = ["IntentMemory"]
