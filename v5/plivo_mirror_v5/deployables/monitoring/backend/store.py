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
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    name TEXT,
    system_prompt TEXT,
    facts TEXT,
    policies TEXT,
    mode TEXT DEFAULT 'shadow',
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS audit_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    turn_id TEXT,
    kind TEXT,
    rationale TEXT,
    verdict_id TEXT,
    category TEXT,
    t REAL
);
CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    label TEXT NOT NULL,
    note TEXT,
    t REAL,
    UNIQUE(target_kind, target_id)
);
CREATE INDEX IF NOT EXISTS idx_labels_call ON labels(call_id);
CREATE INDEX IF NOT EXISTS idx_audit_call ON audit_findings(call_id);
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
            if path != ":memory:":
                # WAL: readers never block the ingest writer; busy_timeout
                # rides out writer contention instead of raising immediately.
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            # Idempotent column migrations for pre-existing db files.
            for ddl in (
                "ALTER TABLE turns ADD COLUMN audio_duration_ms REAL",
                "ALTER TABLE turns ADD COLUMN audio_levels TEXT",
            ):
                try:
                    self._conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # column already exists
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
        levels = r.get(S.ATTR_AUDIO_LEVELS)
        self._conn.execute(
            "INSERT OR REPLACE INTO turns (turn_id, call_id, turn_index, role,"
            " transcript, asr_confidence, audio_offset_ms, audio_duration_ms,"
            " audio_levels, state_snapshot_id, t)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r[S.ATTR_TURN_ID], r[S.ATTR_CALL_ID], r.get(S.ATTR_TURN_INDEX),
             r.get(S.ATTR_ROLE), r.get(S.ATTR_TRANSCRIPT),
             r.get(S.ATTR_ASR_CONFIDENCE), r.get(S.ATTR_AUDIO_OFFSET_MS),
             r.get(S.ATTR_AUDIO_DURATION_MS),
             json.dumps(levels) if levels is not None else None,
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

    # -- post-call audit findings ------------------------------------------

    def save_audit_findings(self, call_id: str, findings: list[dict],
                            *, t: float | None = None) -> None:
        """Replace the stored post-call analysis for a call (re-runnable)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM audit_findings WHERE call_id = ?", (call_id,))
            for f in findings:
                self._conn.execute(
                    "INSERT INTO audit_findings (call_id, turn_id, kind,"
                    " rationale, verdict_id, category, t) VALUES (?,?,?,?,?,?,?)",
                    (call_id, f.get("turn_id"), f.get("kind"),
                     f.get("rationale"), f.get("verdict_id"),
                     f.get("category"), t),
                )
            # mark analyzed even when zero findings
            self._conn.execute(
                "INSERT INTO audit_findings (call_id, turn_id, kind, rationale, t)"
                " SELECT ?, NULL, '_analyzed', '', ? WHERE NOT EXISTS ("
                "   SELECT 1 FROM audit_findings WHERE call_id = ?)",
                (call_id, t, call_id),
            )
            self._conn.commit()

    def get_audit_findings(self, call_id: str) -> dict:
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM audit_findings WHERE call_id = ?", (call_id,))]
        findings = [r for r in rows if r["kind"] != "_analyzed"]
        return {"analyzed": bool(rows), "findings": findings}

    # -- review loop: human labels → measured production precision ------------
    # The differentiator no ungrounded judge can copy: every flag carries a
    # ✓/✗ review, and the dashboard reports the system's measured precision
    # on YOUR traffic — not a benchmark claim, a live number.

    def save_label(self, call_id: str, target_kind: str, target_id: str,
                   label: str, *, note: str | None = None,
                   t: float | None = None) -> dict:
        with self._lock:
            self._conn.execute(
                "INSERT INTO labels (call_id, target_kind, target_id, label,"
                " note, t) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(target_kind, target_id) DO UPDATE SET"
                " label = excluded.label, note = excluded.note, t = excluded.t",
                (call_id, target_kind, target_id, label, note, t))
            self._conn.commit()
        return {"call_id": call_id, "target_kind": target_kind,
                "target_id": target_id, "label": label}

    def labels_for_call(self, call_id: str) -> dict:
        """{(target_kind, target_id): label} flattened for the frontend."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT target_kind, target_id, label FROM labels"
                " WHERE call_id = ?", (call_id,)).fetchall()
        return {f"{r['target_kind']}:{r['target_id']}": r["label"] for r in rows}

    def precision_stats(self) -> dict:
        """Measured precision per detector from reviewer labels: of the
        flags a human reviewed, how many were confirmed real."""
        with self._lock:
            verdict_rows = [dict(r) for r in self._conn.execute(
                "SELECT v.detector AS detector, l.label AS label, COUNT(*) AS n"
                " FROM labels l JOIN verdicts v ON l.target_id = v.verdict_id"
                " WHERE l.target_kind = 'verdict'"
                " GROUP BY v.detector, l.label")]
            finding_rows = [dict(r) for r in self._conn.execute(
                "SELECT 'JUDGE' AS detector, l.label AS label, COUNT(*) AS n"
                " FROM labels l WHERE l.target_kind = 'finding'"
                " GROUP BY l.label")]
        by_detector: dict[str, dict] = {}
        for row in (*verdict_rows, *finding_rows):
            d = by_detector.setdefault(row["detector"],
                                       {"confirmed": 0, "rejected": 0})
            if row["label"] in d:
                d[row["label"]] += row["n"]
        total_c = sum(d["confirmed"] for d in by_detector.values())
        total_r = sum(d["rejected"] for d in by_detector.values())
        for d in by_detector.values():
            n = d["confirmed"] + d["rejected"]
            d["precision"] = d["confirmed"] / n if n else None
        return {
            "reviewed": total_c + total_r,
            "confirmed": total_c,
            "rejected": total_r,
            "precision": (total_c / (total_c + total_r)
                          if (total_c + total_r) else None),
            "by_detector": by_detector,
        }

    # -- agent registry -------------------------------------------------------
    # The dashboard's "connect any LiveKit agent" flow: register an agent_id
    # (any stable name the host also passes to attach_mirror), store its
    # system prompt + facts + policies (the judge's grounding), and flip
    # ``mode`` to turn live intervention on/off from the dashboard.

    def upsert_agent(self, agent: dict, *, t: float) -> dict:
        agent_id = agent["agent_id"]
        with self._lock:
            existing = self._conn.execute(
                "SELECT created_at FROM agents WHERE agent_id = ?",
                (agent_id,)).fetchone()
            self._conn.execute(
                "INSERT OR REPLACE INTO agents (agent_id, name, system_prompt,"
                " facts, policies, mode, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (agent_id,
                 agent.get("name") or agent_id,
                 agent.get("system_prompt") or "",
                 json.dumps(agent.get("facts") or {}),
                 agent.get("policies") or "",
                 agent.get("mode") or "shadow",
                 existing["created_at"] if existing else t,
                 t))
            self._conn.commit()
        return self.get_agent(agent_id)

    def set_agent_mode(self, agent_id: str, mode: str, *, t: float) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE agents SET mode = ?, updated_at = ? WHERE agent_id = ?",
                (mode, t, agent_id))
            self._conn.commit()
        return self.get_agent(agent_id) if cur.rowcount else None

    def get_agent(self, agent_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if row is None:
            return None
        agent = dict(row)
        agent["facts"] = json.loads(agent["facts"] or "{}")
        return agent

    def list_agents(self) -> list[dict]:
        """Registry view with per-agent call rollups (incl. unregistered
        agent_ids seen in calls, so nothing that connected is invisible)."""
        with self._lock:
            registered = {r["agent_id"]: dict(r) for r in self._conn.execute(
                "SELECT * FROM agents ORDER BY created_at")}
            seen = [dict(r) for r in self._conn.execute(
                "SELECT agent_id, COUNT(*) AS calls, MAX(started_at) AS last_seen,"
                " SUM(EXISTS(SELECT 1 FROM verdicts v WHERE v.call_id = c.call_id"
                "            AND v.fired = 1 AND v.severity != 'info')) AS flagged"
                " FROM calls c GROUP BY agent_id")]
        rollups = {s["agent_id"]: s for s in seen}
        agents = []
        for agent_id, agent in registered.items():
            agent["facts"] = json.loads(agent["facts"] or "{}")
            roll = rollups.pop(agent_id, {})
            agent.update(calls=roll.get("calls", 0),
                         flagged=roll.get("flagged", 0) or 0,
                         last_seen=roll.get("last_seen"),
                         registered=True)
            agents.append(agent)
        for agent_id, roll in rollups.items():  # connected but never registered
            if agent_id:
                agents.append({"agent_id": agent_id, "name": agent_id,
                               "mode": "shadow", "registered": False,
                               "calls": roll["calls"],
                               "flagged": roll["flagged"] or 0,
                               "last_seen": roll["last_seen"]})
        return agents

    # -- fleet stats ---------------------------------------------------------

    def stats_overview(self, days: int = 14, *, now: float | None = None) -> dict:
        """Fleet rollups for the dashboard home: KPIs, per-day trend,
        failure-category breakdown, and per-agent-version comparison.
        "Flagged" everywhere means: ≥1 fired, non-info verdict."""
        import time as _time

        now = now or _time.time()
        since = now - days * 86400
        with self._lock:
            kpi = self._conn.execute(
                "SELECT COUNT(*) AS calls,"
                " SUM(EXISTS(SELECT 1 FROM verdicts v WHERE v.call_id = c.call_id"
                "            AND v.fired = 1 AND v.severity != 'info')) AS flagged,"
                " SUM(EXISTS(SELECT 1 FROM audit_findings f"
                "            WHERE f.call_id = c.call_id)) AS audited,"
                " SUM(EXISTS(SELECT 1 FROM audit_findings f"
                "            WHERE f.call_id = c.call_id"
                "            AND f.kind = 'missed_failure')) AS judge_flagged"
                " FROM calls c WHERE c.started_at >= ?", (since,)).fetchone()
            interventions = self._conn.execute(
                "SELECT COUNT(*) AS n FROM actions a JOIN calls c USING (call_id)"
                " WHERE c.started_at >= ? AND a.taken != 'none'", (since,),
            ).fetchone()["n"]
            daily = [dict(r) for r in self._conn.execute(
                "SELECT strftime('%Y-%m-%d', c.started_at, 'unixepoch') AS day,"
                " COUNT(*) AS calls,"
                " SUM(EXISTS(SELECT 1 FROM verdicts v WHERE v.call_id = c.call_id"
                "            AND v.fired = 1 AND v.severity != 'info')) AS flagged"
                " FROM calls c WHERE c.started_at >= ?"
                " GROUP BY day ORDER BY day", (since,))]
            categories = [dict(r) for r in self._conn.execute(
                "SELECT json_extract(v.evidence, '$.claim_type') AS category,"
                " v.detector AS detector, COUNT(*) AS hits,"
                " COUNT(DISTINCT v.call_id) AS calls"
                " FROM verdicts v JOIN calls c USING (call_id)"
                " WHERE c.started_at >= ? AND v.fired = 1 AND v.severity != 'info'"
                " GROUP BY category, detector ORDER BY hits DESC", (since,))]
            judge_categories = [dict(r) for r in self._conn.execute(
                "SELECT f.category AS category, 'JUDGE' AS detector,"
                " COUNT(*) AS hits, COUNT(DISTINCT f.call_id) AS calls"
                " FROM audit_findings f JOIN calls c USING (call_id)"
                " WHERE c.started_at >= ? AND f.kind = 'missed_failure'"
                " AND f.category IS NOT NULL"
                " GROUP BY f.category ORDER BY hits DESC", (since,))]
            versions = [dict(r) for r in self._conn.execute(
                "SELECT c.agent_id, c.agent_version, COUNT(*) AS calls,"
                " SUM(EXISTS(SELECT 1 FROM verdicts v WHERE v.call_id = c.call_id"
                "            AND v.fired = 1 AND v.severity != 'info')) AS flagged"
                " FROM calls c WHERE c.started_at >= ?"
                " GROUP BY c.agent_id, c.agent_version"
                " ORDER BY c.agent_id, c.agent_version", (since,))]
        calls_n = kpi["calls"] or 0
        flagged_n = kpi["flagged"] or 0
        for v in versions:
            v["flag_rate"] = (v["flagged"] or 0) / v["calls"] if v["calls"] else 0.0
        return {
            "window_days": days,
            "calls": calls_n,
            "flagged_calls": flagged_n,
            "flag_rate": flagged_n / calls_n if calls_n else 0.0,
            "interventions": interventions,
            "audited_calls": kpi["audited"] or 0,
            "judge_flagged_calls": kpi["judge_flagged"] or 0,
            "daily": daily,
            "categories": categories + judge_categories,
            "versions": versions,
        }

    def systemic_patterns(self, min_calls: int = 2) -> dict:
        """Cross-call failure clustering — the grounded-evidence superpower:
        the SAME wrong value, against the SAME truth source, across many
        calls is a prompt/config bug, not a one-off. Receipts included."""
        with self._lock:
            facts = [dict(r) for r in self._conn.execute(
                "SELECT json_extract(evidence, '$.source') AS source,"
                " json_extract(evidence, '$.spoken_value') AS spoken_value,"
                " json_extract(evidence, '$.truth_value') AS truth_value,"
                " json_extract(evidence, '$.claim_type') AS claim_type,"
                " COUNT(*) AS hits, COUNT(DISTINCT call_id) AS calls,"
                " MIN(t) AS first_seen, MAX(t) AS last_seen,"
                " GROUP_CONCAT(DISTINCT call_id) AS call_ids"
                " FROM verdicts WHERE fired = 1 AND severity != 'info'"
                " AND source IS NOT NULL"
                " GROUP BY source, spoken_value"
                " HAVING COUNT(DISTINCT call_id) >= ?"
                " ORDER BY calls DESC, hits DESC", (min_calls,))]
            judge = [dict(r) for r in self._conn.execute(
                "SELECT category, COUNT(*) AS hits,"
                " COUNT(DISTINCT call_id) AS calls,"
                " MIN(t) AS first_seen, MAX(t) AS last_seen,"
                " GROUP_CONCAT(DISTINCT call_id) AS call_ids"
                " FROM audit_findings WHERE kind = 'missed_failure'"
                " AND category IS NOT NULL GROUP BY category"
                " HAVING COUNT(DISTINCT call_id) >= ?"
                " ORDER BY calls DESC", (min_calls,))]
        for row in (*facts, *judge):
            row["call_ids"] = (row["call_ids"] or "").split(",")
        return {"fact_patterns": facts, "judge_clusters": judge}

    # -- queries ------------------------------------------------------------

    def list_calls(self, limit: int = 200, offset: int = 0) -> list[dict]:
        """Call-list view: one row per call with rollups for badges.
        Paginated — an unbounded SELECT over months of traffic would melt
        both the backend and the browser."""
        with self._lock:
            calls = [dict(row) for row in self._conn.execute(
                "SELECT * FROM calls ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (limit, offset))]
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
            if turn.get("audio_levels"):
                turn["audio_levels"] = json.loads(turn["audio_levels"])
        call["turns"] = turns
        call["audit"] = self.get_audit_findings(call_id)
        call["labels"] = self.labels_for_call(call_id)
        return call
