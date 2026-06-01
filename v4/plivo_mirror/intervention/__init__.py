"""Intervention — agent-voice corrections that substitute risky output."""

from __future__ import annotations

from plivo_mirror.intervention.correction import (
    correction_for_spans,
    default_block_correction,
    reconfirm_correction,
)
from plivo_mirror.intervention.engine import (
    ESCALATION_LINE,
    InterventionResult,
    deflection_filler,
    run_intervention,
    stream_intervention,
    template_corrected_reply,
)
from plivo_mirror.intervention.packet import (
    CorrectionPacket,
    assert_no_echo,
    build_packet,
    echoes,
)
from plivo_mirror.intervention.regenerate import LLMReplyGenerator, ReplyGenerator

__all__ = [
    "correction_for_spans",
    "default_block_correction",
    "reconfirm_correction",
    "InterventionResult",
    "run_intervention",
    "stream_intervention",
    "deflection_filler",
    "template_corrected_reply",
    "ESCALATION_LINE",
    "CorrectionPacket",
    "build_packet",
    "assert_no_echo",
    "echoes",
    "LLMReplyGenerator",
    "ReplyGenerator",
]
