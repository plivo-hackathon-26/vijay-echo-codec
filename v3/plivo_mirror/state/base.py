"""StateStore protocol — per-call ephemeral state that survives across
async turns within a process.

v1 ships only an in-memory implementation. The protocol is async even
though the in-memory impl doesn't need it — so a Redis/Postgres impl
can slot in without an API change in v2.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StateStore(Protocol):
    async def get_cooldown(self, call_uuid: str) -> float:
        """Return the monotonic deadline before which interventions are
        suppressed. 0.0 means no cooldown active."""
        ...

    async def set_cooldown(self, call_uuid: str, deadline_monotonic: float) -> None: ...

    async def get_override(self, call_uuid: str) -> str | None:
        """One-shot system note injected into the next primary turn after
        an intervention fires."""
        ...

    async def set_override(self, call_uuid: str, note: str) -> None: ...

    async def clear_override(self, call_uuid: str) -> None: ...

    async def get_meta(self, call_uuid: str, key: str) -> Any: ...

    async def set_meta(self, call_uuid: str, key: str, value: Any) -> None: ...

    async def cleanup(self, call_uuid: str) -> None:
        """Drop everything for a call. Called when the WS closes."""
        ...


__all__ = ["StateStore"]
