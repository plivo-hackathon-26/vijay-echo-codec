"""Post-call failure reporting.

When Mirror catches a policy violation, the call ends with a structured
``FailureReport`` describing what went wrong, why, and a proposed code
change to prevent it next time. Reports are stored in a ``ReportSink``
so a developer can review them (CLI: ``plivo-mirror-fix list``) and
approve a fix (CLI: ``plivo-mirror-fix apply <id>``).
"""

from plivo_mirror.reports.generator import ReportGenerator
from plivo_mirror.reports.schema import FailureReport, ReportStatus
from plivo_mirror.reports.sinks.base import ReportSink
from plivo_mirror.reports.sinks.memory import InMemoryReportSink
from plivo_mirror.reports.sinks.sqlite import SQLiteReportSink

__all__ = [
    "FailureReport",
    "ReportStatus",
    "ReportSink",
    "InMemoryReportSink",
    "SQLiteReportSink",
    "ReportGenerator",
]
