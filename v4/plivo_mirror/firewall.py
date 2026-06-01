"""``Firewall`` — the top-level facade. Construct ONCE per process.

Bundles the compiled policies, both guards, the (configurable) grounded
verifier, and the per-call factories (session state, persona guard, intent
memory). Keeps the integration tiny:

    firewall = Firewall.from_env(policies=POLICIES)        # 1
    class MyAgent(SupervisedAgent):                        # 2
        def __init__(self):                                # 3
            super().__init__(firewall=firewall, instructions=PROMPT)  # 4
        @function_tool
        async def place_order(self): ...                   # 5 (reads state)

The "last model for intervening" — the grounded verifier — is fully
configurable: pass ``verifier=...`` (any object implementing the
``Verifier`` protocol), or let ``from_env`` build the default
``LLMJudgeVerifier`` from your OpenAI/Azure creds with a configurable
``model``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from plivo_mirror.authz.service import AuthorizationService
from plivo_mirror.contracts import TurnContext, Verdict
from plivo_mirror.guards.action import ActionGuard, Validator
from plivo_mirror.guards.signal import ConfidenceSignal
from plivo_mirror.guards.speech import SpeechGuard
from plivo_mirror.policy.compiler import compile_policies
from plivo_mirror.runtime.intent_memory import IntentMemory
from plivo_mirror.runtime.loop import review_turn as _review_turn
from plivo_mirror.runtime.persona_guard import PersonaGuard
from plivo_mirror.state.session import SessionState
from plivo_mirror.verifier.base import Verifier

log = logging.getLogger("plivo_mirror.firewall")


class Firewall:
    def __init__(
        self,
        *,
        policies: list[str],
        verifier: Verifier | None = None,
        generator: Any | None = None,
        authz: AuthorizationService | None = None,
        validators: dict[str, list[Validator]] | None = None,
        confidence_signal: ConfidenceSignal | None = None,
        confidence_threshold: float = 0.6,
        persona_system_summary: str = "",
        persona_reinject_every: int = 6,
        persona_escalate_after: int = 20,
        max_correction_retries: int = 2,
    ) -> None:
        self._compiled = compile_policies(list(policies))
        self.verifier = verifier
        self.generator = generator  # reply regenerator (single-LLM by default)
        self._max_correction_retries = max_correction_retries
        self._speech = SpeechGuard(
            verifier,
            signal=confidence_signal,
            confidence_threshold=confidence_threshold,
        )
        self._action = ActionGuard(authz=authz, validators=validators)
        self._persona_cfg = dict(
            system_summary=persona_system_summary,
            reinject_every=persona_reinject_every,
            escalate_after=persona_escalate_after,
        )

    # ── config-time accessors ─────────────────────────────────────────

    @property
    def policies(self):
        return list(self._compiled)

    @property
    def speech_guard(self) -> SpeechGuard:
        return self._speech

    @property
    def action_guard(self) -> ActionGuard:
        return self._action

    # ── per-call factories ────────────────────────────────────────────

    def new_session(self, call_id: str = "") -> SessionState:
        return SessionState(call_id=call_id, policies=list(self._compiled))

    def new_persona_guard(self) -> PersonaGuard:
        return PersonaGuard(**self._persona_cfg)

    def new_intent_memory(self) -> IntentMemory:
        return IntentMemory()

    # ── per-turn entry point ──────────────────────────────────────────

    async def review_turn(self, context: TurnContext) -> Verdict:
        """Run the dual-boundary control loop on one turn."""
        return await _review_turn(self._speech, self._action, context)

    async def intervene(self, verdict: Verdict, context: TurnContext):
        """Turn a violating verdict into a grounded corrected answer:
        deflection filler + a structured-from-state or regenerated reply,
        re-verified through the speech guard (retries capped; escalates via
        ``build_handoff`` on non-convergence). Returns an
        ``InterventionResult``."""
        from plivo_mirror.intervention.engine import run_intervention

        return await run_intervention(
            verdict=verdict,
            context=context,
            speech_guard=self._speech,
            generator=self.generator,
            max_retries=self._max_correction_retries,
        )

    def intervene_stream(self, verdict: Verdict, context: TurnContext, *, on_escalate=None):
        """Stream the intervention as spoken chunks (deflection filler
        FIRST, then the grounded answer / escalation line). Use this on the
        hot path so the filler reaches TTS before the regeneration latency.
        Returns an async iterator of strings."""
        from plivo_mirror.intervention.engine import stream_intervention

        return stream_intervention(
            verdict=verdict,
            context=context,
            speech_guard=self._speech,
            generator=self.generator,
            max_retries=self._max_correction_retries,
            on_escalate=on_escalate,
        )

    # ── ergonomic construction ────────────────────────────────────────

    @classmethod
    def from_env(
        cls,
        *,
        policies: list[str],
        model: str | None = None,
        verifier: Verifier | None = None,
        verifier_model: str | None = None,
        generator: Any | None = None,
        client: Any | None = None,
        **kw: Any,
    ) -> "Firewall":
        """Construct with a SINGLE configured LLM by default.

        ``model`` is the voice agent's model. By default ONE model + creds
        serves all three LLM roles — agent replies, the grounded verifier,
        and reply regeneration. The verifier and generator are built from
        the SAME client. The verifier is still a separate, stateless
        entailment call (NOT the agent persona, NOT an in-context
        self-grade) — see ``LLMJudgeVerifier``.

        Overrides (single-LLM is the default, not a hard-wire):
          - ``verifier=...`` / ``generator=...`` — custom instances.
          - ``verifier_model=...`` — point ONLY the verifier at a different
            model (independent judge / fine-tune) while agent stays on
            ``model``.
          - ``client=...`` — inject an OpenAI-compatible async client
            (tests; skips env client construction).

        Env read when building the default client:
          - ``AZURE_OPENAI_API_KEY`` + ``AZURE_OPENAI_ENDPOINT`` +
            ``AZURE_OPENAI_DEPLOYMENT`` (+ ``AZURE_OPENAI_API_VERSION``)
          - else ``OPENAI_API_KEY`` (+ ``OPENAI_BASE_URL``, ``OPENAI_MODEL``)
        """
        from plivo_mirror.intervention.regenerate import LLMReplyGenerator
        from plivo_mirror.verifier.llm_judge import LLMJudgeVerifier

        resolved = _resolve_verifier_model(model, verifier_model)
        agent_model = model or resolved

        shared = client
        if (verifier is None or generator is None) and shared is None:
            shared = _build_client_from_env()

        if verifier is None and shared is not None:
            verifier = LLMJudgeVerifier(shared, model=resolved)
        if generator is None and shared is not None:
            # Regeneration reuses the MAIN voice model (single-LLM).
            generator = LLMReplyGenerator(shared, model=agent_model)

        if verifier is None:
            log.warning("no verifier wired; speech guard fails open on risky spans")

        return cls(policies=policies, verifier=verifier, generator=generator, **kw)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _resolve_verifier_model(
    model: str | None, verifier_model: str | None
) -> str:
    """The verifier defaults to the agent's ``model``; ``verifier_model``
    overrides only the verifier. Falls back to env, then a safe default."""
    return (
        verifier_model
        or model
        or _env("AZURE_OPENAI_DEPLOYMENT")
        or _env("OPENAI_MODEL")
        or "gpt-4o-mini"
    )


def _build_client_from_env() -> Any | None:
    """Build one OpenAI-compatible async client from env (Azure → OpenAI),
    shared by the verifier and the regenerator. ``None`` if unavailable."""
    azure_key = _env("AZURE_OPENAI_API_KEY")
    azure_endpoint = _env("AZURE_OPENAI_ENDPOINT")
    azure_deployment = _env("AZURE_OPENAI_DEPLOYMENT")
    openai_key = _env("OPENAI_API_KEY")
    try:
        if azure_key and azure_endpoint and azure_deployment:
            from openai import AsyncAzureOpenAI

            return AsyncAzureOpenAI(
                api_key=azure_key,
                azure_endpoint=azure_endpoint,
                api_version=_env("AZURE_OPENAI_API_VERSION") or "2024-08-01-preview",
            )
        if openai_key:
            from openai import AsyncOpenAI

            return AsyncOpenAI(api_key=openai_key, base_url=_env("OPENAI_BASE_URL") or None)
    except ImportError:
        log.warning("openai not installed; no LLM client built")
        return None
    return None


__all__ = ["Firewall"]
