#!/usr/bin/env bash
# Build a local sysforge .pkg.tar.zst from the working tree using the same
# clean-chroot path tools/release.sh uses. Lets `make vm-install-*` validate
# PKGBUILD changes in the VM without first publishing to AUR.

set -euo pipefail

FLAVOR=""
OUT="${HOME}/.local/share/sysforge-vm/build"
CHROOT="/var/lib/archbuild/extra-x86_64"

usage() {
    cat >&2 <<EOF
Usage: $0 <stable|git> [--out=DIR] [--chroot=DIR]

  stable   Tar the working tree and build via PKGBUILD (release flavor).
           Includes uncommitted edits.
  git      Build via PKGBUILD-git from a local bare clone of the repo.
           Only sees committed state.

  --out=DIR     Where the .pkg.tar.zst lands (default: $OUT)
  --chroot=DIR  Clean-chroot root for makechrootpkg (default: $CHROOT)
EOF
    exit 1
}

while (( $# )); do
    case "$1" in
        stable|git)  FLAVOR="$1" ;;
        --out=*)     OUT="${1#--out=}" ;;
        --chroot=*)  CHROOT="${1#--chroot=}" ;;
        -h|--help)   usage ;;
        *) echo "unknown arg: $1" >&2; usage ;;
    esac
    shift
done

[[ -z $FLAVOR ]] && usage

if [[ ! -d $CHROOT/root ]]; then
    cat >&2 <<EOF
ERROR: clean chroot not found at $CHROOT/root
       Run once:
           sudo mkarchroot $CHROOT/root base-devel
EOF
    exit 1
fi

if ! command -v makechrootpkg >/dev/null; then
    echo "ERROR: makechrootpkg not found (install: sudo pacman -S devtools)" >&2
    exit 1
fi

REPO_ROOT=$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)

if [[ $FLAVOR == git ]] && ! git -C "$REPO_ROOT" diff --quiet HEAD 2>/dev/null; then
    echo "WARN: working tree is dirty; PKGBUILD-git only sees committed state." >&2
    echo "      Use the 'stable' flavor to test uncommitted edits." >&2
fi

mkdir -p "$OUT"

SCRATCH=$(mktemp -d -t sysforge-vm-pkg.XXXXXX)
trap 'rm -rf "$SCRATCH"' EXIT

case "$FLAVOR" in
    stable)
        VERSION=$(grep -E '^version[[:space:]]*=' "$REPO_ROOT/pyproject.toml" \
                    | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
        if [[ -z $VERSION ]]; then
            echo "ERROR: could not parse version from pyproject.toml" >&2
            exit 1
        fi
        TARBALL="$SCRATCH/sysforge-${VERSION}.tar.gz"

        # Mirror PKGBUILD's expected source layout: a single sysforge-${VERSION}/
        # directory at the top of the tarball.
        tar \
            --exclude='./.git' \
            --exclude='./.venv' \
            --exclude='./.claude' \
            --exclude='./builds' \
            --exclude='./dist' \
            --exclude='__pycache__' \
            --exclude='.pytest_cache' \
            --exclude='.ruff_cache' \
            --exclude='.coverage*' \
            --exclude='*.egg-info' \
            --transform "s,^\./,sysforge-${VERSION}/," \
            -czf "$TARBALL" \
            -C "$REPO_ROOT" .

        SHA=$(sha256sum "$TARBALL" | awk '{print $1}')
        cp "$REPO_ROOT/PKGBUILD" "$SCRATCH/PKGBUILD"
        # install= scriptlet is read from the build dir, not fetched as a source.
        cp "$REPO_ROOT/sysforge.install" "$SCRATCH/sysforge.install"
        sed -i -E "s/^sha256sums=\(.*\)/sha256sums=('$SHA')/" "$SCRATCH/PKGBUILD"
        export SRCDEST="$SCRATCH"
        ;;

    git)
        # Bare clone preserves all refs+tags; PKGBUILD-git's pkgver() depends
        # on `git describe --long --tags`.
        git clone --bare --quiet "$REPO_ROOT" "$SCRATCH/sysforge.git"
        cp "$REPO_ROOT/PKGBUILD-git" "$SCRATCH/PKGBUILD"
        # install= scriptlet is read from the build dir, not fetched as a source.
        cp "$REPO_ROOT/sysforge.install" "$SCRATCH/sysforge.install"
        sed -i "s|^source=.*|source=(\"\$pkgname::git+file://$SCRATCH/sysforge.git\")|" \
            "$SCRATCH/PKGBUILD"
        ;;
esac

cd "$SCRATCH"
export PKGDEST="$OUT"

echo "Building $FLAVOR flavor in clean chroot $CHROOT..."
makechrootpkg -c -u -r "$CHROOT"

echo
echo "Built packages in $OUT:"
# shellcheck disable=SC2012
# `ls -lh` on purpose: this is a human-readable summary of what was just
# built (size + mtime, aligned), not machine parsing — nothing downstream
# consumes it, and the glob is fixed. install-pkg.sh, which DOES parse the
# result, uses find -printf.
ls -lh "$OUT"/*.pkg.tar.zst 2>/dev/null | tail -5
