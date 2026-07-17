#!/usr/bin/env bash
# Assert the post-install packaging invariants of an installed sysforge inside
# the running VM, over SSH. Companion to build-pkg.sh / install-pkg.sh; driven
# by `make vm-smoke` (and run as the verification tail of `make vm-test`).
#
# Packaging integrity only: runs as root, checks the filesystem a real install
# must have produced plus a version-only liveness gate. It does NOT build or
# install — it assumes sysforge is already installed in the VM.

set -euo pipefail

VM_PORT=10022
VM_HOST=localhost
VM_USER="${VM_USER:-root}"
KNOWN_HOSTS="${HOME}/.local/share/sysforge-vm/known_hosts"

usage() {
    cat >&2 <<EOF
Usage: $0 [-h|--help]

  Verify an already-installed sysforge in the running VM. No arguments.
  Override the SSH user with VM_USER=<name> (default: root).
EOF
    exit 1
}

case "${1:-}" in
    -h|--help) usage ;;
    "") ;;
    *) echo "unknown arg: $1" >&2; usage ;;
esac

if ! timeout 3 bash -c "</dev/tcp/$VM_HOST/$VM_PORT" 2>/dev/null; then
    cat >&2 <<EOF
ERROR: VM SSH port $VM_PORT not reachable on $VM_HOST.
       Is the VM running? Try: make vm-snapshot
EOF
    exit 1
fi

SSH_OPTS=(
    -o "UserKnownHostsFile=$KNOWN_HOSTS"
    -o "StrictHostKeyChecking=accept-new"
    -o "ConnectTimeout=5"
)

# The VM is ephemeral and regenerates SSH host keys on reinstall; evict any
# stale entry so accept-new doesn't hard-fail. Mirrors install-pkg.sh:81.
ssh-keygen -R "[$VM_HOST]:$VM_PORT" -f "$KNOWN_HOSTS" 2>/dev/null || true

# Gather all raw facts in ONE remote invocation. Emit machine-parseable lines
# the host side evaluates. The remote block never aborts (each fact is captured
# independently) so a single missing artifact doesn't hide the others.
# shellcheck disable=SC2016
# Single-quoted on purpose: $?, $ver, etc. must expand IN THE VM, not on the host.
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
'

RAW=$(ssh -p "$VM_PORT" "${SSH_OPTS[@]}" "$VM_USER@$VM_HOST" "$REMOTE_SCRIPT")

# Parse the facts into shell vars (VERSION_RC, VERSION_OUT, QI_RC, HOOK_COUNT,
# BASH_COMP, ZSH_COMP, SENTINEL_DIR).
VERSION_RC=""; VERSION_OUT=""; QI_RC=""; HOOK_COUNT=""
BASH_COMP=""; ZSH_COMP=""; SENTINEL_DIR=""
while IFS='=' read -r key val; do
    case "$key" in
        VERSION_RC)   VERSION_RC="$val" ;;
        VERSION_OUT)  VERSION_OUT="$val" ;;
        QI_RC)        QI_RC="$val" ;;
        HOOK_COUNT)   HOOK_COUNT="$val" ;;
        BASH_COMP)    BASH_COMP="$val" ;;
        ZSH_COMP)     ZSH_COMP="$val" ;;
        SENTINEL_DIR) SENTINEL_DIR="$val" ;;
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

echo "==> sysforge post-install smoke test (VM $VM_USER@$VM_HOST:$VM_PORT)"

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

echo
echo "==> Summary: $pass pass, $fail fail"
if (( fail > 0 )); then
    echo "Smoke test FAILED — the install is missing expected artifacts."
    exit 1
fi
echo "Smoke test passed."
