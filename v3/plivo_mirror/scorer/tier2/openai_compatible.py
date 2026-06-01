"""OpenAICompatibleJudge — Tier 2 judge on any OpenAI-compatible endpoint.

Covers any chat-completions API that follows OpenAI's interface:
  • OpenAI itself
  • Together AI (``api.together.xyz/v1``)
  • Fireworks AI (``api.fireworks.ai/inference/v1``)
  • Anyscale (``api.endpoints.anyscale.com/v1``)
  • Local vLLM / LM Studio / Ollama (with their OpenAI-compat layer)
  • Any reverse-proxy that speaks ``/v1/chat/completions``

For Azure use ``AzureOpenAIJudge`` instead — the Azure SDK has its
own URL building.
For HF Inference Providers use ``HuggingFaceLLMJudge`` instead —
same wire shape but the convention is to point at HF directly.
"""

from __future__ import annotations

import logging
import time

import anyio

from plivo_mirror.context import (
    SupervisorContext,
    TurnPayload,
    Verdict,
)
from plivo_mirror.scorer.tier2._judge_prompt import (
    build_judge_prompt,
    parse_judge_verdict,
)
from plivo_mirror.scorer.tier2.base import Tier2Judge, Tier2Result

log = logging.getLogger("plivo_mirror.scorer.tier2.openai_compatible")


class OpenAICompatibleJudge:
    """Tier 2 judge backed by any OpenAI-compatible chat-completions API.

    Args:
        api_key:    Bearer token for the provider.
        model:      Model name (e.g. ``"gpt-4o-mini"``,
                    ``"meta-llama/Llama-3.1-70B-Instruct-Turbo"``).
        base_url:   Provider's ``/v1`` base URL (e.g.
                    ``"https://api.together.xyz/v1"``).
        organization: Optional ``OpenAI-Organization`` header value.
        policies:   Operator policies for the judge to enforce.
        intervention_threshold: ``score >= threshold`` → intervene.
        timeout_s:  Per-call timeout. Fails open on miss.
    """

    name: str = "openai_compatible_judge"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        organization: str | None = None,
        policies: list[str] | None = None,
        intervention_threshold: float = 0.7,
        timeout_s: float = 8.0,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "OpenAICompatibleJudge requires the `openai` package. "
                "Install with: pip install plivo-mirror[openai]"
            ) from e
        normalised = (base_url or "").strip().rstrip("/") or None
        if normalised and not normalised.startswith(("http://", "https://")):
            normalised = "https://" + normalised
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=normalised, organization=organization
        )
        self._model = model
        self._policies = list(policies or [])
        self.intervention_threshold = intervention_threshold
        self.timeout_s = timeout_s

    async def judge(
        self,
        turn: TurnPayload,
        ctx: SupervisorContext,
        tier1_violation_prob: float,
    ) -> Tier2Result:
        started = time.monotonic()
        prompt = build_judge_prompt(turn, self._policies)
        try:
            with anyio.fail_after(self.timeout_s):
                resp = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                )
        except TimeoutError:
            log.warning(
                "Tier 2 OpenAI-compat judge timed out (call=%s) — fail-open",
                (ctx.call_uuid or "")[:8],
            )
            return Tier2Result(
                verdict=Verdict.no_intervention("tier2_timeout"),
                raw={"error": "timeout"},
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            log.warning(
                "Tier 2 OpenAI-compat judge failed (%s: %s) (call=%s) — fail-open",
                type(exc).__name__, str(exc)[:160],
                (ctx.call_uuid or "")[:8],
            )
            log.debug(
                "Tier 2 OpenAI-compat traceback (call=%s)",
                (ctx.call_uuid or "")[:8],
                exc_info=True,
            )
            return Tier2Result(
                verdict=Verdict.no_intervention("tier2_error"),
                raw={"error": type(exc).__name__, "detail": str(exc)[:200]},
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        raw_dict = {
            "choices": [
                {"message": {"content": resp.choices[0].message.content or ""}}
            ]
        }
        verdict = parse_judge_verdict(
            raw_dict,
            tier1_violation_prob,
            self.intervention_threshold,
            provider="openai_compatible",
            model=self._model,
        )
        return Tier2Result(verdict=verdict, raw=raw_dict, latency_ms=latency_ms)


__all__ = ["OpenAICompatibleJudge"]
