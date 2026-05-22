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
