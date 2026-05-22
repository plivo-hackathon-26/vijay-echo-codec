"""Phase H — /fixes dashboard.

A separate APIRouter so we don't touch dashboard/routes.py. Renders
the failure-reports list, exposes JSON endpoints for HTMX polling,
and handles dismiss / (stubbed) approve actions.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import db
from dashboard import agent_router, mirror_toggle

log = logging.getLogger("mirror.dashboard.fixes")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TEMPLATES_DIR = os.path.join(_THIS_DIR, "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter()


# ───────────────── helpers ──────────────────────────────────────────────


def _decorate(report: dict) -> dict:
    """Add UI-friendly fields to a raw DB row."""
    report = dict(report)
    report["short_call_uuid"] = (report.get("call_uuid") or "")[:8]
    report["created_relative"] = _relative_time(report.get("created_at"))
    return report


def _relative_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        seconds = int(delta.total_seconds())
    except (ValueError, TypeError):
        return ""
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _counts() -> dict:
    return {
        "pending": db.count_failure_reports("pending"),
        "applied": db.count_failure_reports("applied"),
        "dismissed": db.count_failure_reports("dismissed"),
        "all": db.count_failure_reports(None),
    }


# ───────────────── HTML page ────────────────────────────────────────────


@router.get("/fixes", response_class=HTMLResponse)
async def fixes_page(request: Request, status: str = "pending"):
    if status not in ("pending", "applied", "dismissed", "all"):
        status = "pending"
    reports = [
        _decorate(r) for r in db.list_failure_reports(status=status, limit=50)
    ]
    return templates.TemplateResponse(
        request=request,
        name="fixes.html",
        context={
            "reports": reports,
            "active_status": status,
            "counts": _counts(),
            "mirror_enabled": mirror_toggle.get_global_enabled(),
            "current_agent": mirror_toggle.get_current_agent(),
            "agents": agent_router.known_agents(),
        },
    )


# ───────────────── JSON endpoints ───────────────────────────────────────


@router.get("/fixes.json")
async def fixes_json(status: str = "pending", limit: int = 50):
    if status not in ("pending", "applied", "dismissed", "all"):
        status = "pending"
    reports = [
        _decorate(r) for r in db.list_failure_reports(status=status, limit=limit)
    ]
    return JSONResponse({"reports": reports, "counts": _counts()})


@router.get("/fixes/pending-count.json")
async def fixes_pending_count():
    return JSONResponse({"count": db.count_failure_reports("pending")})


@router.get("/fixes/{report_id}.json")
async def fixes_one_json(report_id: int):
    row = db.get_failure_report_by_id(report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    return JSONResponse(_decorate(row))


# ───────────────── Actions ──────────────────────────────────────────────


@router.post("/fixes/{report_id}/dismiss")
async def fixes_dismiss(
    request: Request,
    report_id: int,
    dismissed_by: Optional[str] = Form(None),
):
    row = db.get_failure_report_by_id(report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")

    if row.get("status") == "dismissed":
        # Already dismissed — return idempotent success.
        if request.headers.get("HX-Request"):
            return HTMLResponse("", status_code=200)
        return JSONResponse({"status": "dismissed", "report_id": report_id})

    db.update_failure_report_status(
        report_id,
        "dismissed",
        dismissed_by=(dismissed_by or "user"),
        dismissed_at=datetime.now(timezone.utc).isoformat(),
    )
    log.info("fixes: report id=%d dismissed by=%s", report_id, dismissed_by or "user")

    # HTMX swap target receives empty body → card disappears.
    if request.headers.get("HX-Request"):
        return HTMLResponse("", status_code=200)
    return JSONResponse({"status": "dismissed", "report_id": report_id})


@router.post("/fixes/backfill")
async def fixes_backfill(request: Request, limit: int = 50):
    """Run the failure-report generator for past calls that had
    intervention-grade events but never got a report (e.g. calls from
    before Phase H, or calls where the LLM hiccupped). Useful for
    populating the demo without making fresh phone calls.
    """
    from mirror.backfill import run_backfill

    summary = await run_backfill(limit=limit)
    log.info("backfill via dashboard: %s", summary)
    if request.headers.get("HX-Request"):
        html = (
            '<div class="px-3 py-2 text-sm text-emerald-300 bg-emerald-400/10 '
            'border border-emerald-400/30 rounded-md mb-4">'
            f"Backfilled {summary['created']} report"
            f"{'s' if summary['created'] != 1 else ''} from "
            f"{summary['candidates']} past calls "
            f"({summary['skipped']} skipped, {summary['failed']} failed)."
            "</div>"
        )
        return HTMLResponse(html)
    return JSONResponse(summary)


@router.post("/fixes/{report_id}/approve")
async def fixes_approve(request: Request, report_id: int):
    """Step 4/5 is the actual apply pipeline. For now this is a stub
    so the button is wired but doesn't pretend to do something it
    can't."""
    row = db.get_failure_report_by_id(report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    msg = "Approval flow coming in the next build — code/prompt apply is queued for Step 4."
    if request.headers.get("HX-Request"):
        return HTMLResponse(
            f'<div class="px-3 py-2 text-sm text-amber-300 bg-amber-400/10 '
            f'border border-amber-400/30 rounded-md">{msg}</div>',
            status_code=501,
        )
    return JSONResponse(
        {"status": "not_implemented", "report_id": report_id, "message": msg},
        status_code=501,
    )
