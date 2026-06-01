"""Tier 2 — specialized judge model for ambiguous cases.

Only fires on the ~5% of turns where Tier 1 returns confidence='uncertain'.
The judge emits a full Verdict with reason + suggested_correction so the
intervention orchestrator can speak the correction immediately.
"""

from plivo_mirror.scorer.tier2.atla import AtlaSeleneJudge
from plivo_mirror.scorer.tier2.azure_openai import AzureOpenAIJudge
from plivo_mirror.scorer.tier2.base import Tier2Judge, Tier2Result
from plivo_mirror.scorer.tier2.huggingface_llm import HuggingFaceLLMJudge
from plivo_mirror.scorer.tier2.openai_compatible import OpenAICompatibleJudge

__all__ = [
    "Tier2Judge",
    "Tier2Result",
    "AtlaSeleneJudge",
    "AzureOpenAIJudge",
    "HuggingFaceLLMJudge",
    "OpenAICompatibleJudge",
]
