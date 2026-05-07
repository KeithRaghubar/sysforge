#!/bin/sh
# pacman-hook-helper.sh — drop a sentinel for sysforge update reminders.
#
# Invoked by /usr/share/libalpm/hooks/sysforge-*.hook with one of:
#   kernel       — kernel package changed
#   toolchain    — toolchain package changed
#   buildstate   — any package changed (build-state refresh nudge)
#
# Hooks run as root mid-pacman-transaction. Two correctness rules:
#   (1) never fail — exit 0 even on permission/IO errors so we cannot
#       break a pacman transaction;
#   (2) be fast — total work is one mkdir + one redirect.
#
# Sentinel layout: /var/lib/sysforge/sentinels/<kind> contains the timestamp
# and (for hooks marked NeedsTargets) the target package list piped on stdin.

set -u

kind="${1:-buildstate}"
dir="/var/lib/sysforge/sentinels"
sentinel="$dir/$kind"

mkdir -p "$dir" 2>/dev/null || exit 0

# Append rather than overwrite so the consumer can see all events between
# two `sysforge update` runs. The consumer unlinks after reading, so the
# file does not grow without bound.
{
    printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ ! -t 0 ]; then
        cat 2>/dev/null || true
    fi
    printf '\n'
} >> "$sentinel" 2>/dev/null || exit 0

exit 0
