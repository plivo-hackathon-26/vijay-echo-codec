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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from plivo_mirror_v5.deployables.monitoring.backend.store import CallStore

try:  # repo-root .env supplies the judge creds for post-call analysis
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover — dotenv is optional
    pass

# Call recordings live here as <call_id>.<ext>; the analyzer drops uploaded
# files in, and LiveKit egress output can be pointed here later.
_AUDIO_EXTS = (".wav", ".mp3", ".ogg", ".m4a")
_SAFE_CALL_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def create_app(store: CallStore | None = None,
               recordings_dir: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="plivo-mirror v5 monitoring", version="0.5.0")
    app.state.store = store or CallStore(os.environ.get("MIRROR_DB", ":memory:"))
    app.state.recordings_dir = Path(
        recordings_dir or os.environ.get("MIRROR_RECORDINGS_DIR", "v5/recordings"))
    # The Vite dev server runs on another port; keep the demo friction-free.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    @app.post("/ingest")
    def ingest(records: dict | list[dict]):
        if isinstance(records, dict):
            records = [records]
        for record in records:
            try:
                app.state.store.ingest(record)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            # Optional: auto-run the post-call LLM analysis when a call ends.
            if (record.get("type") == "call_end"
                    and os.environ.get("MIRROR_AUTO_AUDIT") == "1"):
                threading.Thread(
                    target=_safe_analyze,
                    args=(record["mirror.call_id"],),
                    daemon=True, name="mirror-auto-audit",
                ).start()
        return {"ingested": len(records)}

    @app.post("/calls/{call_id}/analyze")
    def analyze_call(call_id: str):
        """Post-call LLM analysis — COMPLETELY OPTIONAL and outside the
        engine: the stored transcript goes to the offline judge (grounded
        with facts/policies when MIRROR_FACTS / MIRROR_POLICIES are set),
        findings are stored and rendered in the dashboard. Never inline."""
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
        if not facts and os.environ.get("MIRROR_FACTS"):
            facts = {k: str(v) for k, v in json.loads(
                Path(os.environ["MIRROR_FACTS"]).read_text()).items()
                if not k.startswith("_")}
        if not policies and os.environ.get("MIRROR_POLICIES"):
            policies = [s.strip()
                        for s in Path(os.environ["MIRROR_POLICIES"]).read_text().splitlines()
                        if s.strip() and not s.startswith("#")]
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
    def register_agent(agent: dict):
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
    def patch_agent(agent_id: str, body: dict):
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
