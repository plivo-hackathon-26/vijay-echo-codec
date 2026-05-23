import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = "mirror.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
                call_uuid  TEXT PRIMARY KEY,
                caller     TEXT,
                started_at TEXT,
                ended_at   TEXT,
                status     TEXT
            );
            CREATE TABLE IF NOT EXISTS turns (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                call_uuid TEXT,
                role      TEXT,
                text      TEXT,
                timestamp TEXT
            );
            CREATE TABLE IF NOT EXISTS orders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                call_uuid  TEXT,
                items_json TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS mirror_events (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                call_uuid           TEXT,
                turn_id             INTEGER,
                pattern_name        TEXT,
                severity            TEXT,
                evidence            TEXT,
                intervention_needed INTEGER,
                timestamp           TEXT
            );
            CREATE TABLE IF NOT EXISTS interventions (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                call_uuid             TEXT,
                triggered_by_event_id INTEGER,
                pattern_name          TEXT,
                strategy              TEXT,
                buffer_text           TEXT,
                correction_text       TEXT,
                cached_audio_used     INTEGER,
                latency_ms            INTEGER,
                timestamp             TEXT
            );
            """
        )

        # Phase 5 — additive columns on `calls` for the dashboard.
        # SQLite's ALTER TABLE ADD COLUMN doesn't support IF NOT
        # EXISTS, so we introspect pragma_table_info to stay
        # idempotent on every boot.
        existing_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(calls)").fetchall()
        }
        if "agent_name" not in existing_cols:
            conn.execute(
                "ALTER TABLE calls ADD COLUMN agent_name TEXT DEFAULT 'pizza-plivo'"
            )
        if "mirror_enabled" not in existing_cols:
            conn.execute(
                "ALTER TABLE calls ADD COLUMN mirror_enabled INTEGER DEFAULT 1"
            )
        if "final_outcome" not in existing_cols:
            conn.execute(
                "ALTER TABLE calls ADD COLUMN final_outcome TEXT"
            )

        # Phase H — failure_reports table (post-call analysis). Strictly
        # additive; existing tables and columns untouched.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS failure_reports (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                call_uuid          TEXT NOT NULL,
                pattern_name       TEXT,
                severity           TEXT,
                summary            TEXT,
                root_cause         TEXT,
                proposed_fix_text  TEXT,
                proposed_file      TEXT,
                suggested_diff     TEXT,
                confidence         REAL,
                status             TEXT NOT NULL DEFAULT 'pending',
                applied_pr_url     TEXT,
                applied_at         TEXT,
                dismissed_by       TEXT,
                dismissed_at       TEXT,
                created_at         TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_failure_reports_status
                ON failure_reports(status);
            CREATE INDEX IF NOT EXISTS idx_failure_reports_call_uuid
                ON failure_reports(call_uuid);
            """
        )


def create_call(call_uuid: str, caller: str, to: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO calls (call_uuid, caller, started_at, status) "
            "VALUES (?, ?, ?, ?)",
            (call_uuid, caller, _now(), "in_progress"),
        )


def end_call(call_uuid: str, status: str = "completed") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE calls SET ended_at = ?, status = ? WHERE call_uuid = ?",
            (_now(), status, call_uuid),
        )


def add_turn(call_uuid: str, role: str, text: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO turns (call_uuid, role, text, timestamp) VALUES (?, ?, ?, ?)",
            (call_uuid, role, text, _now()),
        )
        return cur.lastrowid


def get_recent_turns(call_uuid: str, limit: int = 10) -> list:
    """Return up to `limit` most-recent turns for a call, oldest-first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, role, text, timestamp FROM turns "
            "WHERE call_uuid = ? ORDER BY id DESC LIMIT ?",
            (call_uuid, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def add_mirror_event(
    call_uuid: str,
    turn_id,
    pattern_name: str,
    severity: str,
    evidence_dict: dict,
    intervention_needed: bool,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO mirror_events "
            "(call_uuid, turn_id, pattern_name, severity, evidence, "
            "intervention_needed, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                call_uuid,
                turn_id,
                pattern_name,
                severity,
                json.dumps(evidence_dict),
                1 if intervention_needed else 0,
                _now(),
            ),
        )
        return cur.lastrowid


def add_intervention(
    call_uuid: str,
    triggered_by_event_id,
    pattern_name: str,
    strategy: str,
    buffer_text: str,
    correction_text: str,
    cached_audio_used: bool,
    latency_ms: int,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO interventions "
            "(call_uuid, triggered_by_event_id, pattern_name, strategy, "
            "buffer_text, correction_text, cached_audio_used, latency_ms, "
            "timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call_uuid,
                triggered_by_event_id,
                pattern_name,
                strategy,
                buffer_text,
                correction_text,
                1 if cached_audio_used else 0,
                int(latency_ms),
                _now(),
            ),
        )
        return cur.lastrowid


def place_order(call_uuid: str, items: list) -> str:
    order_id = f"ORD-{uuid.uuid4().hex[:6]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO orders (call_uuid, items_json, created_at) VALUES (?, ?, ?)",
            (call_uuid, json.dumps(items), _now()),
        )
    return order_id


# ───────────────────────── Phase H: failure_reports helpers ──────────────


def create_failure_report(
    call_uuid: str,
    pattern_name: str | None,
    severity: str | None,
    summary: str | None,
    root_cause: str | None,
    proposed_fix_text: str | None,
    proposed_file: str | None,
    suggested_diff: str | None,
    confidence: float | None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO failure_reports "
            "(call_uuid, pattern_name, severity, summary, root_cause, "
            "proposed_fix_text, proposed_file, suggested_diff, confidence, "
            "status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call_uuid,
                pattern_name,
                severity,
                summary,
                root_cause,
                proposed_fix_text,
                proposed_file,
                suggested_diff,
                float(confidence) if confidence is not None else None,
                "pending",
                _now(),
            ),
        )
        return cur.lastrowid


_FAILURE_REPORTS_SELECT = (
    "SELECT f.*, "
    "       COALESCE(c.agent_name, 'pizza-plivo') AS agent_name "
    "FROM failure_reports f "
    "LEFT JOIN calls c ON c.call_uuid = f.call_uuid"
)


def get_failure_report_by_call(call_uuid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            f"{_FAILURE_REPORTS_SELECT} WHERE f.call_uuid = ? "
            "ORDER BY f.id DESC LIMIT 1",
            (call_uuid,),
        ).fetchone()
    return dict(row) if row else None


def get_failure_report_by_id(report_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            f"{_FAILURE_REPORTS_SELECT} WHERE f.id = ?",
            (int(report_id),),
        ).fetchone()
    return dict(row) if row else None


def list_failure_reports(status: str | None = "pending", limit: int = 50) -> list[dict]:
    """List reports (with agent_name joined in), optionally filtered by status.

    status='all' (or None) returns every row regardless of status.
    """
    if status in (None, "all"):
        sql = f"{_FAILURE_REPORTS_SELECT} ORDER BY f.id DESC LIMIT ?"
        params: tuple = (int(limit),)
    else:
        sql = (
            f"{_FAILURE_REPORTS_SELECT} WHERE f.status = ? "
            "ORDER BY f.id DESC LIMIT ?"
        )
        params = (status, int(limit))
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_failure_reports(status: str | None = "pending") -> int:
    if status in (None, "all"):
        sql = "SELECT COUNT(*) AS n FROM failure_reports"
        params: tuple = ()
    else:
        sql = "SELECT COUNT(*) AS n FROM failure_reports WHERE status = ?"
        params = (status,)
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def wipe_all_data() -> dict:
    """Delete every row from every data table. Schema is preserved.

    Returns a per-table count of rows removed so the caller can show
    feedback in the UI.
    """
    tables = (
        "interventions",
        "mirror_events",
        "orders",
        "turns",
        "failure_reports",
        "calls",
    )
    counts: dict[str, int] = {}
    with get_conn() as conn:
        for t in tables:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
            counts[t] = int(row["n"]) if row else 0
            conn.execute(f"DELETE FROM {t}")
        # Reset AUTOINCREMENT counters so new rows start at id=1 again.
        try:
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('turns','orders','mirror_events','interventions','failure_reports')"
            )
        except sqlite3.OperationalError:
            # sqlite_sequence only exists if at least one AUTOINCREMENT row
            # was ever inserted — safe to ignore when absent.
            pass
    return counts


def update_failure_report_status(report_id: int, status: str, **kwargs) -> bool:
    """Update the status (and any optional bookkeeping columns) of a
    failure_report row. Accepted kwargs: dismissed_by, dismissed_at,
    applied_pr_url, applied_at. Unknown kwargs are ignored.
    """
    allowed = {"dismissed_by", "dismissed_at", "applied_pr_url", "applied_at"}
    extra = {k: v for k, v in kwargs.items() if k in allowed}
    cols = ["status = ?"]
    vals: list = [status]
    for k, v in extra.items():
        cols.append(f"{k} = ?")
        vals.append(v)
    vals.append(int(report_id))
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE failure_reports SET {', '.join(cols)} WHERE id = ?",
            tuple(vals),
        )
        return cur.rowcount > 0
