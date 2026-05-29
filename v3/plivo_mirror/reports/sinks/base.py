"""ReportSink protocol — pluggable storage for FailureReports.

Default: ``InMemoryReportSink`` (lost on restart). Recommended for
production: ``SQLiteReportSink`` (durable, single-file). Customers can
plug their own DB by implementing the protocol below.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from plivo_mirror.reports.schema import FailureReport, ReportStatus


@runtime_checkable
class ReportSink(Protocol):
    async def create(self, report: FailureReport) -> int:
        """Persist a new report. Returns the generated id."""
        ...

    async def get(self, report_id: int) -> FailureReport | None: ...

    async def list(
        self,
        *,
        status: ReportStatus | None = None,
        limit: int = 50,
    ) -> list[FailureReport]: ...

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
    ) -> None: ...


__all__ = ["ReportSink"]
