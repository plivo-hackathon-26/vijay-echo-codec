"""Grounded verifier — the single expensive call, behind a swappable
Protocol."""

from __future__ import annotations

from plivo_mirror.verifier.base import GroundingEvidence, Verifier, VerifierResult
from plivo_mirror.verifier.llm_judge import LLMJudgeVerifier

__all__ = ["Verifier", "VerifierResult", "GroundingEvidence", "LLMJudgeVerifier"]
