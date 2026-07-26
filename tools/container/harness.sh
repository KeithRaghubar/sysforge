#!/usr/bin/env bash
# Container tier for the packaging / portability smoke checks (2.6.1-F2).
#
# Builds a throwaway container from a distro-parameterized base image, installs
# a locally-built sysforge package into it, and runs tools/smoke.sh against it
# over `podman exec`. Every check in that script is container-satisfiable —
# filesystem layout, `pacman -Qi`, a `--version` liveness gate, and the
# portability facts — so this answers in seconds what the VM tier needs a
# snapshot and a boot for.
#
# What stays with the VM tier and is NOT checked here: bootstrap/archinstall,
# kernel staging, graphics/DKMS probes, restart detection. Those need a real
# kernel and bootloader.
#
# Flavors (--distro):
#   arch     the primary supported base
#   cachyos  an Arch derivative, from its own upstream image. The point of the
#            tier: it is the only arm that exercises repo/AUR shadowing (extra
#            sync DBs ahead of core/extra), a different makepkg.conf baseline
#            (-march=x86-64-v3 + LTO), and bumped pkgrels on core packages.
#
# Exit codes mirror tools/smoke.sh: 0 = pass, 1 = a check failed, 3 = the
# harness is unavailable (podman missing, image unbuildable, no network). 3 lets
# the release preflight warn on absent optional infrastructure instead of
# failing a release over it.

set -euo pipefail

DISTRO="arch"
PKG_DIR="${SYSFORGE_VM_DIR:-${HOME}/.local/share/sysforge-vm}/build"
FLAVOR="stable"
KEEP=0
ACTION="smoke"

EXIT_UNAVAILABLE=3

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

usage() {
    cat >&2 <<EOF
Usage: $0 [smoke|build|shell|clean] [options]

  smoke   Build the image, start it, install the package, run the checks,
          then remove the container (default).
  build   Build the image only.
  shell   Build + start + install, then drop into an interactive shell.
  clean   Remove the containers and images for this flavor.

Options:
  --distro=arch|cachyos   Which base to test (default: $DISTRO)
  --pkg-dir=DIR           Where to find the built package (default: $PKG_DIR)
  --flavor=stable|git     Which package flavor to install (default: $FLAVOR)
  --keep                  Leave the container running after 'smoke'
EOF
    exit 1
}

while (( $# )); do
    case "$1" in
        smoke|build|shell|clean) ACTION="$1" ;;
        --distro=*)  DISTRO="${1#--distro=}" ;;
        --pkg-dir=*) PKG_DIR="${1#--pkg-dir=}" ;;
        --flavor=*)  FLAVOR="${1#--flavor=}" ;;
        --keep)      KEEP=1 ;;
        -h|--help)   usage ;;
        *) echo "unknown arg: $1" >&2; usage ;;
    esac
    shift
done

# Flavor table. The base image IS the parameterization — see the Containerfile
# comment on why nothing distro-specific is synthesized.
case "$DISTRO" in
    arch)
        BASE_IMAGE="docker.io/library/archlinux:base-devel"
        EXPECT_DISTRO="arch"
        ;;
    cachyos)
        # Upstream's own image, so the repos, makepkg.conf and os-release under
        # test are the distro's, not ours.
        BASE_IMAGE="docker.io/cachyos/cachyos:latest"
        EXPECT_DISTRO="cachyos"
        ;;
    *) echo "unknown --distro: $DISTRO (expected arch or cachyos)" >&2; usage ;;
esac

case "$FLAVOR" in
    stable) PKG_GLOB="sysforge-[0-9]*.pkg.tar.zst" ;;
    git)    PKG_GLOB="sysforge-git-*.pkg.tar.zst" ;;
    *) echo "unknown --flavor: $FLAVOR (expected stable or git)" >&2; usage ;;
esac

IMAGE="sysforge-smoke:$DISTRO"
CONTAINER="sysforge-smoke-$DISTRO"

if ! command -v podman >/dev/null 2>&1; then
    echo "podman not installed — the container tier is unavailable." >&2
    echo "Install it with: sudo pacman -S podman" >&2
    exit "$EXIT_UNAVAILABLE"
fi

# --- clean ------------------------------------------------------------------

if [[ $ACTION == clean ]]; then
    podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
    podman rmi -f "$IMAGE" >/dev/null 2>&1 || true
    echo "Removed container/image for $DISTRO."
    exit 0
fi

# --- build ------------------------------------------------------------------

echo "==> Building $IMAGE from $BASE_IMAGE"
# A build failure here is almost always a missing image or no network, neither of
# which is a sysforge defect — report it as unavailable, not as a failed check.
if ! podman build --pull=newer \
        --build-arg "BASE_IMAGE=$BASE_IMAGE" \
        -t "$IMAGE" -f "$SCRIPT_DIR/Containerfile" "$SCRIPT_DIR"; then
    echo >&2
    echo "Image build failed — the container tier is unavailable (no network, or" >&2
    echo "$BASE_IMAGE cannot be pulled)." >&2
    exit "$EXIT_UNAVAILABLE"
fi

[[ $ACTION == build ]] && exit 0

# --- locate the package -----------------------------------------------------

# find -printf, not a glob: mtime-sorted newest-first, and it cannot swallow the
# prefix path when nothing matches. Mirrors tools/vm/install-pkg.sh.
mapfile -t MATCHES < <(
    find "$PKG_DIR" -maxdepth 1 -type f -name "$PKG_GLOB" -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | awk '{print $2}'
)
if (( ${#MATCHES[@]} == 0 )); then
    cat >&2 <<EOF
ERROR: no $FLAVOR package matching '$PKG_GLOB' in $PKG_DIR
       Run: make vm-pkg-$FLAVOR
EOF
    exit 1
fi
PKG="${MATCHES[0]}"
PKG_BASENAME=$(basename "$PKG")

# --- start + install --------------------------------------------------------

# Always start from a fresh container: a re-used one carries the previous
# install, which would let a packaging regression pass on leftovers.
podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
echo "==> Starting $CONTAINER"
podman run -d --name "$CONTAINER" "$IMAGE" >/dev/null

cleanup() {
    if (( KEEP == 0 )) && [[ $ACTION == smoke ]]; then
        podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

echo "==> Installing $PKG_BASENAME"
podman cp "$PKG" "$CONTAINER:/tmp/$PKG_BASENAME"
# --ask=4 (ALPM_QUESTION_CONFLICT_PKG) auto-confirms the conflict-removal
# prompt: the stable/git flavors declare conflicts=('<other-flavor>'), which
# --noconfirm would otherwise answer N to. Mirrors tools/vm/install-pkg.sh.
podman exec "$CONTAINER" pacman -U --noconfirm --ask=4 "/tmp/$PKG_BASENAME"

if [[ $ACTION == shell ]]; then
    echo "==> Container $CONTAINER is up with $PKG_BASENAME installed."
    echo "    Remove it with: $0 clean --distro=$DISTRO"
    exec podman exec -it "$CONTAINER" bash
fi

# --- checks -----------------------------------------------------------------
#
# One copy of the checks, shared with the VM tier. SMOKE_EXPECT_DISTRO pins the
# identity so a mis-tagged base image fails loudly rather than making the
# derivative arm silently re-test Arch.
echo
SMOKE_EXPECT_DISTRO="$EXPECT_DISTRO" \
    "$REPO_ROOT/tools/smoke.sh" --transport=podman --target="$CONTAINER"
