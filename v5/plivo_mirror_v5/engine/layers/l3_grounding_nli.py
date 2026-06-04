"""L3 — claim-grounding NLI. SECONDARY; prose claims only.

Fires only on free-form claims with no structured referent (L2 has no
jurisdiction): retrieve from the unstructured KB, run NLI, and flag
``contradicted`` / ``unsupported`` claims with the retrieved chunk as
evidence. The only place a model is in the loop — and it runs out of the
hot path (the observer evaluates off the event loop; only L2 is
inline-safe).

The NLI scorer is behind a Protocol. ``KeywordNLI`` is the offline stub:
deterministic token-overlap + number-consistency heuristics, good enough
to exercise the layer end-to-end with no model, no network, no keys.
# TODO: real NLI scorer (cross-encoder or LLM-entailment) — post-v5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from plivo_mirror_v5.engine.kb_retriever import tokenize
from plivo_mirror_v5.engine.layers.base import LayerContext
from plivo_mirror_v5.engine.session_state import SessionState
from plivo_mirror_v5.engine.verdict import Evidence, TurnInput, Verdict, new_verdict_id

NLILabel = str  # "supported" | "contradicted" | "unsupported"

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


@dataclass(frozen=True)
class NLIResult:
    label: NLILabel
    score: float


@runtime_checkable
class NLIScorer(Protocol):
    def score(self, premise: str, hypothesis: str) -> NLIResult: ...


class KeywordNLI:
    """Deterministic offline NLI stand-in.

    - High content-token overlap + consistent numbers → supported.
    - High overlap but the hypothesis asserts a number absent from the
      premise (while the premise asserts numbers of its own) → contradicted.
    - Otherwise → unsupported.
    """

    def __init__(self, support_overlap: float = 0.5) -> None:
        self.support_overlap = support_overlap

    def score(self, premise: str, hypothesis: str) -> NLIResult:
        h_tokens = tokenize(hypothesis)
        p_tokens = tokenize(premise)
        if not h_tokens:
            return NLIResult("unsupported", 0.0)
        overlap = len(h_tokens & p_tokens) / len(h_tokens)

        h_nums = {n.replace(",", "") for n in _NUMBER_RE.findall(hypothesis)}
        p_nums = {n.replace(",", "") for n in _NUMBER_RE.findall(premise)}
        if overlap >= self.support_overlap and h_nums and p_nums and not (h_nums & p_nums):
            # Same topic, conflicting figures.
            return NLIResult("contradicted", overlap)
        if overlap >= self.support_overlap:
            return NLIResult("supported", overlap)
        return NLIResult("unsupported", overlap)


class GroundingNLILayer:
    name = "L3"

    def __init__(self, nli: NLIScorer | None = None) -> None:
        self.nli = nli or KeywordNLI()

    def check(
        self, turn: TurnInput, state: SessionState, ctx: LayerContext
    ) -> list[Verdict]:
        if turn.role != "agent" or ctx.kb is None:
            return []

        verdicts: list[Verdict] = []
        for claim in turn.claims:
            claim_id = claim.get("claim_id")
            if claim_id in ctx.l2_claim_ids:
                continue  # under deterministic jurisdiction (arbitration is backstop)
            if claim.get("claim_type") == "correction":
                continue
            hypothesis = claim.get("text") or str(claim.get("spoken_value") or "")
            if not hypothesis.strip():
                continue

            chunks = ctx.kb.retrieve(hypothesis, k=ctx.config.l3_top_k)
            label, best_chunk, best_score = "unsupported", None, 0.0
            for chunk in chunks:
                result = self.nli.score(chunk.text, hypothesis)
                if result.label == "contradicted":
                    label, best_chunk, best_score = result.label, chunk, result.score
                    break  # a grounded contradiction trumps everything
                if result.label == "supported" and label != "supported":
                    label, best_chunk, best_score = result.label, chunk, result.score
                elif result.score > best_score and label == "unsupported":
                    best_chunk, best_score = chunk, result.score

            if label == "supported":
                fired, severity = False, "info"
            elif label == "contradicted":
                fired, severity = True, ctx.config.l3_contradicted_severity
            else:
                fired, severity = True, ctx.config.l3_unsupported_severity

            extra: dict = {"claim_id": claim_id, "nli_label": label, "nli_score": best_score}
            if fired and ctx.snapshot.untrusted_input:
                severity = "info"  # L1 gate, same rationale as L2
                extra["untrusted_input"] = True

            verdicts.append(
                Verdict(
                    verdict_id=new_verdict_id(),
                    detector=self.name,
                    fired=fired,
                    severity=severity,
                    latency_ms=0.0,  # stamped by the engine per layer
                    evidence=Evidence(
                        claim_type=claim.get("claim_type", "fact"),
                        spoken_value=hypothesis,
                        truth_value=best_chunk.text if best_chunk else None,
                        source=f"kb#{best_chunk.chunk_id}" if best_chunk else "kb#none",
                        extra=extra,
                    ),
                )
            )
        return verdicts
