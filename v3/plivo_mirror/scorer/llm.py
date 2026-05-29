"""LLMScorer — the single detection layer in v1.

Runs in parallel with TTS encoding (the orchestrator wires the parallelism
via ``asyncio.gather``). On timeout / error it returns a no-intervention
verdict; Mirror must never silently degrade the underlying agent.
"""

from __future__ import annotations

import json
import logging
import warnings
from dataclasses import asdict
from typing import Any

import anyio

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import SupervisorContext, TurnPayload, Verdict
from plivo_mirror.llm.base import LLMClient
from plivo_mirror.policy.compiler import compile_policies

log = logging.getLogger("plivo_mirror.scorer.llm")


class LLMScorer:
    """Scores each agent turn against the customer-defined judging prompt."""

    def __init__(self, config: MirrorConfig) -> None:
        self._config = config
        self._llm: LLMClient = config.llm

        # Compile the judging prompt skeleton ONCE so we're only doing
        # `.format(...)` per turn, not LLM compilation.
        if config.judging_prompt is not None:
            if config.policies:
                warnings.warn(
                    "Both `policies` and `judging_prompt` were supplied; "
                    "using `judging_prompt`.",
                    stacklevel=2,
                )
            self._prompt_template = config.judging_prompt
        else:
            assert config.policies is not None  # validated in MirrorConfig
            self._prompt_template = compile_policies(config.policies)

    # ─────────────────────────── public API ──────────────────────────────

    async def score(self, turn: TurnPayload, ctx: SupervisorContext) -> Verdict:
        """Score one turn. Returns a Verdict; never raises."""
        prompt = self._format_prompt(turn)
        try:
            with anyio.fail_after(self._config.semantic_review_timeout_s):
                raw = await self._llm.structured_output(prompt)
        except TimeoutError:
            log.warning(
                "scorer timed out after %.1fs (call=%s) — fail-open",
                self._config.semantic_review_timeout_s,
                ctx.call_uuid[:8],
            )
            return Verdict.no_intervention("scorer_timeout")
        except Exception:
            log.exception(
                "scorer LLM failed (call=%s) — fail-open", ctx.call_uuid[:8]
            )
            return Verdict.no_intervention("scorer_error")

        return self._parse_verdict(raw)

    # ─────────────────────────── internals ───────────────────────────────

    def _format_prompt(self, turn: TurnPayload) -> str:
        tool_calls_payload = [
            {
                "name": tc.name,
                "args": tc.args,
                "irreversible": tc.irreversible,
            }
            for tc in turn.tool_calls
        ]
        history_lines = []
        for h in turn.history[-6:]:
            role = "Customer" if h.role == "customer" else "Agent"
            text = (h.text or "").strip()
            if text:
                history_lines.append(f"{role}: {text}")
        history_summary = "\n".join(history_lines) if history_lines else "(empty)"

        return self._prompt_template.format(
            customer_text=turn.customer_text or "(silence)",
            primary_response=turn.primary_text or "(no response yet)",
            tool_calls_json=json.dumps(tool_calls_payload, ensure_ascii=False),
            history_summary=history_summary,
        )

    def _parse_verdict(self, raw: dict[str, Any]) -> Verdict:
        if not raw:
            return Verdict.no_intervention("empty_verdict")

        score = _coerce_score(raw.get("score"))
        reason = str(raw.get("reason") or "").strip()
        # v0.1.0a4: drop the suggested_correction when the LLM slipped
        # into instruction format ("Please confirm...", "Before placing...",
        # "Tell the customer: ..."). When dropped, CorrectionGenerator
        # falls back to its own (more constrained) generation prompt.
        from plivo_mirror._internal.text_guards import sanitise_suggested_correction
        raw_suggested = str(raw.get("suggested_correction") or "").strip()
        suggested = sanitise_suggested_correction(raw_suggested)
        if raw_suggested and not suggested:
            log.warning(
                "dropping instruction-format suggested_correction: %r",
                raw_suggested[:120],
            )
        blocked_tool = str(raw.get("blocked_tool") or "").strip() or None
        evidence = raw.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {"raw": str(evidence)}

        # The model's own should_intervene is advisory; we re-decide via
        # the configured threshold to keep the threshold knob meaningful.
        should_intervene = score >= self._config.intervention_threshold

        return Verdict(
            score=score,
            reason=reason or "ok",
            should_intervene=should_intervene,
            suggested_correction=suggested,
            should_report=should_intervene,  # v2 reporter consumes this
            blocked_tool=blocked_tool,
            evidence=evidence if isinstance(evidence, dict) else {},
        )


def _coerce_score(val: Any) -> float:
    try:
        v = float(val)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))


__all__ = ["LLMScorer"]
