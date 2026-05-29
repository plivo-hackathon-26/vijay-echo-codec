"""LLMClient protocol — the interface every LLM provider implementation
must satisfy.

We deliberately keep this tiny. The scorer + correction generator only
need two things:
  1. structured_output(system_prompt, user_prompt=None) → dict
     for the JSON-emitting scorer / tool-gate verdicts.
  2. chat(system_prompt, user_prompt=None) → str
     for free-text correction generation.

Streaming is optional — implementations that don't support it just leave
``chat_stream`` returning a single chunk.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    """Minimal protocol for an LLM provider."""

    async def structured_output(
        self,
        system_prompt: str,
        user_prompt: str | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Return a parsed JSON object. Implementations should use the
        provider's JSON-mode / structured-output feature so we don't
        get free-text back."""
        ...

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str | None = None,
        *,
        timeout_s: float | None = None,
    ) -> str:
        """Return a single free-text response."""
        ...

    async def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str | None = None,
        *,
        timeout_s: float | None = None,
    ) -> AsyncIterator[str]:
        """Yield incremental text deltas. Optional — default impls may
        just yield the full ``chat`` result as one chunk."""
        ...


__all__ = ["LLMClient"]
