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


def add_turn(call_uuid: str, role: str, text: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO turns (call_uuid, role, text, timestamp) VALUES (?, ?, ?, ?)",
            (call_uuid, role, text, _now()),
        )


def place_order(call_uuid: str, items: list) -> str:
    order_id = f"ORD-{uuid.uuid4().hex[:6]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO orders (call_uuid, items_json, created_at) VALUES (?, ?, ?)",
            (call_uuid, json.dumps(items), _now()),
        )
    return order_id
