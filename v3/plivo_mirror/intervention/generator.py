"""LLM-driven correction text generator.

Given a Verdict, ask the LLM to produce a short, natural correction
the agent will speak instead of its planned response. The prompt is
generic — no domain vocabulary — but receives the verdict's evidence
and the customer's intent so the correction is specific to the call.

Falls back to ``templates.fallback_correction`` on any failure so a
live call never goes silent.
"""

from __future__ import annotations

import json
import logging

import anyio

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import HistoryTurn, SupervisorContext, Verdict
from plivo_mirror.intervention.templates import fallback_correction

log = logging.getLogger("plivo_mirror.intervention.generator")


_CORRECTION_PROMPT = """\
You are the voice AI agent for this conversation. You just realised
your last planned response may have been wrong. A silent supervisor
has flagged it and given you evidence about what actually happened.

SUPERVISOR'S VERDICT (JSON):
{verdict_json}

RECENT CONVERSATION (oldest first):
{history_summary}

YOUR TASK FOR THIS TURN ONLY:
1. Briefly acknowledge the confusion in a warm, natural way (e.g.
   "Just to make sure I got this right...").
2. State what you NOW understand the customer wants — using the
   `evidence.customer_intent` field as ground truth.
3. Ask the customer to confirm with a yes/no question.

CONSTRAINTS:
- ONE sentence if possible, two max.
- Sound natural, like you just thought of it yourself.
- Do NOT mention Mirror, a supervisor, the system, JSON, evidence,
  policies, or "I was told". The customer must not know any of that
  exists.
- Do NOT call any tools.
- Do NOT commit to anything beyond confirming understanding.

Output ONLY the spoken correction text, no quotes, no preamble.
"""


class CorrectionGenerator:
    def __init__(self, config: MirrorConfig) -> None:
        self._config = config
        self._llm = config.llm

    async def generate(
        self,
        verdict: Verdict,
        history: list[HistoryTurn],
        ctx: SupervisorContext,
    ) -> str:
        # Fast paths: verdict already supplied a clean correction line.
        if verdict.suggested_correction:
            return verdict.suggested_correction

        prompt = _CORRECTION_PROMPT.format(
            verdict_json=json.dumps(
                {
                    "score": verdict.score,
                    "reason": verdict.reason,
                    "blocked_tool": verdict.blocked_tool,
                    "evidence": verdict.evidence,
                },
                ensure_ascii=False,
            ),
            history_summary=_format_history(history),
        )

        try:
            with anyio.fail_after(self._config.semantic_review_timeout_s):
                text = await self._llm.chat(prompt)
        except TimeoutError:
            log.warning(
                "correction LLM timed out (call=%s) — using fallback",
                ctx.call_uuid[:8],
            )
            return fallback_correction(verdict.evidence)
        except Exception:
            log.exception(
                "correction LLM failed (call=%s) — using fallback",
                ctx.call_uuid[:8],
            )
            return fallback_correction(verdict.evidence)

        text = (text or "").strip().strip('"').strip()
        # v0.1.0a4: defense in depth. If the LLM ALSO slipped into
        # instruction format here, drop it and use the bland generic
        # template — better than reading scripting out loud.
        from plivo_mirror._internal.text_guards import looks_like_instruction
        if text and looks_like_instruction(text):
            log.warning(
                "correction LLM returned instruction-format text — using fallback: %r",
                text[:120],
            )
            return fallback_correction(verdict.evidence)
        return text or fallback_correction(verdict.evidence)


def _format_history(history: list[HistoryTurn]) -> str:
    lines = []
    for h in history[-6:]:
        role = "Customer" if h.role == "customer" else "Agent"
        t = (h.text or "").strip()
        if t:
            lines.append(f"{role}: {t}")
    return "\n".join(lines) if lines else "(empty)"


__all__ = ["CorrectionGenerator"]
