"""SSE broadcaster — polls SQLite, fans out events to subscribers.

One background task per FastAPI process. It tracks the highest IDs it
has seen for turns / mirror_events / interventions, plus the most
recent ended_at it has observed in calls. Every 1s it queries for new
rows since those watermarks and pushes them into a shared in-memory
event log. Each subscriber holds an asyncio.Queue and gets every new
event after the moment they connected.

For per-call SSE, the same broadcaster fans events to a per-call
subscriber by filtering on call_uuid.

Scale assumption: ≤1 concurrent call, a handful of dashboard tabs.
This is intentionally simple — no pub/sub, no LISTEN/NOTIFY, just
polling SQLite. Replace later if scale demands.
"""

import asyncio
import json
import logging
from typing import Any

import db

log = logging.getLogger("mirror.dashboard.sse")

# Watermarks for "what we've already broadcast".
_state: dict[str, Any] = {
    "last_turn_id": 0,
    "last_mirror_event_id": 0,
    "last_intervention_id": 0,
    "started_call_uuids": set(),
    "ended_call_uuids": set(),
}

# All subscriber queues. Each is an asyncio.Queue[dict].
_subscribers: list[asyncio.Queue] = []
_subscribers_lock = asyncio.Lock()

_poller_task: asyncio.Task | None = None


async def subscribe() -> asyncio.Queue:
    """Register a new subscriber. Returns a queue the caller can drain."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    async with _subscribers_lock:
        _subscribers.append(q)
    return q


async def unsubscribe(q: asyncio.Queue) -> None:
    async with _subscribers_lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


async def _broadcast(event: dict) -> None:
    async with _subscribers_lock:
        dead: list[asyncio.Queue] = []
        for q in _subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop slowest subscribers rather than blocking the poller
                dead.append(q)
        for q in dead:
            try:
                _subscribers.remove(q)
            except ValueError:
                pass


def _seed_watermarks() -> None:
    """On startup, set watermarks to the current max IDs so we don't
    re-broadcast every row that already exists in the DB."""
    try:
        with db.get_conn() as conn:
            t = conn.execute("SELECT MAX(id) AS m FROM turns").fetchone()
            m = conn.execute("SELECT MAX(id) AS m FROM mirror_events").fetchone()
            i = conn.execute("SELECT MAX(id) AS m FROM interventions").fetchone()
        _state["last_turn_id"] = int(t["m"] or 0)
        _state["last_mirror_event_id"] = int(m["m"] or 0)
        _state["last_intervention_id"] = int(i["m"] or 0)
        # Seed started/ended call uuid sets with whatever's already in DB.
        with db.get_conn() as conn:
            for row in conn.execute(
                "SELECT call_uuid, started_at, ended_at FROM calls"
            ).fetchall():
                if row["started_at"]:
                    _state["started_call_uuids"].add(row["call_uuid"])
                if row["ended_at"]:
                    _state["ended_call_uuids"].add(row["call_uuid"])
        log.info(
            "SSE watermarks seeded: turns=%d events=%d interventions=%d "
            "started=%d ended=%d",
            _state["last_turn_id"],
            _state["last_mirror_event_id"],
            _state["last_intervention_id"],
            len(_state["started_call_uuids"]),
            len(_state["ended_call_uuids"]),
        )
    except Exception:
        log.exception("failed to seed SSE watermarks")


def _agent_for(call_uuid: str) -> str:
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(agent_name, 'pizza-plivo') AS a FROM calls "
                "WHERE call_uuid = ?",
                (call_uuid,),
            ).fetchone()
        return row["a"] if row else "pizza-plivo"
    except Exception:
        return "pizza-plivo"


async def _poll_once() -> None:
    """One pass over the tables, broadcast everything new."""
    try:
        with db.get_conn() as conn:
            new_turns = conn.execute(
                "SELECT id, call_uuid, role, text, timestamp FROM turns "
                "WHERE id > ? ORDER BY id ASC",
                (_state["last_turn_id"],),
            ).fetchall()
            new_events = conn.execute(
                "SELECT id, call_uuid, turn_id, pattern_name, severity, "
                "       evidence, intervention_needed, timestamp "
                "FROM mirror_events WHERE id > ? ORDER BY id ASC",
                (_state["last_mirror_event_id"],),
            ).fetchall()
            new_interventions = conn.execute(
                "SELECT id, call_uuid, pattern_name, strategy, buffer_text, "
                "       correction_text, latency_ms, timestamp "
                "FROM interventions WHERE id > ? ORDER BY id ASC",
                (_state["last_intervention_id"],),
            ).fetchall()
            calls = conn.execute(
                "SELECT call_uuid, caller, started_at, ended_at, status, "
                "       COALESCE(agent_name, 'pizza-plivo') AS agent_name, "
                "       COALESCE(mirror_enabled, 1) AS mirror_enabled, "
                "       final_outcome "
                "FROM calls ORDER BY started_at DESC LIMIT 50"
            ).fetchall()
    except Exception:
        log.exception("SSE poll failed")
        return

    # call_started events
    for c in calls:
        u = c["call_uuid"]
        if u and u not in _state["started_call_uuids"] and c["started_at"]:
            _state["started_call_uuids"].add(u)
            await _broadcast(
                {
                    "type": "call_started",
                    "call_uuid": u,
                    "timestamp": c["started_at"],
                    "agent_name": c["agent_name"],
                    "payload": {
                        "caller": c["caller"],
                        "mirror_enabled": bool(c["mirror_enabled"]),
                    },
                }
            )

    # turn events
    for t in new_turns:
        _state["last_turn_id"] = max(_state["last_turn_id"], int(t["id"]))
        await _broadcast(
            {
                "type": "turn",
                "call_uuid": t["call_uuid"],
                "timestamp": t["timestamp"],
                "agent_name": _agent_for(t["call_uuid"]),
                "payload": {
                    "id": t["id"],
                    "role": t["role"],
                    "text": t["text"],
                },
            }
        )

    # mirror_event events
    for e in new_events:
        _state["last_mirror_event_id"] = max(
            _state["last_mirror_event_id"], int(e["id"])
        )
        try:
            evidence = json.loads(e["evidence"]) if e["evidence"] else {}
        except Exception:
            evidence = {"raw": e["evidence"]}
        await _broadcast(
            {
                "type": "mirror_event",
                "call_uuid": e["call_uuid"],
                "timestamp": e["timestamp"],
                "agent_name": _agent_for(e["call_uuid"]),
                "payload": {
                    "id": e["id"],
                    "turn_id": e["turn_id"],
                    "pattern_name": e["pattern_name"],
                    "severity": e["severity"],
                    "intervention_needed": bool(e["intervention_needed"]),
                    "evidence": evidence,
                },
            }
        )

    # intervention events
    for i in new_interventions:
        _state["last_intervention_id"] = max(
            _state["last_intervention_id"], int(i["id"])
        )
        await _broadcast(
            {
                "type": "intervention",
                "call_uuid": i["call_uuid"],
                "timestamp": i["timestamp"],
                "agent_name": _agent_for(i["call_uuid"]),
                "payload": {
                    "id": i["id"],
                    "pattern_name": i["pattern_name"],
                    "strategy": i["strategy"],
                    "buffer_text": i["buffer_text"],
                    "correction_text": i["correction_text"],
                    "latency_ms": i["latency_ms"],
                },
            }
        )

    # call_ended events
    for c in calls:
        u = c["call_uuid"]
        if u and c["ended_at"] and u not in _state["ended_call_uuids"]:
            _state["ended_call_uuids"].add(u)
            await _broadcast(
                {
                    "type": "call_ended",
                    "call_uuid": u,
                    "timestamp": c["ended_at"],
                    "agent_name": c["agent_name"],
                    "payload": {
                        "status": c["status"],
                        "final_outcome": c["final_outcome"],
                        "mirror_enabled": bool(c["mirror_enabled"]),
                    },
                }
            )


async def _poll_forever() -> None:
    _seed_watermarks()
    while True:
        await _poll_once()
        await asyncio.sleep(1.0)


def ensure_poller_started() -> None:
    """Idempotently start the background poller on first request."""
    global _poller_task
    if _poller_task is None or _poller_task.done():
        loop = asyncio.get_event_loop()
        _poller_task = loop.create_task(_poll_forever())
        log.info("SSE poller started")


def format_sse(event: dict) -> str:
    """Format a single event as an SSE wire message."""
    event_type = event.get("type", "message")
    data = json.dumps(event, ensure_ascii=False)
    return f"event: {event_type}\ndata: {data}\n\n"
