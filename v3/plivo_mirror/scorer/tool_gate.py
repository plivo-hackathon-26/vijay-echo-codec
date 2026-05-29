"""Pre-tool-call gate.

Most voice-agent failures customers actually care about are not "the
agent said the wrong words" — they're "the agent ran `place_order` /
`charge_card` / `cancel_subscription` with the wrong arguments and now
we owe a refund." This gate inspects pending tool-call intents BEFORE
they execute, scores them against the customer's intent, and blocks
the ones that fail.

The prompt is deliberately different from the speech scorer's: tool-gate
asks "do these arguments match what the customer asked for?", not "is
the response polite enough?".

Usage:

    gate = ToolGate(config)
    verdict = await gate.review(intents, customer_text, history, ctx)
    if verdict.should_intervene:
        # Don't execute the tool; let the orchestrator speak the
        # suggested_correction or a confirmation question.
        ...
    else:
        # Safe to execute.
        ...
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anyio

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import HistoryTurn, SupervisorContext, ToolCallIntent, Verdict
from plivo_mirror.llm.base import LLMClient

log = logging.getLogger("plivo_mirror.scorer.tool_gate")


_TOOL_GATE_PROMPT = """\
You are Mirror's tool-call gate. You inspect a voice AI agent's tool
calls BEFORE they execute. Your job is to flag tool calls whose
arguments do NOT match what the customer actually asked for. These
tools have real-world side effects (orders, charges, cancellations,
emails) — getting them wrong costs the operator real money.

═══════════════════════════════════════════════════════════════════
OPERATOR POLICIES (priority order):
{policies_block}
═══════════════════════════════════════════════════════════════════
PENDING TOOL CALLS (JSON):
{{tool_calls_json}}

CUSTOMER'S LAST UTTERANCE:
{{customer_text}}

RECENT HISTORY (oldest first):
{{history_summary}}
═══════════════════════════════════════════════════════════════════

DECISION RULES:

- Score 0.0 → tool args clearly match the customer's stated intent.
- Score 1.0 → tool args contradict the customer's intent (wrong item,
  wrong quantity, wrong destination, retracted item still present,
  third-party preference treated as the customer's order, fabricated
  fields, etc.).
- Score 1.0 → the operator's policies explicitly forbid this tool
  call (e.g. "never confirm a refund").
- Default toward 0.0 when in doubt; spurious blocks frustrate users
  but a wrong tool call costs money.

OUTPUT — single JSON object, no markdown fences:

{{{{
  "score": <float in [0,1]>,
  "reason": "<one sentence>",
  "should_intervene": <true if score >= 0.7, else false>,
  "blocked_tool": "<the offending tool name, or empty string>",
  "suggested_correction": "<EXACTLY what the agent SAYS to the customer, first-person, in quotes — never instructions, never 'Tell the customer', never 'Please confirm', never 'Before placing'. Empty string if not applicable. ONE short sentence.>",
  "evidence": {{{{
    "customer_intent": "<one sentence: what the customer actually wants>",
    "tool_mismatch": "<one sentence: what about the tool args is wrong>"
  }}}}
}}}}

CRITICAL — suggested_correction MUST be the literal spoken line, not
instructions. WRONG:
  "Please confirm the order as chicken sandwich only before placing it."
  "Before placing anything, tell the customer X."
  "Tell the customer: 'I can transfer you.'"
RIGHT:
  "Just to confirm — you'd like a chicken sandwich only, is that right?"
  "Got it — let me transfer you to a supervisor."

If you cannot produce a clean customer-facing line, return an empty
suggested_correction; do NOT return scripting.

Output ONLY the JSON object.
"""


class ToolGate:
    """LLM-driven gate that vets tool calls before they execute."""

    def __init__(self, config: MirrorConfig) -> None:
        self._config = config
        self._llm: LLMClient = config.llm
        self._enabled = config.tool_gate_enabled
        self._irreversible = {name.lower() for name in config.irreversible_tools}
        policies = config.policies or []
        if policies:
            block = "\n".join(f"  {i+1}. {p.strip()}" for i, p in enumerate(policies))
        else:
            block = "  (no policies supplied — fall back to intent-match heuristics)"
        self._prompt_template = _TOOL_GATE_PROMPT.format(policies_block=block)

    # ─────────────────────────── public API ──────────────────────────────

    def is_gated(self, tool_name: str) -> bool:
        """Should this tool name be sent through the gate?

        Always True for any tool in ``MirrorConfig.irreversible_tools``,
        regardless of the global ``tool_gate_enabled`` switch — these
        are the ones with side effects that justify the gate's
        existence.
        """
        if (tool_name or "").lower() in self._irreversible:
            return True
        return self._enabled

    async def review(
        self,
        intents: list[ToolCallIntent],
        customer_text: str,
        history: list[HistoryTurn],
        ctx: SupervisorContext,
    ) -> Verdict:
        """Score the proposed tool calls. Never raises."""
        if not intents:
            return Verdict.no_intervention("no_tool_calls")

        # If none of the intents are gated, short-circuit.
        gated = [tc for tc in intents if self.is_gated(tc.name)]
        if not gated:
            return Verdict.no_intervention("no_gated_tools")

        prompt = self._format_prompt(gated, customer_text, history)
        try:
            with anyio.fail_after(self._config.semantic_review_timeout_s):
                raw = await self._llm.structured_output(prompt)
        except TimeoutError:
            log.warning(
                "tool-gate timed out (call=%s) — fail-open", ctx.call_uuid[:8]
            )
            return Verdict.no_intervention("tool_gate_timeout")
        except Exception:
            log.exception(
                "tool-gate LLM failed (call=%s) — fail-open", ctx.call_uuid[:8]
            )
            return Verdict.no_intervention("tool_gate_error")

        return self._parse(raw)

    # ─────────────────────────── internals ───────────────────────────────

    def _format_prompt(
        self,
        intents: list[ToolCallIntent],
        customer_text: str,
        history: list[HistoryTurn],
    ) -> str:
        payload = [
            {
                "name": tc.name,
                "args": tc.args,
                "irreversible": tc.irreversible
                or tc.name.lower() in self._irreversible,
            }
            for tc in intents
        ]
        lines = []
        for h in history[-6:]:
            role = "Customer" if h.role == "customer" else "Agent"
            text = (h.text or "").strip()
            if text:
                lines.append(f"{role}: {text}")
        history_summary = "\n".join(lines) if lines else "(empty)"
        return self._prompt_template.format(
            tool_calls_json=json.dumps(payload, ensure_ascii=False),
            customer_text=customer_text or "(silence)",
            history_summary=history_summary,
        )

    def _parse(self, raw: dict[str, Any]) -> Verdict:
        if not raw:
            return Verdict.no_intervention("empty_verdict")
        try:
            score = float(raw.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        should_intervene = score >= self._config.intervention_threshold

        evidence = raw.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {"raw": str(evidence)}

        # v0.1.0a4: drop the suggested_correction when the LLM slipped
        # into instruction format. CorrectionGenerator falls back to a
        # cleaner customer-facing prompt when this is empty.
        from plivo_mirror._internal.text_guards import sanitise_suggested_correction
        raw_suggested = str(raw.get("suggested_correction") or "").strip()
        suggested = sanitise_suggested_correction(raw_suggested)
        if raw_suggested and not suggested:
            log.warning(
                "tool-gate dropping instruction-format suggested_correction: %r",
                raw_suggested[:120],
            )

        return Verdict(
            score=score,
            reason=str(raw.get("reason") or "ok").strip(),
            should_intervene=should_intervene,
            suggested_correction=suggested,
            should_report=should_intervene,
            blocked_tool=(str(raw.get("blocked_tool") or "").strip() or None),
            evidence=evidence,
        )


__all__ = ["ToolGate"]
