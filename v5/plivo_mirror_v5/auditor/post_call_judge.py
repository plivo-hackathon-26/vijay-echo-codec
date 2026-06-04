"""Layer-4 — post-call LLM-judge auditor.

The LLM-judge is strictly OFFLINE: a post-call recall backstop (catch what
L2/L3 missed) and a labeling source for the eval loop. It is NEVER in the
inline path — the voice latency budget and the false-alarm budget both
forbid it.

``LLMPostCallJudge`` is the real implementation: a grounded-entailment
judge over the stored call timeline (facts + policies + conversation →
violation y/n + which rule), run turn-by-turn against what the inline
layers already flagged. ``StubPostCallJudge`` remains the no-op default so
nothing ever *requires* a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class AuditFinding:
    """A judge-proposed miss or mislabel, keyed to stored telemetry."""

    call_id: str
    turn_id: str
    kind: str                      # "missed_failure" | "false_alarm" | "label"
    rationale: str
    verdict_id: str | None = None  # set when the finding re-labels a verdict
    extra: dict = field(default_factory=dict)


@runtime_checkable
class PostCallJudge(Protocol):
    """Audits one completed call (as returned by ``CallStore.get_call``)."""

    def audit_call(self, call: dict) -> list[AuditFinding]: ...


class StubPostCallJudge:
    """No-op default: audits nothing, returns nothing. Keeps the auditor
    wiring point alive in installs without a model."""

    def audit_call(self, call: dict) -> list[AuditFinding]:
        return []


_JUDGE_SYSTEM = """You audit ONE agent turn from a completed voice call for
factual/policy violations, grounded ONLY in the evidence provided.

A turn is a VIOLATION when the agent: states a fact/price/hour/policy that
contradicts the REFERENCE FACTS; fabricates a value/product/promo/history it
was never given; claims an action is done with no matching TOOL CALL; breaks
one of the POLICIES; contradicts itself within the conversation; or ignores
an explicit caller correction/negation/quantity/condition in what it
confirms or orders.

A turn is NOT a violation merely for being blunt, asking to check, honestly
saying "I don't know", refusing an out-of-scope request, or stating a
reasonable assumption for confirmation. When the evidence is insufficient to
prove a violation, answer false.

Output STRICT JSON:
{"violation": true|false, "category": "<short snake_case label or null>",
 "reason": "<one sentence grounded in the evidence>"}"""


class LLMPostCallJudge:
    """Grounded-entailment judge over an OpenAI-compatible endpoint
    (Azure-quirk aware via ``llm_client.ChatClient``)."""

    def __init__(self, client=None, *, facts: dict | None = None,
                 policies: list[str] | None = None) -> None:
        if client is None:
            from plivo_mirror_v5.llm_client import ChatClient  # noqa: PLC0415
            client = ChatClient()
        self.client = client
        self.facts = facts or {}
        self.policies = policies or []

    # -- the core check (also used directly by the eval bridge) ------------

    def judge_turn(self, turns: list[dict], agent_turn_index: int) -> dict:
        """``turns``: [{role, text, tool_calls?}], oldest first. Judges the
        agent turn at ``agent_turn_index`` in the context of the rest."""
        convo = []
        for i, t in enumerate(turns):
            marker = "  <-- TURN UNDER AUDIT" if i == agent_turn_index else ""
            tools = ""
            if t.get("tool_calls"):
                tools = "  [tool calls: " + json.dumps(t["tool_calls"]) + "]"
            convo.append(f"{t['role']}: {t['text']}{tools}{marker}")
        user = (
            "REFERENCE FACTS:\n"
            + ("\n".join(f"- {k}: {v}" for k, v in self.facts.items()) or "- (none)")
            + "\n\nPOLICIES:\n"
            + ("\n".join(f"- {p}" for p in self.policies) or "- (none)")
            + "\n\nCONVERSATION (oldest first):\n"
            + "\n".join(convo)
        )
        verdict = self.client.complete_json(_JUDGE_SYSTEM, user)
        return {
            "violation": bool(verdict.get("violation")),
            "category": verdict.get("category"),
            "reason": str(verdict.get("reason", "")),
        }

    # -- audit a stored call (the monitoring-store shape) --------------------

    def audit_call(self, call: dict) -> list[AuditFinding]:
        """``call`` as returned by ``CallStore.get_call``. Emits a finding
        for every disagreement between the judge and the inline layers."""
        turns = call.get("turns", [])
        convo = [{"role": t["role"], "text": t["transcript"]} for t in turns]
        findings: list[AuditFinding] = []
        for i, turn in enumerate(turns):
            if turn["role"] != "agent":
                continue
            judged = self.judge_turn(convo, i)
            inline_fired = [v for v in turn.get("verdicts", [])
                            if v.get("fired") and v.get("severity") != "info"]
            if judged["violation"] and not inline_fired:
                findings.append(AuditFinding(
                    call_id=call["call_id"], turn_id=turn["turn_id"],
                    kind="missed_failure", rationale=judged["reason"],
                    extra={"category": judged["category"]},
                ))
            elif not judged["violation"] and inline_fired:
                findings.append(AuditFinding(
                    call_id=call["call_id"], turn_id=turn["turn_id"],
                    kind="false_alarm", rationale=judged["reason"],
                    verdict_id=inline_fired[0].get("verdict_id"),
                ))
        return findings
