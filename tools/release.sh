#!/usr/bin/env bash
# tools/release.sh — prepare sysforge for AUR publication
#
# Run AFTER creating and pushing the release tag:
#   git tag v0.x.0 && git push origin v0.x.0
#
# What this does:
#   1. Reads version from pyproject.toml
#   2. Verifies the tag exists in the local repo
#   3. Fetches sha256sum from the GitHub tarball
#   4. Updates sha256sums in PKGBUILD
#   5. Generates .SRCINFO (stable) and .SRCINFO-git (VCS)
#   6. Prints instructions for pushing to AUR
#
# Usage:
#   bash tools/release.sh [--dry-run]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DRY_RUN=0
for arg in "$@"; do
    [[ "$arg" == "--dry-run" ]] && DRY_RUN=1
done

# ---------------------------------------------------------------------------
# Read version
# ---------------------------------------------------------------------------

VERSION=$(python3 -c "
import tomllib
with open('pyproject.toml', 'rb') as f:
    d = tomllib.load(f)
print(d['project']['version'])
")
TAG="v$VERSION"

echo "==> sysforge release: $VERSION ($TAG)"

# ---------------------------------------------------------------------------
# Verify versions are in sync
# ---------------------------------------------------------------------------

PKGBUILD_VER=$(grep '^pkgver=' PKGBUILD | cut -d= -f2)
if [[ "$PKGBUILD_VER" != "$VERSION" ]]; then
    echo "ERROR: PKGBUILD pkgver=$PKGBUILD_VER does not match pyproject.toml version=$VERSION"
    echo "       Update PKGBUILD pkgver to $VERSION before releasing."
    exit 1
fi

# ---------------------------------------------------------------------------
# Verify tag exists locally
# ---------------------------------------------------------------------------

if ! git tag -l "$TAG" | grep -qx "$TAG"; then
    echo "ERROR: tag $TAG not found in local repo."
    echo "       Create and push it first:"
    echo "         git tag $TAG && git push origin $TAG"
    exit 1
fi

# ---------------------------------------------------------------------------
# Fetch sha256sum from GitHub tarball
# ---------------------------------------------------------------------------

TARBALL_URL="https://github.com/KeithRaghubar/sysforge/archive/$TAG.tar.gz"
echo "==> Fetching sha256sum from $TARBALL_URL"

if [[ "$DRY_RUN" -eq 1 ]]; then
    SHA256="DRYRUN0000000000000000000000000000000000000000000000000000000000"
    echo "    [dry-run] skipping fetch, using placeholder hash"
else
    SHA256=$(curl -fsSL "$TARBALL_URL" | sha256sum | awk '{print $1}')
fi

echo "    sha256: $SHA256"

# ---------------------------------------------------------------------------
# Update sha256sums in PKGBUILD
# ---------------------------------------------------------------------------

if [[ "$DRY_RUN" -eq 0 ]]; then
    sed -i "s/^sha256sums=.*/sha256sums=('$SHA256')/" PKGBUILD
    echo "==> Updated sha256sums in PKGBUILD"
else
    echo "==> [dry-run] would update sha256sums in PKGBUILD"
fi

# ---------------------------------------------------------------------------
# Regenerate man page
# ---------------------------------------------------------------------------

echo "==> Regenerating man page"
if [[ "$DRY_RUN" -eq 0 ]]; then
    make man
else
    echo "    [dry-run] would run: make man"
fi

# ---------------------------------------------------------------------------
# Generate .SRCINFO for stable PKGBUILD
# ---------------------------------------------------------------------------

echo "==> Generating .SRCINFO"
if [[ "$DRY_RUN" -eq 0 ]]; then
    makepkg --printsrcinfo > .SRCINFO
else
    echo "    [dry-run] would run: makepkg --printsrcinfo > .SRCINFO"
fi

# ---------------------------------------------------------------------------
# Generate .SRCINFO-git for VCS PKGBUILD
# ---------------------------------------------------------------------------

echo "==> Generating .SRCINFO-git"
TMPDIR_GIT=$(mktemp -d)
trap 'rm -rf "$TMPDIR_GIT"' EXIT

if [[ "$DRY_RUN" -eq 0 ]]; then
    cp PKGBUILD-git "$TMPDIR_GIT/PKGBUILD"
    (cd "$TMPDIR_GIT" && makepkg --printsrcinfo > .SRCINFO)
    cp "$TMPDIR_GIT/.SRCINFO" .SRCINFO-git
else
    echo "    [dry-run] would run: makepkg --printsrcinfo > .SRCINFO-git (via tmpdir)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "Done. Files ready:"
echo "  man/sysforge.1 — regenerated from current CLI"
echo "  PKGBUILD       — sha256sums updated"
echo "  .SRCINFO       — for AUR package 'sysforge'"
echo "  .SRCINFO-git   — for AUR package 'sysforge-git'"
echo ""
echo "Push to AUR:"
echo ""
echo "  # sysforge (stable)"
echo "  git clone ssh://aur@aur.archlinux.org/sysforge.git /tmp/aur-sysforge"
echo "  cp PKGBUILD .SRCINFO /tmp/aur-sysforge/"
echo "  cd /tmp/aur-sysforge && git add -A && git commit -m 'Update to $VERSION' && git push"
echo ""
echo "  # sysforge-git (VCS)"
echo "  git clone ssh://aur@aur.archlinux.org/sysforge-git.git /tmp/aur-sysforge-git"
echo "  cp PKGBUILD-git /tmp/aur-sysforge-git/PKGBUILD"
echo "  cp .SRCINFO-git /tmp/aur-sysforge-git/.SRCINFO"
echo "  cd /tmp/aur-sysforge-git && git add -A && git commit -m 'Update to $VERSION' && git push"
