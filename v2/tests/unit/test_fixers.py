"""GitHubFixer tests — mocked git + gh subprocess."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from plivo_mirror.fixers.base import FixerError
from plivo_mirror.fixers.github import (
    GitHubFixer,
    _slugify,
    _strip_code_fences,
)
from plivo_mirror.reports.schema import FailureReport
from tests.unit.conftest import FakeLLM


# ── pure helpers ────────────────────────────────────────────────────────


def test_slugify_kebab_case() -> None:
    assert _slugify("Retracted Item!") == "retracted-item"
    assert _slugify("Policy 3: violated") == "policy-3-violated"
    assert _slugify("") == "fix"


def test_strip_code_fences() -> None:
    assert _strip_code_fences("plain text") == "plain text"
    assert _strip_code_fences("```python\nhello\n```") == "hello"
    assert _strip_code_fences("```\nhello\nworld\n```") == "hello\nworld"
    assert _strip_code_fences("no fence here") == "no fence here"


# ── constructor guards ──────────────────────────────────────────────────


def test_fixer_rejects_non_repo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(FixerError, match="not a git repo"):
            GitHubFixer(
                repo_path=tmp,
                allowed_files=["agent.py"],
                llm=FakeLLM(),
            )


def test_fixer_rejects_missing_path() -> None:
    with pytest.raises(FixerError, match="does not exist"):
        GitHubFixer(
            repo_path="/nonexistent/path/xyz",
            allowed_files=["agent.py"],
            llm=FakeLLM(),
        )


# ── allowlist + path safety ─────────────────────────────────────────────


@pytest.fixture
def fake_repo():
    """Build a temp directory that looks like a git repo."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        (path / ".git").mkdir()
        (path / "agent.py").write_text("# original\n")
        yield path


@pytest.mark.asyncio
async def test_apply_rejects_unallowed_file(fake_repo) -> None:
    fixer = GitHubFixer(
        repo_path=fake_repo,
        allowed_files=["agent.py"],
        llm=FakeLLM(),
    )
    report = FailureReport(
        id=1,
        proposed_file="secrets.env",         # NOT in allowlist
        summary="x",
    )
    with pytest.raises(FixerError, match="not in the allowlist"):
        await fixer.apply(report)


@pytest.mark.asyncio
async def test_apply_rejects_path_traversal(fake_repo) -> None:
    fixer = GitHubFixer(
        repo_path=fake_repo,
        allowed_files=["../etc/passwd"],     # allowlist tries to escape
        llm=FakeLLM(),
    )
    report = FailureReport(id=1, proposed_file="../etc/passwd")
    with pytest.raises(FixerError, match="escapes the repo root"):
        await fixer.apply(report)


@pytest.mark.asyncio
async def test_apply_rejects_missing_proposed_file(fake_repo) -> None:
    fixer = GitHubFixer(
        repo_path=fake_repo,
        allowed_files=["agent.py"],
        llm=FakeLLM(),
    )
    with pytest.raises(FixerError, match="no proposed_file"):
        await fixer.apply(FailureReport(id=1, proposed_file=""))


@pytest.mark.asyncio
async def test_apply_rejects_unpersisted_report(fake_repo) -> None:
    fixer = GitHubFixer(
        repo_path=fake_repo,
        allowed_files=["agent.py"],
        llm=FakeLLM(),
    )
    # id=None means the report was never saved to a sink.
    with pytest.raises(FixerError, match="no id"):
        await fixer.apply(FailureReport(proposed_file="agent.py"))
