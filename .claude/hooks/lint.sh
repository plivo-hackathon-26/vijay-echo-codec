#!/usr/bin/env bash
# PostToolUse (Edit|Write) hook: fast syntax/lint check on edited Python files.
# Non-blocking and informational — always exits 0. py_compile is always run;
# ruff/mypy run only if installed (pip install ruff mypy to activate them).
set -uo pipefail

input="$(cat)"
fp="$(printf '%s' "$input" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); ti=d.get("tool_input",{}) or {}
    print(ti.get("file_path") or ti.get("path") or "")
except Exception:
    print("")' 2>/dev/null || true)"

[ -z "$fp" ] && exit 0
case "$fp" in *.py) ;; *) exit 0 ;; esac
[ -f "$fp" ] || exit 0

ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
PY="$ROOT/venv/bin/python"; [ -x "$PY" ] || PY="python3"

err="$("$PY" -m py_compile "$fp" 2>&1 1>/dev/null || true)"
[ -n "$err" ] && { echo "⚠ syntax error in $fp:"; echo "$err"; exit 0; }

command -v ruff >/dev/null 2>&1 && ruff check "$fp" 2>&1 | head -20 || true
command -v mypy >/dev/null 2>&1 && mypy --no-error-summary "$fp" 2>&1 | head -20 || true
exit 0
