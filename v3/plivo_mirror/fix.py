"""plivo-mirror-fix CLI — human-in-the-loop fix approval.

Three subcommands:

    plivo-mirror-fix list                       # show all pending reports
    plivo-mirror-fix show <id>                  # full report + suggested diff
    plivo-mirror-fix apply <id> --repo-path ./my-agent \
                                --allow agent.py,prompts.py

Reports come from a sink — default ``./plivo_mirror_reports.db``
(SQLite); override via ``--db <path>`` or env ``PLIVO_MIRROR_REPORTS_DB``.

The fixer uses the ``gh`` CLI to open the PR — make sure
``gh auth status`` is logged in.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv

from plivo_mirror.reports.schema import FailureReport, ReportStatus
from plivo_mirror.reports.sinks.sqlite import SQLiteReportSink


# ─── shared helpers ──────────────────────────────────────────────────────


def _sink_from_args(args: argparse.Namespace) -> SQLiteReportSink:
    db = (
        args.db
        or os.getenv("PLIVO_MIRROR_REPORTS_DB")
        or "./plivo_mirror_reports.db"
    )
    return SQLiteReportSink(db_path=db)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _print_row(r: FailureReport) -> None:
    sev_pad = (r.severity or "?").upper().ljust(8)
    pat = (r.pattern_name or "—")[:24].ljust(24)
    summ = (r.summary or "")[:60]
    status = (r.status.value if isinstance(r.status, ReportStatus) else str(r.status)).upper().ljust(10)
    print(f"  #{r.id:<4} {status} {sev_pad} {pat} {summ}")


# ─── list ────────────────────────────────────────────────────────────────


async def cmd_list(args: argparse.Namespace) -> int:
    sink = _sink_from_args(args)
    status = None
    if args.status and args.status != "all":
        status = ReportStatus(args.status)
    rows = await sink.list(status=status, limit=args.limit)
    if not rows:
        print(f"No reports{' with status ' + args.status if args.status else ''}.")
        return 0
    print(f"  {'ID':<5} {'STATUS':<10} {'SEVERITY':<8} {'PATTERN':<24} SUMMARY")
    print(f"  {'─' * 5} {'─' * 10} {'─' * 8} {'─' * 24} {'─' * 60}")
    for r in rows:
        _print_row(r)
    print()
    return 0


# ─── show ────────────────────────────────────────────────────────────────


async def cmd_show(args: argparse.Namespace) -> int:
    sink = _sink_from_args(args)
    r = await sink.get(args.report_id)
    if r is None:
        print(f"report #{args.report_id} not found", file=sys.stderr)
        return 1

    print()
    print(f"  ═════ failure_report #{r.id} ═════")
    print(f"  status:        {r.status.value if isinstance(r.status, ReportStatus) else r.status}")
    print(f"  severity:      {r.severity}")
    print(f"  pattern:       {r.pattern_name}")
    print(f"  confidence:    {r.confidence:.2f}")
    print(f"  call_uuid:     {r.call_uuid}")
    print(f"  created_at:    {r.created_at}")
    if r.applied_pr_url:
        print(f"  applied_pr:    {r.applied_pr_url}")
    print()
    print(f"  ─── summary ───")
    print(f"  {r.summary}")
    print()
    print(f"  ─── root cause ───")
    print(f"  {r.root_cause}")
    print()
    print(f"  ─── proposed fix ───")
    print(f"  {r.proposed_fix_text}")
    print()
    print(f"  ─── target file ───")
    print(f"  {r.proposed_file}")
    print()
    if r.suggested_diff:
        print(f"  ─── suggested diff / snippet ───")
        for line in r.suggested_diff.splitlines():
            print(f"  {line}")
        print()
    return 0


# ─── apply ───────────────────────────────────────────────────────────────


def _build_llm_from_env() -> Any:
    from plivo_mirror.llm.openai import OpenAIClient

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required to apply fixes")
    return OpenAIClient(
        api_key=api_key,
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        base_url=os.getenv("OPENAI_API_URL"),
    )


def _preflight_gh_auth() -> int:
    """Verify `gh` is installed and the user is authenticated before
    we touch the report or the LLM. Returns 0 if OK, non-zero (and
    prints instructions to stderr) otherwise."""
    if shutil.which("gh") is None:
        print(
            "\n  ✗ GitHub CLI (`gh`) is not installed.\n\n"
            "  Mirror opens pull requests via the `gh` CLI. Install it:\n\n"
            "    macOS:   brew install gh\n"
            "    Linux:   https://github.com/cli/cli#installation\n\n"
            "  Then run:\n\n"
            "    gh auth login\n\n"
            "  …and re-run this apply.\n",
            file=sys.stderr,
        )
        return 1

    proc = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        print(
            "\n  ✗ GitHub CLI is installed but you are NOT authenticated.\n\n"
            "  Mirror needs `gh` logged in so it can push the fix branch\n"
            "  and open a pull request. Run:\n\n"
            "    gh auth login\n\n"
            "  Choose GitHub.com → HTTPS → Login with a web browser.\n"
            "  Once `gh auth status` shows ✓ Logged in, re-run this apply.\n",
            file=sys.stderr,
        )
        if proc.stderr:
            print(f"  (gh said: {proc.stderr.strip()[:200]})\n", file=sys.stderr)
        return 1

    return 0


async def cmd_apply(args: argparse.Namespace) -> int:
    from plivo_mirror.fixers.github import GitHubFixer
    from plivo_mirror.fixers.base import FixerError

    # Pre-flight: gh installed + authenticated. Bail loud + clear if not,
    # so the user fixes it BEFORE we waste an LLM call on the rewrite.
    rc = _preflight_gh_auth()
    if rc != 0:
        return rc

    sink = _sink_from_args(args)
    r = await sink.get(args.report_id)
    if r is None:
        print(f"report #{args.report_id} not found", file=sys.stderr)
        return 1

    if r.status != ReportStatus.PENDING:
        cur = r.status.value if isinstance(r.status, ReportStatus) else r.status
        if cur == "failed" and args.retry:
            # Reset failed → pending so the underlying fixer will run.
            await sink.update_status(
                r.id, ReportStatus.PENDING, last_error=None
            )
            r = await sink.get(r.id)  # refresh
            print(f"  retrying failed report #{args.report_id} (--retry)...")
        else:
            print(f"report is not pending (status={cur})", file=sys.stderr)
            if r.applied_pr_url:
                print(f"  existing PR: {r.applied_pr_url}", file=sys.stderr)
            if cur == "failed":
                print(
                    f"  hint: pass --retry to re-attempt a failed report",
                    file=sys.stderr,
                )
            return 1

    allowed = [s.strip() for s in (args.allow or "").split(",") if s.strip()]
    if not allowed:
        print(
            "no --allow files provided. Mirror refuses to rewrite anything "
            "outside an explicit allowlist. Example: --allow agent.py,prompts.py",
            file=sys.stderr,
        )
        return 2

    llm = _build_llm_from_env()
    fixer = GitHubFixer(
        repo_path=args.repo_path,
        allowed_files=allowed,
        llm=llm,
        base_branch=args.base_branch,
        remote=args.remote,
    )

    print(f"  applying report #{r.id} → {r.proposed_file} in {args.repo_path}...")
    try:
        result = await fixer.apply(r)
    except FixerError as e:
        print(f"  ✗ apply failed: {e}", file=sys.stderr)
        await sink.update_status(
            r.id,
            ReportStatus.FAILED,
            last_error=str(e),
        )
        return 1
    except Exception as e:
        print(f"  ✗ unexpected error: {e}", file=sys.stderr)
        await sink.update_status(
            r.id,
            ReportStatus.FAILED,
            last_error=f"{type(e).__name__}: {e}",
        )
        return 1

    await sink.update_status(
        r.id,
        ReportStatus.APPLIED,
        applied_pr_url=result.pr_url,
        applied_at=_ts(),
    )
    print(f"  ✓ PR opened: {result.pr_url}")
    print(f"    branch: {result.branch}")
    return 0


# ─── dismiss ─────────────────────────────────────────────────────────────


async def cmd_dismiss(args: argparse.Namespace) -> int:
    sink = _sink_from_args(args)
    r = await sink.get(args.report_id)
    if r is None:
        print(f"report #{args.report_id} not found", file=sys.stderr)
        return 1
    await sink.update_status(
        r.id,
        ReportStatus.DISMISSED,
        dismissed_by=args.by or os.getenv("USER") or "cli",
        dismissed_at=_ts(),
    )
    print(f"  ✓ report #{r.id} dismissed")
    return 0


# ─── entry ───────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="plivo-mirror-fix",
        description="Review and apply plivo-mirror failure reports.",
    )
    p.add_argument("--db", default=None,
                   help="Path to the SQLite report store. "
                        "Defaults to ./plivo_mirror_reports.db or "
                        "$PLIVO_MIRROR_REPORTS_DB.")
    sub = p.add_subparsers(dest="cmd", required=True)

    lst = sub.add_parser("list", help="List failure reports.")
    lst.add_argument("--status", default="pending",
                     choices=["pending", "applied", "dismissed", "failed", "all"])
    lst.add_argument("--limit", type=int, default=50)

    show = sub.add_parser("show", help="Show one report in detail.")
    show.add_argument("report_id", type=int)

    apply = sub.add_parser("apply", help="Apply a report — open a PR.")
    apply.add_argument("report_id", type=int)
    apply.add_argument("--repo-path", required=True,
                       help="Local checkout of the customer's repo.")
    apply.add_argument("--allow", required=True,
                       help="Comma-separated list of files Mirror is "
                            "permitted to rewrite, relative to --repo-path. "
                            "Example: --allow agent.py,prompts.py")
    apply.add_argument("--base-branch", default="main")
    apply.add_argument("--remote", default="origin")
    apply.add_argument("--retry", action="store_true",
                       help="Re-attempt a report whose previous apply "
                            "failed. Resets status back to pending first.")

    dis = sub.add_parser("dismiss", help="Dismiss a report without applying.")
    dis.add_argument("report_id", type=int)
    dis.add_argument("--by", default=None)

    return p


async def _async_main(argv: list[str] | None = None) -> int:
    load_dotenv(find_dotenv())
    args = _build_parser().parse_args(argv)
    if args.cmd == "list":
        return await cmd_list(args)
    if args.cmd == "show":
        return await cmd_show(args)
    if args.cmd == "apply":
        return await cmd_apply(args)
    if args.cmd == "dismiss":
        return await cmd_dismiss(args)
    return 2


def main() -> None:
    raise SystemExit(asyncio.run(_async_main()))


if __name__ == "__main__":
    main()
