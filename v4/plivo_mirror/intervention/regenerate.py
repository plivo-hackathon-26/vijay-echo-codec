"""Reply regeneration — re-prompt the MAIN voice LLM with the correction
packet as a SYSTEM/developer message (NEVER a synthesized customer turn).

Swappable behind the ``ReplyGenerator`` protocol. The default impl, under
single-LLM, runs on the SAME model/creds as the agent and verifier.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from plivo_mirror.intervention.packet import CorrectionPacket

log = logging.getLogger("plivo_mirror.intervention.regenerate")


@runtime_checkable
class ReplyGenerator(Protocol):
    async def regenerate(
        self, *, packet: CorrectionPacket, customer_text: str
    ) -> str: ...


class LLMReplyGenerator:
    """Regenerate a grounded reply via an OpenAI-compatible async client.

    The packet is the SYSTEM message; the REAL customer utterance is the
    user message. We never fabricate a customer turn.
    """

    def __init__(self, client: Any, model: str, *, max_completion_tokens: int = 400) -> None:
        self._client = client
        self._model = model
        self._max_completion_tokens = max_completion_tokens

    @property
    def model(self) -> str:
        return self._model

    async def regenerate(self, *, packet: CorrectionPacket, customer_text: str) -> str:
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": packet.as_developer_message()},
                    {"role": "user", "content": customer_text or ""},
                ],
                max_completion_tokens=self._max_completion_tokens,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception:
            # A regeneration fault yields an empty answer; the engine then
            # treats it as non-converged and escalates rather than crashing.
            log.warning("reply regeneration failed", exc_info=True)
            return ""


__all__ = ["ReplyGenerator", "LLMReplyGenerator"]
