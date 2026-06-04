"""Call-ID-keyed store for the monitoring backend.

SQLite (``:memory:`` for tests, a file path otherwise). The store is itself
a ``TelemetrySink`` — ``emit()`` ingests a record directly, which is the
"local exporter" path when no OTLP endpoint is configured.

PII note: transcripts and evidence live here. Access-controlled deployment
and per-field redaction (see the emitter) are required in production.
# TODO: auth + retention policy — post-v5.
"""

from __future__ import annotations

import json
import sqlite3
import threading

from plivo_mirror_v5.engine.verdict import SEVERITIES
from plivo_mirror_v5.telemetry import schema as S

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    agent_id TEXT,
    agent_version TEXT,
    channel TEXT,
    started_at REAL,
    ended_at REAL,
    outcome TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL,
    turn_index INTEGER,
    role TEXT,
    transcript TEXT,
    asr_confidence REAL,
    audio_offset_ms REAL,
    state_snapshot_id TEXT,
    t REAL
);
CREATE TABLE IF NOT EXISTS verdicts (
    verdict_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    detector TEXT,
    fired INTEGER,
    severity TEXT,
    latency_ms REAL,
    evidence TEXT,
    arbitration TEXT,
    t REAL
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    taken TEXT,
    hook TEXT,
    correction_text TEXT,
    t REAL
);
CREATE INDEX IF NOT EXISTS idx_turns_call ON turns(call_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_verdicts_call ON verdicts(call_id);
CREATE INDEX IF NOT EXISTS idx_actions_call ON actions(call_id);
"""


class CallStore:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- TelemetrySink ------------------------------------------------------

    def emit(self, record: dict) -> None:
        self.ingest(record)

    def ingest(self, record: dict) -> None:
        handler = {
            S.REC_CALL_START: self._ingest_call_start,
            S.REC_CALL_END: self._ingest_call_end,
            S.REC_TURN: self._ingest_turn,
            S.REC_VERDICT: self._ingest_verdict,
            S.REC_ACTION: self._ingest_action,
            S.REC_METRIC: self._ingest_metric,
        }.get(record.get("type"))
        if handler is None:
            raise ValueError(f"unknown record type: {record.get('type')!r}")
        with self._lock:
            handler(record)
            self._conn.commit()

    def _ingest_call_start(self, r: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO calls (call_id, agent_id, agent_version, channel,"
            " started_at, ended_at, outcome) VALUES (?,?,?,?,?,NULL,'in_progress')",
            (r[S.ATTR_CALL_ID], r.get(S.ATTR_AGENT_ID), r.get(S.ATTR_AGENT_VERSION),
             r.get(S.ATTR_CHANNEL), r.get("t")),
        )

    def _ingest_call_end(self, r: dict) -> None:
        self._conn.execute(
            "UPDATE calls SET ended_at = ?, outcome = ? WHERE call_id = ?",
            (r.get("t"), r.get(S.ATTR_OUTCOME, "completed"), r[S.ATTR_CALL_ID]),
        )

    def _ingest_turn(self, r: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO turns (turn_id, call_id, turn_index, role,"
            " transcript, asr_confidence, audio_offset_ms, state_snapshot_id, t)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (r[S.ATTR_TURN_ID], r[S.ATTR_CALL_ID], r.get(S.ATTR_TURN_INDEX),
             r.get(S.ATTR_ROLE), r.get(S.ATTR_TRANSCRIPT),
             r.get(S.ATTR_ASR_CONFIDENCE), r.get(S.ATTR_AUDIO_OFFSET_MS),
             r.get(S.ATTR_STATE_SNAPSHOT_ID), r.get("t")),
        )

    def _ingest_verdict(self, r: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO verdicts (verdict_id, call_id, turn_id, detector,"
            " fired, severity, latency_ms, evidence, arbitration, t)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r[S.ATTR_VERDICT_ID], r[S.ATTR_CALL_ID], r[S.ATTR_TURN_ID],
             r.get(S.ATTR_DETECTOR), int(bool(r.get(S.ATTR_FIRED))),
             r.get(S.ATTR_SEVERITY), r.get(S.ATTR_LATENCY_MS),
             json.dumps(r.get(S.ATTR_EVIDENCE)), json.dumps(r.get(S.ATTR_ARBITRATION)),
             r.get("t")),
        )

    def _ingest_action(self, r: dict) -> None:
        self._conn.execute(
            "INSERT INTO actions (call_id, turn_id, taken, hook, correction_text, t)"
            " VALUES (?,?,?,?,?,?)",
            (r[S.ATTR_CALL_ID], r[S.ATTR_TURN_ID], r.get(S.ATTR_ACTION_TAKEN),
             r.get(S.ATTR_ACTION_HOOK), r.get(S.ATTR_ACTION_CORRECTION), r.get("t")),
        )

    def _ingest_metric(self, r: dict) -> None:
        # Trend metrics belong in a real metrics backend (OTLP). The store
        # ignores them; rollups are computed from turns/verdicts at query.
        pass

    # -- queries ------------------------------------------------------------

    def list_calls(self) -> list[dict]:
        """Call-list view: one row per call with rollups for badges."""
        with self._lock:
            calls = [dict(row) for row in self._conn.execute(
                "SELECT * FROM calls ORDER BY started_at DESC")]
            for call in calls:
                call_id = call["call_id"]
                # Suppressed verdicts arrive with fired=0 (arbitration runs
                # before emission), and info-level gate markers aren't flags.
                flags = {row["detector"]: row["n"] for row in self._conn.execute(
                    "SELECT detector, COUNT(*) AS n FROM verdicts"
                    " WHERE call_id = ? AND fired = 1 AND severity != 'info'"
                    " GROUP BY detector", (call_id,))}
                severities = [row["severity"] for row in self._conn.execute(
                    "SELECT DISTINCT severity FROM verdicts"
                    " WHERE call_id = ? AND fired = 1 AND severity != 'info'",
                    (call_id,))]
                interventions = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM actions WHERE call_id = ?"
                    " AND taken NOT IN ('none')", (call_id,)).fetchone()["n"]
                call["flags_by_layer"] = flags
                call["flag_count"] = sum(flags.values())
                call["max_severity"] = max(
                    severities, key=SEVERITIES.index, default=None)
                call["intervention_count"] = interventions
        return calls

    def get_call(self, call_id: str) -> dict | None:
        """Full turn timeline with verdicts + actions, for the detail view."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            if row is None:
                return None
            call = dict(row)
            turns = [dict(t) for t in self._conn.execute(
                "SELECT * FROM turns WHERE call_id = ? ORDER BY turn_index",
                (call_id,))]
            verdicts_by_turn: dict[str, list[dict]] = {}
            for v in self._conn.execute(
                    "SELECT * FROM verdicts WHERE call_id = ?", (call_id,)):
                d = dict(v)
                d["fired"] = bool(d["fired"])
                d["evidence"] = json.loads(d["evidence"]) if d["evidence"] else None
                d["arbitration"] = json.loads(d["arbitration"]) if d["arbitration"] else None
                verdicts_by_turn.setdefault(d["turn_id"], []).append(d)
            actions_by_turn: dict[str, list[dict]] = {}
            for a in self._conn.execute(
                    "SELECT * FROM actions WHERE call_id = ?", (call_id,)):
                actions_by_turn.setdefault(a["turn_id"], []).append(dict(a))
        for turn in turns:
            turn["verdicts"] = verdicts_by_turn.get(turn["turn_id"], [])
            turn["actions"] = actions_by_turn.get(turn["turn_id"], [])
        call["turns"] = turns
        return call
