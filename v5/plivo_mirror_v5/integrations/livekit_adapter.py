"""attach_mirror — wire a REAL livekit-agents ``AgentSession`` to the
monitoring backend in a few lines:

    from plivo_mirror_v5.integrations import attach_mirror

    session = AgentSession(stt=..., llm=..., tts=...)
    ...
    await ctx.connect()
    attach_mirror(
        session,
        room_id=ctx.room.name,                  # call_id == LiveKit room id
        reference=ReferenceStore.from_file("reference.json"),
        kb=KeywordKBRetriever.from_file("kb.json"),
        backend_url="http://localhost:8500",    # the monitoring backend
        agent_id="aurora-support", agent_version="1.0.0",
        action_verbs={"cancel_service": ["cancelled", "canceled"]},
    )
    await session.start(agent=MyAgent(), room=ctx.room)

What it hooks (all sync handlers; evaluation is scheduled off the loop and
telemetry goes through a ``ThreadedSink`` — the live call NEVER waits):

- ``conversation_item_added``  → both roles flow to the engine (the agent's
  turns are what we verify; user turns feed the L1 gate). The item's
  ``transcript_confidence`` becomes ``asr_confidence``.
- ``function_tools_executed``  → executed tools land in ``SessionState``'s
  tool log BEFORE the agent speaks about them, so "I've cancelled it" is
  diffed against reality (speech-vs-action).
- ``close``                    → ends the call span.

The module is duck-typed against livekit's event payloads on purpose — it
imports nothing from livekit, so the engine + tests stay dependency-free.

# TODO: real per-turn audio levels (RMS taps on the audio stream) for the
# signal view — the timeline currently uses turn offsets only.
# TODO: map livekit STT word-level confidence once exposed per-item.
"""

from __future__ import annotations

import json
import time

from plivo_mirror_v5.engine import Engine, EngineConfig, KBRetriever, ReferenceStore
from plivo_mirror_v5.engine.claims import LexiconClaimExtractor
from plivo_mirror_v5.integrations.livekit_observer import (
    ConversationItem,
    InterventionHandler,
    MirrorObserver,
)
from plivo_mirror_v5.telemetry import (
    HTTPSink,
    TelemetryEmitter,
    TelemetrySink,
    ThreadedSink,
)

_ROLE_MAP = {"assistant": "agent", "user": "user"}


def attach_mirror(
    session,
    *,
    room_id: str,
    reference: ReferenceStore,
    kb: KBRetriever | None = None,
    backend_url: str = "http://localhost:8500",
    sink: TelemetrySink | None = None,
    agent_id: str = "unknown",
    agent_version: str = "unknown",
    mode: str = "shadow",
    config: EngineConfig | None = None,
    action_verbs: dict[str, list[str]] | None = None,
    claim_extractor=None,
    intervention_handler: InterventionHandler | None = None,
    room=None,
    audio_tap=None,
) -> MirrorObserver:
    """Build engine + emitter + observer and subscribe to the session.
    Returns the observer (handy for tests and graceful shutdown).

    Pass ``room=ctx.room`` to also tap audio tracks for real per-turn
    waveform levels in the dashboard (best-effort; cosmetic only)."""
    engine = Engine(config or EngineConfig(mode=mode), reference=reference, kb=kb)
    sink = sink or ThreadedSink(HTTPSink(backend_url))
    emitter = TelemetryEmitter(sink)
    observer = MirrorObserver(
        engine,
        emitter,
        mode=mode,
        agent_id=agent_id,
        agent_version=agent_version,
        claim_extractor=claim_extractor
        or LexiconClaimExtractor(reference, action_verbs=action_verbs),
        intervention_handler=intervention_handler,
    )
    bridge = _Bridge(room_id)
    observer.attach(bridge)  # registers observer._on_item on the bridge
    t0 = time.monotonic()

    tap = audio_tap
    if tap is None and room is not None:
        from plivo_mirror_v5.integrations.audio_levels import AudioLevelTap  # noqa: PLC0415
        tap = AudioLevelTap()
        tap.tap_room(room)
    # A conversation item lands when the utterance COMMITS, so the turn's
    # audio window is [previous item's commit, this commit].
    last_commit_ms = [0.0]

    def _on_conversation_item(ev) -> None:
        item = getattr(ev, "item", ev)
        if getattr(item, "type", "message") != "message":
            return  # agent handoffs etc.
        role = _ROLE_MAP.get(getattr(item, "role", None))
        if role is None:
            return  # system/developer messages are not spoken turns
        text = getattr(item, "text_content", None) or ""
        if not text.strip():
            return
        now_ms = (time.monotonic() - t0) * 1000.0
        start_ms = last_commit_ms[0]
        last_commit_ms[0] = now_ms
        levels = tap.levels_for(role, start_ms, now_ms) if tap else None
        bridge.dispatch(ConversationItem(
            role=role,
            text=text,
            asr_confidence=getattr(item, "transcript_confidence", None),
            audio_offset_ms=start_ms,
            audio_duration_ms=now_ms - start_ms,
            audio_levels=levels,
        ))

    def _on_tools_executed(ev) -> None:
        # Into the tool log BEFORE the agent's next utterance is evaluated.
        for call, output in ev.zipped():
            observer.state.record_tool_call({
                "name": call.name,
                "args": _parse_json(getattr(call, "arguments", None)),
                "result": _tool_result(output),
                "t_result": (time.monotonic() - t0) * 1000.0,
            })

    def _on_close(_ev=None) -> None:
        observer.close()
        if isinstance(sink, ThreadedSink):
            sink.close()

    session.on("conversation_item_added", _on_conversation_item)
    session.on("function_tools_executed", _on_tools_executed)
    session.on("close", _on_close)
    return observer


class _Bridge:
    """Minimal session-shaped object the MirrorObserver attaches to; the
    adapter translates real livekit events into ``ConversationItem``s and
    pushes them through it."""

    def __init__(self, room_id: str) -> None:
        self.room_id = room_id
        self._handler = None

    def on(self, _event: str, handler) -> None:
        self._handler = handler

    def dispatch(self, item: ConversationItem) -> None:
        self._handler(item)


def _parse_json(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except ValueError:
            return {"_raw": raw}
    return {}


def _tool_result(output) -> dict:
    if output is None:
        return {}
    if getattr(output, "is_error", False):
        return {"error": str(getattr(output, "output", "error"))}
    return {"output": _parse_json(getattr(output, "output", None)) or
            str(getattr(output, "output", ""))}
