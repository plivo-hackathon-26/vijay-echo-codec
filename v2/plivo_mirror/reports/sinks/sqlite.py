"""SQLite ReportSink — durable single-file storage.

Default location: ``./plivo_mirror_reports.db`` (overridable). Uses
asyncio.to_thread to keep the sync sqlite3 client off the event loop.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from plivo_mirror.reports.schema import FailureReport, ReportStatus


_SCHEMA = """
CREATE TABLE IF NOT EXISTS failure_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    call_uuid       TEXT,
    tenant_id       TEXT,
    pattern_name    TEXT,
    severity        TEXT,
    summary         TEXT,
    root_cause      TEXT,
    proposed_fix_text TEXT,
    proposed_file   TEXT,
    suggested_diff  TEXT,
    confidence      REAL,
    status          TEXT,
    applied_pr_url  TEXT,
    applied_at      TEXT,
    dismissed_by    TEXT,
    dismissed_at    TEXT,
    last_error      TEXT,
    created_at      TEXT,
    extras_json     TEXT
);
CREATE INDEX IF NOT EXISTS ix_failure_reports_status ON failure_reports(status);
CREATE INDEX IF NOT EXISTS ix_failure_reports_call ON failure_reports(call_uuid);
"""


class SQLiteReportSink:
    def __init__(self, db_path: str | Path = "./plivo_mirror_reports.db") -> None:
        self._db_path = str(Path(db_path).resolve())
        self._init_schema()

    # ───────────────────────── public API ────────────────────────────────

    async def create(self, report: FailureReport) -> int:
        return await asyncio.to_thread(self._create_blocking, report)

    async def get(self, report_id: int) -> FailureReport | None:
        return await asyncio.to_thread(self._get_blocking, report_id)

    async def list(
        self,
        *,
        status: ReportStatus | None = None,
        limit: int = 50,
    ) -> list[FailureReport]:
        return await asyncio.to_thread(self._list_blocking, status, limit)

    async def update_status(
        self,
        report_id: int,
        status: ReportStatus,
        *,
        applied_pr_url: str | None = None,
        applied_at: str | None = None,
        dismissed_by: str | None = None,
        dismissed_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._update_status_blocking,
            report_id,
            status,
            applied_pr_url,
            applied_at,
            dismissed_by,
            dismissed_at,
            last_error,
        )

    # ───────────────────────── internals ─────────────────────────────────

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _row_to_report(self, row: sqlite3.Row) -> FailureReport:
        extras = {}
        if row["extras_json"]:
            try:
                extras = json.loads(row["extras_json"]) or {}
            except (ValueError, TypeError):
                extras = {}
        return FailureReport(
            id=row["id"],
            call_uuid=row["call_uuid"] or "",
            tenant_id=row["tenant_id"],
            pattern_name=row["pattern_name"] or "",
            severity=row["severity"] or "medium",
            summary=row["summary"] or "",
            root_cause=row["root_cause"] or "",
            proposed_fix_text=row["proposed_fix_text"] or "",
            proposed_file=row["proposed_file"] or "",
            suggested_diff=row["suggested_diff"] or "",
            confidence=float(row["confidence"] or 0.5),
            status=ReportStatus(row["status"] or "pending"),
            applied_pr_url=row["applied_pr_url"],
            applied_at=row["applied_at"],
            dismissed_by=row["dismissed_by"],
            dismissed_at=row["dismissed_at"],
            last_error=row["last_error"],
            created_at=row["created_at"] or "",
            extras=extras,
        )

    def _create_blocking(self, report: FailureReport) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO failure_reports
                   (call_uuid, tenant_id, pattern_name, severity, summary,
                    root_cause, proposed_fix_text, proposed_file, suggested_diff,
                    confidence, status, applied_pr_url, applied_at, dismissed_by,
                    dismissed_at, last_error, created_at, extras_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    report.call_uuid,
                    report.tenant_id,
                    report.pattern_name,
                    report.severity,
                    report.summary,
                    report.root_cause,
                    report.proposed_fix_text,
                    report.proposed_file,
                    report.suggested_diff,
                    report.confidence,
                    (report.status.value if isinstance(report.status, ReportStatus) else str(report.status)),
                    report.applied_pr_url,
                    report.applied_at,
                    report.dismissed_by,
                    report.dismissed_at,
                    report.last_error,
                    report.created_at,
                    json.dumps(report.extras or {}, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid or 0)

    def _get_blocking(self, report_id: int) -> FailureReport | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM failure_reports WHERE id = ?", (report_id,)
            ).fetchone()
        return self._row_to_report(row) if row else None

    def _list_blocking(
        self, status: ReportStatus | None, limit: int
    ) -> list[FailureReport]:
        with self._conn() as c:
            if status is not None:
                rows = c.execute(
                    "SELECT * FROM failure_reports WHERE status = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (status.value, int(limit)),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM failure_reports ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [self._row_to_report(r) for r in rows]

    def _update_status_blocking(
        self,
        report_id: int,
        status: ReportStatus,
        applied_pr_url: str | None,
        applied_at: str | None,
        dismissed_by: str | None,
        dismissed_at: str | None,
        last_error: str | None,
    ) -> None:
        sets = ["status = ?"]
        vals: list = [status.value if isinstance(status, ReportStatus) else str(status)]
        if applied_pr_url is not None:
            sets.append("applied_pr_url = ?")
            vals.append(applied_pr_url)
        if applied_at is not None:
            sets.append("applied_at = ?")
            vals.append(applied_at)
        if dismissed_by is not None:
            sets.append("dismissed_by = ?")
            vals.append(dismissed_by)
        if dismissed_at is not None:
            sets.append("dismissed_at = ?")
            vals.append(dismissed_at)
        if last_error is not None:
            sets.append("last_error = ?")
            vals.append(last_error)
        vals.append(report_id)
        sql = f"UPDATE failure_reports SET {', '.join(sets)} WHERE id = ?"
        with self._conn() as c:
            c.execute(sql, vals)


__all__ = ["SQLiteReportSink"]
