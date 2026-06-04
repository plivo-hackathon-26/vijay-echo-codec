"""Monitoring backend — FastAPI ingest + query API, call-ID keyed.

    venv/bin/python -m uvicorn \
        plivo_mirror_v5.deployables.monitoring.backend.app:app --port 8500

Set ``MIRROR_DB`` to persist to a SQLite file (defaults to in-memory).

PII note: transcripts/evidence carry PII — call_id is the only identifier
that ever appears in a URL; everything else travels in bodies.
# TODO: auth + PII redaction policy — post-v5.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from plivo_mirror_v5.deployables.monitoring.backend.store import CallStore


def create_app(store: CallStore | None = None) -> FastAPI:
    app = FastAPI(title="plivo-mirror v5 monitoring", version="0.5.0")
    app.state.store = store or CallStore(os.environ.get("MIRROR_DB", ":memory:"))
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
        return {"ingested": len(records)}

    @app.get("/calls")
    def list_calls():
        return app.state.store.list_calls()

    @app.get("/calls/{call_id}")
    def get_call(call_id: str):
        call = app.state.store.get_call(call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="unknown call_id")
        return call

    return app


app = create_app()
