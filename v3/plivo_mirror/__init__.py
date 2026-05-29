"""plivo-mirror — self-correcting voice agent supervisor.

v0.2.0 introduces the Mirror Judge: a three-tier scoring ensemble
(deterministic checks → NLI classifier → LLM judge) that replaces the
v0.1.0 single-LLM scorer. Median scoring latency drops to ~35ms and
per-call cost drops ~10×, while the deciding-tier evidence makes every
verdict auditable.

Public API (import from here):

    Supervisor, CallSupervisor    — main entry points
    MirrorConfig                  — single configuration object
    Verdict                       — scorer output
    ToolCallIntent, HistoryTurn   — context dataclasses
    SupervisorContext             — per-call context
    MirrorJudge                   — three-tier scorer (v0.2.0+)

Sub-packages:
    plivo_mirror.scorer.mirror_judge  — MirrorJudge orchestrator
    plivo_mirror.scorer.tier0         — deterministic checks
    plivo_mirror.scorer.tier1         — HF DeBERTa NLI classifier
    plivo_mirror.scorer.tier2         — Atla Selene judge
    plivo_mirror.llm.openai           — OpenAIClient (+ Azure auto-detect)
    plivo_mirror.state.memory         — InMemoryStateStore (default)
    plivo_mirror.voice.tts.{ws_inject, plivo_speak}
                                       — Plivo TTS sinks
    plivo_mirror.plivo.{stream_sdk, raw_ws}
                                       — Plivo binding helpers
    plivo_mirror.replay               — offline replay CLI for tuning

This module has **zero import-time side effects**. Importing
`plivo_mirror` never monkey-patches third-party modules, never starts
background tasks, never reads env vars. All wiring is explicit.
"""

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import (
    HistoryTurn,
    SupervisorContext,
    ToolCallIntent,
    TurnOutcome,
    TurnPayload,
    Verdict,
)
from plivo_mirror.scorer.mirror_judge import MirrorJudge
from plivo_mirror.supervisor import CallSupervisor, Supervisor

__version__ = "0.2.0"

__all__ = [
    "Supervisor",
    "CallSupervisor",
    "MirrorConfig",
    "MirrorJudge",
    "Verdict",
    "TurnPayload",
    "TurnOutcome",
    "ToolCallIntent",
    "HistoryTurn",
    "SupervisorContext",
    "__version__",
]
