"""attach_mirror — wire a REAL livekit-agents ``AgentSession`` to the
monitoring backend in a few lines:

    from plivo_mirror_v5.integrations import attach_mirror

    session = AgentSession(stt=..., llm=..., tts=...)
    my_agent = MyAgent()
    ...
    await ctx.connect()
    attach_mirror(
        session,
        room_id=ctx.room.name,                  # call_id == LiveKit room id
        backend_url="http://localhost:8500",    # the monitoring backend
        agent_id="aurora-support",              # ← the id you REGISTERED in
        agent_version="1.0.0",                  #   the dashboard's Agents tab
        agent=my_agent,                         # enables dashboard-toggled intervene
    )
    await session.start(agent=my_agent, room=ctx.room)

REGISTRATION-DRIVEN CONFIG: at attach time the adapter best-effort fetches
``GET {backend_url}/agents/{agent_id}/config``. A dashboard-registered
agent supplies its facts (→ ReferenceStore for L2) and its mode — flipping
the agent to "intervene" in the dashboard makes the NEXT call attach with
Hook A wired (requires ``agent=``). No registration, no reachable backend →
shadow mode with whatever ``reference=`` was passed locally; attaching
never fails because the registry is down.

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

from plivo_mirror_v5.engine import Engine, EngineConfig, ReferenceStore
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


def fetch_agent_config(backend_url: str, agent_id: str,
                       timeout: float = 2.0) -> dict | None:
    """Best-effort pull of the dashboard-registered config. Never raises —
    an unreachable registry must never stop a call from being supervised."""
    import urllib.parse  # noqa: PLC0415 — stdlib, lazy
    import urllib.request  # noqa: PLC0415

    url = f"{backend_url.rstrip('/')}/agents/{urllib.parse.quote(agent_id)}/config"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def attach_mirror(
    session,
    *,
    room_id: str,
    reference: ReferenceStore | None = None,
    backend_url: str = "http://localhost:8500",
    sink: TelemetrySink | None = None,
    agent_id: str = "unknown",
    agent_version: str = "unknown",
    mode: str | None = None,
    config: EngineConfig | None = None,
    action_verbs: dict[str, list[str]] | None = None,
    claim_extractor=None,
    intervention_handler: InterventionHandler | None = None,
    agent=None,
    room=None,
    audio_tap=None,
) -> MirrorObserver:
    """Build engine + emitter + observer and subscribe to the session.
    Returns the observer (handy for tests and graceful shutdown).

    Config resolution (explicit args always win over the registry):
    - ``reference``: local arg → registered facts → empty store.
    - ``mode``: local arg → registered mode → "shadow".
    - intervene mode + ``agent=`` and no handler → Hook A auto-wired.

    Pass ``room=ctx.room`` to also tap audio tracks for real per-turn
    waveform levels in the dashboard (best-effort; cosmetic only)."""
    registered = fetch_agent_config(backend_url, agent_id) or {}
    if mode is None:
        mode = registered.get("mode") or "shadow"
    if reference is None:
        reference = ReferenceStore(registered.get("facts") or {})

    if (mode == "intervene" and intervention_handler is None
            and agent is not None):
        from plivo_mirror_v5.deployables.intervention import (  # noqa: PLC0415
            HookANextTurn,
        )
        # session= enables PROACTIVE delivery: filler + immediate corrected
        # reply, instead of waiting for the caller's next utterance.
        intervention_handler = HookANextTurn(agent, config, session=session)

    engine_config = config or EngineConfig(mode=mode)

    engine = Engine(engine_config, reference=reference)
    sink = sink or ThreadedSink(HTTPSink(backend_url))
    emitter = TelemetryEmitter(sink)
    observer = MirrorObserver(
        engine,
        emitter,
        mode=mode,
        agent_id=agent_id,
        agent_version=agent_version,
        # LIVE default: action claims only (speech-vs-action) — lexicon
        # fact-claims misattribute numbers in free speech (live FPs) and
        # the grounded judge owns factual claims at the gate / post-call.
        claim_extractor=claim_extractor
        or LexiconClaimExtractor(reference, action_verbs=action_verbs,
                                 fact_claims=False),
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
    # Executed tools buffer until the agent's NEXT utterance and ride that
    # TurnInput — so the tool-side policy checks (arg bindings, authorization
    # separation) actually SEE the call's args live, and the engine commits
    # them to the session tool log in turn order (speech-vs-action).
    pending_tools: list[dict] = []

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
        tool_calls = []
        if role == "agent" and pending_tools:
            tool_calls = list(pending_tools)
            pending_tools.clear()
        bridge.dispatch(ConversationItem(
            role=role,
            text=text,
            asr_confidence=getattr(item, "transcript_confidence", None),
            tool_calls=tool_calls,
            audio_offset_ms=start_ms,
            audio_duration_ms=now_ms - start_ms,
            audio_levels=levels,
        ))

    _MAX_PENDING_TOOLS = 50  # bound the buffer: a tool-looping agent that
    # never speaks must not grow memory without limit; oldest are dropped
    # (they'd be stale by the time the agent finally reports anyway).

    def _on_tools_executed(ev) -> None:
        for call, output in ev.zipped():
            pending_tools.append({
                "name": call.name,
                "args": _parse_json(getattr(call, "arguments", None)),
                "result": _tool_result(output),
                "t_result": (time.monotonic() - t0) * 1000.0,
            })
        del pending_tools[:-_MAX_PENDING_TOOLS]

    def _on_close(_ev=None) -> None:
        observer.close()
        if isinstance(sink, ThreadedSink):
            sink.close()

    session.on("conversation_item_added", _on_conversation_item)
    session.on("function_tools_executed", _on_tools_executed)
    session.on("close", _on_close)

    # >>> pre-TTS gate (Hook B live): in intervene mode, the flagged draft
    # NEVER reaches the speaker. The agent opts in with a 2-line llm_node
    # override that routes its stream through agent._mirror_pre_tts.
    if mode == "intervene" and agent is not None:
        try:
            from plivo_mirror_v5.auditor import LLMPostCallJudge  # noqa: PLC0415
            from plivo_mirror_v5.deployables.intervention import (  # noqa: PLC0415
                JudgedPreTTSGate,
            )
            from plivo_mirror_v5.integrations.pre_tts import (  # noqa: PLC0415
                PreTTSGateRunner,
            )

            facts_store = ReferenceStore(registered.get("facts") or {})
            judge = LLMPostCallJudge(
                facts={k: facts_store.get(k) for k in facts_store.keys()},
                policies=[s.strip() for s in
                          (registered.get("policies") or "").splitlines()
                          if s.strip()],
                system_prompt=registered.get("system_prompt") or None,
            )
            gate = JudgedPreTTSGate(engine, judge, call_id=room_id)
            agent._mirror_pre_tts = PreTTSGateRunner(
                gate, observer.state,
                claim_extractor or LexiconClaimExtractor(
                    reference, action_verbs=action_verbs,
                    fact_claims=False))
            # The gate already corrects BEFORE speech — Hook A degrades to
            # silent context injection (no spoken double-correction); its
            # verdicts/actions still land in the dashboard.
            from plivo_mirror_v5.deployables.intervention import (  # noqa: PLC0415
                HookANextTurn as _HookA,
            )
            if isinstance(intervention_handler, _HookA):
                intervention_handler.session = None
        except Exception:  # noqa: BLE001 — gate wiring must never kill attach
            import logging  # noqa: PLC0415
            logging.getLogger("plivo_mirror_v5.adapter").exception(
                "pre-TTS gate wiring failed; falling back to Hook A only")
    # >>> end pre-TTS gate

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
