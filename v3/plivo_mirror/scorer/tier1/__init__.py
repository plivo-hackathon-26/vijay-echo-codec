"""Tier 1 — specialized NLI classifier for fast, calibrated scoring.

The default implementation hits Hugging Face's Inference API with a
DeBERTa-v3 zero-shot model. Customers can plug their own classifier
endpoint via the Tier1Classifier protocol.
"""

from plivo_mirror.scorer.tier1.base import Tier1Classifier, Tier1Result
from plivo_mirror.scorer.tier1.huggingface import HuggingFaceClassifier

__all__ = ["Tier1Classifier", "Tier1Result", "HuggingFaceClassifier"]
