"""Supervisor — the public entry point.

A Supervisor is constructed ONCE per process. Per-call wiring happens
through ``Supervisor.attach(handler, tts_provider)`` which yields a
``CallSupervisor`` scoped to one Plivo call.

Usage:

    from plivo_mirror import Supervisor, MirrorConfig
    from plivo_mirror.llm.openai import OpenAIClient

    supervisor = Supervisor(MirrorConfig(
        llm=OpenAIClient(api_key="...", model="gpt-4o-mini"),
        policies=[
            "Never confirm a refund — transfer to a human.",
            "Always read back the order before placing it.",
        ],
    ))

    @app.websocket("/stream")
    async def my_handler(ws):
        handler = PlivoFastAPIStreamingHandler(ws)
        async with supervisor.attach(handler, tts_provider=my_tts) as sup:
            @handler.on_start
            async def _(event):
                sup.bind_call(event.start.call_id)

            # ... agent loop calls sup.review_turn / sup.speak / sup.intervene
            await handler.start()
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import (
    HistoryTurn,
    SupervisorContext,
    ToolCallIntent,
    TurnOutcome,
    TurnPayload,
    Verdict,
)
from plivo_mirror.intervention.generator import CorrectionGenerator
from plivo_mirror.intervention.orchestrator import (
    InterventionOrchestrator,
    InterventionResult,
)
from plivo_mirror.scorer.llm import LLMScorer
from plivo_mirror.scorer.pregate import should_score
from plivo_mirror.scorer.streaming import StreamingScorer
from plivo_mirror.scorer.tool_gate import ToolGate
from plivo_mirror.state.base import StateStore
from plivo_mirror.state.memory import InMemoryStateStore
from plivo_mirror.voice.tts.base import TTSSink
from plivo_mirror.voice.tts.ws_inject import PlivoStreamTTSSink, TTSProvider

log = logging.getLogger("plivo_mirror.supervisor")


class Supervisor:
    """Top-level Mirror instance. Construct once per process."""

    def __init__(
        self,
        config: MirrorConfig,
        *,
        state: StateStore | None = None,
        report_sink: Any | None = None,
        scorer: Any | None = None,
    ) -> None:
        """Construct a Supervisor.

        Args:
            config: MirrorConfig — single configuration object.
            state: Optional StateStore implementation. Defaults to
                InMemoryStateStore.
            report_sink: Optional persistent failure-report sink.
            scorer: Optional custom scorer that implements the
                Scorer protocol (``async score(turn, ctx) -> Verdict``).
                When None, falls back to the v0.1.0 LLMScorer (single
                LLM judge from ``config.llm``). Pass a ``MirrorJudge``
                instance to enable the v0.2.0 three-tier ensemble.
        """
        self._config = config
        self._state: StateStore = state or InMemoryStateStore()
        # v0.2.0: caller may inject any Scorer. v0.1.0 callers omit this
        # arg and transparently keep the LLMScorer behaviour.
        self._scorer = scorer if scorer is not None else LLMScorer(config)
        self._tool_gate = ToolGate(config)
        self._generator = CorrectionGenerator(config)
        self._orchestrator = InterventionOrchestrator(
            config, self._generator, self._state
        )
        # Optional post-call report sink. When set, every call that had
        # any intervention will produce a FailureReport at aclose().
        self._report_sink = report_sink
        self._report_generator = None
        if report_sink is not None:
            from plivo_mirror.reports.generator import ReportGenerator
            self._report_generator = ReportGenerator(config)

    # ─────────────────── v0.3.0: from_env() ergonomics ──────────────────

    @classmethod
    def from_env(
        cls,
        *,
        policies: list[str],
        intervention_threshold: float = 0.7,
        report_sink: Any | None = None,
        state: StateStore | None = None,
    ) -> "Supervisor":
        """Construct a Supervisor with sensible defaults from the
        environment. Auto-detects the best available Tier 2 brain.

        Tier 1 (NLI classifier): enabled when ``HF_API_KEY`` is set
            and ``MIRROR_DISABLE_TIER1`` is unset (or 0/false).

        Tier 2 (judge LLM): the first of these whose creds are
            present (unless ``MIRROR_TIER2`` forces a choice):

              1. ``ATLA_API_KEY``           → AtlaSeleneJudge
              2. ``AZURE_OPENAI_API_KEY``   → AzureOpenAIJudge
                 + ``AZURE_OPENAI_ENDPOINT``
                 + ``AZURE_OPENAI_DEPLOYMENT``
              3. ``OPENAI_API_KEY``          → OpenAICompatibleJudge
                 (with optional ``OPENAI_BASE_URL``, ``OPENAI_MODEL``)
              4. ``HF_API_KEY``              → HuggingFaceLLMJudge
              5. (nothing) → no Tier 2

        Override with ``MIRROR_TIER2=atla|azure|openai|hf|none``.

        The primary-agent LLM (``MirrorConfig.llm``) defaults to a
        stub since v0.2.0 callers using ``MirrorJudge`` don't need it
        for scoring. Callers that need the v0.1.x LLMScorer path
        should construct the Supervisor explicitly.
        """
        from plivo_mirror.scorer.mirror_judge import MirrorJudge
        from plivo_mirror.scorer.tier1 import HuggingFaceClassifier
        from plivo_mirror.scorer.tier2 import (
            AtlaSeleneJudge,
            AzureOpenAIJudge,
            HuggingFaceLLMJudge,
            OpenAICompatibleJudge,
        )

        # Inert LLM for MirrorConfig — MirrorJudge handles its own LLM
        # calls via the Tier 2 instance; LLMScorer fallback is gated
        # behind the no-tier2 case below.
        class _NoOpLLM:
            async def structured_output(self, *a, **kw): return {}
            async def chat(self, *a, **kw): return ""

        config = MirrorConfig(
            llm=_NoOpLLM(),
            policies=policies,
            intervention_threshold=intervention_threshold,
        )

        def _envstr(name: str) -> str:
            return (os.environ.get(name) or "").strip()

        def _envflag(name: str, default: bool = False) -> bool:
            v = _envstr(name).lower()
            if v in ("1", "true", "yes", "on"):
                return True
            if v in ("0", "false", "no", "off"):
                return False
            return default

        hf_key = _envstr("HF_API_KEY")
        atla_key = _envstr("ATLA_API_KEY")
        openai_key = _envstr("OPENAI_API_KEY")
        azure_key = _envstr("AZURE_OPENAI_API_KEY")
        azure_endpoint = _envstr("AZURE_OPENAI_ENDPOINT")
        azure_deployment = _envstr("AZURE_OPENAI_DEPLOYMENT")
        azure_api_version = (
            _envstr("AZURE_OPENAI_API_VERSION") or "2024-08-01-preview"
        )
        forced = _envstr("MIRROR_TIER2").lower()
        disable_tier1 = _envflag("MIRROR_DISABLE_TIER1", default=False)

        # Tier 1
        tier1 = None
        if hf_key and not disable_tier1:
            tier1 = HuggingFaceClassifier(api_key=hf_key)

        # Tier 2 — priority order: explicit forced choice → Atla →
        # Azure → OpenAI (or OpenAI-compatible) → HF Llama
        tier2: Any = None
        tier2_label = "none"
        azure_ready = bool(azure_key and azure_endpoint and azure_deployment)

        def _make_atla():
            nonlocal tier2_label
            tier2_label = "atla_selene"
            return AtlaSeleneJudge(
                api_key=atla_key,
                policies=policies,
                intervention_threshold=intervention_threshold,
            )

        def _make_azure():
            nonlocal tier2_label
            tier2_label = f"azure:{azure_deployment}"
            return AzureOpenAIJudge(
                api_key=azure_key,
                azure_endpoint=azure_endpoint,
                azure_deployment=azure_deployment,
                api_version=azure_api_version,
                policies=policies,
                intervention_threshold=intervention_threshold,
            )

        def _make_openai():
            nonlocal tier2_label
            base = _envstr("OPENAI_BASE_URL") or "https://api.openai.com/v1"
            model = _envstr("OPENAI_MODEL") or "gpt-4o-mini"
            tier2_label = f"openai:{model}"
            return OpenAICompatibleJudge(
                api_key=openai_key,
                model=model,
                base_url=base,
                policies=policies,
                intervention_threshold=intervention_threshold,
            )

        def _make_hf():
            nonlocal tier2_label
            tier2_label = "hf_llama"
            return HuggingFaceLLMJudge(
                api_key=hf_key,
                policies=policies,
                intervention_threshold=intervention_threshold,
            )

        if forced == "atla" and atla_key:
            tier2 = _make_atla()
        elif forced == "azure" and azure_ready:
            tier2 = _make_azure()
        elif forced == "openai" and openai_key:
            tier2 = _make_openai()
        elif forced == "hf" and hf_key:
            tier2 = _make_hf()
        elif forced == "none":
            tier2 = None
        elif atla_key:
            tier2 = _make_atla()
        elif azure_ready:
            tier2 = _make_azure()
        elif openai_key:
            tier2 = _make_openai()
        elif hf_key:
            tier2 = _make_hf()

        scorer = MirrorJudge(config=config, tier1=tier1, tier2=tier2)

        log.info(
            "Mirror Supervisor.from_env wired — Tier 0: ON, Tier 1: %s, Tier 2: %s",
            "HF DeBERTa" if tier1 else ("DISABLED" if disable_tier1 else "OFF (no HF_API_KEY)"),
            tier2_label,
        )
        return cls(
            config,
            state=state,
            report_sink=report_sink,
            scorer=scorer,
        )

    # ─────────────────────────── public surface ──────────────────────────

    @asynccontextmanager
    async def attach(
        self,
        handler: Any | None = None,
        *,
        tts_provider: TTSProvider | None = None,
        tts_sink: TTSSink | None = None,
    ) -> AsyncIterator["CallSupervisor"]:
        """Open a per-call supervisor.

        Provide either:
          - ``handler`` + ``tts_provider`` (we build the WS-inject sink)
          - ``tts_sink`` directly (e.g. ``PlivoRESTTTSSink`` for non-bidi
            calls, or a fake in tests)
        """
        if tts_sink is None:
            if handler is None or tts_provider is None:
                raise ValueError(
                    "attach() requires either `tts_sink=...` or both "
                    "`handler=...` and `tts_provider=...`"
                )
            tts_sink = PlivoStreamTTSSink(handler, tts_provider)

        call_sup = CallSupervisor(
            config=self._config,
            scorer=self._scorer,
            tool_gate=self._tool_gate,
            orchestrator=self._orchestrator,
            state=self._state,
            tts=tts_sink,
            report_sink=self._report_sink,
            report_generator=self._report_generator,
        )
        try:
            yield call_sup
        finally:
            await call_sup.aclose()

    async def review(
        self,
        *,
        customer_text: str,
        primary_text: str,
        tool_calls: list[ToolCallIntent] | None = None,
        history: list[HistoryTurn] | None = None,
        call_uuid: str = "",
        tenant_id: str | None = None,
    ) -> Verdict:
        """Detection-only API: score a single turn without auto-intervention.

        Useful when the customer wants to log Mirror's verdicts but
        intervene themselves (or just observe). Never raises.
        """
        turn = TurnPayload(
            customer_text=customer_text,
            primary_text=primary_text,
            tool_calls=tool_calls or [],
            history=history or [],
        )
        ctx = SupervisorContext(
            call_uuid=call_uuid or "review-only",
            tenant_id=tenant_id or self._config.tenant_id,
        )
        return await self._score_pipeline(turn, ctx, prev_intervention=False)

    # ─────────────────────────── shared pipeline ─────────────────────────

    async def _score_pipeline(
        self,
        turn: TurnPayload,
        ctx: SupervisorContext,
        *,
        prev_intervention: bool,
    ) -> Verdict:
        # 1. Cooldown check — skip everything if we're still in the
        #    suppression window from a prior intervention.
        cd = await self._state.get_cooldown(ctx.call_uuid)
        if cd > time.monotonic():
            return Verdict.no_intervention("in_cooldown")

        # 2. Tiered pre-gate — cheap heuristic decides if the LLM
        #    scorer needs to run.
        run_scorer, reason = should_score(
            turn, self._config, prev_intervention=prev_intervention
        )
        if not run_scorer:
            log.debug(
                "pregate skipped scorer (call=%s reason=%s)",
                ctx.call_uuid[:8],
                reason,
            )
            return Verdict.no_intervention(f"pregate:{reason}")

        # 3. Speech scorer.
        verdict = await self._scorer.score(turn, ctx)
        if verdict.should_intervene:
            return verdict

        # 4. Tool-gate (only when there are gated tools).
        if turn.tool_calls and any(
            self._tool_gate.is_gated(tc.name) for tc in turn.tool_calls
        ):
            tg_verdict = await self._tool_gate.review(
                turn.tool_calls, turn.customer_text, turn.history, ctx
            )
            if tg_verdict.should_intervene:
                return tg_verdict

        return verdict


class CallSupervisor:
    """Per-call supervisor handle. One instance per Plivo call.

    The user's agent loop interacts with this — never with the parent
    ``Supervisor`` directly during a call.
    """

    def __init__(
        self,
        config: MirrorConfig,
        scorer: Any,            # Scorer protocol — LLMScorer or MirrorJudge
        tool_gate: ToolGate,
        orchestrator: InterventionOrchestrator,
        state: StateStore,
        tts: TTSSink,
        *,
        report_sink: Any | None = None,
        report_generator: Any | None = None,
    ) -> None:
        self._config = config
        self._scorer = scorer
        self._tool_gate = tool_gate
        self._orchestrator = orchestrator
        self._state = state
        self._tts = tts
        self._ctx = SupervisorContext(call_uuid="", tenant_id=config.tenant_id)
        self._history: list[HistoryTurn] = []
        self._prev_intervention = False
        self._stream_scorer: StreamingScorer | None = None
        # Post-call reporting plumbing — both None means reporting is off.
        self._report_sink = report_sink
        self._report_generator = report_generator
        # Every Verdict produced this call is appended here for the
        # post-call ReportGenerator. We deliberately keep ALL verdicts
        # (not just intervened ones) so the generator has context.
        self._verdicts: list[Verdict] = []
        self._started_at = datetime.now(timezone.utc)
        # v0.1.0a4: dedupe irreversible tool calls across turns. Maps
        # "tool_name::args_json" → previous result dict. When the agent
        # tries to call the same irreversible tool with the same args
        # again on a later turn, run_supervised_openai_loop returns the
        # cached result instead of re-executing — preventing duplicate
        # place_orders / charges when the LLM gets confused.
        self._committed_tools: dict[str, dict[str, Any]] = {}
        # v0.3.0: sticky intent note. When Mirror intervenes, the LLM
        # loses the original tool intent (we substitute the response).
        # We persist a one-shot system note here so the NEXT few turns
        # know the customer's real intent and don't ask them to repeat.
        # Cleared on: tool fire / turn-count decay / explicit clear.
        self._pending_intent_note: str | None = None
        self._intent_note_turns_remaining: int = 0

    # ── tool dedupe (v0.1.0a4) ────────────────────────────────────────────

    def already_committed(self, tool_name: str, args_json: str) -> dict | None:
        """Return the previous result for (tool_name, args_json) if this
        irreversible tool has already fired with these args on this call.
        Used by run_supervised_openai_loop to short-circuit duplicate
        calls — the LLM sometimes re-emits the same place_order on a
        confirmation turn; we don't actually execute it twice."""
        return self._committed_tools.get(f"{tool_name}::{args_json}")

    def note_committed(self, tool_name: str, args_json: str, result: dict) -> None:
        """Memoize a successful irreversible tool call so future
        invocations with the same args return the cached result.

        v0.3.0: also auto-clears the pending intent note since the
        order has just been placed (no further "remind LLM what the
        customer wanted" is needed)."""
        self._committed_tools[f"{tool_name}::{args_json}"] = result
        if self._pending_intent_note:
            log.debug(
                "auto-clearing intent note (call=%s) after tool %r",
                self._ctx.call_uuid[:8], tool_name,
            )
            self._pending_intent_note = None
            self._intent_note_turns_remaining = 0

    # ── sticky intent note (v0.3.0) ───────────────────────────────────────

    def set_intent_note(self, note: str, *, turns: int = 3) -> None:
        """Persist a system-level note that should be injected into the
        next few LLM calls. Used after an intervention to remind the
        primary LLM of the customer's actual intent.

        Adapters (LiveKit, OpenAI loop, etc.) call ``consume_intent_note()``
        on each turn to fetch + decay the note.
        """
        self._pending_intent_note = note
        self._intent_note_turns_remaining = max(0, int(turns))

    def consume_intent_note(self) -> str | None:
        """Return the pending intent note and decrement its TTL.

        Returns ``None`` when no note is active. Each call decrements
        the remaining-turn counter; when it hits zero the note auto-
        clears. Adapters should call this once per LLM turn.
        """
        if not self._pending_intent_note:
            return None
        note = self._pending_intent_note
        self._intent_note_turns_remaining -= 1
        if self._intent_note_turns_remaining <= 0:
            self._pending_intent_note = None
        return note

    def clear_intent_note(self) -> None:
        """Force-clear the pending intent note. Called automatically
        by ``note_committed`` and on customer-initiated topic change."""
        self._pending_intent_note = None
        self._intent_note_turns_remaining = 0

    @property
    def pending_intent_note(self) -> str | None:
        """Read-only peek at the current intent note without consuming
        it. Returns ``None`` when no note is active."""
        return self._pending_intent_note

    # ── lifecycle ─────────────────────────────────────────────────────────

    def bind_call(self, call_uuid: str) -> None:
        """Set the call_uuid once Plivo's start event arrives. Required
        before any other per-call operation."""
        self._ctx = SupervisorContext(
            call_uuid=call_uuid, tenant_id=self._config.tenant_id
        )

    @property
    def call_uuid(self) -> str:
        return self._ctx.call_uuid

    @property
    def history(self) -> list[HistoryTurn]:
        """The conversation history Mirror has seen on this call.

        Returned by reference — append-only; do not mutate elements."""
        return self._history

    # ── transcript bookkeeping ────────────────────────────────────────────

    def note_customer_turn(self, text: str) -> None:
        if text:
            self._history.append(HistoryTurn(role="customer", text=text))

    def note_agent_turn(self, text: str) -> None:
        if text:
            self._history.append(HistoryTurn(role="agent", text=text))

    async def consume_override(self) -> str | None:
        """Pop the one-shot post-correction system note for the NEXT
        primary turn. Customer's agent should inject this as an extra
        system message so the rigged item-capture / multi-leg-capture
        rule is neutralised for that one turn."""
        note = await self._state.get_override(self._ctx.call_uuid)
        if note is not None:
            await self._state.clear_override(self._ctx.call_uuid)
        return note

    # ── detection ─────────────────────────────────────────────────────────

    async def review_turn(
        self,
        *,
        customer_text: str,
        primary_text: str,
        tool_calls: list[ToolCallIntent] | None = None,
    ) -> Verdict:
        """Score one full agent turn. Use this in turn-based stacks
        (full LLM response → review → speak)."""
        turn = TurnPayload(
            customer_text=customer_text,
            primary_text=primary_text,
            tool_calls=tool_calls or [],
            history=list(self._history),
        )
        return await self._run_pipeline(turn)

    async def review_stream_delta(
        self,
        *,
        customer_text: str,
        delta: str,
        tool_calls: list[ToolCallIntent] | None = None,
    ) -> Verdict | None:
        """Stream-based review. Feed each LLM delta. Returns ``None``
        while buffering; returns a ``Verdict`` once the first sentence
        boundary is hit (or when ``flush_stream`` is called).

        When a verdict is produced, the underlying StreamingScorer is
        reset automatically so the next turn starts fresh — callers
        don't have to call ``flush_stream`` to clear state."""
        if self._stream_scorer is None:
            self._stream_scorer = StreamingScorer(self._scorer)
        turn = TurnPayload(
            customer_text=customer_text,
            primary_text="",  # streaming scorer maintains its own buffer
            tool_calls=tool_calls or [],
            history=list(self._history),
            is_partial=True,
        )
        # Cooldown short-circuit — same as the turn-based path.
        cd = await self._state.get_cooldown(self._ctx.call_uuid)
        if cd > time.monotonic():
            return None
        verdict = await self._stream_scorer.feed(delta, turn, self._ctx)
        if verdict is not None:
            self._stream_scorer = None  # ready for next turn
        return verdict

    async def flush_stream(
        self,
        *,
        customer_text: str,
        tool_calls: list[ToolCallIntent] | None = None,
    ) -> Verdict | None:
        """Force the streaming scorer to produce a verdict on whatever
        has been buffered. Call this at end-of-stream if no boundary
        landed during ``review_stream_delta``."""
        if self._stream_scorer is None:
            return None
        turn = TurnPayload(
            customer_text=customer_text,
            primary_text="",
            tool_calls=tool_calls or [],
            history=list(self._history),
            is_partial=True,
        )
        v = await self._stream_scorer.flush(turn, self._ctx)
        self._stream_scorer = None  # ready for next turn
        return v

    async def gate_tool_call(
        self,
        *,
        customer_text: str,
        intents: list[ToolCallIntent],
    ) -> Verdict:
        """Score pending tool calls before they execute. Customer
        should check the returned verdict and skip the tool if
        ``should_intervene`` is true."""
        v = await self._tool_gate.review(
            intents, customer_text, list(self._history), self._ctx
        )
        # Record so the post-call ReportGenerator sees tool-gate verdicts.
        self._verdicts.append(v)
        return v

    async def run_supervised_loop(
        self,
        *,
        llm_client: Any,
        model: str,
        system_prompt: str,
        tool_specs: list[dict[str, Any]],
        tool_executors: dict[str, Any],
        customer_text: str,
        extra_system_note: str | None = None,
        irreversible: tuple[str, ...] = (),
        max_rounds: int = 3,
    ) -> "AgentResult":
        """High-level helper that runs an OpenAI chat-completions
        tool-use loop with Mirror's tool-gate INLINE — irreversible
        tools never fire until Mirror approves the agent's intent.

        See ``plivo_mirror.agents.openai_loop.run_supervised_openai_loop``
        for the full docstring. This method just plumbs through the
        per-call ``transcript`` and the supervisor instance.
        """
        # Import here to avoid circular imports.
        from plivo_mirror.agents.openai_loop import (
            AgentResult,
            run_supervised_openai_loop,
        )

        return await run_supervised_openai_loop(
            llm_client=llm_client,
            model=model,
            system_prompt=system_prompt,
            transcript=list(self._history),
            tool_specs=tool_specs,
            tool_executors=tool_executors,
            supervisor=self,
            customer_text=customer_text,
            extra_system_note=extra_system_note,
            irreversible=irreversible,
            max_rounds=max_rounds,
        )

    # ── action ────────────────────────────────────────────────────────────

    async def intervene(self, verdict: Verdict) -> InterventionResult:
        """Run the buffer + correction sequence. Records the correction
        text into history.

        v0.3.0: also auto-sets a sticky intent note so the next ~3
        LLM turns know the customer's actual intent. Adapters call
        ``consume_intent_note()`` to fetch + decay it.
        """
        result = await self._orchestrator.handle(
            verdict, list(self._history), self._tts, self._ctx
        )
        self.note_agent_turn(result.correction_text)
        self._prev_intervention = True

        # Capture the customer's most recent utterance for the note
        # synthesis. ``verdict.evidence.customer_intent`` is the
        # judge's read; fall back to history.
        latest_customer = ""
        for h in reversed(self._history):
            if h.role == "customer" and (h.text or "").strip():
                latest_customer = h.text
                break
        try:
            note = verdict.post_correction_context(customer_text=latest_customer)
            self.set_intent_note(note, turns=3)
            log.debug(
                "set sticky intent note (call=%s, 3 turns)",
                self._ctx.call_uuid[:8],
            )
        except Exception:
            log.exception("could not build post-correction context")
        return result

    async def speak(self, text: str) -> None:
        """Speak the agent's planned response through the TTS sink.
        Records it in history. Use this on the happy path (no
        intervention) so Mirror's view of the conversation stays in
        sync."""
        if not text:
            return
        await self._tts.speak(text)
        self.note_agent_turn(text)
        self._prev_intervention = False

    async def review_and_speak(
        self,
        *,
        customer_text: str,
        primary_text: str,
        tool_calls: list[ToolCallIntent] | None = None,
    ) -> "TurnOutcome":
        """One-call orchestration: parallel TTS encode + Mirror scorer,
        then either speak ``primary_text`` (happy path) or run
        ``intervene`` (correction path).

        This is the recommended high-level API for the WS handler. It
        kicks off TTS pre-rendering immediately (in parallel with the
        scorer LLM) so the happy path doesn't pay scorer latency on
        top of TTS latency.

        If the TTS sink doesn't support ``precompute`` (e.g. Plivo REST
        speak which has built-in TTS), the parallelism degrades to
        sequential automatically — no caller change needed.
        """
        if not primary_text:
            return TurnOutcome(
                verdict=Verdict.no_intervention("empty_primary"),
                spoken_text="",
                intervened=False,
            )

        # Kick off the TTS pre-render the moment we know what the agent
        # plans to say — runs concurrently with the scorer below.
        precompute_task: asyncio.Task | None = None
        try:
            precompute_task = asyncio.create_task(
                self._tts.precompute(primary_text)
            )
        except NotImplementedError:
            precompute_task = None  # sink can't pre-render; fall back to sequential

        # Score the turn (pre-gate + LLM scorer + tool-gate).
        turn = TurnPayload(
            customer_text=customer_text,
            primary_text=primary_text,
            tool_calls=tool_calls or [],
            history=list(self._history),
        )
        verdict = await self._run_pipeline(turn)

        # Intervention path — throw away the precomputed bytes.
        if verdict.should_intervene:
            if precompute_task is not None:
                precompute_task.cancel()
            result = await self.intervene(verdict)
            return TurnOutcome(
                verdict=verdict,
                spoken_text=result.correction_text,
                intervened=True,
            )

        # Happy path — use the precomputed bytes if the sink produced any.
        audio: bytes | None = None
        if precompute_task is not None:
            try:
                audio = await precompute_task
            except Exception:
                log.exception("precompute failed; falling back to sequential speak")
                audio = None

        try:
            if audio:
                await self._tts.play_precomputed(audio)
            else:
                await self._tts.speak(primary_text)
        except Exception:
            log.exception("speak path failed (call=%s)", self._ctx.call_uuid[:8])

        self.note_agent_turn(primary_text)
        self._prev_intervention = False
        return TurnOutcome(
            verdict=verdict,
            spoken_text=primary_text,
            intervened=False,
        )

    # ── cleanup ───────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Drop per-call state. Called automatically when the
        ``attach`` context manager exits.

        If a ``report_sink`` was wired on the parent ``Supervisor``,
        this also fires the post-call ``ReportGenerator`` for any call
        that had at least one intervention. Generation runs inline (we
        await it) so the caller has the report ID by the time
        ``attach`` returns — but failures are swallowed so the call
        teardown never crashes."""
        if self._ctx.call_uuid:
            try:
                await self._state.cleanup(self._ctx.call_uuid)
            except Exception:
                log.exception("state cleanup failed (call=%s)", self._ctx.call_uuid[:8])

        # Post-call report generation.
        if (
            self._report_sink is not None
            and self._report_generator is not None
            and any(v.should_intervene for v in self._verdicts)
        ):
            try:
                report = await self._report_generator.generate(
                    call_uuid=self._ctx.call_uuid,
                    tenant_id=self._ctx.tenant_id,
                    history=list(self._history),
                    verdicts=list(self._verdicts),
                    started_at=self._started_at,
                    ended_at=datetime.now(timezone.utc),
                )
                if report is not None:
                    rid = await self._report_sink.create(report)
                    log.info(
                        "failure_report id=%d created for call=%s severity=%s",
                        rid,
                        self._ctx.call_uuid[:8],
                        report.severity,
                    )
            except Exception:
                log.exception(
                    "post-call report generation failed (call=%s)",
                    self._ctx.call_uuid[:8],
                )

    # ── internals ─────────────────────────────────────────────────────────

    async def _run_pipeline(self, turn: TurnPayload) -> Verdict:
        # Cooldown.
        cd = await self._state.get_cooldown(self._ctx.call_uuid)
        if cd > time.monotonic():
            v = Verdict.no_intervention("in_cooldown")
            self._verdicts.append(v)
            return v

        # Pre-gate.
        run_scorer, reason = should_score(
            turn, self._config, prev_intervention=self._prev_intervention
        )
        if not run_scorer:
            v = Verdict.no_intervention(f"pregate:{reason}")
            self._verdicts.append(v)
            return v

        # Scorer.
        verdict = await self._scorer.score(turn, self._ctx)
        if verdict.should_intervene:
            self._verdicts.append(verdict)
            return verdict

        # Tool-gate (only if there's anything gated).
        if turn.tool_calls and any(
            self._tool_gate.is_gated(tc.name) for tc in turn.tool_calls
        ):
            tg_verdict = await self._tool_gate.review(
                turn.tool_calls,
                turn.customer_text,
                turn.history,
                self._ctx,
            )
            if tg_verdict.should_intervene:
                self._verdicts.append(tg_verdict)
                return tg_verdict

        self._verdicts.append(verdict)
        return verdict


__all__ = ["Supervisor", "CallSupervisor"]
