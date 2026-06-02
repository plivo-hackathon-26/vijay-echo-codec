"""The grounded verifier interface — the ONLY expensive call in the
speech path, and a swappable ``Protocol`` so a small hosted model or a
fine-tune can drop in later as a one-line replacement.

It answers exactly one question: *is this claim/commitment supported by
{state, policy, retrieved facts}?* — yes/no plus which policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class GroundingEvidence:
    """Everything the verifier may ground a claim against. Built by the
    speech guard from ``SessionState`` — kept decoupled from the contracts
    module so alternative verifiers don't import the whole stack."""

    reply: str
    flagged_spans: list[str] = field(default_factory=list)
    # validated entities, as ``key -> stringified value``
    facts: dict[str, str] = field(default_factory=dict)
    # compiled policies, as ``{"id": ..., "text": ...}`` so the verifier
    # can cite which policy a claim violates
    policies: list[dict[str, str]] = field(default_factory=list)
    retrieved_facts: list[str] = field(default_factory=list)
    # the customer's stated request for THIS turn — lets the verifier judge
    # whether the reply contradicts/ignores/drops a stated constraint (the
    # precision check behind the semantic/NLI recall tier). Empty when unused.
    customer_text: str = ""


@dataclass
class VerifierResult:
    supported: bool
    policy_id: str | None = None
    reason: str = ""


@runtime_checkable
class Verifier(Protocol):
    async def verify(self, claim: str, evidence: GroundingEvidence) -> VerifierResult: ...


__all__ = ["GroundingEvidence", "VerifierResult", "Verifier"]
