"""HuggingFaceLLMJudge — Tier 2 judge on HF Inference Providers.

Uses HF's OpenAI-compatible chat-completions endpoint at
``router.huggingface.co/v1/chat/completions`` with the same
``HF_API_KEY`` used for the Tier 1 classifier. One key, two tiers.

Default model: ``meta-llama/Llama-3.1-8B-Instruct`` — free on the
Inference Providers tier, judge-capable for simple cases. For
production, swap to a stronger model (e.g.
``meta-llama/Llama-3.3-70B-Instruct``) via the ``model`` arg.

Caveats:
  • The free tier rate-limits and queues. ~3-15s latency is normal
    for an 8B free call.
  • The 8B chat models are not as strong as Atla Selene or Azure
    gpt-5-mini at nuanced policy judgment. For demos / dev this is
    fine; for production, use a stronger Tier 2.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import anyio
import httpx

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

log = logging.getLogger("plivo_mirror.scorer.tier2.huggingface_llm")


DEFAULT_MODEL = "meta-llama/Llama-3.1-8B-Instruct"
DEFAULT_BASE_URL = "https://router.huggingface.co/v1"


class HuggingFaceLLMJudge:
    """Tier 2 judge backed by HF's Inference Providers gateway.

    Args:
        api_key:    Hugging Face token (same one used by Tier 1).
        model:      Chat model id. Defaults to Llama-3.1-8B-Instruct.
        base_url:   Override for self-hosted / proxied deployments.
                    Defaults to ``router.huggingface.co/v1``.
        policies:   Operator policies for the judge to enforce.
        intervention_threshold: ``score >= threshold`` → intervene.
        timeout_s:  Per-call timeout. Fails open on miss.
        max_tokens: Cap on response length (judge JSON is usually < 300).
    """

    name: str = "huggingface_llm_judge"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        policies: list[str] | None = None,
        intervention_threshold: float = 0.7,
        timeout_s: float = 8.0,
        max_tokens: int = 400,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.intervention_threshold = intervention_threshold
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self._policies = list(policies or [])
        self._client: httpx.AsyncClient | None = None

    def _shared_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

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
                raw = await self._call_api(prompt)
        except TimeoutError:
            log.warning(
                "Tier 2 HF judge timed out (call=%s) — fail-open",
                (ctx.call_uuid or "")[:8],
            )
            return Tier2Result(
                verdict=Verdict.no_intervention("tier2_timeout"),
                raw={"error": "timeout"},
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            log.warning(
                "Tier 2 HF judge failed (%s: %s) (call=%s) — fail-open",
                type(exc).__name__, str(exc)[:160],
                (ctx.call_uuid or "")[:8],
            )
            log.debug(
                "Tier 2 HF traceback (call=%s)",
                (ctx.call_uuid or "")[:8],
                exc_info=True,
            )
            return Tier2Result(
                verdict=Verdict.no_intervention("tier2_error"),
                raw={"error": type(exc).__name__, "detail": str(exc)[:200]},
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        verdict = parse_judge_verdict(
            raw,
            tier1_violation_prob,
            self.intervention_threshold,
            provider="huggingface",
            model=self.model,
        )
        return Tier2Result(
            verdict=verdict,
            raw=raw if isinstance(raw, dict) else {"raw": str(raw)[:500]},
            latency_ms=latency_ms,
        )

    async def _call_api(self, prompt: str) -> Any:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "plivo-mirror/0.3.0",
        }
        client = self._shared_client()
        resp = await client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        if resp.status_code >= 400:
            text = (resp.text or "")[:400]
            raise httpx.HTTPStatusError(
                f"HF Tier 2 returned {resp.status_code}: {text}",
                request=resp.request,
                response=resp,
            )
        return resp.json()


__all__ = ["HuggingFaceLLMJudge", "DEFAULT_MODEL", "DEFAULT_BASE_URL"]
