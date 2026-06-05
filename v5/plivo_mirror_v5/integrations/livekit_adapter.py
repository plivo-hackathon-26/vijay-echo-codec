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


def _upload_recording(backend_url: str, call_id: str, recorder):
    """Render the call's WAV and POST it to the backend on a thread; returns
    the thread so the caller can briefly join it at teardown (otherwise a
    Ctrl-C in console mode kills the process before the upload finishes).
    Best-effort: failure just means no playback — it never raises."""
    import logging  # noqa: PLC0415
    import os  # noqa: PLC0415
    import threading  # noqa: PLC0415

    log = logging.getLogger("plivo_mirror_v5.adapter")

    def _send() -> None:
        try:
            wav = recorder.render_wav()
            if not wav:
                log.info("recording: nothing captured for %s (no audio frames)",
                         call_id)
                return
            import urllib.parse  # noqa: PLC0415
            import urllib.request  # noqa: PLC0415

            url = (f"{backend_url.rstrip('/')}/calls/"
                   f"{urllib.parse.quote(call_id)}/audio")
            headers = {"Content-Type": "audio/wav"}
            key = os.environ.get("MIRROR_API_KEY")
            if key:
                headers["X-API-Key"] = key
            req = urllib.request.Request(url, data=wav, headers=headers,
                                         method="POST")
            urllib.request.urlopen(req, timeout=30)  # noqa: S310
            log.info("recording: uploaded %d bytes for %s", len(wav), call_id)
        except Exception:  # noqa: BLE001
            log.warning("recording upload failed for %s", call_id, exc_info=True)

    t = threading.Thread(target=_send, name="mirror-rec-upload")
    t.start()
    return t


def _registered_judge(registered: dict):
    """Grounded judge from the dashboard-registered config (facts +
    policies + system prompt). MIRROR_JUDGE=two_stage selects the voting
    fast/strong judge. Shared by the pre-TTS gate and the shadow judge."""
    from plivo_mirror_v5.auditor import judge_from_env  # noqa: PLC0415

    facts_store = ReferenceStore(registered.get("facts") or {})
    return judge_from_env(
        facts={k: facts_store.get(k) for k in facts_store.keys()},
        policies=[s.strip() for s in
                  (registered.get("policies") or "").splitlines()
                  if s.strip()],
        system_prompt=registered.get("system_prompt") or None,
    )


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
    record: bool | None = None,
    shadow_judge=None,
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

    # Shadow-mode inline judge (flag-only): closes the real-time factual-
    # recall seam — a wrong price surfaces as would_have DURING the call,
    # not only post-call. Opt-in (costs one judge call per assertive agent
    # turn): pass shadow_judge= or set MIRROR_SHADOW_JUDGE=1.
    import os as _os_judge  # noqa: PLC0415
    if (shadow_judge is None and mode == "shadow"
            and _os_judge.environ.get("MIRROR_SHADOW_JUDGE") == "1"):
        try:
            shadow_judge = _registered_judge(registered)
        except Exception:  # noqa: BLE001 — judge wiring must never kill attach
            import logging  # noqa: PLC0415
            logging.getLogger("plivo_mirror_v5.adapter").exception(
                "shadow judge wiring failed; continuing without it")

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
        shadow_judge=shadow_judge,
    )
    bridge = _Bridge(room_id)
    observer.attach(bridge)  # registers observer._on_item on the bridge
    t0 = time.monotonic()  # ONE clock: per-turn offsets AND the recording share it

    def _now_ms() -> float:
        return (time.monotonic() - t0) * 1000.0

    # Recording is opt-in (record=True or MIRROR_RECORD=1); a real call only.
    import os as _os  # noqa: PLC0415
    recording_on = record if record is not None else _os.environ.get(
        "MIRROR_RECORD") == "1"
    recorder = None

    from plivo_mirror_v5.integrations.audio_levels import AudioLevelTap  # noqa: PLC0415
    if recording_on:
        from plivo_mirror_v5.integrations.recording import CallRecorder  # noqa: PLC0415
        recorder = CallRecorder()

    tap = audio_tap
    if tap is None and recording_on and agent is not None:
        # SESSION-LEVEL capture: tees the STT/TTS pipeline audio, so a local
        # `console` call records too (the room tap only sees `dev`-mode tracks).
        from plivo_mirror_v5.integrations.recording import (  # noqa: PLC0415
            install_session_recorder,
        )
        tap = AudioLevelTap()
        try:
            install_session_recorder(agent, recorder=recorder, tap=tap,
                                     now_ms=_now_ms)
        except Exception:  # noqa: BLE001 — fall back to room tap below
            import logging  # noqa: PLC0415
            logging.getLogger("plivo_mirror_v5.adapter").warning(
                "session recorder install failed; trying room tap", exc_info=True)
            tap = None
    if tap is None and room is not None:
        # Fallback / levels-only path: tap the room's audio tracks (dev mode).
        tap = AudioLevelTap(recorder=recorder)
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
        if recorder is not None:
            # Join briefly so the WAV finishes uploading before the worker
            # process exits (matters for console mode + Ctrl-C). Capped so a
            # slow/unreachable backend can't hang teardown.
            _upload_recording(backend_url, room_id, recorder).join(timeout=20)
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
            from plivo_mirror_v5.deployables.intervention import (  # noqa: PLC0415
                JudgedPreTTSGate,
            )
            from plivo_mirror_v5.integrations.pre_tts import (  # noqa: PLC0415
                PreTTSGateRunner,
            )

            # MIRROR_JUDGE=two_stage swaps in the voting fast/strong judge.
            judge = _registered_judge(registered)
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

            # Action-boundary block: a host tool calls agent._mirror_tool_gate
            # .check(name, args, agent._mirror_state) BEFORE its side effect to
            # STOP an unauthorized irreversible action (not just correct the
            # speech after). No policy → allows everything.
            from plivo_mirror_v5.engine import ToolGate  # noqa: PLC0415
            agent._mirror_tool_gate = ToolGate(engine_config.policy)
            agent._mirror_state = observer.state

            import logging as _logging  # noqa: PLC0415
            _alog = _logging.getLogger("plivo_mirror_v5.adapter")
            # DEFAULT-ON auto-wiring (each best-effort; a failure degrades
            # to the documented manual pattern, never kills attach):
            # 1. ToolGate wraps policy-named tools — block BEFORE execution.
            try:
                wrapped = _autowrap_tool_gate(
                    agent, agent._mirror_tool_gate, observer.state)
                if wrapped:
                    _alog.info("tool gate auto-wrapped: %s", ", ".join(wrapped))
            except Exception:  # noqa: BLE001
                _alog.exception("tool-gate auto-wrap failed; call "
                                "agent._mirror_tool_gate.check() manually")
            # 2. Hook B: route the default llm_node through the pre-TTS gate
            #    (skipped when the host already overrides llm_node).
            try:
                if _autowrap_llm_node(agent):
                    _alog.info("pre-TTS gate auto-wired into llm_node")
            except Exception:  # noqa: BLE001
                _alog.exception("llm_node auto-wrap failed; add the 2-line "
                                "llm_node override (see examples)")
        except Exception:  # noqa: BLE001 — gate wiring must never kill attach
            import logging  # noqa: PLC0415
            logging.getLogger("plivo_mirror_v5.adapter").exception(
                "pre-TTS gate wiring failed; falling back to Hook A only")
    # >>> end pre-TTS gate

    return observer


# ── intervene-mode auto-wiring (best-effort, SDK-coupled) ───────────────────
# Both helpers touch livekit-agents surface (lazy import; the adapter keeps
# no hard livekit dependency). They are called inside a guarded try/except —
# an unrecognized SDK shape degrades to the documented MANUAL patterns
# (agent llm_node override / explicit _mirror_tool_gate.check()).


def _autowrap_tool_gate(agent, gate, state) -> list[str]:
    """ACTION-BOUNDARY default-on: wrap every ``@function_tool`` whose name
    appears in the policy's ``tool_authorization`` / ``arg_bindings`` so
    ``ToolGate.check`` runs BEFORE the tool body — an unauthorized
    irreversible action is BLOCKED, not just flagged after the fact. The
    blocked result carries ``{"error": ...}`` so the tool log records it as
    failed (a later "I've done it" claim then diffs dirty) plus a
    ``say`` line the model can voice. Returns the wrapped tool names."""
    import inspect  # noqa: PLC0415

    from livekit.agents import function_tool  # noqa: PLC0415 — lazy, guarded

    guarded = set(gate.pack.tool_authorization) | set(gate.pack.arg_bindings)
    if not guarded:
        return []
    tools = list(getattr(agent, "tools", None) or [])
    new_tools, wrapped_names = [], []
    for tool in tools:
        info = getattr(tool, "info", None)
        name = getattr(info, "name", None) or getattr(tool, "__name__", None)
        if name not in guarded or not callable(tool):
            new_tools.append(tool)
            continue

        def _make(tool=tool, name=name):
            async def gated(*args, **kwargs):
                decision = gate.check(name, kwargs, state)
                if not decision.allow:
                    import logging  # noqa: PLC0415
                    logging.getLogger("plivo_mirror_v5.adapter").warning(
                        "tool gate BLOCKED %s: %s", name, decision.reason)
                    return {"error": decision.reason,
                            "blocked_by": decision.policy_id,
                            "say": decision.spoken_refusal}
                return await tool(*args, **kwargs)

            # The LLM-facing schema is rebuilt from the wrapper — copy the
            # original surface so the schema is byte-identical.
            gated.__signature__ = inspect.signature(tool)
            gated.__name__ = getattr(tool, "__name__", name)
            gated.__doc__ = tool.__doc__
            gated.__annotations__ = dict(getattr(tool, "__annotations__", {}))
            return function_tool(gated, name=name,
                                 description=getattr(info, "description", None))

        new_tools.append(_make())
        wrapped_names.append(name)
    if not wrapped_names:
        return []
    # Pre-start, Agent.update_tools completes synchronously (no awaits) —
    # drive it to completion deterministically; if it suspends the agent is
    # already live and we abort the wrap (manual pattern still applies).
    coro = agent.update_tools(new_tools)
    try:
        coro.send(None)
    except StopIteration:
        return wrapped_names
    coro.close()
    raise RuntimeError("agent already started; tool auto-wrap skipped")


def _autowrap_llm_node(agent) -> bool:
    """Hook B default-on: route the agent's default ``llm_node`` stream
    through ``agent._mirror_pre_tts`` (the judged pre-TTS gate) without the
    per-agent override. Skipped (returns False) when the host already
    overrode ``llm_node`` — their wiring stays canonical."""
    from livekit.agents import Agent as _LKAgent  # noqa: PLC0415 — lazy, guarded

    if type(agent).llm_node is not _LKAgent.llm_node:
        return False  # host override (e.g. the documented 2-line pattern)
    if "llm_node" in vars(agent):
        return False  # instance-level override already installed

    async def _gated_llm_node(chat_ctx, tools, model_settings):
        runner = getattr(agent, "_mirror_pre_tts", None)

        def default(ctx):
            return _LKAgent.default.llm_node(agent, ctx, tools, model_settings)

        if runner is None:  # gate gone → zero-cost passthrough
            async for chunk in default(chat_ctx):
                yield chunk
            return
        async for out in runner.gate_stream(chat_ctx, default):
            yield out

    agent.llm_node = _gated_llm_node
    return True


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
