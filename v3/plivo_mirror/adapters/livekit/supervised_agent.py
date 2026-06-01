"""``SupervisedAgent`` — drop-in livekit-agents ``Agent`` subclass.

What it does, in order, on every LLM turn:

  1. **Latest-user-text extraction.** Scans ``chat_ctx`` backwards
     for the most recent user message and exposes it as
     ``self._last_customer_text``. Robust across all LiveKit v1.x
     ChatContext API shapes (``items`` vs ``messages``,
     ``text_content`` property vs callable, ``content`` as str vs
     list-of-parts).

  2. **Sticky intent-note injection.** If the parent
     ``CallSupervisor`` has a pending intent note (set after a
     previous intervention), prepend it to the most recent user
     message in ``chat_ctx``. This is more reliable than
     ``chat_ctx.add_message(role="system")`` because Azure (and
     some other providers) ignore system messages added after the
     initial system prompt.

  3. **Default LLM call.** Delegates to ``Agent.default.llm_node``
     and buffers the entire stream (text + tool_calls) so the
     supervisor can inspect both before TTS starts.

  4. **MirrorJudge scoring.** Runs the three-tier scorer. On
     intervention:
        a. apply cooldown (one correction per N seconds; LiveKit's
           preemptive generation re-invokes llm_node otherwise)
        b. set sticky intent note via supervisor.set_intent_note
        c. yield only the substituted correction text — original
           tool calls are dropped (never fire)

  5. **No intervention.** Yields the original ChatChunks unchanged
     so LiveKit's tool execution + TTS proceed normally.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterable

from livekit.agents import Agent, llm
from livekit.agents.voice.agent import ModelSettings

from plivo_mirror.context import (
    SupervisorContext,
    ToolCallIntent,
    TurnPayload,
    Verdict,
)
from plivo_mirror.supervisor import CallSupervisor, Supervisor

log = logging.getLogger("plivo_mirror.adapters.livekit")


class SupervisedAgent(Agent):
    """Mixin that supervises a LiveKit ``Agent``'s LLM turns via Mirror.

    Subclass it the same way you'd subclass ``Agent``, but pass
    ``supervisor=<your Supervisor instance>`` to ``__init__``. The
    SupervisedAgent attaches a ``CallSupervisor`` to itself on
    ``on_enter`` and runs the supervision pipeline on every ``llm_node``
    call. Tool definitions, instructions, voice pipeline configuration
    — all the regular LiveKit Agent stuff — are unchanged.

    Args:
        supervisor: A constructed plivo-mirror ``Supervisor``. Typically
                    ``Supervisor.from_env(policies=...)`` (v0.3.0+).
        intervention_cooldown_s: Min seconds between corrections per
                    customer turn. Defaults to 8s — long enough that
                    LiveKit's preemptive-generation re-invocations
                    don't all speak the correction.
        **kw:       Forwarded to the base ``Agent`` constructor
                    (``instructions=...``, ``tools=...``, etc.).
    """

    def __init__(
        self,
        *,
        supervisor: Supervisor,
        intervention_cooldown_s: float = 8.0,
        **kw,
    ) -> None:
        super().__init__(**kw)
        self._supervisor: Supervisor = supervisor
        self._call_sup: CallSupervisor | None = None
        self._last_customer_text: str = ""
        self._intervention_cooldown_s = intervention_cooldown_s
        self._cooldown_until: float = 0.0
        # Latency tracking + telemetry hook
        self._last_review_latency_ms: int = 0

    # ── lifecycle ────────────────────────────────────────────────────

    async def on_enter(self) -> None:
        """Open a CallSupervisor for this room.

        Picks the room name as the call_uuid for traceability.
        """
        room_name = "lk-session"
        try:
            sess = self.session
            if sess and sess.room and sess.room.name:
                room_name = sess.room.name
        except Exception:
            pass
        # Lazily attach; we don't have a TTSSink to give the supervisor,
        # but we don't need one — interventions are done via yielding
        # text from llm_node, and LiveKit handles the TTS path.
        try:
            self._call_sup = await self._supervisor.attach(
                tts_sink=_NullTTSSink(),
            ).__aenter__()
            self._call_sup.bind_call(room_name)
            log.info("Mirror supervised agent attached to room=%s", room_name)
        except Exception:
            log.exception("could not attach CallSupervisor — running without supervision")
        await super().on_enter()

    async def on_exit(self) -> None:
        if self._call_sup is not None:
            try:
                await self._call_sup.aclose()
            except Exception:
                log.debug("CallSupervisor.aclose() raised", exc_info=True)
            self._call_sup = None
        await super().on_exit()

    # ── LLM-node interception ────────────────────────────────────────

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk | str]:
        # 1. Pull customer text directly from chat_ctx (authoritative)
        latest = _latest_user_text(chat_ctx)
        if latest:
            self._last_customer_text = latest
            if self._call_sup is not None:
                self._call_sup.note_customer_turn(latest)

        # 2. Inject pending intent note (sticky across N turns)
        if self._call_sup is not None:
            note = self._call_sup.consume_intent_note()
            if note:
                _inject_intent_note(chat_ctx, note)
                log.info(
                    "🪞 Mirror — injected intent note (chars=%d)", len(note)
                )

        # 3. Run the default LLM and buffer the full stream
        default_stream = Agent.default.llm_node(
            self, chat_ctx, tools, model_settings
        )
        if hasattr(default_stream, "__await__"):
            default_stream = await default_stream  # type: ignore[assignment]

        buffered: list[Any] = []
        full_text_parts: list[str] = []
        tool_intents: list[ToolCallIntent] = []
        async for chunk in default_stream:  # type: ignore[union-attr]
            buffered.append(chunk)
            _accumulate_text_and_tools(chunk, full_text_parts, tool_intents)
        full_text = "".join(full_text_parts).strip()

        # 4. Run Mirror review (supervised path)
        if self._call_sup is None or not self._last_customer_text.strip():
            # No supervisor attached, or no customer text yet (greeting):
            # pass the original stream straight through.
            for c in buffered:
                yield c
            return

        # Build turn payload + score
        ctx = SupervisorContext(call_uuid=self._call_sup.call_uuid or "lk-session")
        turn = TurnPayload(
            customer_text=self._last_customer_text,
            primary_text=full_text,
            tool_calls=tool_intents,
            history=[],  # CallSupervisor owns history via note_*_turn
        )

        review_start = time.monotonic()
        try:
            # Use the public scorer attached to the parent Supervisor.
            verdict = await self._supervisor._scorer.score(turn, ctx)  # noqa: SLF001
        except Exception:
            log.warning(
                "🪞 Mirror review failed — failing open, passing through",
                exc_info=True,
            )
            for c in buffered:
                yield c
            return

        self._last_review_latency_ms = int((time.monotonic() - review_start) * 1000)

        # Compact one-line verdict log
        decided = (verdict.evidence or {}).get("deciding_tier", "?")
        log.info(
            "🪞 Mirror %s  score=%.2f  intervene=%s  reason=%s  latency=%dms",
            decided, verdict.score, verdict.should_intervene,
            (verdict.reason or "")[:120],
            self._last_review_latency_ms,
        )

        if not verdict.should_intervene:
            # Happy path — pass original chunks through
            for c in buffered:
                yield c
            return

        # ─── intervention path ───
        # Cooldown: LiveKit's preemptive generation can re-invoke
        # llm_node 2-3× per real customer turn. Suppress duplicate
        # correction speech but still drop the (bad) tool calls.
        now = time.monotonic()
        if now < self._cooldown_until:
            remaining = self._cooldown_until - now
            log.info(
                "🪞 Mirror cooldown active (%.1fs left) — suppressing duplicate intervention",
                remaining,
            )
            yield ""
            return
        self._cooldown_until = now + self._intervention_cooldown_s

        # Sticky intent-note for the NEXT turn so the LLM doesn't re-ask
        try:
            note = verdict.post_correction_context(
                customer_text=self._last_customer_text
            )
            self._call_sup.set_intent_note(note, turns=3)
        except Exception:
            log.debug("could not build post-correction context", exc_info=True)

        spoken = verdict.spoken_correction()
        log.info("🪞 Mirror correction: %r", spoken[:160])
        # Yield ONLY the substitute text. Original tool_calls dropped.
        yield spoken


# ─────────────────────── helpers ────────────────────────────────────


class _NullTTSSink:
    """Inert TTSSink — Mirror's CallSupervisor demands one but for the
    LiveKit adapter we drive TTS through ``llm_node`` returns instead.
    The sink is never actually used to play audio."""

    async def clear_audio(self) -> None: ...
    async def speak(self, text: str, *, checkpoint: str | None = None) -> None: ...
    async def wait_checkpoint(self, name: str, *, timeout_s: float = 10.0) -> bool:
        return True
    async def precompute(self, text: str) -> bytes | None:
        return None
    async def play_precomputed(self, audio, *, checkpoint=None) -> None: ...


def _latest_user_text(chat_ctx: llm.ChatContext) -> str:
    """Scan ``chat_ctx`` backwards for the most recent user message and
    return its concatenated text content. Handles all v1.x API shapes.
    """
    items = (
        getattr(chat_ctx, "items", None)
        or getattr(chat_ctx, "messages", None)
        or []
    )
    if not items:
        return ""
    for msg in reversed(items):
        if getattr(msg, "role", None) != "user":
            continue
        # Try text_content first
        tc = getattr(msg, "text_content", None)
        if tc is not None:
            if callable(tc):
                try:
                    tc = tc()
                except Exception:
                    tc = None
            if isinstance(tc, str) and tc.strip():
                return tc.strip()
        # Walk content
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                else:
                    text = getattr(part, "text", None)
                    if isinstance(text, str):
                        parts.append(text)
            joined = " ".join(p.strip() for p in parts if p).strip()
            if joined:
                return joined
        # Found latest user message but it's empty — don't keep walking
        break
    return ""


def _inject_intent_note(chat_ctx: llm.ChatContext, note: str) -> None:
    """Prepend a Mirror context note to the most recent user message
    in ``chat_ctx``.

    We mutate the user message rather than calling
    ``chat_ctx.add_message(role="system", ...)`` because Azure
    (and certain other providers) treat the initial system message
    as authoritative and ignore subsequent system messages added
    mid-conversation. User-message prefixes are always read.
    """
    items = (
        getattr(chat_ctx, "items", None)
        or getattr(chat_ctx, "messages", None)
        or []
    )
    for i in range(len(items) - 1, -1, -1):
        msg = items[i]
        if getattr(msg, "role", None) != "user":
            continue
        current = ""
        tc = getattr(msg, "text_content", None)
        if tc is not None:
            if callable(tc):
                try:
                    current = tc() or ""
                except Exception:
                    current = ""
            elif isinstance(tc, str):
                current = tc
        if not current:
            content = getattr(msg, "content", None)
            if isinstance(content, str):
                current = content
            elif isinstance(content, list):
                current = " ".join(
                    p for p in content if isinstance(p, str)
                )
        prefixed = (
            "[Internal context for the agent only — never read aloud:\n"
            + note
            + "\nEnd of internal context.]\n\n"
            + current
        )
        try:
            if isinstance(getattr(msg, "content", None), list):
                msg.content = [prefixed]
            else:
                msg.content = prefixed
        except Exception:
            # Immutable message — fall back to add_message
            try:
                chat_ctx.add_message(role="system", content=note)
            except Exception:
                log.debug("could not inject intent note", exc_info=True)
        return
    # No user message found — fall back to system message
    try:
        chat_ctx.add_message(role="system", content=note)
    except Exception:
        log.debug("could not inject intent note (no user msg)", exc_info=True)


def _accumulate_text_and_tools(
    chunk: Any,
    text_parts: list[str],
    tool_intents: list[ToolCallIntent],
) -> None:
    """Pull text content + tool-call intents out of a single LiveKit
    LLM chunk. Tolerates both ``ChatChunk`` and bare string yields."""
    if chunk is None:
        return
    if isinstance(chunk, str):
        text_parts.append(chunk)
        return
    delta = getattr(chunk, "delta", None)
    if delta is None:
        return
    content = getattr(delta, "content", None)
    if content:
        text_parts.append(content)
    raw_tools = getattr(delta, "tool_calls", None) or []
    for tc in raw_tools:
        name = (
            getattr(tc, "name", None)
            or getattr(tc, "function_name", None)
            or ""
        )
        args = getattr(tc, "arguments", None)
        parsed_args: dict = {}
        if isinstance(args, dict):
            parsed_args = args
        elif isinstance(args, str) and args.strip():
            try:
                parsed_args = json.loads(args)
            except json.JSONDecodeError:
                parsed_args = {"_raw": args}
        if name:
            tool_intents.append(
                ToolCallIntent(name=name, args=parsed_args, irreversible=False)
            )


__all__ = ["SupervisedAgent"]
