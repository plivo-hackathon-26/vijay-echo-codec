"""OpenAI LLMClient — works for OpenAI and Azure OpenAI deployments.

Point ``base_url`` at an Azure OpenAI host and the same client works
unchanged: this implementation only sends the request params Azure
accepts (no ``max_tokens``, no ``tool_choice="none"``, no
``temperature``), and uses ``response_format={"type":"json_object"}``
for structured output — which Azure supports.

Customers who use a different provider implement the LLMClient protocol
themselves; we deliberately don't ship a giant catalogue here.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

log = logging.getLogger("plivo_mirror.llm.openai")


class OpenAIClient:
    """Async client over ``openai.AsyncOpenAI``.

    Set ``base_url`` to point at OpenAI, Azure OpenAI, or any other
    OpenAI-compatible endpoint.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        organization: str | None = None,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "OpenAIClient requires the `openai` package. "
                "Install with: pip install plivo-mirror[openai]"
            ) from e

        normalised = (base_url or "").strip().rstrip("/") or None
        if normalised and not normalised.startswith(("http://", "https://")):
            normalised = "https://" + normalised
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=normalised, organization=organization
        )
        self._model = model

    # ─────────────────────────── public API ──────────────────────────────

    async def structured_output(
        self,
        system_prompt: str,
        user_prompt: str | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt}
        ]
        if user_prompt:
            messages.append({"role": "user", "content": user_prompt})

        kwargs: dict[str, Any] = dict(
            model=self._model,
            messages=messages,
            response_format={"type": "json_object"},
        )
        if timeout_s is not None:
            kwargs["timeout"] = timeout_s

        resp = await self._client.chat.completions.create(**kwargs)
        raw = (resp.choices[0].message.content or "").strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("structured_output got non-JSON: %r", raw[:200])
            return {}

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str | None = None,
        *,
        timeout_s: float | None = None,
    ) -> str:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt}
        ]
        if user_prompt:
            messages.append({"role": "user", "content": user_prompt})

        kwargs: dict[str, Any] = dict(model=self._model, messages=messages)
        if timeout_s is not None:
            kwargs["timeout"] = timeout_s

        resp = await self._client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()

    async def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str | None = None,
        *,
        timeout_s: float | None = None,
    ) -> AsyncIterator[str]:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt}
        ]
        if user_prompt:
            messages.append({"role": "user", "content": user_prompt})

        kwargs: dict[str, Any] = dict(
            model=self._model, messages=messages, stream=True
        )
        if timeout_s is not None:
            kwargs["timeout"] = timeout_s

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content


__all__ = ["OpenAIClient"]
