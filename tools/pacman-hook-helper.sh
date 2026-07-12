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

# Heal the dir to root:sysforge 2775 (B1). The shipped tmpfiles.d entry sets
# this, but this hook — running as root mid-transaction — can win the ordering
# race and create the dir root:root 0755, which then blocks the unprivileged
# `sysforge update` process (build user, in the sysforge group) from writing the
# self-install sentinel. Without that sentinel the external-install reconcile
# treats sysforge's own `pacman -U` builds as external and mass-demotes them.
# Best-effort — never fail the transaction (the group may not exist yet).
chgrp sysforge "$dir" 2>/dev/null || true
chmod 2775 "$dir" 2>/dev/null || true

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
