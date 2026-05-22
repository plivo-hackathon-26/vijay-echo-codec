"""Post-call failure-report generator.

Runs in the background after a call ends. Reads the call's full
trace from the existing tables (calls / turns / mirror_events /
interventions / orders), asks the LLM for a structured analysis,
writes one row to `failure_reports`.

Idempotent: if a report already exists for the call_uuid, returns it
without making another LLM call. Defensive: any failure — LLM error,
JSON parse fail, missing required fields — logs and returns None
without crashing the caller (which is itself a fire-and-forget task
scheduled by the patched db.end_call).

LLM: Azure OpenAI gpt-5-mini via the standard OpenAI SDK, same shape
as agent/primary.py and mirror/semantic.py. Uses response_format=
{"type": "json_object"} to force valid JSON. Low temperature for
consistency.

A report is NOT generated when:
- No mirror_events with intervention_needed=1 exist for the call.
  These calls had no failure to report on; pending count stays clean.
- An existing report already covers this call_uuid (idempotency).
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

from openai import AsyncOpenAI

import db
from prompts import REPORT_GENERATION_PROMPT

log = logging.getLogger("mirror.reporter")

_client: AsyncOpenAI | None = None

_REQUIRED_FIELDS = (
    "pattern_name",
    "severity",
    "summary",
    "root_cause",
    "proposed_fix_text",
    "proposed_file",
    "suggested_diff",
)
_VALID_SEVERITY = {"critical", "high", "medium", "low"}


def _openai() -> AsyncOpenAI:
    global _client
    if _client is None:
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        base_url = os.getenv("OPENAI_API_URL", "").strip().rstrip("/") or None
        if base_url and not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url
        _client = AsyncOpenAI(api_key=key, base_url=base_url)
    return _client


def _duration_seconds(started_at: Any, ended_at: Any) -> int | None:
    if not started_at or not ended_at:
        return None
    try:
        s = datetime.fromisoformat(str(started_at))
        e = datetime.fromisoformat(str(ended_at))
        return int((e - s).total_seconds())
    except (ValueError, TypeError):
        return None


def _load_call_context(call_uuid: str) -> dict | None:
    """Return everything the LLM needs about the call, or None if the
    call doesn't exist."""
    with db.get_conn() as conn:
        call_row = conn.execute(
            "SELECT call_uuid, caller, started_at, ended_at, status, "
            "       COALESCE(agent_name, 'pizza-plivo') AS agent_name, "
            "       COALESCE(mirror_enabled, 1) AS mirror_enabled "
            "FROM calls WHERE call_uuid = ?",
            (call_uuid,),
        ).fetchone()
        if call_row is None:
            return None
        turns = conn.execute(
            "SELECT id, role, text, timestamp FROM turns "
            "WHERE call_uuid = ? ORDER BY id ASC",
            (call_uuid,),
        ).fetchall()
        mirror_events = conn.execute(
            "SELECT id, turn_id, pattern_name, severity, evidence, "
            "       intervention_needed, timestamp "
            "FROM mirror_events WHERE call_uuid = ? ORDER BY id ASC",
            (call_uuid,),
        ).fetchall()
        interventions = conn.execute(
            "SELECT id, pattern_name, strategy, buffer_text, "
            "       correction_text, latency_ms, timestamp "
            "FROM interventions WHERE call_uuid = ? ORDER BY id ASC",
            (call_uuid,),
        ).fetchall()
        orders = conn.execute(
            "SELECT id, items_json, created_at FROM orders "
            "WHERE call_uuid = ? ORDER BY id ASC",
            (call_uuid,),
        ).fetchall()
    return {
        "call": dict(call_row),
        "turns": [dict(r) for r in turns],
        "mirror_events": [dict(r) for r in mirror_events],
        "interventions": [dict(r) for r in interventions],
        "orders": [dict(r) for r in orders],
    }


def _format_turns(turns: list) -> str:
    if not turns:
        return "(no turns recorded)"
    lines = []
    for t in turns:
        role = "Customer" if t.get("role") == "customer" else "Agent"
        text = (t.get("text") or "").strip()
        lines.append(f"  {role}: {text}")
    return "\n".join(lines)


def _format_events(events: list) -> str:
    if not events:
        return "(none)"
    lines = []
    for e in events:
        try:
            evidence = json.loads(e["evidence"]) if e.get("evidence") else {}
        except (ValueError, TypeError):
            evidence = {"raw": e.get("evidence")}
        lines.append(
            f"  - {e.get('pattern_name')} ({e.get('severity')}) "
            f"intervention_needed={bool(e.get('intervention_needed'))} "
            f"evidence={json.dumps(evidence, ensure_ascii=False)}"
        )
    return "\n".join(lines)


def _format_interventions(interventions: list) -> str:
    if not interventions:
        return "(none)"
    lines = []
    for i in interventions:
        lines.append(
            f"  - {i.get('pattern_name')} strategy={i.get('strategy')} "
            f"latency={i.get('latency_ms')}ms "
            f"correction=\"{(i.get('correction_text') or '').strip()}\""
        )
    return "\n".join(lines)


def _format_orders(orders: list) -> str:
    if not orders:
        return "(no order placed)"
    last = orders[-1]
    items = last.get("items_json") or ""
    return f"  items_json: {items}"


def _validate(parsed: dict) -> bool:
    for f in _REQUIRED_FIELDS:
        if f not in parsed:
            log.warning("report missing required field: %s", f)
            return False
    sev = str(parsed.get("severity", "")).lower()
    if sev not in _VALID_SEVERITY:
        log.warning("report has invalid severity: %r", parsed.get("severity"))
        return False
    return True


def _coerce_confidence(val: Any) -> float:
    try:
        v = float(val)
    except (ValueError, TypeError):
        return 0.5
    return max(0.0, min(1.0, v))


async def generate_failure_report(call_uuid: str) -> dict | None:
    """Generate (or return existing) post-call failure report for a call.

    Returns the persisted row as a dict, or None if no report is needed
    (no intervention-grade failures on this call) or generation failed.
    Never raises — caller is a fire-and-forget background task.
    """
    if not call_uuid:
        return None

    try:
        existing = db.get_failure_report_by_call(call_uuid)
        if existing:
            log.info(
                "failure_report already exists for %s (id=%s)",
                call_uuid[:8],
                existing.get("id"),
            )
            return existing

        ctx = _load_call_context(call_uuid)
        if ctx is None:
            log.info("no calls row for %s; skipping report", call_uuid[:8])
            return None

        had_intervention_grade = any(
            bool(e.get("intervention_needed"))
            for e in ctx.get("mirror_events", [])
        )
        if not had_intervention_grade:
            log.info(
                "no intervention-grade events on %s; skipping report",
                call_uuid[:8],
            )
            return None

        call = ctx["call"]
        prompt = REPORT_GENERATION_PROMPT.format(
            agent_name=call.get("agent_name", "pizza-plivo"),
            call_uuid=call.get("call_uuid", ""),
            mirror_enabled=bool(call.get("mirror_enabled", 1)),
            duration_seconds=_duration_seconds(
                call.get("started_at"), call.get("ended_at")
            ),
            turns_formatted=_format_turns(ctx["turns"]),
            mirror_events_formatted=_format_events(ctx["mirror_events"]),
            interventions_formatted=_format_interventions(ctx["interventions"]),
            order_formatted=_format_orders(ctx["orders"]),
        )

        try:
            client = _openai()
            # Match the pattern that's already working in mirror/semantic.py:
            # Azure gpt-5-mini rejects `max_tokens` (needs
            # max_completion_tokens) and silently treats `temperature` as
            # unsupported on some deployments. response_format=json_object
            # is the one extra param Azure DOES accept.
            resp = await client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
                messages=[{"role": "system", "content": prompt}],
                response_format={"type": "json_object"},
            )
        except Exception:
            log.exception("failure-report LLM call failed for %s", call_uuid[:8])
            return None

        raw = (resp.choices[0].message.content or "").strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            log.warning(
                "failure-report LLM returned non-JSON for %s: %r",
                call_uuid[:8],
                raw[:200],
            )
            return None

        if not _validate(parsed):
            return None

        report_id = db.create_failure_report(
            call_uuid=call_uuid,
            pattern_name=str(parsed.get("pattern_name") or "").strip() or None,
            severity=str(parsed.get("severity") or "").strip().lower(),
            summary=str(parsed.get("summary") or "").strip() or None,
            root_cause=str(parsed.get("root_cause") or "").strip() or None,
            proposed_fix_text=str(parsed.get("proposed_fix_text") or "").strip() or None,
            proposed_file=str(parsed.get("proposed_file") or "").strip() or None,
            suggested_diff=str(parsed.get("suggested_diff") or "").strip() or None,
            confidence=_coerce_confidence(parsed.get("confidence", 0.5)),
        )
        log.info(
            "failure_report id=%d created for call=%s severity=%s",
            report_id,
            call_uuid[:8],
            parsed.get("severity"),
        )
        return db.get_failure_report_by_id(report_id)
    except Exception:
        log.exception("generate_failure_report crashed for %s", call_uuid)
        return None
