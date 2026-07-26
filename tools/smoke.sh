#!/usr/bin/env bash
# Assert the post-install packaging and portability invariants of an installed
# sysforge on a throwaway target. Driven by `make vm-smoke` (VM, over SSH) and
# `make container-smoke` (container, over `podman exec`), and by the release
# preflight.
#
# Transport-agnostic by construction: every fact is gathered in ONE remote
# invocation and evaluated here on the host, so all a transport must provide is
# "run this shell snippet over there". That is why both tiers share one copy of
# the checks. The VM tier still exists for what a container cannot host —
# bootloader, kernel staging, graphics/DKMS, real hardware — none of which is
# checked here.
#
# It does NOT build or install: sysforge must already be installed on the target.
#
# Exit codes: 0 = all checks passed, 1 = a check failed, 3 = target unreachable
# (no VM running / no container). 3 is distinct on purpose — absent *optional*
# infrastructure must be distinguishable from a real failure by a caller that
# wants to warn rather than fail (the release preflight does exactly that).

set -euo pipefail

TRANSPORT="ssh"
TARGET=""             # podman transport: container name
VM_PORT=10022
VM_HOST=localhost
VM_USER="${VM_USER:-root}"
VM_DIR="${SYSFORGE_VM_DIR:-${HOME}/.local/share/sysforge-vm}"
KNOWN_HOSTS="$VM_DIR/known_hosts"

# Which distro the target is expected to be, as an os-release ID. Empty = accept
# any Arch-derived host. The container harness sets it per flavor so a mis-tagged
# image — the derivative arm silently running plain Arch, which would make the
# whole tier vacuous while reporting all-pass — fails loudly instead.
EXPECT_DISTRO="${SMOKE_EXPECT_DISTRO:-}"

EXIT_UNREACHABLE=3

usage() {
    cat >&2 <<EOF
Usage: $0 [--transport=ssh|podman] [--target=NAME] [-h|--help]

  Verify an already-installed sysforge on a throwaway target.

  --transport=ssh     Reach the test VM over SSH on port $VM_PORT (default).
                      Override the user with VM_USER=<name> (default: root).
  --transport=podman  Reach a running container with 'podman exec'.
  --target=NAME       Container name (required for --transport=podman).

  SMOKE_EXPECT_DISTRO=<os-release ID>   Require this exact distro (default:
                      accept any Arch-derived host).
EOF
    exit 1
}

while (( $# )); do
    case "$1" in
        --transport=*) TRANSPORT="${1#--transport=}" ;;
        --target=*)    TARGET="${1#--target=}" ;;
        -h|--help)     usage ;;
        *) echo "unknown arg: $1" >&2; usage ;;
    esac
    shift
done

case "$TRANSPORT" in
    ssh) ;;
    podman)
        [[ -z $TARGET ]] && { echo "--transport=podman needs --target=NAME" >&2; usage; }
        ;;
    *) echo "unknown transport: $TRANSPORT" >&2; usage ;;
esac

# --- transports -------------------------------------------------------------
#
# A transport is exactly two functions: reachable_<name> and remote_<name> (run
# the gather snippet, print its stdout). Adding a tier adds a pair here and
# touches no check.

reachable_ssh() {
    timeout 3 bash -c "</dev/tcp/$VM_HOST/$VM_PORT" 2>/dev/null
}

remote_ssh() {
    local opts=(
        -o "UserKnownHostsFile=$KNOWN_HOSTS"
        -o "StrictHostKeyChecking=accept-new"
        -o "ConnectTimeout=5"
    )
    # The VM is ephemeral and regenerates SSH host keys on reinstall; evict any
    # stale entry so accept-new doesn't hard-fail. Mirrors install-pkg.sh.
    ssh-keygen -R "[$VM_HOST]:$VM_PORT" -f "$KNOWN_HOSTS" 2>/dev/null || true
    ssh -p "$VM_PORT" "${opts[@]}" "$VM_USER@$VM_HOST" "$1"
}

reachable_podman() {
    command -v podman >/dev/null 2>&1 || return 1
    [[ $(podman inspect -f '{{.State.Running}}' "$TARGET" 2>/dev/null) == true ]]
}

remote_podman() {
    # `sh -c` rather than argv: the gather block is a shell program, not a
    # command line. `sh` (not bash) because the snippet must stay POSIX enough
    # for whatever the base image ships.
    podman exec "$TARGET" sh -c "$1"
}

if ! "reachable_$TRANSPORT"; then
    if [[ $TRANSPORT == ssh ]]; then
        cat >&2 <<EOF
ERROR: VM SSH port $VM_PORT not reachable on $VM_HOST.
       Is the VM running? Try: make vm-snapshot
EOF
    else
        cat >&2 <<EOF
ERROR: container '$TARGET' is not running (or podman is not installed).
       Start one with: make container-smoke
EOF
    fi
    exit "$EXIT_UNREACHABLE"
fi

# --- fact gathering ---------------------------------------------------------
#
# One remote invocation, machine-parseable lines, evaluated host-side. The block
# never aborts (each fact is captured independently) so one missing artifact
# doesn't hide the others. Every value must stay single-line: the host parses one
# KEY=value per line.
#
# shellcheck disable=SC2016
# Single-quoted on purpose: $?, $ver, etc. must expand ON THE TARGET, not here.
REMOTE_SCRIPT='
    ver=$(sysforge --version 2>/dev/null); vrc=$?
    printf "VERSION_RC=%s\n" "$vrc"
    printf "VERSION_OUT=%s\n" "$ver"
    pacman -Qi sysforge >/dev/null 2>&1; printf "QI_RC=%s\n" "$?"
    n=$(ls /usr/share/libalpm/hooks/sysforge-*.hook 2>/dev/null | wc -l)
    printf "HOOK_COUNT=%s\n" "$n"
    [ -f /usr/share/bash-completion/completions/sysforge ] && printf "BASH_COMP=1\n" || printf "BASH_COMP=0\n"
    [ -f /usr/share/zsh/site-functions/_sysforge ] && printf "ZSH_COMP=1\n" || printf "ZSH_COMP=0\n"
    [ -d /var/lib/sysforge/sentinels ] && printf "SENTINEL_DIR=1\n" || printf "SENTINEL_DIR=0\n"

    # --- portability facts -------------------------------------------------
    # What sysforge believes about the host, next to the ground truth read
    # straight out of /etc. A derivative breaks these, not the artifacts above,
    # so they are the substance of the derivative arm.

    # Distro identity. Ground truth by sourcing os-release; sysforge reports it
    # through the doctor axis over primitives/os_release.py.
    printf "OS_ID=%s\n" "$(. /etc/os-release 2>/dev/null; printf "%s" "${ID:-}")"
    printf "OS_ID_LIKE=%s\n" "$(. /etc/os-release 2>/dev/null; printf "%s" "${ID_LIKE:-}")"
    sysforge doctor --distro --quiet >/dev/null 2>&1; printf "DISTRO_AXIS_RC=%s\n" "$?"

    # Sync repos: every [section] in pacman.conf must reach the alpm handle. A
    # derivative carries extra repos ahead of core/extra, and a hardcoded repo
    # list is what breaks the repo-vs-AUR makedep split. Reaching for the
    # private resolver is deliberate: it is the seam under test, and there is no
    # public surface that prints the registered DBs.
    printf "CONF_REPOS=%s\n" "$(sed -n "s/^\[\([^]]*\)\].*/\1/p" /etc/pacman.conf 2>/dev/null | grep -v "^options$" | paste -sd, -)"
    printf "SF_REPOS=%s\n" "$(python -c "from sysforge.primitives import pacman; print(\",\".join(pacman._read_sync_repo_names()))" 2>/dev/null)"

    # makepkg.conf merge baseline: what sysforge parses out of the SYSTEM conf
    # must be the system value, never a vendored default. A derivative ships its
    # own -march/LTO defaults and they have to survive into the merge. CFLAGS
    # specifically because it is a literal in both Arch and derivative confs —
    # CXXFLAGS is "$CFLAGS ...", which sourcing expands and the verbatim parse
    # does not, so it is not comparable. The parse is pinned to the system path
    # (no user-conf layering); the trailing tr drops the quotes, since
    # parse_system_makepkg_conf returns the value text verbatim.
    printf "CONF_CFLAGS=%s\n" "$(. /etc/makepkg.conf 2>/dev/null; printf "%s" "${CFLAGS:-}")"
    printf "SF_CFLAGS=%s\n" "$(python -c "from sysforge.primitives.config import parse_system_makepkg_conf as p; print(p(\"/etc/makepkg.conf\").get(\"CFLAGS\",\"\"))" 2>/dev/null | tr -d "\"")"

    # Version comparison against a real local-db version string: derivatives
    # carry bumped pkgrels on core packages, and that is the format the
    # already-built fingerprint has to order correctly.
    pv=$(pacman -Q pacman 2>/dev/null | awk "{print \$2}")
    printf "PACMAN_VER=%s\n" "$pv"
    printf "VERCMP_EQ=%s\n" "$(vercmp "$pv" "$pv" 2>/dev/null)"
    printf "VERCMP_OLDER=%s\n" "$(vercmp "$pv" "$pv.1" 2>/dev/null)"
'

RAW=$("remote_$TRANSPORT" "$REMOTE_SCRIPT")

VERSION_RC=""; VERSION_OUT=""; QI_RC=""; HOOK_COUNT=""
BASH_COMP=""; ZSH_COMP=""; SENTINEL_DIR=""
OS_ID=""; OS_ID_LIKE=""; DISTRO_AXIS_RC=""
CONF_REPOS=""; SF_REPOS=""; CONF_CFLAGS=""; SF_CFLAGS=""
PACMAN_VER=""; VERCMP_EQ=""; VERCMP_OLDER=""
# IFS='=' with `val` last: everything after the first '=' lands in val, so flag
# strings containing '=' (-march=x86-64-v3) survive intact.
while IFS='=' read -r key val; do
    case "$key" in
        VERSION_RC)     VERSION_RC="$val" ;;
        VERSION_OUT)    VERSION_OUT="$val" ;;
        QI_RC)          QI_RC="$val" ;;
        HOOK_COUNT)     HOOK_COUNT="$val" ;;
        BASH_COMP)      BASH_COMP="$val" ;;
        ZSH_COMP)       ZSH_COMP="$val" ;;
        SENTINEL_DIR)   SENTINEL_DIR="$val" ;;
        OS_ID)          OS_ID="$val" ;;
        OS_ID_LIKE)     OS_ID_LIKE="$val" ;;
        DISTRO_AXIS_RC) DISTRO_AXIS_RC="$val" ;;
        CONF_REPOS)     CONF_REPOS="$val" ;;
        SF_REPOS)       SF_REPOS="$val" ;;
        CONF_CFLAGS)    CONF_CFLAGS="$val" ;;
        SF_CFLAGS)      SF_CFLAGS="$val" ;;
        PACMAN_VER)     PACMAN_VER="$val" ;;
        VERCMP_EQ)      VERCMP_EQ="$val" ;;
        VERCMP_OLDER)   VERCMP_OLDER="$val" ;;
    esac
done <<< "$RAW"

pass=0
fail=0

# Evaluate each check WITHOUT aborting the run (checks are allowed to fail).
check() {
    # $1 = human label, $2 = "ok" if the check passed, anything else = fail
    local label="$1" ok="$2"
    if [[ $ok == ok ]]; then
        printf '  [PASS] %s\n' "$label"
        pass=$((pass + 1))
    else
        printf '  [FAIL] %s\n' "$label"
        fail=$((fail + 1))
    fi
}

if [[ $TRANSPORT == ssh ]]; then
    echo "==> sysforge post-install smoke test (VM $VM_USER@$VM_HOST:$VM_PORT)"
else
    echo "==> sysforge post-install smoke test (container $TARGET)"
fi

# --- packaging integrity ----------------------------------------------------

[[ $VERSION_RC == 0 && -n $VERSION_OUT ]] && v=ok || v=no
check "sysforge --version runs (${VERSION_OUT:-<empty>})" "$v"

[[ $QI_RC == 0 ]] && v=ok || v=no
check "pacman -Qi sysforge (package registered)" "$v"

[[ $HOOK_COUNT == 3 ]] && v=ok || v=no
check "3 pacman hooks installed (found ${HOOK_COUNT:-0})" "$v"

[[ $BASH_COMP == 1 ]] && v=ok || v=no
check "bash completion installed" "$v"

[[ $ZSH_COMP == 1 ]] && v=ok || v=no
check "zsh completion installed" "$v"

[[ $SENTINEL_DIR == 1 ]] && v=ok || v=no
check "tmpfiles.d created /var/lib/sysforge/sentinels" "$v"

# --- portability ------------------------------------------------------------

if [[ -n $EXPECT_DISTRO ]]; then
    [[ $OS_ID == "$EXPECT_DISTRO" ]] && v=ok || v=no
    check "target is $EXPECT_DISTRO (os-release ID=${OS_ID:-<none>})" "$v"
else
    # No expectation given: still require an Arch-derived host, or every
    # invariant below is asserted against the wrong package manager.
    [[ $OS_ID == arch || $OS_ID_LIKE == *arch* ]] && v=ok || v=no
    check "target is Arch-derived (ID=${OS_ID:-<none>}, ID_LIKE=${OS_ID_LIKE:-<none>})" "$v"
fi

[[ $DISTRO_AXIS_RC == 0 ]] && v=ok || v=no
check "doctor --distro runs clean (identity is readable)" "$v"

[[ -n $CONF_REPOS && $SF_REPOS == "$CONF_REPOS" ]] && v=ok || v=no
check "sync repos match pacman.conf (conf=${CONF_REPOS:-<none>} sysforge=${SF_REPOS:-<none>})" "$v"

[[ -n $CONF_CFLAGS && $SF_CFLAGS == "$CONF_CFLAGS" ]] && v=ok || v=no
check "system makepkg.conf CFLAGS parsed verbatim (${CONF_CFLAGS:-<none>})" "$v"

[[ $VERCMP_EQ == 0 && $VERCMP_OLDER == -1 ]] && v=ok || v=no
check "version compare on a live pkgver (${PACMAN_VER:-<none>}: eq=${VERCMP_EQ:-?} older=${VERCMP_OLDER:-?})" "$v"

echo
echo "==> Summary: $pass pass, $fail fail"
if (( fail > 0 )); then
    echo "Smoke test FAILED — the install is missing expected artifacts, or a"
    echo "portability invariant does not hold on this distro."
    exit 1
fi
echo "Smoke test passed."
