"""Semantic recall signal — the second gate tier (the "NLI lever").

The lexical risk-span tagger (``risk_spans.py``) only fires on numbers,
prices, and commitment words. A whole class of violations is **lexically
invisible**: the agent's reply is fluent and carries no red-flag token, but
it *contradicts or drops a constraint the customer stated* — an ignored
negation ("no onions" → "extra onions"), a dropped compound modifier ("extra
cheese on the veggie half" → "extra cheese all over"), an ignored conditional.
The lexicon can never route these to the verifier, so they are missed AT THE
GATE no matter how good the judge is.

This module adds a cheap semantic signal that asks: *does the reply contradict
the customer's stated request?* When it fires, the speech guard synthesizes a
flagged span and routes the turn to the SAME reliable grounded verifier — so
this is purely a **recall** widener; precision is still the verifier's job (it
can overrule a semantic false positive). The signal is a swappable
``Protocol`` exactly like ``Verifier`` and ``ConfidenceSignal``.

HONEST LATENCY NOTE: unlike the ~0 ms deterministic layer, this signal runs on
turns the lexicon passed — i.e. most clean turns — at the cost of one local
NLI forward pass (~10–50 ms CPU for a small cross-encoder). It is therefore
NOT zero-compute on clean turns; it trades a bounded, local, no-network cost
for recall. It is OFF by default (``semantic_signal=None``) so the core stays
light and the heavy ML dependency is opt-in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("plivo_mirror.guards.semantic")


@dataclass
class SemanticResult:
    """Output of a semantic signal. ``contradiction`` is the routing decision;
    ``score`` is the model's contradiction probability (for thresholding /
    observability); ``premise``/``hypothesis`` record what was compared."""

    contradiction: bool
    score: float = 0.0
    premise: str = ""
    hypothesis: str = ""


@runtime_checkable
class SemanticSignal(Protocol):
    """Decides whether ``reply`` contradicts the customer's stated request.
    Synchronous (local CPU inference), mirroring ``ConfidenceSignal``."""

    def contradicts(
        self, customer_text: str, reply: str, *, state: Any | None = None
    ) -> SemanticResult: ...


class NoSemanticSignal:
    """Default null signal — never fires. Lets the router carry the semantic
    tier unconditionally while keeping behavior identical to 'no signal'."""

    def contradicts(
        self, customer_text: str, reply: str, *, state: Any | None = None
    ) -> SemanticResult:
        return SemanticResult(contradiction=False)


class NLICrossEncoderSignal:
    """Default real impl: a local cross-encoder NLI model (e.g. a small
    DeBERTa-MNLI) scoring ``contradiction(premise=customer_text,
    hypothesis=reply)``.

    The ``transformers``/``torch`` dependency is OPTIONAL and lazily imported
    on first use. If it is not installed (or the model can't load), the signal
    degrades to never-fires and logs ONCE — a missing optional model must
    never break a call (fail-open, same discipline as the verifier).

    The contradiction label index is read from the model's own
    ``config.id2label`` so it works across NLI models with different label
    orderings.
    """

    def __init__(
        self,
        model_name: str = "cross-encoder/nli-deberta-v3-small",
        *,
        threshold: float = 0.9,
        max_length: int = 256,
    ) -> None:
        self.model_name = model_name
        self.threshold = threshold
        self.max_length = max_length
        self._tok = None
        self._model = None
        self._contra_idx: int | None = None
        self._loaded = False
        self._unavailable = False

    # ── lazy model load ───────────────────────────────────────────────

    def _ensure_loaded(self) -> bool:
        if self._loaded:
            return True
        if self._unavailable:
            return False
        try:
            import torch  # noqa: F401
            from transformers import (  # type: ignore
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )

            self._tok = AutoTokenizer.from_pretrained(self.model_name)
            self._model = AutoModelForSequenceClassification.from_pretrained(
                self.model_name
            )
            self._model.eval()
            self._contra_idx = _find_contradiction_index(self._model.config)
            self._loaded = True
            return True
        except Exception:
            self._unavailable = True
            log.warning(
                "NLI semantic signal unavailable (install plivo-mirror[nli] and "
                "ensure the model %r is reachable); semantic tier disabled",
                self.model_name,
                exc_info=True,
            )
            return False

    # ── inference ─────────────────────────────────────────────────────

    def contradicts(
        self, customer_text: str, reply: str, *, state: Any | None = None
    ) -> SemanticResult:
        premise = (customer_text or "").strip()
        hypothesis = (reply or "").strip()
        if not premise or not hypothesis:
            return SemanticResult(contradiction=False, premise=premise, hypothesis=hypothesis)
        if not self._ensure_loaded():
            return SemanticResult(contradiction=False, premise=premise, hypothesis=hypothesis)

        import torch  # local: only when the model is actually loaded

        with torch.no_grad():
            inputs = self._tok(
                premise,
                hypothesis,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            logits = self._model(**inputs).logits[0]
            probs = torch.softmax(logits, dim=-1)
            score = float(probs[self._contra_idx])
        return SemanticResult(
            contradiction=score >= self.threshold,
            score=score,
            premise=premise,
            hypothesis=hypothesis,
        )


def _find_contradiction_index(config: Any) -> int:
    """Locate the 'contradiction' class index from the model config's
    ``id2label`` map. Falls back to 0 (the conventional NLI ordering
    [contradiction, neutral, entailment]) when labels are unlabeled."""
    id2label = getattr(config, "id2label", None) or {}
    for idx, label in id2label.items():
        if "contradict" in str(label).lower():
            try:
                return int(idx)
            except (TypeError, ValueError):
                continue
    return 0


__all__ = [
    "SemanticResult",
    "SemanticSignal",
    "NoSemanticSignal",
    "NLICrossEncoderSignal",
]
