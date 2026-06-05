"""Monitoring backend — FastAPI ingest + query API, call-ID keyed.

    venv/bin/python -m uvicorn \
        plivo_mirror_v5.deployables.monitoring.backend.app:app --port 8500

Set ``MIRROR_DB`` to persist to a SQLite file (defaults to in-memory).

PII note: transcripts/evidence carry PII — call_id is the only identifier
that ever appears in a URL; everything else travels in bodies.
# TODO: auth + PII redaction policy — post-v5.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from plivo_mirror_v5.deployables.monitoring.backend.store import CallStore
from plivo_mirror_v5.telemetry import schema as S

try:  # repo-root .env supplies the judge creds for post-call analysis
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover — dotenv is optional
    pass

# Call recordings live here as <call_id>.<ext>; the analyzer drops uploaded
# files in, and LiveKit egress output can be pointed here later.
_AUDIO_EXTS = (".wav", ".mp3", ".ogg", ".m4a")
_SAFE_CALL_ID = re.compile(r"^[A-Za-z0-9._-]+$")


# PII redaction (opt-in via MIRROR_REDACT_PII=1). Masks the obvious direct
# identifiers in stored transcripts/evidence so the dashboard + receipts are
# safe to share. Deterministic regex — not a substitute for a full DLP pass.
_PII_PATTERNS = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
    (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[card]"),          # card-ish
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[ssn]"),
    (re.compile(r"(?<!\d)(?:\+?1[ -.]?)?\(?\d{3}\)?[ -.]\d{3}[ -.]\d{4}(?!\d)"),
     "[phone]"),
]
def _redaction_on() -> bool:
    return os.environ.get("MIRROR_REDACT_PII") == "1"


def _redact_text(value):
    if not _redaction_on() or not isinstance(value, str):
        return value
    for pattern, repl in _PII_PATTERNS:
        value = pattern.sub(repl, value)
    return value


def _redact_record(record: dict) -> dict:
    """Mask PII in a telemetry record in place before it is stored."""
    if not _redaction_on():
        return record
    if S.ATTR_TRANSCRIPT in record:
        record[S.ATTR_TRANSCRIPT] = _redact_text(record[S.ATTR_TRANSCRIPT])
    if S.ATTR_ACTION_CORRECTION in record:
        record[S.ATTR_ACTION_CORRECTION] = _redact_text(record[S.ATTR_ACTION_CORRECTION])
    ev = record.get(S.ATTR_EVIDENCE)
    if isinstance(ev, dict):
        for k in ("spoken_value", "truth_value"):
            if k in ev:
                ev[k] = _redact_text(ev[k])
    return record


def _post_webhook(payload: dict) -> None:
    """Fire-and-forget alert webhook (MIRROR_ALERT_WEBHOOK). Slack-compatible
    ('text' field) AND machine-readable (full payload). Never raises, never
    blocks ingest — runs on a daemon thread."""
    url = os.environ.get("MIRROR_ALERT_WEBHOOK")
    if not url:
        return

    def _send() -> None:
        import urllib.request  # noqa: PLC0415

        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)  # noqa: S310
        except Exception:  # noqa: BLE001
            logging.getLogger("plivo_mirror_v5.backend").warning(
                "alert webhook delivery failed", exc_info=True)

    threading.Thread(target=_send, daemon=True, name="mirror-alert").start()


def create_app(store: CallStore | None = None,
               recordings_dir: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="plivo-mirror v5 monitoring", version="0.5.0")
    app.state.store = store or CallStore(os.environ.get("MIRROR_DB", ":memory:"))
    app.state.recordings_dir = Path(
        recordings_dir or os.environ.get("MIRROR_RECORDINGS_DIR", "v5/recordings"))

    def _require_key(request: Request) -> None:
        """Opt-in write protection: set MIRROR_API_KEY and every mutating
        endpoint demands it (X-API-Key header). Unset → open (demo mode).
        Reads stay open either way — the dashboard is view-only without it."""
        expected = os.environ.get("MIRROR_API_KEY")
        if not expected:
            return
        provided = request.headers.get("x-api-key") or (
            request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if provided != expected:
            raise HTTPException(status_code=401, detail="invalid or missing API key")
    # CORS: env-restrictable (MIRROR_CORS_ORIGINS="https://a.com,https://b.com").
    # Default stays open for the demo path; production sets the env var.
    origins = [o.strip() for o in
               os.environ.get("MIRROR_CORS_ORIGINS", "*").split(",")]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins, allow_methods=["*"], allow_headers=["*"],
    )

    _MAX_BATCH = int(os.environ.get("MIRROR_MAX_INGEST_BATCH", "500"))

    @app.post("/ingest")
    def ingest(records: dict | list[dict], request: Request):
        _require_key(request)
        if isinstance(records, dict):
            records = [records]
        if len(records) > _MAX_BATCH:
            raise HTTPException(status_code=413,
                                detail=f"batch too large (max {_MAX_BATCH})")
        for record in records:
            _redact_record(record)
            try:
                app.state.store.ingest(record)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            _maybe_alert(record)
            # Optional: auto-run the post-call LLM analysis when a call ends.
            if (record.get("type") == "call_end"
                    and os.environ.get("MIRROR_AUTO_AUDIT") == "1"):
                threading.Thread(
                    target=_safe_analyze,
                    args=(record["mirror.call_id"],),
                    daemon=True, name="mirror-auto-audit",
                ).start()
        return {"ingested": len(records)}

    def _maybe_alert(record: dict) -> None:
        """High-severity fired verdicts and interventions page someone —
        nobody watches a dashboard. Slack-compatible + machine-readable."""
        kind = record.get("type")
        if kind == "verdict" and record.get("mirror.fired") \
                and record.get("mirror.severity") == "high":
            ev = record.get("mirror.evidence") or {}
            _post_webhook({
                "text": (f"🔴 plivo-mirror: HIGH violation on call "
                         f"{record.get('mirror.call_id')} — "
                         f"{ev.get('claim_type')}: spoke "
                         f"{ev.get('spoken_value')!r}, truth "
                         f"{ev.get('truth_value')!r} ({ev.get('source')})"),
                "event": "violation", "record": record,
            })
        elif kind == "action" and record.get("mirror.action.taken") in (
                "correct", "handoff"):
            _post_webhook({
                "text": (f"🛠 plivo-mirror: intervention "
                         f"({record.get('mirror.action.taken')}) on call "
                         f"{record.get('mirror.call_id')}"),
                "event": "intervention", "record": record,
            })

    @app.post("/calls/{call_id}/labels")
    def save_label(call_id: str, body: dict, request: Request):
        """Reviewer verdict on a flag: the review loop that turns every ✓/✗
        into a MEASURED production-precision number (see /stats/precision)."""
        _require_key(request)
        kind = body.get("target_kind")
        label = body.get("label")
        target_id = str(body.get("target_id") or "")
        if kind not in ("verdict", "finding") or not target_id:
            raise HTTPException(status_code=422,
                                detail="target_kind: verdict|finding + target_id")
        if label not in ("confirmed", "rejected"):
            raise HTTPException(status_code=422, detail="label: confirmed|rejected")
        if app.state.store.get_call(call_id) is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return app.state.store.save_label(
            call_id, kind, target_id, label,
            note=body.get("note"), t=time.time())

    @app.get("/calls/{call_id}/receipts")
    def export_receipts(call_id: str):
        """Audit-grade evidence packet for a call: every violation with its
        {spoken, truth, source} receipt, reviewer labels, interventions and
        judge findings — a defensible artifact for compliance teams, not a
        screenshot of a dashboard."""
        call = app.state.store.get_call(call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        labels = call.get("labels", {})
        violations, interventions = [], []
        for turn in call["turns"]:
            for v in turn.get("verdicts", []):
                if not v.get("fired") or v.get("severity") == "info":
                    continue
                ev = v.get("evidence") or {}
                violations.append({
                    "turn_index": turn["turn_index"],
                    "turn_id": turn["turn_id"],
                    "agent_said": turn["transcript"],
                    "detector": v.get("detector"),
                    "severity": v.get("severity"),
                    "claim_type": ev.get("claim_type"),
                    "spoken_value": ev.get("spoken_value"),
                    "truth_value": ev.get("truth_value"),
                    "truth_source": ev.get("source"),
                    "detection_latency_ms": v.get("latency_ms"),
                    "verdict_id": v.get("verdict_id"),
                    "review": labels.get(f"verdict:{v.get('verdict_id')}"),
                })
            for a in turn.get("actions", []):
                if a.get("taken") in ("correct", "hold", "handoff", "would_have"):
                    interventions.append({
                        "turn_id": turn["turn_id"], "taken": a.get("taken"),
                        "hook": a.get("hook"),
                        "correction": a.get("correction_text"),
                    })
        findings = call.get("audit", {}).get("findings", [])
        return {
            "call_id": call_id,
            "agent_id": call.get("agent_id"),
            "agent_version": call.get("agent_version"),
            "started_at": call.get("started_at"),
            "ended_at": call.get("ended_at"),
            "generated_at": time.time(),
            "violations": violations,
            "interventions": interventions,
            "judge_findings": [
                {**f, "review": labels.get(f"finding:{f.get('id')}")}
                for f in findings
            ],
            "summary": {
                "violation_count": len(violations),
                "intervention_count": len(interventions),
                "reviewed": sum(1 for v in violations if v["review"]),
                "confirmed": sum(1 for v in violations
                                 if v["review"] == "confirmed"),
            },
        }

    @app.get("/stats/precision")
    def stats_precision():
        """Measured production precision from reviewer labels — a live
        number computed on YOUR traffic, not a benchmark claim."""
        return app.state.store.precision_stats()

    @app.post("/calls/{call_id}/analyze")
    def analyze_call(call_id: str, request: Request):
        """Post-call LLM analysis — COMPLETELY OPTIONAL and outside the
        engine: the stored transcript goes to the offline judge (grounded
        with facts/policies when MIRROR_FACTS / MIRROR_POLICIES are set),
        findings are stored and rendered in the dashboard. Never inline."""
        _require_key(request)
        call = app.state.store.get_call(call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        try:
            findings = _run_judge(call)
        except Exception as exc:  # noqa: BLE001 — surface, don't 500-blank
            raise HTTPException(status_code=502,
                                detail=f"analysis failed: {exc}") from exc
        app.state.store.save_audit_findings(call_id, findings, t=time.time())
        return app.state.store.get_audit_findings(call_id)

    def _run_judge(call: dict) -> list[dict]:
        from plivo_mirror_v5.auditor import LLMPostCallJudge  # noqa: PLC0415

        # Grounding priority: the call's REGISTERED agent (system prompt +
        # facts + policies from the registry) → env-var files → ungrounded.
        facts, policies, system_prompt = {}, [], None
        registered = app.state.store.get_agent(call.get("agent_id") or "")
        if registered:
            facts = {k: str(v) for k, v in (registered["facts"] or {}).items()}
            policies = [s.strip() for s in (registered["policies"] or "").splitlines()
                        if s.strip()]
            system_prompt = registered["system_prompt"] or None
        try:  # env-file grounding fallback: malformed/missing files must
            # degrade to an ungrounded audit, never crash the analysis
            if not facts and os.environ.get("MIRROR_FACTS"):
                facts = {k: str(v) for k, v in json.loads(
                    Path(os.environ["MIRROR_FACTS"]).read_text()).items()
                    if not k.startswith("_")}
            if not policies and os.environ.get("MIRROR_POLICIES"):
                policies = [s.strip()
                            for s in Path(os.environ["MIRROR_POLICIES"]).read_text().splitlines()
                            if s.strip() and not s.startswith("#")]
        except (OSError, json.JSONDecodeError):
            logging.getLogger("plivo_mirror_v5.backend").exception(
                "MIRROR_FACTS/MIRROR_POLICIES unreadable — judging ungrounded")
        judge = LLMPostCallJudge(facts=facts, policies=policies,
                                 system_prompt=system_prompt)
        return [
            {"turn_id": f.turn_id, "kind": f.kind, "rationale": f.rationale,
             "verdict_id": f.verdict_id, "category": f.extra.get("category")}
            for f in judge.audit_call(call)
        ]

    def _safe_analyze(call_id: str) -> None:
        try:
            call = app.state.store.get_call(call_id)
            if call is not None:
                app.state.store.save_audit_findings(
                    call_id, _run_judge(call), t=time.time())
        except Exception:  # noqa: BLE001
            logging.getLogger("plivo_mirror_v5.backend").exception(
                "auto-audit failed for %s", call_id)

    # -- agent registry: the "connect any LiveKit agent" window --------------

    @app.post("/agents")
    def register_agent(agent: dict, request: Request):
        _require_key(request)
        agent_id = (agent.get("agent_id") or "").strip()
        if not agent_id or not _SAFE_CALL_ID.match(agent_id):
            raise HTTPException(status_code=422,
                                detail="agent_id required: letters, digits, . _ - only")
        mode = agent.get("mode") or "shadow"
        if mode not in ("shadow", "intervene"):
            raise HTTPException(status_code=422, detail="mode: shadow | intervene")
        if agent.get("facts") is not None and not isinstance(agent["facts"], dict):
            raise HTTPException(status_code=422, detail="facts must be a JSON object")
        return app.state.store.upsert_agent(agent, t=time.time())

    @app.get("/agents")
    def list_agents():
        return app.state.store.list_agents()

    @app.get("/agents/{agent_id}")
    def get_agent(agent_id: str):
        agent = app.state.store.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="unknown agent_id")
        return agent

    @app.patch("/agents/{agent_id}")
    def patch_agent(agent_id: str, body: dict, request: Request):
        _require_key(request)
        mode = body.get("mode")
        if mode not in ("shadow", "intervene"):
            raise HTTPException(status_code=422, detail="mode: shadow | intervene")
        agent = app.state.store.set_agent_mode(agent_id, mode, t=time.time())
        if agent is None:
            raise HTTPException(status_code=404, detail="unknown agent_id")
        return agent

    @app.get("/agents/{agent_id}/config")
    def agent_config(agent_id: str):
        """What attach_mirror pulls at call start: mode + judge grounding.
        Unregistered ids get shadow defaults, so connecting never fails."""
        agent = app.state.store.get_agent(agent_id)
        if agent is None:
            return {"agent_id": agent_id, "registered": False, "mode": "shadow",
                    "facts": {}, "policies": "", "system_prompt": ""}
        return {"agent_id": agent_id, "registered": True, "mode": agent["mode"],
                "facts": agent["facts"], "policies": agent["policies"],
                "system_prompt": agent["system_prompt"]}

    @app.get("/stats/overview")
    def stats_overview(days: int = 14):
        """Fleet rollups: KPIs, per-day trend, categories, version compare."""
        return app.state.store.stats_overview(days=max(1, min(days, 90)))

    @app.get("/stats/patterns")
    def stats_patterns(min_calls: int = 2):
        """Cross-call failure clusters with call-id receipts."""
        return app.state.store.systemic_patterns(min_calls=max(1, min_calls))

    @app.get("/calls")
    def list_calls():
        return app.state.store.list_calls()

    @app.get("/calls/{call_id}")
    def get_call(call_id: str):
        call = app.state.store.get_call(call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        call["has_audio"] = _find_recording(call_id) is not None
        return call

    @app.get("/calls/{call_id}/audio")
    def get_audio(call_id: str):
        path = _find_recording(call_id)
        if path is None:
            raise HTTPException(status_code=404, detail="no recording for this call")
        return FileResponse(path)

    def _find_recording(call_id: str) -> Path | None:
        if not _SAFE_CALL_ID.match(call_id):  # path-traversal guard
            return None
        for ext in _AUDIO_EXTS:
            path = app.state.recordings_dir / f"{call_id}{ext}"
            if path.is_file():
                return path
        return None

    # Single-URL deployment: serve the built frontend (vite dist/) from the
    # backend itself. API routes are matched first; the static mount catches
    # the rest. Skipped when no build exists (dev mode uses the vite proxy).
    dist = Path(os.environ.get(
        "MIRROR_FRONTEND_DIST",
        Path(__file__).resolve().parents[1] / "frontend" / "dist"))
    if dist.is_dir():
        from fastapi.staticfiles import StaticFiles  # noqa: PLC0415

        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")

    return app


app = create_app()
