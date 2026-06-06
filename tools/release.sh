#!/usr/bin/env bash
# tools/release.sh — automated sysforge release
#
# Usage:
#   bash tools/release.sh --bump=<major|minor|patch> [--skip-chroot] [--dry-run]
#
# Driven by `make release-major | release-minor | release-patch`.
#
# Phase 1: bump versions across files, regen uv.lock + man, single commit, tag.
# Phase 2: pause for `git push origin main && git push origin vNEW`.
# Phase 3: fetch sha256, update PKGBUILD, chroot validate, regen .SRCINFOs,
#          second commit (PKGBUILD only — .SRCINFOs are gitignored).
# Phase 4: print final push + AUR instructions.
#
# If interrupted between phases, re-run the same command. Resume is detected
# automatically when the local tag for the current pyproject.toml version
# already exists at HEAD.
#
# Env:
#   SYSFORGE_CHROOT — override chroot root (default /var/lib/archbuild/extra-x86_64)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

BUMP=""
DRY_RUN=0
SKIP_CHROOT=0

usage() {
    cat <<EOF
Usage: bash tools/release.sh --bump=<major|minor|patch> [--skip-chroot] [--dry-run]

Required:
  --bump=major   X.Y.Z -> (X+1).0.0
  --bump=minor   X.Y.Z -> X.(Y+1).0
  --bump=patch   X.Y.Z -> X.Y.(Z+1)

Options:
  --skip-chroot  Skip clean-chroot validation (only for iterating on this script).
  --dry-run      Walk through every step without writing files, committing,
                 hitting the network, or running the chroot build. Implies
                 --skip-chroot.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --bump=major|--bump=minor|--bump=patch) BUMP="${arg#--bump=}" ;;
        --skip-chroot) SKIP_CHROOT=1 ;;
        --dry-run)     DRY_RUN=1; SKIP_CHROOT=1 ;;
        -h|--help)     usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "$BUMP" ]]; then
    echo "ERROR: --bump=<major|minor|patch> is required" >&2
    usage >&2
    exit 2
fi

CHROOT_ROOT="${SYSFORGE_CHROOT:-/var/lib/archbuild/extra-x86_64}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

bump_version() {
    local cur="$1" kind="$2"
    local x y z
    IFS='.' read -r x y z <<<"$cur"
    case "$kind" in
        major) echo "$((x+1)).0.0" ;;
        minor) echo "$x.$((y+1)).0" ;;
        patch) echo "$x.$y.$((z+1))" ;;
        *) echo "ERROR: bad bump kind: $kind" >&2; exit 2 ;;
    esac
}

read_pyproject_version() {
    python3 -c "
import tomllib
with open('pyproject.toml', 'rb') as f:
    d = tomllib.load(f)
print(d['project']['version'])
"
}

regex_escape() {
    # Escape dots and slashes for use in a sed regex.
    printf '%s' "$1" | sed 's/[.\/]/\\&/g'
}

chroot_build() {
    local pkgbuild_src="$1" label="$2"
    local scratch
    scratch=$(mktemp -d)
    cp "$pkgbuild_src" "$scratch/PKGBUILD"
    echo "    building $label in $CHROOT_ROOT (scratch: $scratch)"
    (cd "$scratch" && PKGDEST="$scratch" SRCPKGDEST="$scratch" LOGDEST="$scratch" \
        makechrootpkg -c -u -r "$CHROOT_ROOT")
    if ! compgen -G "$scratch"/*.pkg.tar.zst > /dev/null; then
        echo "ERROR: chroot build of $label produced no package" >&2
        rm -rf "$scratch"
        exit 1
    fi
    rm -rf "$scratch"
    echo "    $label: chroot build OK"
}

# ---------------------------------------------------------------------------
# Phase 0a: read current version, decide fresh vs resume
# ---------------------------------------------------------------------------

CUR="$(read_pyproject_version)"
NEW="$(bump_version "$CUR" "$BUMP")"
TAG="v$NEW"
RESUME=0

# If a local tag for the *current* pyproject.toml version points to HEAD,
# Phase 1 has already run (this is a resume after a Ctrl-C at Phase 2).
if git rev-parse --quiet --verify "v$CUR" >/dev/null 2>&1; then
    if [[ "$(git rev-parse "v$CUR")" == "$(git rev-parse HEAD)" ]]; then
        RESUME=1
        NEW="$CUR"
        TAG="v$NEW"
    fi
fi

# ---------------------------------------------------------------------------
# Phase 0b: pre-flight
# ---------------------------------------------------------------------------

preflight_common() {
    local branch
    branch="$(git rev-parse --abbrev-ref HEAD)"
    if [[ "$branch" != "main" ]]; then
        echo "ERROR: not on main (currently on '$branch')" >&2
        exit 1
    fi
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo "ERROR: working tree not clean. Commit or stash first." >&2
        exit 1
    fi
    if [[ "$SKIP_CHROOT" -eq 0 ]]; then
        if ! command -v makechrootpkg >/dev/null 2>&1; then
            echo "ERROR: makechrootpkg not found. Install: sudo pacman -S --needed devtools" >&2
            exit 1
        fi
        if [[ ! -d "$CHROOT_ROOT/root" ]]; then
            echo "ERROR: chroot $CHROOT_ROOT/root missing. Create it once:" >&2
            echo "  sudo mkarchroot $CHROOT_ROOT/root base-devel" >&2
            exit 1
        fi
    fi
}

preflight_fresh() {
    preflight_common
    if git rev-parse --quiet --verify "$TAG" >/dev/null 2>&1; then
        echo "ERROR: tag $TAG already exists locally" >&2
        exit 1
    fi
    # Fetch latest tags from origin so the duplicate-on-remote check is honest.
    git fetch --tags origin >/dev/null 2>&1 || true
    if git ls-remote --tags origin "refs/tags/$TAG" 2>/dev/null | grep -q "refs/tags/$TAG$"; then
        echo "ERROR: tag $TAG already exists on origin" >&2
        exit 1
    fi
    # Shipped-file consistency gate. Validates every shipped TOML schema,
    # PKGBUILD install graph, hook->helper parity, completions<->CLI parity,
    # version markers (subsumes the prior README/DESIGN grep checks), and
    # man-page freshness. Hard fail — nothing in Phase 1 rewrites a file
    # unless this passes.
    if ! make --no-print-directory check-shipped >&2; then
        echo "ERROR: shipped-file checks failed — fix the findings above and re-run." >&2
        exit 1
    fi
    # De-personalization gate. Fails if personal identity/path tokens leak into
    # the published surface (docs, source comments, shipped configs). Legitimate
    # attribution (copyright/maintainer/--author) is allowed.
    if ! make --no-print-directory check-personal >&2; then
        echo "ERROR: de-personalization checks failed — fix the findings above and re-run." >&2
        exit 1
    fi
    # DESIGN.md drift gate. DESIGN.md is generated from docs/design/; fail if the
    # committed copy is stale (mirrors the manpage freshness check).
    if ! make --no-print-directory check-design >&2; then
        echo "ERROR: DESIGN.md is stale — run 'make design' and commit the result." >&2
        exit 1
    fi
}

preflight_resume() {
    preflight_common
}

if [[ "$RESUME" -eq 1 ]]; then
    preflight_resume
else
    preflight_fresh
fi

# ---------------------------------------------------------------------------
# Phase 0c: print summary, prompt for approval
# ---------------------------------------------------------------------------

CHROOT_NOTE=""
if [[ "$SKIP_CHROOT" -eq 1 ]]; then
    CHROOT_NOTE=" — SKIPPED"
fi

if [[ "$RESUME" -eq 1 ]]; then
    cat <<EOF
==> sysforge release: resuming v$NEW (Phase 1 already complete)

Tag $TAG is present at HEAD; pyproject.toml + PKGBUILD + docs already bumped.
This run will:

  Phase 2: verify $TAG is on origin (otherwise pause for manual push)
  Phase 3:
    - fetch sha256 from https://github.com/KeithRaghubar/sysforge/archive/$TAG.tar.gz
    - update sha256sums in PKGBUILD
    - clean-chroot validate PKGBUILD and PKGBUILD-git ($CHROOT_ROOT)$CHROOT_NOTE
    - regenerate .SRCINFO and .SRCINFO-git (gitignored — local artifacts for AUR push)
    - git add PKGBUILD; git commit -m "release: $TAG sha256"
  Phase 4: print final push + AUR push instructions
EOF
else
    cat <<EOF
==> sysforge release: $CUR -> $NEW ($BUMP bump, tag $TAG)

Phase 1 — bump, commit, tag:
  - rewrite pyproject.toml         version $CUR -> $NEW
  - rewrite PKGBUILD               pkgver $CUR -> $NEW
  - rewrite PKGBUILD-git           pkgver $CUR.r0.g0000000 -> $NEW.r0.g0000000
  - rewrite README.md, DESIGN.md   <!--version-->v$NEW<!--/version-->
  - regenerate uv.lock             (uv lock)
  - regenerate man/sysforge.1      (make man)
  - git add pyproject.toml PKGBUILD PKGBUILD-git README.md DESIGN.md uv.lock man/sysforge.1
  - git commit -m "release: $TAG"
  - git tag $TAG

Phase 2 — manual push pause:
  - prompts you to run: git push origin main && git push origin $TAG
  - resumes once $TAG is visible on origin

Phase 3 — post-tag artifacts:
  - fetch sha256 from https://github.com/KeithRaghubar/sysforge/archive/$TAG.tar.gz
  - update sha256sums in PKGBUILD
  - clean-chroot validate PKGBUILD and PKGBUILD-git ($CHROOT_ROOT)$CHROOT_NOTE
  - regenerate .SRCINFO and .SRCINFO-git (gitignored — local artifacts for AUR push)
  - git add PKGBUILD; git commit -m "release: $TAG sha256"

Phase 4 — final instructions:
  - print: git push origin main, AUR clone+copy+commit+push commands

EOF
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[--dry-run] no actions will be executed; proceeding through walk-through."
fi

read -r -p "Proceed? [y/N]: " ans
case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 0 ;;
esac

# ---------------------------------------------------------------------------
# Phase 1: bump, commit, tag
# ---------------------------------------------------------------------------

if [[ "$RESUME" -eq 0 ]]; then
    echo
    echo "==> Phase 1: bump, commit, tag"

    CUR_RE="$(regex_escape "$CUR")"

    # pyproject.toml
    if [[ "$DRY_RUN" -eq 0 ]]; then
        sed -i -E "s/^version = \"$CUR_RE\"$/version = \"$NEW\"/" pyproject.toml
        if ! grep -q "^version = \"$NEW\"$" pyproject.toml; then
            echo "ERROR: failed to update pyproject.toml version" >&2
            exit 1
        fi
    fi
    echo "    pyproject.toml: version $CUR -> $NEW"

    # PKGBUILD pkgver
    if [[ "$DRY_RUN" -eq 0 ]]; then
        sed -i -E "s/^pkgver=$CUR_RE$/pkgver=$NEW/" PKGBUILD
        if ! grep -q "^pkgver=$NEW$" PKGBUILD; then
            echo "ERROR: failed to update PKGBUILD pkgver" >&2
            exit 1
        fi
    fi
    echo "    PKGBUILD: pkgver $CUR -> $NEW"

    # PKGBUILD-git pkgver — only the leading X.Y.Z portion, suffix preserved.
    if [[ "$DRY_RUN" -eq 0 ]]; then
        sed -i -E "s/^pkgver=$CUR_RE(\\.r[0-9]+\\.g[0-9a-f]+.*)$/pkgver=$NEW\\1/" PKGBUILD-git
        if ! grep -q "^pkgver=$NEW\\." PKGBUILD-git; then
            echo "ERROR: failed to update PKGBUILD-git pkgver" >&2
            exit 1
        fi
    fi
    echo "    PKGBUILD-git: pkgver $CUR -> $NEW (suffix preserved)"

    # README.md and DESIGN.md markers
    if [[ "$DRY_RUN" -eq 0 ]]; then
        sed -i -E "s|<!--version-->v[0-9]+\\.[0-9]+\\.[0-9]+<!--/version-->|<!--version-->v$NEW<!--/version-->|g" README.md DESIGN.md
    fi
    echo "    README.md, DESIGN.md: marker token -> v$NEW"

    # uv.lock
    if command -v uv >/dev/null 2>&1; then
        echo "    \$ uv lock"
        if [[ "$DRY_RUN" -eq 0 ]]; then
            uv lock
        fi
    else
        echo "WARN: uv not on PATH — skipping uv.lock regen" >&2
    fi

    # man page
    echo "    \$ make man"
    if [[ "$DRY_RUN" -eq 0 ]]; then
        make man >/dev/null
    fi

    # Commit + tag
    if [[ "$DRY_RUN" -eq 0 ]]; then
        git add pyproject.toml PKGBUILD PKGBUILD-git README.md DESIGN.md uv.lock man/sysforge.1
        git commit -m "release: $TAG"
        git tag "$TAG"
        echo "    committed: release: $TAG"
        echo "    tagged:    $TAG"
    else
        echo "    [dry-run] git add pyproject.toml PKGBUILD PKGBUILD-git README.md DESIGN.md uv.lock man/sysforge.1"
        echo "    [dry-run] git commit -m \"release: $TAG\""
        echo "    [dry-run] git tag $TAG"
    fi
fi

# ---------------------------------------------------------------------------
# Phase 2: pause for manual push
# ---------------------------------------------------------------------------

TAG_ON_REMOTE=0
if git ls-remote --tags origin "refs/tags/$TAG" 2>/dev/null | grep -q "refs/tags/$TAG$"; then
    TAG_ON_REMOTE=1
fi

if [[ "$TAG_ON_REMOTE" -eq 0 ]]; then
    echo
    echo "==> Phase 2: manual push required"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "    [dry-run] would prompt for: git push origin main && git push origin $TAG"
    else
        echo "    Run in another shell:"
        echo
        echo "        git push origin main && git push origin $TAG"
        echo
        read -r -p "    Press ENTER once pushed (or Ctrl-C to abort): " _
        # Verify (one retry).
        for _attempt in 1 2; do
            if git ls-remote --tags origin "refs/tags/$TAG" 2>/dev/null | grep -q "refs/tags/$TAG$"; then
                TAG_ON_REMOTE=1
                break
            fi
            sleep 2
        done
        if [[ "$TAG_ON_REMOTE" -eq 0 ]]; then
            echo "ERROR: $TAG not visible on origin." >&2
            echo "       Push it (git push origin main && git push origin $TAG) and re-run this script." >&2
            exit 1
        fi
        echo "    $TAG visible on origin."
    fi
else
    echo
    echo "==> Phase 2: skipped — $TAG already on origin"
fi

# ---------------------------------------------------------------------------
# Phase 3: sha256, chroot, .SRCINFO
# ---------------------------------------------------------------------------

echo
echo "==> Phase 3: post-tag artifacts"

TARBALL_URL="https://github.com/KeithRaghubar/sysforge/archive/$TAG.tar.gz"
echo "    Fetching sha256 from $TARBALL_URL"

if [[ "$DRY_RUN" -eq 1 ]]; then
    SHA256="DRYRUN0000000000000000000000000000000000000000000000000000000000"
    echo "    [dry-run] would fetch; using placeholder $SHA256"
else
    SHA256=$(curl -fsSL "$TARBALL_URL" | sha256sum | awk '{print $1}')
    echo "    sha256: $SHA256"
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
    sed -i "s/^sha256sums=.*/sha256sums=('$SHA256')/" PKGBUILD
    echo "    PKGBUILD sha256sums updated"
else
    echo "    [dry-run] would update sha256sums in PKGBUILD"
fi

# Chroot validation
if [[ "$SKIP_CHROOT" -eq 0 ]]; then
    echo "    Validating PKGBUILDs in clean chroot ($CHROOT_ROOT)"
    chroot_build PKGBUILD     "PKGBUILD (stable)"
    chroot_build PKGBUILD-git "PKGBUILD-git (VCS)"
else
    echo "    [--skip-chroot or --dry-run] skipping clean-chroot validation"
fi

# .SRCINFO (stable)
echo "    Generating .SRCINFO"
if [[ "$DRY_RUN" -eq 0 ]]; then
    makepkg --printsrcinfo > .SRCINFO
else
    echo "    [dry-run] makepkg --printsrcinfo > .SRCINFO"
fi

# .SRCINFO-git via tmpdir
echo "    Generating .SRCINFO-git"
if [[ "$DRY_RUN" -eq 0 ]]; then
    TMPDIR_GIT=$(mktemp -d)
    trap 'rm -rf "$TMPDIR_GIT"' EXIT
    cp PKGBUILD-git "$TMPDIR_GIT/PKGBUILD"
    (cd "$TMPDIR_GIT" && makepkg --printsrcinfo > .SRCINFO)
    cp "$TMPDIR_GIT/.SRCINFO" .SRCINFO-git
else
    echo "    [dry-run] makepkg --printsrcinfo > .SRCINFO-git (via tmpdir)"
fi

# Second commit — only PKGBUILD has tracked changes (.SRCINFOs are gitignored).
if [[ "$DRY_RUN" -eq 0 ]]; then
    if ! git diff --quiet PKGBUILD; then
        git add PKGBUILD
        git commit -m "release: $TAG sha256"
        echo "    committed: release: $TAG sha256"
    else
        echo "    PKGBUILD unchanged — nothing to commit"
    fi
else
    echo "    [dry-run] git add PKGBUILD"
    echo "    [dry-run] git commit -m \"release: $TAG sha256\""
fi

# ---------------------------------------------------------------------------
# Phase 4: final instructions
# ---------------------------------------------------------------------------

cat <<EOF

==> Phase 4: done. Final manual steps:

1. Push the sha256 commit:

    git push origin main

2. Push to AUR (sysforge stable):

    git clone ssh://aur@aur.archlinux.org/sysforge.git /tmp/aur-sysforge
    cp PKGBUILD .SRCINFO /tmp/aur-sysforge/
    cd /tmp/aur-sysforge && git add -A && git commit -m "Update to $NEW" && git push

3. Push to AUR (sysforge-git VCS):

    git clone ssh://aur@aur.archlinux.org/sysforge-git.git /tmp/aur-sysforge-git
    cp PKGBUILD-git /tmp/aur-sysforge-git/PKGBUILD
    cp .SRCINFO-git /tmp/aur-sysforge-git/.SRCINFO
    cd /tmp/aur-sysforge-git && git add -A && git commit -m "Update to $NEW" && git push
EOF
