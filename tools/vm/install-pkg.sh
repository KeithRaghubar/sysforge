#!/usr/bin/env bash
# Install a locally-built sysforge .pkg.tar.zst into the running VM over SSH.
# Companion to build-pkg.sh; driven by `make vm-install-*`.

set -euo pipefail

FLAVOR=""
PKG_DIR="${HOME}/.local/share/sysforge-vm/build"
VM_PORT=10022
# Connect as root: it always exists and can run pacman directly. The builder
# account only exists post-install under the default username, so it breaks for
# non-default usernames / ISO runs. Override with VM_USER=<name> if needed.
VM_USER="${VM_USER:-root}"
VM_HOST=localhost
KNOWN_HOSTS="${HOME}/.local/share/sysforge-vm/known_hosts"

usage() {
    cat >&2 <<EOF
Usage: $0 <stable|git> [--pkg-dir=DIR]

  stable   Install the newest sysforge-X.Y.Z-*.pkg.tar.zst in PKG_DIR.
  git      Install the newest sysforge-git-*.pkg.tar.zst in PKG_DIR.

  --pkg-dir=DIR   Where to look for built packages (default: $PKG_DIR)
EOF
    exit 1
}

while (( $# )); do
    case "$1" in
        stable|git)   FLAVOR="$1" ;;
        --pkg-dir=*)  PKG_DIR="${1#--pkg-dir=}" ;;
        -h|--help)    usage ;;
        *) echo "unknown arg: $1" >&2; usage ;;
    esac
    shift
done

[[ -z $FLAVOR ]] && usage

case "$FLAVOR" in
    stable) GLOB="sysforge-[0-9]*.pkg.tar.zst" ;;
    git)    GLOB="sysforge-git-*.pkg.tar.zst" ;;
esac

# `find -printf` gives us mtime-sorted matches without tripping on nullglob
# (which would consume the prefix path when no files match the pattern).
mapfile -t MATCHES < <(
    find "$PKG_DIR" -maxdepth 1 -type f -name "$GLOB" -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | awk '{print $2}'
)

if (( ${#MATCHES[@]} == 0 )); then
    cat >&2 <<EOF
ERROR: no $FLAVOR package matching '$GLOB' in $PKG_DIR
       Run: make vm-pkg-$FLAVOR
EOF
    exit 1
fi
PKG="${MATCHES[0]}"

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

PKG_BASENAME=$(basename "$PKG")
echo "Copying $PKG_BASENAME to VM..."
scp -P "$VM_PORT" "${SSH_OPTS[@]}" "$PKG" "$VM_USER@$VM_HOST:/tmp/"

echo "Installing $PKG_BASENAME in VM..."
# root runs pacman directly; a non-root override needs sudo.
INSTALL_CMD="pacman -U --noconfirm /tmp/$PKG_BASENAME"
[[ $VM_USER != root ]] && INSTALL_CMD="sudo $INSTALL_CMD"
ssh -t -p "$VM_PORT" "${SSH_OPTS[@]}" "$VM_USER@$VM_HOST" "$INSTALL_CMD"

echo
echo "Done. Verify in VM:  make vm-ssh  then  sysforge --version"
