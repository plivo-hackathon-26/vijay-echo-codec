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

        facts, policies = {}, []
        facts_path = os.environ.get("MIRROR_FACTS")
        if facts_path:
            facts = {k: str(v) for k, v in json.loads(
                Path(facts_path).read_text()).items() if not k.startswith("_")}
        policies_path = os.environ.get("MIRROR_POLICIES")
        if policies_path:
            policies = [s.strip() for s in Path(policies_path).read_text().splitlines()
                        if s.strip() and not s.startswith("#")]
        judge = LLMPostCallJudge(facts=facts, policies=policies)
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

    return app


app = create_app()
