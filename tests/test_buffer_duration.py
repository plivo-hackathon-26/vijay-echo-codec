"""Regression guards for buffer-duration calculation.

The bug we're guarding against: the buffer audio takes ~5s to play
on the line but we were sending the correction after a hardcoded
2.5s, causing the two utterances to overlap. The duration must scale
with the actual buffer text length.
"""

import os

import pytest

from mirror import interventions
from mirror.canned_corrections import CANNED


def test_buffer_duration_scales_with_text_length():
    short = "Hmm..."
    long = "Sorry — let me make sure I got that right, just a moment..."
    # Ensure no env override is interfering
    os.environ.pop("MIRROR_BUFFER_DURATION_MS", None)
    assert interventions._buffer_duration_s(long) > interventions._buffer_duration_s(short)


def test_buffer_duration_long_enough_for_contradiction_buffer():
    """Default contradiction buffer is ~60 chars. Must be at least
    4.5s to actually finish playing on Plivo before correction fires."""
    os.environ.pop("MIRROR_BUFFER_DURATION_MS", None)
    text = CANNED["contradiction"]["buffer"]
    assert interventions._buffer_duration_s(text) >= 4.5, (
        "buffer duration is too short — correction will overlap with buffer audio"
    )


def test_env_override_takes_precedence():
    os.environ["MIRROR_BUFFER_DURATION_MS"] = "1000"
    try:
        # Even for a very long buffer text, the override should win
        assert interventions._buffer_duration_s("x" * 1000) == 1.0
    finally:
        os.environ.pop("MIRROR_BUFFER_DURATION_MS", None)


def test_minimum_two_seconds():
    os.environ.pop("MIRROR_BUFFER_DURATION_MS", None)
    # Even a near-empty buffer should hold at least 2s for safety
    assert interventions._buffer_duration_s("hi") >= 2.0
