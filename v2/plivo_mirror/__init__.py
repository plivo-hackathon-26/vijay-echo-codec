"""plivo-mirror — silent supervisor for Plivo voice agents.

Public API (import from here):

    Supervisor, CallSupervisor    — main entry points
    MirrorConfig                  — single configuration object
    Verdict                       — scorer output
    ToolCallIntent, HistoryTurn   — context dataclasses
    SupervisorContext             — per-call context

Sub-packages:
    plivo_mirror.llm.openai       — OpenAIClient (+ Azure auto-detect)
    plivo_mirror.state.memory     — InMemoryStateStore (default)
    plivo_mirror.voice.tts.{ws_inject, plivo_speak}
                                   — Plivo TTS sinks
    plivo_mirror.plivo.{stream_sdk, raw_ws}
                                   — Plivo binding helpers
    plivo_mirror.replay           — offline replay CLI for tuning

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
from plivo_mirror.supervisor import CallSupervisor, Supervisor

__version__ = "0.1.0"

__all__ = [
    "Supervisor",
    "CallSupervisor",
    "MirrorConfig",
    "Verdict",
    "TurnPayload",
    "TurnOutcome",
    "ToolCallIntent",
    "HistoryTurn",
    "SupervisorContext",
    "__version__",
]
