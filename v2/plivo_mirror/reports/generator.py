"""ReportGenerator — turns a call's history + verdicts into a
structured ``FailureReport`` via a single LLM call.

Triggered automatically at call end (from ``CallSupervisor.aclose``) if
any Verdict on the call had ``should_intervene=True`` OR if explicit
``mark_for_report`` was called during a turn.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import anyio

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import HistoryTurn, Verdict
from plivo_mirror.llm.base import LLMClient
from plivo_mirror.reports.schema import FailureReport, ReportStatus

log = logging.getLogger("plivo_mirror.reports.generator")


_REPORT_PROMPT = """\
You are Mirror's post-call analyst. Mirror is a real-time supervisor
that watches voice AI agents on phone calls. A call just ended after
Mirror intervened on one or more turns. Your job: produce a structured
failure report explaining what went wrong, why, and how to fix the
underlying agent prompt or code so this doesn't happen again.

Be precise. Engineers will read this and ship fixes based on it.

═══════════════════════════════════════════════════════════════════
CALL CONTEXT:

call_uuid:            {call_uuid}
tenant_id:            {tenant_id}
duration_seconds:     {duration_seconds}
intervention_count:   {intervention_count}

POLICIES Mirror was enforcing:
{policies_block}

TRANSCRIPT (oldest first):
{transcript_block}

VERDICTS Mirror produced this call:
{verdicts_block}
═══════════════════════════════════════════════════════════════════

{fixable_files_block}
RETURN A JSON OBJECT WITH EXACTLY THESE FIELDS:

{{
  "pattern_name": "<short snake_case label, e.g. 'retracted_item', 'third_party_preference', 'unauthorised_refund'>",
  "severity": "<critical | high | medium | low>",
  "summary": "<one sentence: what went wrong from the customer's perspective>",
  "root_cause": "<2-3 sentences: WHY the agent failed, in technical terms — usually a prompt deficiency>",
  "proposed_fix_text": "<2-3 sentences: how to change the agent's prompt or code so this doesn't repeat>",
  "proposed_file": "<MUST be one of the FIXABLE FILES listed above. Do NOT invent a filename that isn't in the list.>",
  "suggested_diff": "<small replacement snippet or unified-diff-style block showing the change>",
  "confidence": <float 0-1: how confident you are in this diagnosis>
}}

EXAMPLES OF GOOD ROOT CAUSES:
- "Agent's system prompt instructs it to capture every mentioned item, so when the customer changes their mind mid-utterance the agent captures both."
- "Agent has no lookup_order tool but its prompt encourages it to 'recall from memory', causing fabricated past-order details when asked."

EXAMPLES OF GOOD PROPOSED FIXES:
- "Update SYSTEM_PROMPT in agent.py to instruct the agent to use the LATEST stated preference when a customer changes their mind. Remove the 'capture every item' rule."
- "Add an instruction: 'If asked about past orders or refunds, transfer to a human supervisor — do not invent details.'"

Output ONLY the JSON object. No prose, no markdown fences.
"""


class ReportGenerator:
    def __init__(self, config: MirrorConfig) -> None:
        self._config = config
        self._llm: LLMClient = config.llm

    async def generate(
        self,
        *,
        call_uuid: str,
        tenant_id: str | None,
        history: list[HistoryTurn],
        verdicts: list[Verdict],
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> FailureReport | None:
        """Produce a FailureReport from a call's full trace.

        Returns None if there's nothing worth reporting (no interventions,
        empty transcript), or if the LLM round-trip fails.
        """
        intervened = [v for v in verdicts if v.should_intervene]
        if not intervened:
            log.debug("no interventions on call=%s — skipping report", call_uuid[:8])
            return None

        prompt = self._format_prompt(
            call_uuid=call_uuid,
            tenant_id=tenant_id,
            history=history,
            verdicts=verdicts,
            started_at=started_at,
            ended_at=ended_at,
        )

        try:
            with anyio.fail_after(self._config.semantic_review_timeout_s * 3):
                raw = await self._llm.structured_output(prompt)
        except TimeoutError:
            log.warning("report generation timed out for call=%s", call_uuid[:8])
            return None
        except Exception:
            log.exception("report generation failed for call=%s", call_uuid[:8])
            return None

        if not isinstance(raw, dict) or not raw:
            log.warning("report generator got empty/non-dict from LLM")
            return None

        try:
            confidence = float(raw.get("confidence") or 0.5)
        except (TypeError, ValueError):
            confidence = 0.5

        report = FailureReport(
            call_uuid=call_uuid,
            tenant_id=tenant_id,
            pattern_name=str(raw.get("pattern_name") or "unknown").strip(),
            severity=str(raw.get("severity") or "medium").strip().lower(),
            summary=str(raw.get("summary") or "").strip(),
            root_cause=str(raw.get("root_cause") or "").strip(),
            proposed_fix_text=str(raw.get("proposed_fix_text") or "").strip(),
            proposed_file=str(raw.get("proposed_file") or "").strip(),
            suggested_diff=str(raw.get("suggested_diff") or "").strip(),
            confidence=max(0.0, min(1.0, confidence)),
            status=ReportStatus.PENDING,
            extras={
                "intervention_count": sum(1 for v in verdicts if v.should_intervene),
                "verdict_count": len(verdicts),
            },
        )
        return report

    # ─────────────────────────── internals ───────────────────────────────

    def _format_prompt(
        self,
        *,
        call_uuid: str,
        tenant_id: str | None,
        history: list[HistoryTurn],
        verdicts: list[Verdict],
        started_at: datetime | None,
        ended_at: datetime | None,
    ) -> str:
        duration = 0
        if started_at and ended_at:
            try:
                duration = int((ended_at - started_at).total_seconds())
            except (TypeError, ValueError):
                pass

        policies = self._config.policies or []
        if policies:
            policies_block = "\n".join(f"  {i+1}. {p}" for i, p in enumerate(policies))
        else:
            policies_block = "  (custom judging_prompt — see operator's MirrorConfig)"

        transcript_lines: list[str] = []
        for h in history:
            role = "Customer" if h.role == "customer" else "Agent"
            text = (h.text or "").strip()
            if text:
                transcript_lines.append(f"  {role}: {text}")
        transcript_block = "\n".join(transcript_lines) if transcript_lines else "  (empty)"

        verdict_lines: list[str] = []
        for i, v in enumerate(verdicts):
            verdict_lines.append(
                f"  [{i+1}] score={v.score:.2f} intervene={v.should_intervene} "
                f"reason={v.reason!r} blocked_tool={v.blocked_tool!r}"
            )
        verdicts_block = "\n".join(verdict_lines) if verdict_lines else "  (none)"

        fixable = getattr(self._config, "fixable_files", None) or []
        if fixable:
            files_list = "\n".join(f"  - {f}" for f in fixable)
            fixable_files_block = (
                "FIXABLE FILES (proposed_file MUST be one of these — do not invent others):\n"
                f"{files_list}\n\n"
            )
        else:
            fixable_files_block = ""

        return _REPORT_PROMPT.format(
            call_uuid=call_uuid,
            tenant_id=tenant_id or "(none)",
            duration_seconds=duration,
            intervention_count=sum(1 for v in verdicts if v.should_intervene),
            policies_block=policies_block,
            transcript_block=transcript_block,
            verdicts_block=verdicts_block,
            fixable_files_block=fixable_files_block,
        )


__all__ = ["ReportGenerator"]
