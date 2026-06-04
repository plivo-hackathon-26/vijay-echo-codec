"""L2 — deterministic diff. The PRIMARY detector and the workhorse.

For each structured claim in an agent turn, resolve the truth from one of
the three deterministic sources and diff:

- ``session.<path>``  — runtime per-call validated facts (state snapshot)
- ``reference.<key>`` — static structured data (ReferenceStore)
- ``tool.<name>``     — the committed tool log (speech-vs-action)

Microseconds, no model, fully explainable: every verdict carries
``{claim_type, spoken_value, truth_value, source}``. Claims whose referent
does not resolve are simply outside L2 jurisdiction and fall through to L3.

While the L1 gate is set (untrusted caller input), mismatches are
downgraded to ``info`` — the agent may be correctly answering a
mis-transcribed question.
"""

from __future__ import annotations

import re
from typing import Any

from plivo_mirror_v5.engine.layers.base import LayerContext
from plivo_mirror_v5.engine.session_state import SessionState
from plivo_mirror_v5.engine.verdict import Evidence, TurnInput, Verdict, new_verdict_id

def _as_number(value: Any) -> float | None:
    """Strict numeric parse: the WHOLE value must be one number (currency
    sign / thousands separators / %% allowed). "9am-6pm" must NOT compare
    numerically on its first digit — that would mask real mismatches."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "").lstrip("$€£").rstrip("%").strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


_TRUTH_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def values_match(spoken: Any, truth: Any) -> bool:
    """Deterministic comparison: numeric when both sides parse as numbers
    (so "$79.99" == 79.99); a numeric spoken value also matches a prose
    truth containing exactly ONE number ("6" vs "6 wings per order") —
    ambiguous multi-number truths fall through to exact text. Otherwise
    case/whitespace-insensitive text."""
    s_num, t_num = _as_number(spoken), _as_number(truth)
    if s_num is not None and t_num is not None:
        return abs(s_num - t_num) < 1e-9
    if s_num is not None and isinstance(truth, str):
        nums = _TRUTH_NUMBER_RE.findall(truth.replace(",", ""))
        if len(nums) == 1:
            return abs(s_num - float(nums[0])) < 1e-9
    return _norm_text(spoken) == _norm_text(truth)


class DeterministicDiffLayer:
    name = "L2"

    def check(
        self, turn: TurnInput, state: SessionState, ctx: LayerContext
    ) -> list[Verdict]:
        if turn.role != "agent":
            return []

        verdicts: list[Verdict] = self._policy_checks(turn, state, ctx)
        for claim in turn.claims:
            ref = claim.get("ref")
            if not ref or claim.get("claim_type") == "correction":
                continue  # no structured referent → L3 jurisdiction

            resolved = self._resolve(ref, turn, ctx)
            if resolved is None:
                continue  # referent unknown to structured truth → L3
            truth_value, source = resolved

            claim_id = claim.get("claim_id")
            ctx.l2_claim_ids.add(claim_id)  # L2 has jurisdiction over this claim
            claim_type = claim.get("claim_type", "fact")
            spoken = claim.get("spoken_value")

            if ref.startswith("tool."):
                mismatch = truth_value != "fired"
            else:
                mismatch = not values_match(spoken, truth_value)

            severity = ctx.config.severity_for(claim_type) if mismatch else "info"
            extra: dict = {"claim_id": claim_id}
            if mismatch and ctx.snapshot.untrusted_input:
                # L1 gate: don't penalise the agent for answering a
                # mis-transcribed question; keep the diff but as audit-only.
                severity = "info"
                extra["untrusted_input"] = True

            verdicts.append(
                Verdict(
                    verdict_id=new_verdict_id(),
                    detector=self.name,
                    fired=mismatch,
                    severity=severity,
                    latency_ms=0.0,  # stamped by the engine per layer
                    evidence=Evidence(
                        claim_type=claim_type,
                        spoken_value=None if spoken is None else str(spoken),
                        truth_value=None if truth_value is None else str(truth_value),
                        source=source,
                        extra=extra,
                    ),
                )
            )
        return verdicts

    @staticmethod
    def _policy_checks(turn, state, ctx) -> list[Verdict]:
        """The parallel policy checks (arg bindings, authorization
        separation, commitments, disclosures, persona) — see l2_checks."""
        from plivo_mirror_v5.engine.layers.l2_checks import run_policy_checks  # noqa: PLC0415

        return run_policy_checks(turn, state, ctx, DeterministicDiffLayer.name)

    # -- truth resolution ----------------------------------------------------

    def _resolve(
        self, ref: str, turn: TurnInput, ctx: LayerContext
    ) -> tuple[Any, str] | None:
        """Resolve a structured referent to ``(truth_value, source)``, or
        None when structured truth has no entry (→ outside L2 jurisdiction)."""
        if ref.startswith("session."):
            key = ref[len("session."):]
            if not ctx.snapshot.has(key):
                return None
            return ctx.snapshot.get(key), ref

        if ref.startswith("reference."):
            key = ref[len("reference."):]
            value, found = ctx.reference.lookup(key)
            if not found:
                return None
            return value, ref

        if ref.startswith("tool."):
            # Speech-vs-action: did the named tool actually fire (this turn
            # or earlier in the call) and not error?
            name = ref[len("tool."):]
            calls = [
                tc
                for tc in (*ctx.snapshot.tool_log, *turn.tool_calls)
                if tc.get("name") == name
            ]
            ok = any(not _errored(tc) for tc in calls)
            if calls:
                return ("fired" if ok else "failed"), ref
            return "not_fired", ref

        return None  # unknown namespace → not structured truth


def _errored(tool_call: dict) -> bool:
    result = tool_call.get("result")
    return isinstance(result, dict) and bool(result.get("error"))
