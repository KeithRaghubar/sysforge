#!/usr/bin/env bash
# SessionStart hook: print a one-screen banner with sysforge repo state.
# Output goes to stdout and is shown to Claude as a session-start system message.
set -euo pipefail

repo="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$repo" || exit 0

[ -d .git ] || exit 0

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
last=$(git log -1 --oneline 2>/dev/null || echo "(no commits)")

dirty=$(git status --porcelain 2>/dev/null)
if [ -n "$dirty" ]; then
    dirty_summary=$(printf '%s\n' "$dirty" | head -20)
    untracked_extra=$(printf '%s\n' "$dirty" | tail -n +21 | wc -l)
else
    dirty_summary="(clean)"
    untracked_extra=0
fi

ahead_behind=$(git rev-list --left-right --count "@{u}...HEAD" 2>/dev/null || echo "")
if [ -n "$ahead_behind" ]; then
    behind=$(printf '%s' "$ahead_behind" | awk '{print $1}')
    ahead=$(printf '%s' "$ahead_behind" | awk '{print $2}')
    upstream_note="upstream: $ahead ahead, $behind behind"
else
    upstream_note="upstream: (no tracking branch)"
fi

printf 'sysforge repo state\n'
printf '  branch: %s\n' "$branch"
printf '  last:   %s\n' "$last"
printf '  %s\n' "$upstream_note"
printf '  working tree:\n'
printf '%s\n' "$dirty_summary" | sed 's/^/    /'
if [ "$untracked_extra" -gt 0 ]; then
    printf '    ... and %s more\n' "$untracked_extra"
fi
