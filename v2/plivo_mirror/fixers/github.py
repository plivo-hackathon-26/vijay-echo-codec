"""GitHubFixer — uses the `gh` CLI for PR creation.

Flow (all-or-nothing — on any failure we roll back to clean main):

  1.  Refuse if report status is not PENDING.
  2.  ALLOWLIST check — Mirror can only rewrite files explicitly listed
      in ``allowed_files``. Anything else is rejected with a loud, clear
      error.
  3.  PATH SAFETY — resolved path must live inside ``repo_path``.
  4.  GIT PRE-FLIGHT — working tree clean, current branch is the base
      branch (default ``main``).
  5.  LLM REWRITE — send the LLM the current file + the report's
      diagnosis. Ask for the complete new file. Strip any code fences
      it produces anyway.
  6.  VALIDATE — non-empty, different from current, ``ast.parse`` for
      ``.py`` files.
  7.  GIT BRANCH + COMMIT + PUSH:
        git checkout -b mirror/fix-{id}-{slug}
        write file
        git add <path>
        git commit -m "..."
        git push origin <branch> -u
  8.  ``gh pr create`` — capture PR URL.
  9.  Return ``ApplyResult``.
  10. On any failure: rollback (reset --hard, checkout original branch,
      delete orphan local branch).

Concurrency: locks the entire git working tree for its duration. Don't
call concurrently against the same repo.

Requirements on the customer's machine:
  - ``git`` and ``gh`` on PATH
  - ``gh auth status`` is logged in
  - The repo at ``repo_path`` has an ``origin`` remote configured
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plivo_mirror.fixers.base import ApplyResult, FixerError
from plivo_mirror.reports.schema import FailureReport

log = logging.getLogger("plivo_mirror.fixers.github")


_APPLY_PROMPT = """\
You are applying a fix to a source file in a Python codebase. Your
output will be written directly to disk and shipped as a pull request,
so it MUST be the COMPLETE, VALID, READY-TO-RUN new content of the
file — not a diff, not a snippet, not a description.

TARGET FILE: {path}

CURRENT FILE CONTENT (everything between the markers, exclusive):
<<<CURRENT_FILE_START>>>
{current_content}
<<<CURRENT_FILE_END>>>

MIRROR'S DIAGNOSIS:
  Pattern:      {pattern_name}
  Summary:      {summary}
  Root cause:   {root_cause}
  Proposed fix: {proposed_fix_text}

MIRROR'S SUGGESTED DIFF / SNIPPET (advisory only — apply the idea
correctly, don't paste it verbatim):
{suggested_diff}

RULES (NON-NEGOTIABLE):
1. Output ONLY the new file content. No prose before or after. No
   markdown code fences (no ``` anywhere). No "Here is the new file:".
2. The change should be MINIMAL — only modify what's required to
   address the diagnosis. Leave every unrelated line untouched.
3. Preserve imports, function and class names, docstrings, blank
   lines, and the file's overall structure.
4. For a .py file, the output MUST be syntactically valid Python.
5. If you cannot apply a sensible fix, output the file content
   UNCHANGED — better to no-op than ship broken code.

Begin output now (raw file content, nothing else):
"""


class GitHubFixer:
    """Open a real GitHub PR via ``gh`` CLI.

    Args:
        repo_path: local checkout of the customer's repo.
        allowed_files: list of paths Mirror is permitted to rewrite.
            Relative to ``repo_path``. Anything outside this list is
            rejected. Empty list = no files allowed (effectively
            disables the fixer).
        llm: an LLMClient used to rewrite files.
        base_branch: branch to create the fix branch from. Default
            "main".
        remote: git remote name. Default "origin".
    """

    def __init__(
        self,
        *,
        repo_path: str | Path,
        allowed_files: list[str],
        llm: Any,                      # LLMClient
        base_branch: str = "main",
        remote: str = "origin",
    ) -> None:
        self._repo = Path(repo_path).resolve()
        if not self._repo.exists():
            raise FixerError(f"repo_path does not exist: {self._repo}")
        if not (self._repo / ".git").exists():
            raise FixerError(f"not a git repo: {self._repo}")
        self._allowed = set(allowed_files or [])
        self._llm = llm
        self._base_branch = base_branch
        self._remote = remote

    # ─────────────────────────── public API ──────────────────────────────

    async def apply(
        self,
        report: FailureReport,
        *,
        new_content: str | None = None,
    ) -> ApplyResult:
        """Open a PR. If ``new_content`` is supplied (e.g. from a prior
        ``preview()`` call), skip the LLM rewrite step and commit
        exactly those bytes. Guarantees what the user saw in the
        preview is what gets committed."""
        return await asyncio.to_thread(self._apply_blocking, report, new_content)

    async def preview(self, report: FailureReport) -> dict[str, str]:
        """Generate the rewrite WITHOUT committing or pushing. Returns
        a dict with ``proposed_file``, ``current_content``,
        ``new_content``. Cheap to call from a UI that wants to show
        a diff before asking for human approval. Same allowlist + path
        + ast validation as apply()."""
        return await asyncio.to_thread(self._preview_blocking, report)

    # ─────────────────────────── internals ───────────────────────────────

    def _preview_blocking(self, report: FailureReport) -> dict[str, str]:
        if report.id is None:
            raise FixerError("report has no id; persist it via a ReportSink first")

        proposed = (report.proposed_file or "").strip()
        if not proposed:
            raise FixerError("report has no proposed_file")
        if proposed not in self._allowed:
            raise FixerError(
                f"'{proposed}' is not in the allowlist. Mirror can only "
                f"rewrite: {', '.join(sorted(self._allowed)) or '(none)'}"
            )
        abs_path = (self._repo / proposed).resolve()
        try:
            abs_path.relative_to(self._repo)
        except ValueError:
            raise FixerError("proposed_file path escapes the repo root")
        if not abs_path.exists():
            raise FixerError(f"file does not exist: {proposed}")

        current = abs_path.read_text()
        new_content = self._rewrite_via_llm(proposed, current, report)
        self._validate_rewrite(proposed, current, new_content)
        return {
            "proposed_file": proposed,
            "current_content": current,
            "new_content": new_content,
        }

    def _apply_blocking(
        self,
        report: FailureReport,
        precomputed_new: str | None = None,
    ) -> ApplyResult:
        if report.id is None:
            raise FixerError("report has no id; persist it via a ReportSink first")

        proposed = (report.proposed_file or "").strip()
        if not proposed:
            raise FixerError("report has no proposed_file")
        if proposed not in self._allowed:
            raise FixerError(
                f"'{proposed}' is not in the allowlist. Mirror can only "
                f"rewrite: {', '.join(sorted(self._allowed)) or '(none)'}"
            )

        abs_path = (self._repo / proposed).resolve()
        try:
            abs_path.relative_to(self._repo)
        except ValueError:
            raise FixerError("proposed_file path escapes the repo root")
        if not abs_path.exists():
            raise FixerError(f"file does not exist: {proposed}")

        # Pre-flight: clean tree + on base branch.
        if self._run_git(["status", "--porcelain"]):
            raise FixerError(
                "git working tree is dirty — commit or stash before applying"
            )
        starting_branch = self._run_git(["symbolic-ref", "--short", "HEAD"])
        if starting_branch != self._base_branch:
            raise FixerError(
                f"must be on `{self._base_branch}`; currently on `{starting_branch}`"
            )

        # LLM rewrite — unless a precomputed rewrite was passed in
        # (e.g. from a UI that previewed the diff first; we commit
        # the exact bytes the user saw).
        current = abs_path.read_text()
        if precomputed_new is not None:
            new_content = precomputed_new
        else:
            new_content = self._rewrite_via_llm(proposed, current, report)
        self._validate_rewrite(proposed, current, new_content)

        # Build branch.
        slug = _slugify(f"{report.pattern_name or 'fix'}-{(report.summary or '')[:60]}")
        branch = f"mirror/fix-{report.id}-{slug}"
        if self._local_branch_exists(branch):
            branch = f"{branch}-{int(datetime.now().timestamp())}"

        branch_created = False
        file_written = False
        pushed = False
        try:
            self._run_git(["checkout", "-b", branch])
            branch_created = True
            abs_path.write_text(new_content)
            file_written = True
            self._run_git(["add", proposed])
            commit_msg = self._commit_message(report)
            self._run_git(["commit", "-m", commit_msg])
            self._run_git(["push", "-u", self._remote, branch])
            pushed = True

            pr_url = self._gh_pr_create(report, branch)
            log.info(
                "applied report id=%s → %s (branch=%s)", report.id, pr_url, branch
            )

            return ApplyResult(
                pr_url=pr_url,
                branch=branch,
                file_path=proposed,
            )
        except Exception as e:
            log.exception("apply failed (report=%s); rolling back", report.id)
            try:
                if file_written and not pushed:
                    self._run_git(["reset", "--hard", "HEAD"], check=False)
                self._run_git(["checkout", starting_branch], check=False)
                if branch_created and not pushed and self._local_branch_exists(branch):
                    self._run_git(["branch", "-D", branch], check=False)
            except Exception:
                log.exception("rollback failed")
            if isinstance(e, FixerError):
                raise
            raise FixerError(f"apply failed: {e}")
        finally:
            try:
                cur = self._run_git(["symbolic-ref", "--short", "HEAD"], check=False)
                if cur and cur != starting_branch:
                    self._run_git(["checkout", starting_branch], check=False)
            except Exception:
                pass

    # ─── git helpers ────────────────────────────────────────────────────

    def _run_git(self, args: list[str], *, check: bool = True) -> str:
        return _run(["git"] + args, cwd=self._repo, check=check)

    def _local_branch_exists(self, branch: str) -> bool:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=self._repo,
            capture_output=True,
        )
        return proc.returncode == 0

    # ─── LLM rewrite ────────────────────────────────────────────────────

    def _rewrite_via_llm(
        self, rel_path: str, current_content: str, report: FailureReport
    ) -> str:
        prompt = _APPLY_PROMPT.format(
            path=rel_path,
            current_content=current_content,
            pattern_name=report.pattern_name or "",
            summary=report.summary or "",
            root_cause=report.root_cause or "",
            proposed_fix_text=report.proposed_fix_text or "",
            suggested_diff=report.suggested_diff or "",
        )

        async def _call():
            return await self._llm.chat(prompt)

        try:
            text = asyncio.run(_call())
        except RuntimeError:
            # Already inside a running loop (rare here since we're called
            # from to_thread → fresh thread → no loop). Fall back to a
            # nested asyncio.run via a new loop.
            loop = asyncio.new_event_loop()
            try:
                text = loop.run_until_complete(_call())
            finally:
                loop.close()
        except Exception as e:
            raise FixerError(f"LLM rewrite call failed: {e}")

        return _strip_code_fences((text or "").strip())

    def _validate_rewrite(
        self, rel_path: str, current_content: str, new_content: str
    ) -> None:
        if not new_content.strip():
            raise FixerError("LLM returned empty content")
        if new_content == current_content:
            raise FixerError(
                "LLM produced no change — fix already applied or model declined"
            )
        if rel_path.endswith(".py"):
            try:
                ast.parse(new_content)
            except SyntaxError as e:
                raise FixerError(
                    f"new {rel_path} has a Python syntax error at line "
                    f"{e.lineno}: {e.msg}"
                )

    # ─── commit + PR body ───────────────────────────────────────────────

    def _commit_message(self, report: FailureReport) -> str:
        return (
            f"Mirror fix: {(report.summary or '')[:72]}\n\n"
            f"Source: failure_report #{report.id} "
            f"(call {(report.call_uuid or '')[:8]})\n"
            f"Pattern: {report.pattern_name or ''}\n"
            f"Severity: {report.severity or ''}\n"
            f"Confidence: {report.confidence:.2f}\n\n"
            f"{report.root_cause or ''}\n\n"
            "Generated by plivo-mirror."
        )

    def _gh_pr_body(self, report: FailureReport, branch: str) -> str:
        return (
            "plivo-mirror caught a policy violation during a customer "
            "call and proposes this fix.\n\n"
            f"**Call:** `{(report.call_uuid or '')[:8]}`\n"
            f"**Pattern:** `{report.pattern_name or 'unknown'}`\n"
            f"**Severity:** {report.severity or 'medium'}\n"
            f"**Confidence:** {report.confidence:.2f}\n"
            f"**Branch:** `{branch}`\n\n"
            "## Summary\n"
            f"{report.summary or '_no summary_'}\n\n"
            "## Root cause\n"
            f"{report.root_cause or '_no root cause_'}\n\n"
            "## Proposed fix\n"
            f"{report.proposed_fix_text or '_no fix description_'}\n\n"
            "## Target file\n"
            f"`{report.proposed_file or ''}`\n\n"
            "---\n"
            f"*Generated by plivo-mirror — failure_report #{report.id}. "
            "Reviewed and approved by a human before this PR was opened.*"
        )

    def _gh_pr_create(self, report: FailureReport, branch: str) -> str:
        title = f"[Mirror] {(report.summary or 'fix')[:72]}"
        body = self._gh_pr_body(report, branch)
        out = _run(
            [
                "gh", "pr", "create",
                "--title", title,
                "--body", body,
                "--head", branch,
                "--base", self._base_branch,
            ],
            cwd=self._repo,
        )
        url = (out.split("\n")[-1] or "").strip()
        if not url.startswith("http"):
            raise FixerError(
                f"gh pr create did not return a PR URL (got: {out[:200]})"
            )
        return url


# ─────────────────────────── module helpers ──────────────────────────────


def _run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> str:
    log.info("$ %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise FixerError(
            f"`{' '.join(cmd)}` failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
        )
    return (proc.stdout or "").strip()


def _slugify(s: str, max_len: int = 40) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len] or "fix"


def _strip_code_fences(content: str) -> str:
    """LLMs sometimes wrap output in ``` despite being told not to."""
    content = content.strip()
    if not content.startswith("```"):
        return content
    lines = content.split("\n")
    lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


__all__ = ["GitHubFixer"]
