"""Hugging Face Inference API client for the Tier 1 classifier.

Hits the zero-shot-classification endpoint with a DeBERTa-v3 NLI model
(default: MoritzLaurer/deberta-v3-large-zeroshot-v2.0). Output is a
labelled probability distribution; we collapse it to a single
"violation_prob" score using the candidate labels.

Why DeBERTa-v3 zero-shot v2.0:
  • Top-of-Hub NLI cross-encoder as of mid-2026 (verified May 2026)
  • 304M params — ~80ms on CPU, ~30ms on GPU
  • Works zero-shot — no training data needed for v0.2.0 ship
  • MIT-licensed, runs on HF Serverless or dedicated Endpoint

The classifier reformulates the turn as a zero-shot task:
    premise   = "Customer said: '...'. Agent plans to say: '...'."
    candidates = ["policy violation", "fine"]
    → returns prob("policy violation")

This is the cheapest defensible signal that can run under 100ms.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx

from plivo_mirror.context import SupervisorContext, TurnPayload
from plivo_mirror.scorer.tier1.base import Tier1Classifier, Tier1Result

log = logging.getLogger("plivo_mirror.scorer.tier1.huggingface")


DEFAULT_MODEL = "MoritzLaurer/deberta-v3-large-zeroshot-v2.0"
DEFAULT_LABELS = ["policy violation", "fine"]


def _build_premise(turn: TurnPayload) -> str:
    """Construct the premise text the classifier scores.

    Includes the customer's last utterance + the agent's planned
    response, optionally with the most-recent history turn for context.
    Tool calls are summarised compactly if present.
    """
    parts: list[str] = []
    if turn.history:
        # One turn of context is plenty for an NLI classifier; more
        # blows past its 512-token window with no accuracy gain.
        last = turn.history[-1]
        role = "Customer" if last.role == "customer" else "Agent"
        text = (last.text or "").strip()
        if text:
            parts.append(f"Previously, {role}: {text}")
    parts.append(f"Customer said: {(turn.customer_text or '').strip() or '(silence)'}")
    parts.append(f"Agent plans to say: {(turn.primary_text or '').strip() or '(no response)'}")
    if turn.tool_calls:
        tc_summary = "; ".join(
            f"{tc.name}({_format_args(tc.args)})" for tc in turn.tool_calls
        )
        parts.append(f"Agent's planned tool calls: {tc_summary}")
    return " ".join(parts)


def _format_args(args: dict) -> str:
    """Render tool args compactly, capped at ~120 chars to keep the
    classifier's tokenizer happy."""
    if not args:
        return ""
    pairs = []
    for k, v in args.items():
        s = repr(v) if not isinstance(v, str) else v
        if len(s) > 60:
            s = s[:57] + "..."
        pairs.append(f"{k}={s}")
    out = ", ".join(pairs)
    return out[:120]


@dataclass
class HuggingFaceClassifier:
    """Tier1Classifier backed by Hugging Face Inference API.

    Two URL modes:
      • Serverless: ``https://api-inference.huggingface.co/models/<model>``
        — free tier w/ rate limits, cold-start hazard. Set this for dev.
      • Dedicated endpoint: ``https://<endpoint>.endpoints.huggingface.cloud``
        — always-warm, paid. Set this for production.

    Args:
        api_key: HF token (sub-token with ``inference`` scope works).
        model: HF model id; defaults to deberta-v3-large-zeroshot-v2.0.
        endpoint_url: Optional override for a dedicated endpoint URL.
            When set, requests go here directly instead of
            ``api-inference.huggingface.co/models/<model>``.
        candidate_labels: Two-label list; first label is "violation",
            second is "fine". The classifier returns prob(violation).
        timeout_s: Per-call timeout. The orchestrator caps total
            scoring latency above this.
        high_confidence_low: violation_prob <= this → confidence=high
            (means the turn is fine, no intervention needed).
        high_confidence_high: violation_prob >= this → confidence=high
            (means the turn clearly violates; intervene).
        uncertain band between the two thresholds → escalate to Tier 2.
        warmup_on_init: Issue a single warming request at construction
            to dodge cold-start latency on the first real call.
    """

    api_key: str
    model: str = DEFAULT_MODEL
    endpoint_url: str | None = None
    candidate_labels: list[str] = field(default_factory=lambda: list(DEFAULT_LABELS))
    timeout_s: float = 5.0
    high_confidence_low: float = 0.20
    high_confidence_high: float = 0.85
    warmup_on_init: bool = False
    name: str = "huggingface_deberta_zeroshot"

    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if len(self.candidate_labels) != 2:
            raise ValueError("candidate_labels must be exactly two strings")
        if not (
            0.0 <= self.high_confidence_low
            <= self.high_confidence_high
            <= 1.0
        ):
            raise ValueError(
                "expected 0 <= high_confidence_low <= high_confidence_high <= 1"
            )

    @property
    def _request_url(self) -> str:
        if self.endpoint_url:
            return self.endpoint_url.rstrip("/")
        return f"https://api-inference.huggingface.co/models/{self.model}"

    def _shared_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def warmup(self) -> None:
        """Issue a no-op classification to wake up the endpoint.

        On HF Serverless this also pulls the model into the inference
        worker; on dedicated endpoints it's a heartbeat. Best-effort —
        any error is swallowed.
        """
        try:
            await self._call_api(
                "Customer said: hello. Agent plans to say: hi.",
            )
        except Exception:
            log.debug("warmup request failed (ignored)")

    async def classify(
        self, turn: TurnPayload, ctx: SupervisorContext
    ) -> Tier1Result:
        premise = _build_premise(turn)
        started = time.monotonic()
        try:
            with anyio.fail_after(self.timeout_s):
                raw = await self._call_api(premise)
        except TimeoutError:
            log.warning(
                "Tier1 HF classifier timed out after %.2fs (call=%s) — uncertain",
                self.timeout_s,
                (ctx.call_uuid or "")[:8],
            )
            return Tier1Result(
                violation_prob=0.5,
                confidence="uncertain",
                raw={"error": "timeout"},
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            log.exception(
                "Tier1 HF classifier failed (call=%s) — uncertain",
                (ctx.call_uuid or "")[:8],
            )
            return Tier1Result(
                violation_prob=0.5,
                confidence="uncertain",
                raw={"error": type(exc).__name__, "detail": str(exc)[:200]},
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        prob = self._parse_violation_prob(raw)
        confidence = self._confidence_band(prob)
        return Tier1Result(
            violation_prob=prob,
            confidence=confidence,
            raw=raw if isinstance(raw, dict) else {"raw": raw},
            latency_ms=latency_ms,
        )

    # ─────────────────────── internals ───────────────────────────────

    async def _call_api(self, premise: str) -> Any:
        """Hit the HF zero-shot-classification endpoint."""
        payload = {
            "inputs": premise,
            "parameters": {
                "candidate_labels": self.candidate_labels,
                # `multi_label=False` → labels are mutually exclusive,
                # softmax-normalised. Single prob(violation) = scores[i]
                # where labels[i] == candidate_labels[0].
                "multi_label": False,
            },
            # Allow the model to warm up if it's on serverless.
            "options": {"wait_for_model": True},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        client = self._shared_client()
        resp = await client.post(self._request_url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def _parse_violation_prob(self, raw: Any) -> float:
        """Read the violation probability out of the HF response.

        HF's zero-shot endpoint returns:
            {"labels": ["...", "..."], "scores": [0.x, 0.y], "sequence": "..."}
        with the highest-scoring label first.
        """
        try:
            if isinstance(raw, list) and raw:
                raw = raw[0]
            if not isinstance(raw, dict):
                return 0.5
            labels = raw.get("labels") or []
            scores = raw.get("scores") or []
            target = self.candidate_labels[0]
            for label, score in zip(labels, scores):
                if str(label).strip().lower() == target.strip().lower():
                    return max(0.0, min(1.0, float(score)))
            # Fall back to the highest score as violation_prob if the
            # label naming didn't match (shouldn't happen, but defensive).
            if scores:
                return max(0.0, min(1.0, float(scores[0])))
        except (TypeError, ValueError, KeyError, IndexError):
            pass
        return 0.5

    def _confidence_band(self, prob: float) -> str:
        if prob <= self.high_confidence_low or prob >= self.high_confidence_high:
            return "high"
        return "uncertain"


__all__ = ["HuggingFaceClassifier", "DEFAULT_MODEL", "DEFAULT_LABELS"]
