"""In-memory StateStore — the default for v1 single-tenant deployments.

Thread-safe via asyncio.Lock. Process-local: state does NOT survive
restarts. v2 will ship a Redis implementation for durable state.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


def _new_entry() -> dict[str, Any]:
    return {"cooldown": 0.0, "override": None, "meta": {}}


class InMemoryStateStore:
    """Default StateStore: a dict guarded by an asyncio lock."""

    def __init__(self) -> None:
        self._state: dict[str, dict[str, Any]] = defaultdict(_new_entry)
        self._lock = asyncio.Lock()

    async def get_cooldown(self, call_uuid: str) -> float:
        async with self._lock:
            return float(self._state[call_uuid].get("cooldown", 0.0))

    async def set_cooldown(self, call_uuid: str, deadline_monotonic: float) -> None:
        async with self._lock:
            self._state[call_uuid]["cooldown"] = float(deadline_monotonic)

    async def get_override(self, call_uuid: str) -> str | None:
        async with self._lock:
            return self._state[call_uuid].get("override")

    async def set_override(self, call_uuid: str, note: str) -> None:
        async with self._lock:
            self._state[call_uuid]["override"] = note

    async def clear_override(self, call_uuid: str) -> None:
        async with self._lock:
            self._state[call_uuid]["override"] = None

    async def get_meta(self, call_uuid: str, key: str) -> Any:
        async with self._lock:
            return self._state[call_uuid]["meta"].get(key)

    async def set_meta(self, call_uuid: str, key: str, value: Any) -> None:
        async with self._lock:
            self._state[call_uuid]["meta"][key] = value

    async def cleanup(self, call_uuid: str) -> None:
        async with self._lock:
            self._state.pop(call_uuid, None)


__all__ = ["InMemoryStateStore"]
