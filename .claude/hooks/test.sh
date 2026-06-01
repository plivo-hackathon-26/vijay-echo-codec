#!/usr/bin/env bash
# Stop hook: run the v3 unit tests when Python under v3/ has uncommitted changes.
# Non-blocking and informational — always exits 0 (never blocks the turn).
set -uo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$ROOT" || exit 0

# Skip unless plivo_mirror or tests have changes (modified, staged, OR new
# untracked .py files) — keeps it off pure-conversation turns.
changes="$(git status --porcelain -- v3/plivo_mirror v3/tests 2>/dev/null | grep -E '\.py$' || true)"
[ -z "$changes" ] && exit 0

PY="$ROOT/venv/bin/python"; [ -x "$PY" ] || PY="python3"
cd "$ROOT/v3" || exit 0
echo "▶ v3 unit tests (Python changes detected):"
PYTHONPATH=. "$PY" -m pytest -q tests/ 2>&1 | tail -15 || true
exit 0
