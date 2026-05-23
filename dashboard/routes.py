"""FastAPI router exposing the Phase 5 dashboard.

All routes are namespaced — they don't collide with /voice/answer,
/voice/stream, or /health. The router itself is mounted in main.py
with one include_router() call; everything else lives in this package.
"""

import asyncio
import json
import logging
import os
from typing import Optional

import db
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from dashboard import agent_router, mirror_toggle, sse, stats

log = logging.getLogger("mirror.dashboard.routes")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TEMPLATES_DIR = os.path.join(_THIS_DIR, "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter()


# ---------- HTML pages -----------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    sse.ensure_poller_started()
    payload = stats.collect_stats()
    calls = stats.recent_calls(limit=20, filter_mode="all")
    # Polish B: server-render today's value-saved so the stat card has
    # a real number on first paint (no blank "$0.00" flash while HTMX
    # polls in).
    from mirror import value_model as _vm
    value_today = _vm.calculate_total_value_saved_today()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "stats": payload,
            "calls": calls,
            "mirror_enabled": mirror_toggle.get_global_enabled(),
            "active_filter": "all",
            "current_agent": mirror_toggle.get_current_agent(),
            "agents": agent_router.known_agents(),
            "value_today": value_today,
        },
    )


@router.post("/set-agent")
async def set_agent(request: Request, agent_name: str = Form(...)):
    """Switch the active agent for FUTURE calls. In-flight calls are
    unaffected (agent_name is frozen at create_call time)."""
    if agent_name not in agent_router.known_agents():
        raise HTTPException(status_code=400, detail="unknown agent")
    mirror_toggle.set_current_agent(agent_name)
    if request.headers.get("HX-Request"):
        return HTMLResponse(_render_agent_pill(agent_name, agent_router.known_agents()))
    return RedirectResponse(url="/", status_code=303)


@router.get("/agent-status.json")
async def agent_status_json():
    return JSONResponse(
        {
            "current_agent": mirror_toggle.get_current_agent(),
            "available": agent_router.known_agents(),
        }
    )


def _render_agent_pill(current: str, available: list) -> str:
    options = "".join(
        f'<option value="{a}"{" selected" if a == current else ""}>{a}</option>'
        for a in available
    )
    return (
        '<select hx-post="/set-agent" hx-trigger="change" '
        'hx-target="#agent-selector" hx-swap="outerHTML" '
        'name="agent_name" id="agent-selector" '
        'class="bg-transparent border border-faint rounded-md px-2 py-1 text-sm '
        'focus:outline-none focus:ring-1 focus:ring-emerald-500/30">'
        f"{options}"
        "</select>"
    )


@router.get("/calls/{call_uuid}", response_class=HTMLResponse)
async def call_detail_page(request: Request, call_uuid: str):
    sse.ensure_poller_started()
    detail = stats.call_detail(call_uuid)
    if detail is None:
        raise HTTPException(status_code=404, detail="call not found")
    from mirror import value_model as _vm
    value_saved = _vm.calculate_value_saved(call_uuid)
    return templates.TemplateResponse(
        request=request,
        name="call_detail.html",
        context={
            **detail,
            "mirror_enabled": mirror_toggle.get_global_enabled(),
            "current_agent": mirror_toggle.get_current_agent(),
            "agents": agent_router.known_agents(),
            "value_saved": value_saved,
        },
    )


@router.get("/compare", response_class=HTMLResponse)
async def compare_page(
    request: Request,
    a: Optional[str] = None,
    b: Optional[str] = None,
):
    sse.ensure_poller_started()
    if a is None:
        a = stats.latest_call_uuid_by_mirror(enabled=False)
    if b is None:
        b = stats.latest_call_uuid_by_mirror(enabled=True)
    detail_a = stats.call_detail(a) if a else None
    detail_b = stats.call_detail(b) if b else None
    candidates = stats.recent_calls(limit=30, filter_mode="all")
    from mirror import value_model as _vm
    value_compare = _vm.calculate_value_saved_for_compare(a, b)
    return templates.TemplateResponse(
        request=request,
        name="compare.html",
        context={
            "detail_a": detail_a,
            "detail_b": detail_b,
            "candidates": candidates,
            "selected_a": a,
            "selected_b": b,
            "mirror_enabled": mirror_toggle.get_global_enabled(),
            "current_agent": mirror_toggle.get_current_agent(),
            "agents": agent_router.known_agents(),
            "value_compare": value_compare,
        },
    )


# ---------- JSON endpoints (used by HTMX polling) --------------------------


@router.get("/stats.json")
async def stats_json():
    return JSONResponse(stats.collect_stats())


@router.get("/calls.json")
async def calls_json(filter: str = "all", limit: int = 20):
    if filter not in {"all", "with", "without", "failed", "today"}:
        filter = "all"
    return JSONResponse(stats.recent_calls(limit=limit, filter_mode=filter))


@router.get("/calls/{call_uuid}.json")
async def call_detail_json(call_uuid: str):
    detail = stats.call_detail(call_uuid)
    if detail is None:
        raise HTTPException(status_code=404, detail="call not found")
    return JSONResponse(detail)


@router.get("/mirror-status.json")
async def mirror_status_json():
    return JSONResponse({"enabled": mirror_toggle.get_global_enabled()})


@router.post("/toggle-mirror")
async def toggle_mirror(request: Request, enabled: Optional[str] = Form(None)):
    """Toggle the global Mirror flag.

    POST body may include `enabled=on|off` explicitly (e.g. from a
    plain HTML form). If omitted, we flip the current value.
    """
    if enabled is None:
        new_value = not mirror_toggle.get_global_enabled()
    else:
        new_value = enabled.lower() in ("on", "true", "1", "yes", "enabled")
    mirror_toggle.set_global_enabled(new_value)

    # HTMX-aware response: if the request came from HTMX, return a
    # small fragment so the toggle pill swaps inline. Otherwise
    # redirect back to /.
    if request.headers.get("HX-Request"):
        return HTMLResponse(_render_toggle_pill(new_value))
    return RedirectResponse(url="/", status_code=303)


def _render_toggle_pill(enabled: bool) -> str:
    """Inline HTML for the header toggle pill (HTMX swap target)."""
    if enabled:
        return (
            '<button hx-post="/toggle-mirror" hx-target="#mirror-toggle" '
            'hx-swap="outerHTML" id="mirror-toggle" '
            'title="Toggles Mirror for new calls. Active calls are unaffected." '
            'class="inline-flex items-center gap-2 rounded-full px-3 py-1.5 '
            'text-sm font-medium bg-emerald-400/10 text-emerald-300 '
            'border border-emerald-400/30 hover:bg-emerald-400/20 transition">'
            '<span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>'
            'Mirror: ON</button>'
        )
    return (
        '<button hx-post="/toggle-mirror" hx-target="#mirror-toggle" '
        'hx-swap="outerHTML" id="mirror-toggle" '
        'title="Toggles Mirror for new calls. Active calls are unaffected." '
        'class="inline-flex items-center gap-2 rounded-full px-3 py-1.5 '
        'text-sm font-medium bg-zinc-700/40 text-zinc-300 '
        'border border-zinc-600 hover:bg-zinc-700/60 transition">'
        '<span class="w-2 h-2 rounded-full bg-zinc-400"></span>'
        'Mirror: OFF</button>'
    )


# ---------- SSE endpoints --------------------------------------------------


@router.get("/sse/events")
async def sse_events(request: Request):
    sse.ensure_poller_started()
    queue = await sse.subscribe()

    async def stream():
        try:
            # Initial hello so EventSource onopen fires immediately.
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield sse.format_sse(event)
                except asyncio.TimeoutError:
                    # Heartbeat
                    yield ": keepalive\n\n"
        finally:
            await sse.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/sse/calls/{call_uuid}")
async def sse_one_call(request: Request, call_uuid: str):
    sse.ensure_poller_started()
    queue = await sse.subscribe()

    async def stream():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    if event.get("call_uuid") == call_uuid:
                        yield sse.format_sse(event)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            await sse.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ───────────────── Admin — start-fresh wipe ─────────────────────────────
# Drops every row from calls/turns/orders/mirror_events/interventions/
# failure_reports. Schema is preserved. Refuses if a call is in-flight so
# a live demo doesn't get truncated mid-conversation.


@router.post("/admin/wipe-data")
async def admin_wipe_data(request: Request):
    with db.get_conn() as conn:
        in_flight = conn.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE status = 'in_progress'"
        ).fetchone()
    if in_flight and int(in_flight["n"]) > 0:
        msg = "Refusing to wipe — a call is in progress. Hang up first."
        if request.headers.get("HX-Request"):
            return HTMLResponse(
                f'<span class="text-xs text-red-300">{msg}</span>',
                status_code=409,
            )
        raise HTTPException(status_code=409, detail=msg)

    counts = db.wipe_all_data()
    sse.reset_state()
    log.info("DB wiped via /admin/wipe-data: %s", counts)

    if request.headers.get("HX-Request"):
        total = sum(counts.values())
        return HTMLResponse(
            '<button hx-post="/admin/wipe-data" hx-target="#wipe-btn" '
            'hx-swap="outerHTML" hx-confirm="Delete ALL calls, turns, orders, '
            'mirror events, interventions, and failure reports? This cannot be undone." '
            'id="wipe-btn" '
            'title="Delete every row from every table. Schema is preserved." '
            'class="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs '
            'font-medium bg-red-500/10 text-red-300 border border-red-500/30 '
            'hover:bg-red-500/20 transition">'
            f'<span class="mono">✓ wiped {total}</span>'
            "</button>"
        )
    return RedirectResponse(url="/", status_code=303)


# ───────────────── Polish B — value-saved endpoints (append-only) ────────
# Translates Mirror's interventions into estimated dollar value preserved
# (churn avoided + support tickets avoided + reputation damage avoided).
# All math lives in mirror/value_model.py — these routes are thin wrappers.

from mirror import value_model as _value_model


@router.get("/api/value-saved/today")
async def api_value_saved_today():
    return JSONResponse(_value_model.calculate_total_value_saved_today())


@router.get("/api/value-saved/timeseries")
async def api_value_saved_timeseries():
    return JSONResponse(_value_model.calculate_timeseries_today())


@router.get("/api/value-saved/call/{call_uuid}")
async def api_value_saved_call(call_uuid: str):
    return JSONResponse(_value_model.calculate_value_saved(call_uuid))


@router.get("/api/value-saved/compare")
async def api_value_saved_compare(call_a: str = "", call_b: str = ""):
    return JSONResponse(
        _value_model.calculate_value_saved_for_compare(call_a or None, call_b or None)
    )
