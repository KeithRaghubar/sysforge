#!/bin/sh
# pacman-hook-helper.sh — drop a sentinel for sysforge update reminders.
#
# Invoked by /usr/share/libalpm/hooks/sysforge-*.hook with one of:
#   kernel       — kernel package changed
#   toolchain    — toolchain package changed
#   buildstate   — any package changed (build-state refresh nudge)
#   artifacts    — any package changed (artifact-drift recheck nudge)
#
# Hooks run as root mid-pacman-transaction. Three correctness rules:
#   (1) never fail — exit 0 even on permission/IO errors so we cannot
#       break a pacman transaction;
#   (2) be fast — total work is one mkdir + one append;
#   (3) never follow a link (B37) — /var/lib/sysforge is root:sysforge 2775
#       with no sticky bit, so any group member can rename `sentinels` and
#       plant a symlink (or a symlinked/FIFO `sentinels/<kind>`) in its place.
#       We cd into the dir and verify the physical path and owner, so a later
#       swap cannot redirect us, then append with O_NOFOLLOW|O_NONBLOCK.
#
# Sentinel layout: /var/lib/sysforge/sentinels/<kind> contains the timestamp
# and (for hooks marked NeedsTargets) the target package list piped on stdin.
# SYSFORGE_SENTINEL_DIR overrides the directory for the test suite only.

set -u

kind="${1:-buildstate}"
dir="${SYSFORGE_SENTINEL_DIR:-/var/lib/sysforge/sentinels}"

case "$kind" in
    ''|*/*|.|..) exit 0 ;;
esac

mkdir -p "$dir" 2>/dev/null || exit 0
cd "$dir" 2>/dev/null || exit 0

# A symlinked `sentinels` resolves elsewhere; a dir a group member created
# in a race is not ours. Either way, touch nothing.
[ "$(pwd -P)" = "$dir" ] || exit 0
[ "$(stat -c %u . 2>/dev/null)" = "$(id -u)" ] || exit 0

# Heal the dir to root:sysforge 2775 (B1). The shipped tmpfiles.d entry sets
# this, but this hook — running as root mid-transaction — can win the ordering
# race and create the dir root:root 0755, which then blocks the unprivileged
# `sysforge update` process (build user, in the sysforge group) from writing the
# self-install sentinel. Without that sentinel the external-install reconcile
# treats sysforge's own `pacman -U` builds as external and mass-demotes them.
# Acts on `.`, the verified inode we hold, never on a path a member can swap.
# Best-effort — never fail the transaction (the group may not exist yet).
chgrp sysforge . 2>/dev/null || true
chmod 2775 . 2>/dev/null || true

# Only append to a regular file (or create one). A symlink is refused here and
# again by O_NOFOLLOW at open; nonblock makes a planted FIFO fail rather than
# hang the transaction.
if [ -e "$kind" ] || [ -L "$kind" ]; then
    [ -f "$kind" ] && [ ! -L "$kind" ] || exit 0
fi

# Append rather than overwrite so the consumer can see all events between
# two `sysforge update` runs. The consumer unlinks after reading, so the
# file does not grow without bound.
{
    printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ ! -t 0 ]; then
        cat 2>/dev/null || true
    fi
    printf '\n'
} | dd of="./$kind" oflag=append,nofollow,nonblock conv=notrunc status=none \
    2>/dev/null || exit 0

exit 0
