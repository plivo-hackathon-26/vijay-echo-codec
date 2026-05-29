"""Fixer protocol + ApplyResult."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from plivo_mirror.reports.schema import FailureReport


class FixerError(Exception):
    """Raised when a fix can't be applied. Message is user-safe."""


@dataclass
class ApplyResult:
    pr_url: str
    branch: str
    file_path: str
    diff_summary: str = ""


@runtime_checkable
class Fixer(Protocol):
    async def apply(self, report: FailureReport) -> ApplyResult:
        """Open a PR with the fix from ``report`` applied. Raises
        ``FixerError`` on any failure (allowlist refusal, syntax error
        in LLM output, git push failure, etc.). Implementations must be
        all-or-nothing — partial state should be rolled back."""
        ...


__all__ = ["Fixer", "FixerError", "ApplyResult"]
