"""In-memory ReportSink — for tests and ephemeral demos."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

from plivo_mirror.reports.schema import FailureReport, ReportStatus


class InMemoryReportSink:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_id = 1
        self._rows: dict[int, FailureReport] = {}

    async def create(self, report: FailureReport) -> int:
        async with self._lock:
            rid = self._next_id
            self._next_id += 1
            stored = replace(report, id=rid)
            self._rows[rid] = stored
            return rid

    async def get(self, report_id: int) -> FailureReport | None:
        async with self._lock:
            return self._rows.get(report_id)

    async def list(
        self,
        *,
        status: ReportStatus | None = None,
        limit: int = 50,
    ) -> list[FailureReport]:
        async with self._lock:
            items = list(self._rows.values())
        if status is not None:
            items = [r for r in items if r.status == status]
        items.sort(key=lambda r: r.id or 0, reverse=True)
        return items[:limit]

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
        async with self._lock:
            row = self._rows.get(report_id)
            if row is None:
                return
            row.status = status
            if applied_pr_url is not None:
                row.applied_pr_url = applied_pr_url
            if applied_at is not None:
                row.applied_at = applied_at
            if dismissed_by is not None:
                row.dismissed_by = dismissed_by
            if dismissed_at is not None:
                row.dismissed_at = dismissed_at
            if last_error is not None:
                row.last_error = last_error


__all__ = ["InMemoryReportSink"]
