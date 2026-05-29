"""Read-only aggregate queries for the dashboard stats cards.

Nothing here writes. Everything is a single SQLite read against the
existing tables (calls, turns, orders, mirror_events, interventions).
"""

import json
from datetime import datetime, timezone

import db
from agents.travel.primary import _BASE_PRICES as _TRAVEL_PRICES
from mirror.patterns import PIZZA_ITEMS

_PIZZA_VOCAB = {w.lower() for w in PIZZA_ITEMS}
_TRAVEL_DESTINATIONS = {w.lower() for w in _TRAVEL_PRICES.keys()}


def _today_iso_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def calls_today_count() -> int:
    today = _today_iso_prefix()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE started_at LIKE ?",
            (f"{today}%",),
        ).fetchone()
    return int(row["n"]) if row else 0


def mirror_events_today_count() -> int:
    today = _today_iso_prefix()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM mirror_events WHERE timestamp LIKE ?",
            (f"{today}%",),
        ).fetchone()
    return int(row["n"]) if row else 0


def interventions_today_count() -> int:
    today = _today_iso_prefix()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM interventions WHERE timestamp LIKE ?",
            (f"{today}%",),
        ).fetchone()
    return int(row["n"]) if row else 0


def avg_call_health_today() -> float:
    """Health = 10 - (intervention_count / max(turn_count, 1)) * 10, clamped.

    Averaged across today's calls. If there are no calls today, returns
    10.0 (nothing has gone wrong yet).
    """
    today = _today_iso_prefix()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT c.call_uuid, "
            "  (SELECT COUNT(*) FROM turns t WHERE t.call_uuid = c.call_uuid) AS turn_count, "
            "  (SELECT COUNT(*) FROM interventions i WHERE i.call_uuid = c.call_uuid) AS interv_count "
            "FROM calls c WHERE c.started_at LIKE ?",
            (f"{today}%",),
        ).fetchall()
    if not rows:
        return 10.0
    scores = []
    for row in rows:
        turns = max(int(row["turn_count"] or 0), 1)
        interv = int(row["interv_count"] or 0)
        score = 10.0 - (interv / turns) * 10.0
        scores.append(max(0.0, min(10.0, score)))
    return round(sum(scores) / len(scores), 1) if scores else 10.0


def collect_stats() -> dict:
    """One-shot payload for the index page / /stats.json polling."""
    return {
        "calls_today": calls_today_count(),
        "mirror_events_today": mirror_events_today_count(),
        "customer_rescues_today": interventions_today_count(),
        "avg_call_health": avg_call_health_today(),
    }


def recent_calls(limit: int = 20, filter_mode: str = "all") -> list:
    """Recent calls for the fleet view. filter_mode: all | with | without | failed.

    Returns dicts ready to render — includes derived fields like
    duration_seconds, intervention_count, masked caller, etc.
    """
    where = ""
    params: list = []
    if filter_mode == "with":
        where = "WHERE mirror_enabled = 1"
    elif filter_mode == "without":
        where = "WHERE mirror_enabled = 0"
    elif filter_mode == "failed":
        where = "WHERE final_outcome = 'wrong_order'"
    elif filter_mode == "today":
        where = "WHERE started_at LIKE ?"
        params.append(f"{_today_iso_prefix()}%")

    sql = (
        "SELECT c.call_uuid, c.caller, c.started_at, c.ended_at, c.status, "
        "       COALESCE(c.agent_name, 'pizza-plivo') AS agent_name, "
        "       COALESCE(c.mirror_enabled, 1) AS mirror_enabled, "
        "       c.final_outcome, "
        "       (SELECT COUNT(*) FROM interventions i WHERE i.call_uuid = c.call_uuid) AS intervention_count, "
        "       (SELECT COUNT(*) FROM mirror_events m WHERE m.call_uuid = c.call_uuid AND m.intervention_needed = 1) AS flagged_count, "
        # Agent-specific output. For pizza-plivo this is the last
        # place_order's items_json; for future agents it would be
        # whatever their equivalent "result" record is.
        "       (SELECT items_json FROM orders o WHERE o.call_uuid = c.call_uuid ORDER BY o.id DESC LIMIT 1) AS last_order_items_json "
        f"FROM calls c {where} "
        "ORDER BY c.started_at DESC LIMIT ?"
    )
    params.append(limit)
    with db.get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_decorate_call_row(dict(row)) for row in rows]


def _decorate_call_row(row: dict) -> dict:
    row["caller_masked"] = _mask_caller(row.get("caller") or "")
    row["duration_seconds"] = _duration_seconds(
        row.get("started_at"), row.get("ended_at")
    )
    row["short_uuid"] = (row.get("call_uuid") or "")[:8]
    raw_items = _parse_output_items(row.get("last_order_items_json"))
    agent_name = row.get("agent_name") or "pizza-plivo"
    row["output_items"] = _filter_output_for_agent(agent_name, raw_items)
    row["output_display"] = _format_output_display(row["output_items"])
    return row


def _parse_output_items(items_json) -> list:
    """Best-effort parse of the order's items_json string. Returns a
    list of item-name strings. Agent-specific: pizza-plivo writes a
    JSON array of strings via db.place_order(). Other agents would
    write a different shape; their renderer would handle it."""
    if not items_json:
        return []
    if isinstance(items_json, list):
        return [str(x) for x in items_json]
    try:
        parsed = json.loads(items_json)
    except (TypeError, ValueError):
        return [str(items_json)]
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    return [str(parsed)]


def _format_output_display(items: list) -> str:
    """Compact human-readable rendering for table cells."""
    if not items:
        return ""
    return ", ".join(items)


def _filter_output_for_agent(agent_name: str, items: list) -> list:
    """Agent-specific sanity filter for the Output column.

    Defends the dashboard against the rigged primaries occasionally
    placing nonsense orders (e.g. capturing customer chitchat as a pizza
    item, fabricating "unknown" dates for flights). If after filtering
    there's nothing left, the cell renders as "—".

    Pizza items must mention something from PIZZA_ITEMS. Travel items
    must mention a known destination word. Both checks are substring
    on lowercased text — agent-specific, opt-in per agent."""
    if not items:
        return []
    if agent_name == "pizza-plivo":
        return [i for i in items if _looks_like_pizza_item(i)]
    if agent_name == "travel-plivo":
        return [i for i in items if _looks_like_travel_booking(i)]
    # Unknown agent — pass through unfiltered.
    return list(items)


def _looks_like_pizza_item(item: str) -> bool:
    s = (item or "").lower()
    if not s:
        return False
    return any(word in s for word in _PIZZA_VOCAB)


def _looks_like_travel_booking(item: str) -> bool:
    """Bookings are formatted as "{destination} {date} {class} x{N}".
    The first token must be a known destination; otherwise the LLM
    likely put junk in the destination slot (e.g. "January Bangalore..."
    where the month landed where the city should be)."""
    s = (item or "").strip().lower()
    if not s:
        return False
    tokens = s.split()
    if not tokens:
        return False
    first = tokens[0]
    return any(first.startswith(city) or city in first for city in _TRAVEL_DESTINATIONS)


def _mask_caller(caller: str) -> str:
    if not caller or len(caller) < 7:
        return caller or "—"
    # +91-XXXXX-12345 style. Show country prefix + last 5.
    return f"{caller[:3]}-XXXXX-{caller[-5:]}"


def _duration_seconds(started_at, ended_at) -> int | None:
    if not started_at or not ended_at:
        return None
    try:
        s = datetime.fromisoformat(started_at)
        e = datetime.fromisoformat(ended_at)
        return int((e - s).total_seconds())
    except (ValueError, TypeError):
        return None


def call_detail(call_uuid: str) -> dict | None:
    """Full timeline for a single call: turns + mirror_events + interventions + order."""
    with db.get_conn() as conn:
        call_row = conn.execute(
            "SELECT call_uuid, caller, started_at, ended_at, status, "
            "       COALESCE(agent_name, 'pizza-plivo') AS agent_name, "
            "       COALESCE(mirror_enabled, 1) AS mirror_enabled, "
            "       final_outcome "
            "FROM calls WHERE call_uuid = ?",
            (call_uuid,),
        ).fetchone()
        if call_row is None:
            return None
        turns = conn.execute(
            "SELECT id, role, text, timestamp FROM turns "
            "WHERE call_uuid = ? ORDER BY id ASC",
            (call_uuid,),
        ).fetchall()
        mirror_events = conn.execute(
            "SELECT id, turn_id, pattern_name, severity, evidence, "
            "       intervention_needed, timestamp "
            "FROM mirror_events WHERE call_uuid = ? ORDER BY id ASC",
            (call_uuid,),
        ).fetchall()
        interventions = conn.execute(
            "SELECT id, triggered_by_event_id, pattern_name, strategy, "
            "       buffer_text, correction_text, cached_audio_used, "
            "       latency_ms, timestamp "
            "FROM interventions WHERE call_uuid = ? ORDER BY id ASC",
            (call_uuid,),
        ).fetchall()
        orders = conn.execute(
            "SELECT id, items_json, created_at FROM orders "
            "WHERE call_uuid = ? ORDER BY id ASC",
            (call_uuid,),
        ).fetchall()

    call = _decorate_call_row(dict(call_row))
    return {
        "call": call,
        "turns": [dict(t) for t in turns],
        "mirror_events": [dict(e) for e in mirror_events],
        "interventions": [dict(i) for i in interventions],
        "orders": [dict(o) for o in orders],
    }


def latest_call_uuid_by_mirror(enabled: bool) -> str | None:
    """For the /compare default selection."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT call_uuid FROM calls "
            "WHERE COALESCE(mirror_enabled, 1) = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (1 if enabled else 0,),
        ).fetchone()
    return row["call_uuid"] if row else None
