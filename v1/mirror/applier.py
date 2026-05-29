"""Apply a failure_report as a real git branch + PR.

Flow (all-or-nothing — on any failure we roll back to clean main):

  1.  Look up the report. Refuse if it's not pending, or if it's already
      applied (return the existing PR url instead).
  2.  ALLOWLIST check. Mirror is only permitted to touch:
        - prompts.py
        - agent/primary.py
        - agents/travel/primary.py
        - agents/travel/prompts.py
      Anything else is rejected with a loud, user-safe error.
  3.  Resolve the path and verify it lives inside the repo (no path
      traversal — refuse if abs_path escapes REPO_ROOT).
  4.  Git pre-flight:
        - working tree must be clean (porcelain status empty)
        - current branch must be `main`
  5.  LLM rewrite. Send the LLM the CURRENT file content + Mirror's
      diagnosis. Ask for the complete new file. Strip any code fences
      the LLM produced anyway.
  6.  Validate:
        - new content must be non-empty
        - new content must differ from current (else "no-op fix")
        - if .py, run ast.parse() to catch any syntax errors before
          we commit. NEVER push a syntactically-broken file.
  7.  Git branch + commit + push:
        git checkout -b mirror/fix-{id}-{slug}
        write file
        git add <path>
        git commit -m "..."
        git push origin <branch> -u
  8.  gh pr create. Capture the PR url from stdout.
  9.  Update failure_reports SET status='applied', applied_pr_url=...,
      applied_at=NOW.
  10. Cleanup: always end on main, regardless of success or failure.

Concurrency: this function locks the entire git repo state during its
run (we mutate working tree + branches). Don't call concurrently. The
HTTP endpoint serializes calls implicitly because each apply takes
30-60s and the user clicks one button at a time.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from openai import AsyncOpenAI

import db
from prompts import APPLY_FIX_PROMPT

log = logging.getLogger("mirror.applier")

# Resolve to the repo root: parent of the `mirror/` directory.
REPO_ROOT = Path(__file__).resolve().parent.parent

# The ONLY files Mirror is permitted to rewrite. Anything else gets a
# hard "not in allowlist" error. This is the safety net that lets us
# trust the LLM not to wreck the codebase — even if it picks
# `voice/stream.py` as proposed_file, the apply will refuse.
ALLOWED_FILES = {
    "prompts.py",
    "agent/primary.py",
    "agents/travel/primary.py",
    "agents/travel/prompts.py",
}


class ApplyError(Exception):
    """Raised when an apply step fails. Message is user-safe (no secrets)."""


# ─────────────────── subprocess helpers ─────────────────────────────────


def _run(cmd: list[str], *, check: bool = True, cwd: Path | None = None) -> str:
    """Run a subprocess in REPO_ROOT and return stdout (stripped).

    Raises ApplyError on non-zero exit when check=True.
    """
    where = cwd or REPO_ROOT
    log.info("$ %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=where, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise ApplyError(
            f"`{' '.join(cmd)}` failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
        )
    return (proc.stdout or "").strip()


def _git_clean() -> bool:
    return _run(["git", "status", "--porcelain"], check=False) == ""


def _current_branch() -> str:
    return _run(["git", "symbolic-ref", "--short", "HEAD"], check=False)


def _local_branch_exists(branch: str) -> bool:
    rc = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=REPO_ROOT,
        capture_output=True,
    ).returncode
    return rc == 0


def _slugify(s: str, max_len: int = 40) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len] or "fix"


# ─────────────────── LLM rewrite ────────────────────────────────────────


_client: AsyncOpenAI | None = None


def _openai() -> AsyncOpenAI:
    global _client
    if _client is None:
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise ApplyError("OPENAI_API_KEY is not set")
        base_url = os.getenv("OPENAI_API_URL", "").strip().rstrip("/") or None
        if base_url and not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url
        _client = AsyncOpenAI(api_key=key, base_url=base_url)
    return _client


def _strip_code_fences(content: str) -> str:
    """The prompt forbids markdown fences, but LLMs slip them in anyway.
    Strip leading and trailing ``` blocks if present."""
    content = content.strip()
    if not content.startswith("```"):
        return content
    lines = content.split("\n")
    # Drop the opening fence line (might be ``` or ```python)
    lines = lines[1:]
    # Drop the trailing fence if present
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


async def _rewrite_file(rel_path: str, report: dict) -> str:
    abs_path = REPO_ROOT / rel_path
    try:
        current = abs_path.read_text()
    except Exception as e:
        raise ApplyError(f"could not read {rel_path}: {e}")

    prompt = APPLY_FIX_PROMPT.format(
        path=rel_path,
        current_content=current,
        summary=report.get("summary") or "",
        root_cause=report.get("root_cause") or "",
        proposed_fix_text=report.get("proposed_fix_text") or "",
        suggested_diff=report.get("suggested_diff") or "",
    )

    client = _openai()
    try:
        # Match the rest of the codebase: no max_tokens, no temperature
        # (Azure gpt-5-mini rejects max_tokens and ignores temperature).
        resp = await client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            messages=[{"role": "system", "content": prompt}],
        )
    except Exception as e:
        raise ApplyError(f"LLM rewrite call failed: {e}")

    raw = (resp.choices[0].message.content or "").strip()
    return _strip_code_fences(raw)


def _validate_new_content(rel_path: str, new_content: str, current_content: str) -> None:
    if not new_content.strip():
        raise ApplyError("LLM returned empty content")
    if new_content == current_content:
        raise ApplyError(
            "LLM produced no change — either the fix is already applied "
            "or the model declined to edit"
        )
    if rel_path.endswith(".py"):
        try:
            ast.parse(new_content)
        except SyntaxError as e:
            raise ApplyError(
                f"new {rel_path} has a Python syntax error at line "
                f"{e.lineno}: {e.msg}"
            )


def _format_pr_body(report: dict, branch: str) -> str:
    return (
        "Mirror caught a failure during a customer call and proposes "
        "this fix.\n\n"
        f"**Agent:** `{report.get('agent_name') or 'pizza-plivo'}`\n"
        f"**Call:** `{(report.get('call_uuid') or '')[:8]}`\n"
        f"**Pattern:** `{report.get('pattern_name') or 'unknown'}`\n"
        f"**Severity:** {report.get('severity') or 'medium'}\n"
        f"**Confidence:** {float(report.get('confidence') or 0):.2f}\n"
        f"**Branch:** `{branch}`\n\n"
        "## Summary\n"
        f"{report.get('summary') or '_no summary_'}\n\n"
        "## Root cause\n"
        f"{report.get('root_cause') or '_no root cause_'}\n\n"
        "## Proposed fix\n"
        f"{report.get('proposed_fix_text') or '_no fix description_'}\n\n"
        "## Target file\n"
        f"`{report.get('proposed_file') or ''}`\n\n"
        "---\n"
        f"*Generated by Mirror — failure_report #{report.get('id')}. "
        "Reviewed and approved by a human before this PR was opened.*"
    )


# ─────────────────── main entry point ───────────────────────────────────


async def apply_failure_report(report_id: int) -> dict:
    """Apply a report — branch, commit, push, PR. Returns dict on success."""
    report = db.get_failure_report_by_id(report_id)
    if report is None:
        raise ApplyError("report not found")

    status = report.get("status")
    if status == "applied":
        return {
            "status": "already_applied",
            "pr_url": report.get("applied_pr_url"),
            "branch": None,
        }
    if status == "dismissed":
        raise ApplyError("this report has been dismissed; reopen first to apply")
    if status != "pending":
        raise ApplyError(f"unexpected report status: {status}")

    # ── ALLOWLIST ───────────────────────────────────────────────────
    proposed_file = (report.get("proposed_file") or "").strip()
    if not proposed_file:
        raise ApplyError("report has no proposed_file")
    if proposed_file not in ALLOWED_FILES:
        raise ApplyError(
            f"'{proposed_file}' is not in Mirror's allowlist. Mirror can "
            f"only modify: {', '.join(sorted(ALLOWED_FILES))}"
        )

    # ── PATH SAFETY ─────────────────────────────────────────────────
    abs_path = (REPO_ROOT / proposed_file).resolve()
    try:
        abs_path.relative_to(REPO_ROOT)
    except ValueError:
        raise ApplyError("proposed file path escapes the repository root")
    if not abs_path.exists():
        raise ApplyError(f"file does not exist on disk: {proposed_file}")

    # ── GIT PRE-FLIGHT ──────────────────────────────────────────────
    if not _git_clean():
        raise ApplyError(
            "git working tree is dirty — commit or stash uncommitted "
            "changes before applying a Mirror fix"
        )
    starting_branch = _current_branch()
    if starting_branch != "main":
        raise ApplyError(
            f"must be on `main`; currently on `{starting_branch}`"
        )

    # ── LLM REWRITE + VALIDATE ──────────────────────────────────────
    current_content = abs_path.read_text()
    new_content = await _rewrite_file(proposed_file, report)
    _validate_new_content(proposed_file, new_content, current_content)

    # ── BRANCH NAME ─────────────────────────────────────────────────
    slug = _slugify(
        f"{report.get('pattern_name', 'fix')}-{(report.get('summary') or '')[:60]}"
    )
    branch = f"mirror/fix-{report_id}-{slug}"
    if _local_branch_exists(branch):
        # Idempotency safety — avoid recreating a stale local branch.
        branch = f"{branch}-{int(datetime.now().timestamp())}"

    # ── APPLY (all-or-nothing) ──────────────────────────────────────
    branch_created = False
    file_written = False
    pushed = False
    try:
        _run(["git", "checkout", "-b", branch])
        branch_created = True
        abs_path.write_text(new_content)
        file_written = True
        _run(["git", "add", proposed_file])
        commit_msg = (
            f"Mirror fix: {(report.get('summary') or '')[:72]}\n\n"
            f"Source: failure_report #{report_id} "
            f"(call {(report.get('call_uuid') or '')[:8]})\n"
            f"Agent: {report.get('agent_name') or 'pizza-plivo'}\n"
            f"Pattern: {report.get('pattern_name') or ''}\n"
            f"Severity: {report.get('severity') or ''}\n"
            f"Confidence: {float(report.get('confidence') or 0):.2f}\n\n"
            f"{report.get('root_cause') or ''}\n\n"
            "Generated by Mirror — Plivo Hackathon 2026."
        )
        _run(["git", "commit", "-m", commit_msg])
        _run(["git", "push", "-u", "origin", branch])
        pushed = True

        # gh pr create — capture URL from stdout (last line)
        pr_title = f"[Mirror] {(report.get('summary') or 'fix')[:72]}"
        pr_body = _format_pr_body(report, branch)
        pr_output = _run([
            "gh", "pr", "create",
            "--title", pr_title,
            "--body", pr_body,
            "--head", branch,
            "--base", "main",
        ])
        pr_url = (pr_output.split("\n")[-1] or "").strip()
        if not pr_url.startswith("http"):
            raise ApplyError(
                f"gh pr create did not return a PR URL (got: {pr_output[:200]})"
            )

        # ── PERSIST ─────────────────────────────────────────────────
        db.update_failure_report_status(
            report_id,
            "applied",
            applied_pr_url=pr_url,
            applied_at=datetime.now(timezone.utc).isoformat(),
        )
        log.info(
            "applied report id=%d → %s (branch=%s)",
            report_id,
            pr_url,
            branch,
        )
        return {"status": "applied", "pr_url": pr_url, "branch": branch}

    except Exception as e:
        log.exception("apply failed for report %d; rolling back", report_id)
        try:
            # If we modified the file but didn't push, reset everything.
            if file_written and not pushed:
                _run(["git", "reset", "--hard", "HEAD"], check=False)
            _run(["git", "checkout", starting_branch], check=False)
            if branch_created and _local_branch_exists(branch) and not pushed:
                _run(["git", "branch", "-D", branch], check=False)
        except Exception:
            log.exception("rollback failed")
        if isinstance(e, ApplyError):
            raise
        raise ApplyError(f"apply failed: {e}")

    finally:
        # Always end on the starting branch.
        try:
            if _current_branch() != starting_branch:
                _run(["git", "checkout", starting_branch], check=False)
        except Exception:
            pass
