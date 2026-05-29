"""Tier 2 — specialized judge model for ambiguous cases.

Only fires on the ~5% of turns where Tier 1 returns confidence='uncertain'.
The judge emits a full Verdict with reason + suggested_correction so the
intervention orchestrator can speak the correction immediately.
"""

from plivo_mirror.scorer.tier2.atla import AtlaSeleneJudge
from plivo_mirror.scorer.tier2.base import Tier2Judge, Tier2Result

__all__ = ["Tier2Judge", "Tier2Result", "AtlaSeleneJudge"]
