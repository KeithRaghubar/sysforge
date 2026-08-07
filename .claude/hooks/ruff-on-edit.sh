#!/usr/bin/env bash
# PostToolUse hook: auto-lint Python files after Edit/Write/MultiEdit.
#
# Reads the tool input as JSON on stdin, extracts file_path, and runs
# `ruff check --fix-only` on it. If ruff issues remain after auto-fix,
# the hook exits 2 so Claude sees the diagnostic and addresses them
# instead of leaving the cleanup for `make lint` / release time.
set -euo pipefail

[ -t 0 ] && exit 0  # nothing on stdin -> nothing to do

input=$(cat)
file_path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)

[ -z "$file_path" ] && exit 0
[[ "$file_path" != *.py ]] && exit 0

repo="${CLAUDE_PROJECT_DIR:-$(pwd)}"

# The same three trees `make lint-py` gates (2.6.1-STD8) — the hook and the
# gate must agree, or one of them is training people to ignore it.
case "$file_path" in
    "$repo"/sysforge/*|"$repo"/tests/*|"$repo"/tools/*) ;;
    *) exit 0 ;;
esac

[ ! -f "$file_path" ] && exit 0

cd "$repo"

command -v ruff >/dev/null 2>&1 || exit 0

ruff check --fix-only --quiet "$file_path" >/dev/null 2>&1 || true

if remaining=$(ruff check "$file_path" 2>&1); then
    exit 0
fi

printf 'ruff issues remain in %s after auto-fix:\n%s\n' "$file_path" "$remaining" >&2
exit 2
