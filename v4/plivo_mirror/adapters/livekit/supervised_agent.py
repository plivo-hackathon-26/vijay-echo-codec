"""``SupervisedAgent`` — drop-in livekit-agents ``Agent`` subclass for v4.

Preserves v3's ~5-line integration shape but wires the v4 dual-boundary
firewall. On every LLM turn, in order:

  1. **Continuous state grounding (the v4 difference).** Inject a
     read-only summary of the validated ``SessionState`` (confirmed
     entities, intent, what's already done) into ``chat_ctx`` — every
     turn, not just after an intervention. Plus any held intent-memory
     note.
  2. **Buffer the LLM stream** (text + tool intents) so both guards can
     inspect the full planned reply and the pending tool calls.
  3. **Dual-boundary review** via ``firewall.review_turn`` — speech guard
     then action guard.
  4. **Persona guard** observes the exchange; can trigger a system-prompt
     re-injection or a code-enforced warm handoff.
  5. **Act:** on a clean pass, yield the original chunks (speak + let
     tools fire); on an intervention, drop the tool calls and STREAM the
     correction — the deflection filler is yielded to TTS first (no LLM),
     then the grounded answer is awaited and yielded, so the filler is
     already being spoken while regeneration runs.

Speculative-speech decision (documented, not faked): this adapter
**buffers the full LLM reply, then runs the guards, then yields**. The
guard *compute* on a clean turn is ~0 ms (deterministic risk-span + policy
checks; the verifier is NOT called when no risky span is present), so the
guards add no measurable compute latency on clean turns. BUT first-audio
on every turn waits on full-reply buffering — we do not stream tokens to
TTS before the reply is complete, because LiveKit buffers at the
``llm_node`` boundary and downstream TTS consumes the post-``llm_node``
text, leaving no clean hook to suppress a sub-span once audio has begun.
True token-streaming-until-a-risky-span (early-release) is a documented
FUTURE enhancement, not implemented here.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterable

from livekit.agents import Agent, llm
from livekit.agents.voice.agent import ModelSettings

from plivo_mirror.contracts import ToolCallIntent, TurnContext
from plivo_mirror.firewall import Firewall
from plivo_mirror.runtime.escalation import build_handoff
from plivo_mirror.runtime.grounding import (
    build_grounding_block,
    compose_injection,
    intent_note_block,
    persona_reinject_block,
)

log = logging.getLogger("plivo_mirror.adapters.livekit")


class SupervisedAgent(Agent):
    """Subclass exactly like ``Agent``, but pass ``firewall=<Firewall>``.

    ``grounding_channel`` selects how the read-only CONFIRMED-FACTS block
    is injected: ``"system"`` (default) or ``"developer"`` use a dedicated
    message — the correct channel for read-only context. Some Azure
    deployments ignore mid-conversation system messages; set
    ``grounding_channel="user_prefix"`` to fall back to prefixing the
    latest user message (the v3 workaround). If the system/developer
    channel raises, we fall back to the user prefix automatically.
    """

    def __init__(
        self,
        *,
        firewall: Firewall,
        grounding_channel: str = "system",
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self._firewall = firewall
        self._grounding_channel = grounding_channel
        self._last_customer_text = ""
        self._pending_reinject = ""  # persona summary to inject NEXT turn
        self._init_call_state()

    def _init_call_state(self, call_id: str = "") -> None:
        self._state = self._firewall.new_session(call_id)
        self._persona = self._firewall.new_persona_guard()
        self._intent = self._firewall.new_intent_memory()
        self._pending_reinject = ""
        # Auto-clear intent memory the moment any action commits.
        self._state.on_commit(lambda _ca: self._intent.clear())

    # ── lifecycle ─────────────────────────────────────────────────────

    async def on_enter(self) -> None:
        room_name = "lk-session"
        try:
            sess = self.session
            if sess and sess.room and sess.room.name:
                room_name = sess.room.name
        except Exception:
            pass
        self._init_call_state(room_name)
        log.info("Mirror v4 firewall attached to room=%s", room_name)
        await super().on_enter()

    # ── hook for writing validated entities into state ────────────────

    async def extract_state(self, customer_text: str) -> None:
        """Validate committable values out of the caller's utterance and
        write them to ``self.state`` OUTSIDE the model's context (the
        structural backbone). Default: delegate to the firewall's configured
        ``EntityExtractor`` (deterministic, ~0 ms) when one is wired; no-op
        otherwise. Override for richer, domain-specific NLU."""
        extractor = getattr(self._firewall, "extractor", None)
        if extractor is None:
            return None
        try:
            extractor.extract(customer_text, self._state)
        except Exception:
            log.debug("entity extractor raised", exc_info=True)
        return None

    @property
    def state(self):
        return self._state

    # ── LLM-node interception ─────────────────────────────────────────

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk | str]:
        # 1. Latest customer text → history + validated state writes.
        latest = _latest_user_text(chat_ctx)
        if latest:
            self._last_customer_text = latest
            try:
                await self.extract_state(latest)
            except Exception:
                log.debug("extract_state hook raised", exc_info=True)

        # 2. Continuous state-grounding injection (every turn) + held
        #    intent + any pending persona re-injection from last turn.
        injected = compose_injection(
            build_grounding_block(self._state),
            intent_note_block(self._intent.consume()),
            persona_reinject_block(self._pending_reinject),
        )
        self._pending_reinject = ""  # consumed
        if injected:
            _inject_context(chat_ctx, injected, channel=self._grounding_channel)

        # 3. Run the default LLM and buffer the full stream.
        default_stream = Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        if hasattr(default_stream, "__await__"):
            default_stream = await default_stream  # type: ignore[assignment]

        buffered: list[Any] = []
        text_parts: list[str] = []
        tool_intents: list[ToolCallIntent] = []
        async for chunk in default_stream:  # type: ignore[union-attr]
            buffered.append(chunk)
            _accumulate(chunk, text_parts, tool_intents)
        full_text = "".join(text_parts).strip()

        # No customer text yet (greeting) or nothing said: pass through.
        if not self._last_customer_text.strip():
            for c in buffered:
                yield c
            return

        # 4. Dual-boundary review.
        # NOTE (confidence signal): we do NOT populate ctx.logprobs here.
        # LiveKit's llm_node streams ChatChunks without token logprobs, and
        # the Azure gpt-5-mini agent model does not expose them. So the
        # confidence gate in SpeechGuard is inactive and routing is
        # risk-span-driven only (see CLAUDE.md). TODO(future): if a
        # logprob-capable agent model is used, thread its top-K logprobs
        # into TurnContext.logprobs to activate semantic-entropy routing.
        ctx = TurnContext(
            state=self._state,
            planned_reply=full_text,
            tool_intents=tool_intents,
            customer_text=self._last_customer_text,
        )
        try:
            verdict = await self._firewall.review_turn(ctx)
        except Exception:
            log.warning("Mirror review failed — failing open", exc_info=True)
            for c in buffered:
                yield c
            return

        log.info(
            "🛡  Mirror v4 %s  reason=%s",
            verdict.decision,
            (verdict.reason or "")[:120],
        )

        # 5. Persona guard — re-injection / escalation.
        signal = self._persona.observe_turn(
            customer_text=self._last_customer_text, agent_text=full_text
        )
        if signal.escalate:
            handoff = build_handoff(self._state, signal.reason)
            log.info("🛡  Mirror v4 escalation: %s", signal.reason)
            self._state.note_spoken("Let me bring in a specialist who can help.")
            yield (
                "I want to make sure you're taken care of — let me connect you "
                "with someone who can help directly."
            )
            # Hand `handoff.as_briefing()` to your transfer/SIP path here.
            _ = handoff
            return
        if signal.reinject:
            # Apply the persona-prompt summary on the NEXT turn's injection.
            self._pending_reinject = signal.reinject_text

        # 6. Act.
        if not verdict.intervened:
            self._state.note_spoken(full_text)
            for c in buffered:
                yield c
            return

        # Regeneration loop, STREAMED: the deflection filler is yielded to
        # TTS FIRST (it derives from the verdict and needs no LLM), then the
        # grounded answer is awaited and yielded — so the filler is already
        # being spoken while regeneration runs. Original tool calls dropped.
        if self._state.confirmed_intent:
            self._intent.hold(self._state.confirmed_intent, turns=3)

        def _on_escalate(handoff):
            log.info("🛡  Mirror v4 escalation: %s", handoff.reason)
            # Hand `handoff.as_briefing()` to your transfer/SIP path here.

        spoke_any = False
        try:
            async for chunk in self._firewall.intervene_stream(
                verdict, ctx, on_escalate=_on_escalate
            ):
                spoke_any = True
                self._state.note_spoken(chunk)
                yield chunk
        except Exception:
            log.warning("intervention stream failed", exc_info=True)
            if not spoke_any:  # fail open: at least speak the deflection
                fb = verdict.spoken_correction or "Let me make sure I have that right."
                self._state.note_spoken(fb)
                yield fb
        return


# ─────────────────────────── helpers ─────────────────────────────────


def _latest_user_text(chat_ctx: "llm.ChatContext") -> str:
    items = getattr(chat_ctx, "items", None) or getattr(chat_ctx, "messages", None) or []
    for msg in reversed(items):
        if getattr(msg, "role", None) != "user":
            continue
        tc = getattr(msg, "text_content", None)
        if callable(tc):
            try:
                tc = tc()
            except Exception:
                tc = None
        if isinstance(tc, str) and tc.strip():
            return tc.strip()
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [p if isinstance(p, str) else getattr(p, "text", "") for p in content]
            joined = " ".join(p.strip() for p in parts if p).strip()
            if joined:
                return joined
        break
    return ""


def _inject_context(
    chat_ctx: "llm.ChatContext", note: str, *, channel: str = "system"
) -> None:
    """Inject the read-only CONFIRMED-FACTS block.

    Preferred channel is a dedicated ``system``/``developer`` message (the
    correct home for read-only context). Some Azure deployments ignore
    mid-conversation system messages — pass ``channel="user_prefix"`` to
    prefix the latest user message instead. If the dedicated channel
    raises, we fall back to the user prefix automatically.
    """
    if channel in ("system", "developer"):
        try:
            chat_ctx.add_message(role=channel, content=note)
            return
        except Exception:
            log.debug("system/developer inject failed; falling back to user prefix", exc_info=True)

    _prefix_latest_user_message(chat_ctx, note)


def _prefix_latest_user_message(chat_ctx: "llm.ChatContext", note: str) -> None:
    items = getattr(chat_ctx, "items", None) or getattr(chat_ctx, "messages", None) or []
    wrapped = (
        "[Internal context for the agent only — never read aloud:\n"
        + note
        + "\nEnd of internal context.]\n\n"
    )
    for i in range(len(items) - 1, -1, -1):
        msg = items[i]
        if getattr(msg, "role", None) != "user":
            continue
        current = ""
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            current = content
        elif isinstance(content, list):
            current = " ".join(p for p in content if isinstance(p, str))
        try:
            if isinstance(content, list):
                msg.content = [wrapped + current]
            else:
                msg.content = wrapped + current
        except Exception:
            log.debug("could not prefix user message with grounding block", exc_info=True)
        return
    try:
        chat_ctx.add_message(role="system", content=note)
    except Exception:
        log.debug("could not inject grounding block (no user msg)", exc_info=True)


def _accumulate(
    chunk: Any, text_parts: list[str], tool_intents: list[ToolCallIntent]
) -> None:
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
    for tc in getattr(delta, "tool_calls", None) or []:
        name = getattr(tc, "name", None) or getattr(tc, "function_name", None) or ""
        args = getattr(tc, "arguments", None)
        parsed: dict = {}
        if isinstance(args, dict):
            parsed = args
        elif isinstance(args, str) and args.strip():
            try:
                parsed = json.loads(args)
            except json.JSONDecodeError:
                parsed = {"_raw": args}
        if name:
            tool_intents.append(ToolCallIntent(name=name, args=parsed))


__all__ = ["SupervisedAgent"]
