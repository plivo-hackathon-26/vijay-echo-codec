"""Phase 2 — confidence signal."""

from __future__ import annotations

import math

from plivo_mirror.guards.signal import FixedConfidence, LogprobEntropySignal


def test_no_logprobs_is_zero_confidence():
    sig = LogprobEntropySignal()
    assert sig.confidence("anything", None) == 0.0
    assert sig.confidence("anything", []) == 0.0


def test_low_entropy_is_high_confidence():
    # one near-certain token: top alt has logprob ~0 (prob ~1), others tiny
    logprobs = [[("yes", math.log(0.98)), ("no", math.log(0.01)), ("maybe", math.log(0.01))]]
    sig = LogprobEntropySignal()
    assert sig.confidence("yes", logprobs) > 0.8


def test_high_entropy_is_low_confidence():
    # three equally likely alternatives ⇒ max entropy ⇒ ~0 confidence
    p = math.log(1 / 3)
    logprobs = [[("a", p), ("b", p), ("c", p)]]
    sig = LogprobEntropySignal()
    assert sig.confidence("a", logprobs) < 0.05


def test_fixed_confidence_clamps():
    assert FixedConfidence(2.0).confidence("x") == 1.0
    assert FixedConfidence(-1.0).confidence("x") == 0.0
    assert FixedConfidence(0.7).confidence("x") == 0.7
