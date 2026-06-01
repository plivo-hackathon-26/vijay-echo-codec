"""Confidence signal — behind an interface, per the spec.

The agent model in this setup (Azure ``gpt-5-mini`` via LiveKit's
``llm_node``, which streams ``ChatChunk``s) does NOT expose per-token
logprobs, and reasoning models generally don't return them. So the
default ``LogprobEntropySignal`` computes top-K logprob entropy ONLY when
logprobs are actually present; with none it returns ``0.0`` (unknown ⇒
uncertain), which makes the router escalate every risky span. Swapping in
a logprob-capable model or a semantic-entropy probe is a one-line change:
pass a different ``ConfidenceSignal`` to the ``SpeechGuard``.
"""

from __future__ import annotations

import math
from typing import Any, Protocol, Sequence, runtime_checkable


@runtime_checkable
class ConfidenceSignal(Protocol):
    """Returns the model's confidence in ``reply`` as a float in [0, 1]
    (1.0 = fully confident). ``logprobs`` is the adapter-supplied top-K
    logprob structure, or ``None`` when the model doesn't expose it."""

    def confidence(self, reply: str, logprobs: Any | None = None) -> float: ...


class LogprobEntropySignal:
    """Top-K logprob entropy → confidence.

    Expected ``logprobs`` shape: a sequence of per-token entries, each a
    sequence of ``(token, logprob)`` pairs for that token's top-K
    alternatives (OpenAI ``logprobs.content[i].top_logprobs`` flattened to
    tuples). Confidence = ``1 - mean(normalized per-token entropy)``.
    """

    def __init__(self, top_k: int = 5) -> None:
        self._top_k = top_k

    def confidence(self, reply: str, logprobs: Any | None = None) -> float:
        if not logprobs:
            return 0.0  # unknown ⇒ uncertain ⇒ router escalates risky spans
        norm_entropies: list[float] = []
        for token_alts in logprobs:
            probs = self._token_probs(token_alts)
            if not probs:
                continue
            ent = -sum(p * math.log(p) for p in probs if p > 0.0)
            max_ent = math.log(len(probs)) if len(probs) > 1 else 0.0
            norm_entropies.append(ent / max_ent if max_ent > 0.0 else 0.0)
        if not norm_entropies:
            return 0.0
        mean_norm = sum(norm_entropies) / len(norm_entropies)
        return max(0.0, min(1.0, 1.0 - mean_norm))

    def _token_probs(self, token_alts: Sequence[Any]) -> list[float]:
        raw = []
        for pair in list(token_alts)[: self._top_k]:
            try:
                _, lp = pair
                raw.append(math.exp(float(lp)))
            except (TypeError, ValueError):
                continue
        total = sum(raw)
        if total <= 0.0:
            return []
        return [p / total for p in raw]


class FixedConfidence:
    """A constant confidence — for tests and for forcing a policy (e.g.
    always-escalate with 0.0, or trust-the-model with 1.0)."""

    def __init__(self, value: float) -> None:
        self._value = max(0.0, min(1.0, value))

    def confidence(self, reply: str, logprobs: Any | None = None) -> float:
        return self._value


__all__ = ["ConfidenceSignal", "LogprobEntropySignal", "FixedConfidence"]
