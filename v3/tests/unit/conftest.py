"""Shared fixtures for plivo_mirror unit tests."""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable

import pytest

from plivo_mirror.config import MirrorConfig


class FakeLLM:
    """Test double for LLMClient.

    Routes structured_output calls through a customer-provided
    ``responder``: a callable that takes (system_prompt, user_prompt)
    and returns a dict that the scorer/tool-gate will parse.
    """

    def __init__(
        self,
        responder: Callable[[str, str | None], dict[str, Any]] | None = None,
        chat_responder: Callable[[str, str | None], str] | None = None,
    ) -> None:
        self._responder = responder or (lambda s, u: {"score": 0.0, "reason": "ok"})
        self._chat = chat_responder or (lambda s, u: "Just to confirm — is that right?")
        self.calls: list[tuple[str, str | None]] = []

    async def structured_output(
        self,
        system_prompt: str,
        user_prompt: str | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append((system_prompt, user_prompt))
        return self._responder(system_prompt, user_prompt)

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str | None = None,
        *,
        timeout_s: float | None = None,
    ) -> str:
        return self._chat(system_prompt, user_prompt)

    async def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str | None = None,
        *,
        timeout_s: float | None = None,
    ) -> AsyncIterator[str]:
        yield self._chat(system_prompt, user_prompt)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def make_config():
    def _factory(
        *,
        llm: Any,
        policies: list[str] | None = None,
        judging_prompt: str | None = None,
        threshold: float = 0.7,
        tiered: bool = True,
        tool_gate: bool = True,
        streaming: bool = False,
    ) -> MirrorConfig:
        return MirrorConfig(
            llm=llm,
            policies=policies,
            judging_prompt=judging_prompt,
            intervention_threshold=threshold,
            tiered_scoring_enabled=tiered,
            tool_gate_enabled=tool_gate,
            streaming_mode=streaming,
        )

    return _factory
