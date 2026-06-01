"""AzureOpenAIJudge — Tier 2 judge on an Azure OpenAI deployment.

Useful when:
  • You already have Azure OpenAI credentials (the primary agent uses
    them anyway) and want Mirror's supervision LLM in the same account.
  • Atla's hosted Selene is unavailable / you don't want a separate
    provider signup.
  • You need a strong gpt-5-class judge that costs nothing extra
    beyond your existing Azure spend.

Latency is typically 2-4s end-to-end against gpt-5-mini. Strong
enough for nuanced cases (third-party preference, policy-rule
violations) that Tier 0 + Tier 1 can't catch.

The chat-completions prompt + verdict parsing live in
``_judge_prompt`` and are shared with the other built-in judges so
any prompt improvement lands across the whole library at once.
"""

from __future__ import annotations

import logging
import time
from typing import Any

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

log = logging.getLogger("plivo_mirror.scorer.tier2.azure_openai")


DEFAULT_API_VERSION = "2024-08-01-preview"


class AzureOpenAIJudge:
    """Tier 2 judge backed by an Azure OpenAI chat-completions deployment.

    Args:
        api_key:          Azure API key.
        azure_endpoint:   ``https://<resource>.openai.azure.com`` — no
                          ``/openai/v1`` suffix, no trailing slash.
        azure_deployment: The deployment NAME you created in Azure AI
                          Studio (not the model name). Must match what
                          the resource exposes exactly.
        api_version:      Azure REST API version. Defaults to a recent
                          preview that supports ``response_format``.
        policies:         Operator policies for the judge to enforce.
        intervention_threshold: ``score >= threshold`` → intervene.
                          Should match ``MirrorConfig.intervention_threshold``.
        timeout_s:        Per-call timeout. Fails open on miss so the
                          call never goes silent.
    """

    name: str = "azure_openai_judge"

    def __init__(
        self,
        *,
        api_key: str,
        azure_endpoint: str,
        azure_deployment: str,
        api_version: str = DEFAULT_API_VERSION,
        policies: list[str] | None = None,
        intervention_threshold: float = 0.7,
        timeout_s: float = 8.0,
    ) -> None:
        try:
            from openai import AsyncAzureOpenAI
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "AzureOpenAIJudge requires the `openai` package. "
                "Install with: pip install plivo-mirror[openai]"
            ) from e
        self._client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=azure_endpoint,
            api_version=api_version,
        )
        self._deployment = azure_deployment
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
                    model=self._deployment,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                )
        except TimeoutError:
            log.warning(
                "Tier 2 Azure judge timed out (call=%s) — fail-open",
                (ctx.call_uuid or "")[:8],
            )
            return Tier2Result(
                verdict=Verdict.no_intervention("tier2_timeout"),
                raw={"error": "timeout"},
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            log.warning(
                "Tier 2 Azure judge failed (%s: %s) (call=%s) — fail-open",
                type(exc).__name__, str(exc)[:160],
                (ctx.call_uuid or "")[:8],
            )
            log.debug(
                "Tier 2 Azure traceback (call=%s)",
                (ctx.call_uuid or "")[:8],
                exc_info=True,
            )
            return Tier2Result(
                verdict=Verdict.no_intervention("tier2_error"),
                raw={"error": type(exc).__name__, "detail": str(exc)[:200]},
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        raw_dict: dict[str, Any] = {
            "choices": [
                {"message": {"content": resp.choices[0].message.content or ""}}
            ]
        }
        verdict = parse_judge_verdict(
            raw_dict,
            tier1_violation_prob,
            self.intervention_threshold,
            provider="azure",
            model=self._deployment,
        )
        return Tier2Result(verdict=verdict, raw=raw_dict, latency_ms=latency_ms)


__all__ = ["AzureOpenAIJudge", "DEFAULT_API_VERSION"]
