#!/usr/bin/env bash
# tools/release.sh — automated sysforge release
#
# Usage:
#   bash tools/release.sh --bump=<major|minor|patch> [--skip-chroot] [--dry-run]
#   bash tools/release.sh --resume [--skip-chroot] [--dry-run]
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
# already exists at HEAD. When a failure *after* the tag needed fix commits on
# top of the release commit (so the tag is no longer at HEAD), auto-detection
# cannot fire — pass --resume explicitly to re-enter at Phase 3 for the
# current version (the tag must be an ancestor of a clean HEAD).
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
RESUME_REQ=0

usage() {
    cat <<EOF
Usage: bash tools/release.sh --bump=<major|minor|patch> [--skip-chroot] [--dry-run]
       bash tools/release.sh --resume [--skip-chroot] [--dry-run]

Required (exactly one):
  --bump=major   X.Y.Z -> (X+1).0.0
  --bump=minor   X.Y.Z -> X.(Y+1).0
  --bump=patch   X.Y.Z -> X.Y.(Z+1)
  --resume       Finish the in-flight release for the *current* pyproject.toml
                 version: no bump, re-enter at Phase 3 (sha256/chroot/.SRCINFO).
                 For when a post-tag failure needed fix commits on top of the
                 release commit; requires tag v<current> to be an ancestor of
                 a clean HEAD.

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
        --resume)      RESUME_REQ=1 ;;
        --skip-chroot) SKIP_CHROOT=1 ;;
        --dry-run)     DRY_RUN=1; SKIP_CHROOT=1 ;;
        -h|--help)     usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ "$RESUME_REQ" -eq 1 && -n "$BUMP" ]]; then
    echo "ERROR: --resume finishes the current version; it cannot be combined with --bump" >&2
    usage >&2
    exit 2
fi
if [[ "$RESUME_REQ" -eq 0 && -z "$BUMP" ]]; then
    echo "ERROR: --bump=<major|minor|patch> (or --resume) is required" >&2
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
    # install= scriptlet must sit beside the PKGBUILD or makepkg refuses to run.
    cp sysforge.install "$scratch/"
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
RESUME=0

if [[ "$RESUME_REQ" -eq 1 ]]; then
    # Explicit resume (2.0.0-B1): finish the current version even when fix
    # commits landed on top of the release commit, so the tag is no longer at
    # HEAD. An *ancestor* check alone can't drive auto-detection (every
    # completed release's tag is also an ancestor of HEAD), hence the flag.
    NEW="$CUR"
    TAG="v$NEW"
    if ! git rev-parse --quiet --verify "$TAG" >/dev/null 2>&1; then
        echo "ERROR: --resume: tag $TAG (current pyproject.toml version) does not exist —" >&2
        echo "       nothing to resume; Phase 1 has not run for v$CUR." >&2
        exit 1
    fi
    if ! git merge-base --is-ancestor "$TAG^{commit}" HEAD; then
        echo "ERROR: --resume: tag $TAG is not an ancestor of HEAD —" >&2
        echo "       HEAD does not contain the v$CUR release commit." >&2
        exit 1
    fi
    RESUME=1
else
    NEW="$(bump_version "$CUR" "$BUMP")"
    TAG="v$NEW"

    # If a local tag for the *current* pyproject.toml version points to HEAD,
    # Phase 1 has already run (this is a resume after a Ctrl-C at Phase 2).
    if git rev-parse --quiet --verify "v$CUR" >/dev/null 2>&1; then
        # Resolve through the tag object to the commit ("^{commit}") — a signed/
        # annotated tag is its own object, so a bare `git rev-parse v$CUR` would
        # return the tag SHA (never == HEAD) and silently defeat resume.
        if [[ "$(git rev-parse "v$CUR^{commit}")" == "$(git rev-parse HEAD)" ]]; then
            RESUME=1
            NEW="$CUR"
            TAG="v$NEW"
        fi
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
    # Signing preflight — every release tag, commit, and tarball is GPG-signed,
    # and the published PKGBUILD verifies that signature via validpgpkeys. Fail
    # before any file is rewritten if the key is not usable, so we can never cut
    # an *unsigned* release. (Skipped on --dry-run: no commit/tag/sign happens.)
    if [[ "$DRY_RUN" -eq 0 ]]; then
        local signingkey
        signingkey="$(git config --get user.signingkey || true)"
        if [[ -z "$signingkey" ]]; then
            echo "ERROR: git user.signingkey is unset — release tags/commits must be signed." >&2
            echo "       Set it once: git config --global user.signingkey <KEYID>" >&2
            exit 1
        fi
        if [[ "$(git config --get commit.gpgsign || true)" != "true" ]]; then
            echo "ERROR: git commit.gpgsign is not 'true' — the release commits would be unsigned." >&2
            echo "       Enable it: git config --global commit.gpgsign true" >&2
            exit 1
        fi
        if ! gpg --list-secret-keys "$signingkey" >/dev/null 2>&1; then
            echo "ERROR: no usable GPG secret key for '$signingkey' (git user.signingkey)." >&2
            echo "       Verify with: gpg --list-secret-keys $signingkey" >&2
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
    # Signing trust-anchor gate. The stable PKGBUILD must carry the real
    # maintainer key fingerprint (not the shipped sentinel) so installers can
    # verify the release signature this run uploads.
    if grep -q "REPLACE_WITH_MAINTAINER_KEY_FINGERPRINT" PKGBUILD; then
        echo "ERROR: PKGBUILD validpgpkeys still holds the placeholder sentinel." >&2
        echo "       Replace it with your key fingerprint: gpg --fingerprint" >&2
        exit 1
    fi
    # Release-notes gate. Notes are authored incrementally in the running
    # accumulator (docs/release-notes/unreleased.md); Phase 1 renames it to
    # $TAG.md, stamps the title, and reseeds a fresh accumulator. Require the
    # accumulator to exist and hold at least one authored Keep a Changelog
    # section — otherwise the release would ship empty notes.
    if [[ ! -f "docs/release-notes/unreleased.md" ]]; then
        echo "ERROR: docs/release-notes/unreleased.md is missing." >&2
        echo "       It is the running accumulator — restore it (git checkout) or reseed it." >&2
        exit 1
    fi
    if ! grep -q '^## ' "docs/release-notes/unreleased.md"; then
        echo "ERROR: docs/release-notes/unreleased.md has no authored entries." >&2
        echo "       Landing commits should append entries as items ship; run /release-notes" >&2
        echo "       to reconcile/lint the accumulator before releasing." >&2
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
    # Standards-compliance gate. Validates the mechanically-checkable committed
    # standards (XDG/FHS paths, SPDX/REUSE licensing, Keep a Changelog headings,
    # UTF-8 encoding) documented in docs/design/21-standards.md.
    if ! make --no-print-directory check-standards >&2; then
        echo "ERROR: standards checks failed — fix the findings above and re-run." >&2
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
  - stamp + rename release notes   docs/release-notes/unreleased.md -> $TAG.md (title dated)
  - reseed accumulator             fresh docs/release-notes/unreleased.md
  - git add pyproject.toml PKGBUILD PKGBUILD-git README.md DESIGN.md docs/design/00-header.md uv.lock man/sysforge.1 docs/release-notes/$TAG.md docs/release-notes/unreleased.md
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

    # README.md and DESIGN.md markers. DESIGN.md is generated from
    # docs/design/, so its marker's *source* (00-header.md) must be rewritten
    # in the same pass — otherwise the next `make design` reverts the marker.
    if [[ "$DRY_RUN" -eq 0 ]]; then
        sed -i -E "s|<!--version-->v[0-9]+\\.[0-9]+\\.[0-9]+<!--/version-->|<!--version-->v$NEW<!--/version-->|g" README.md DESIGN.md docs/design/00-header.md
    fi
    echo "    README.md, DESIGN.md, docs/design/00-header.md: marker token -> v$NEW"

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

    # Release notes: rename the running accumulator to the versioned file, stamp
    # its `# ` title with the version + ISO date, and reseed a fresh accumulator
    # for the next cycle. Notes were authored incrementally into unreleased.md,
    # so nothing is reconstructed here.
    RELEASE_DATE="$(date +%F)"
    if [[ "$DRY_RUN" -eq 0 ]]; then
        git mv "docs/release-notes/unreleased.md" "docs/release-notes/$TAG.md"
        # Rewrite the first `# ` heading to the dated versioned title.
        sed -i -E "0,/^# .*/s//# sysforge $TAG — $RELEASE_DATE/" "docs/release-notes/$TAG.md"
        cat > "docs/release-notes/unreleased.md" <<'UNRELEASED_EOF'
# sysforge (unreleased)

<!--
Running accumulator for the next release. Every landing commit that COMPLETES a
ROADMAP item appends its entry here (in the same commit that drops the item from
ROADMAP.md), under the matching Keep a Changelog section — one of Added, Changed,
Deprecated, Removed, Fixed, Security, in that order. Reference the roadmap ID
inline, e.g. (1.2.0-F35). Flag breaking changes with a **Breaking:** prefix and
the migration path. At release time tools/release.sh (Phase 1) renames this file
to vX.Y.Z.md, stamps the `# ` title with the version and date, and reseeds a fresh
accumulator. Run the release-notes skill first to reconcile/lint the entries and
finalize the one-line summary below (drop this comment). Keep a Changelog:
https://keepachangelog.com/en/1.1.0/
-->
UNRELEASED_EOF
    fi
    echo "    release notes: unreleased.md -> $TAG.md (title: sysforge $TAG — $RELEASE_DATE); accumulator reseeded"

    # Commit + tag
    if [[ "$DRY_RUN" -eq 0 ]]; then
        git add pyproject.toml PKGBUILD PKGBUILD-git README.md DESIGN.md docs/design/00-header.md uv.lock man/sysforge.1 "docs/release-notes/$TAG.md" "docs/release-notes/unreleased.md"
        git commit -m "release: $TAG"
        # Signed annotated tag — the release's authenticity anchor. `git tag -v`
        # fails (and aborts the release) if the signature does not verify.
        git tag -s "$TAG" -m "sysforge $TAG"
        git tag -v "$TAG" >/dev/null
        echo "    committed: release: $TAG (signed)"
        echo "    tagged:    $TAG (signed, verified)"
    else
        echo "    [dry-run] git mv docs/release-notes/unreleased.md docs/release-notes/$TAG.md (title stamped) + reseed accumulator"
        echo "    [dry-run] git add pyproject.toml PKGBUILD PKGBUILD-git README.md DESIGN.md uv.lock man/sysforge.1 docs/release-notes/$TAG.md docs/release-notes/unreleased.md"
        echo "    [dry-run] git commit -m \"release: $TAG\"  (commit.gpgsign)"
        echo "    [dry-run] git tag -s $TAG -m \"sysforge $TAG\" && git tag -v $TAG"
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
# Release artifacts (signed tarball, SHA256SUMS) are staged here and uploaded to
# the GitHub release in Phase 4. Kept until script exit so Phase 4 can read them.
RELEASE_STAGE="$(mktemp -d)"
trap 'rm -rf "$RELEASE_STAGE"' EXIT
ASSET_TARBALL="$RELEASE_STAGE/sysforge-$NEW.tar.gz"
ASSET_SIG="$ASSET_TARBALL.asc"
echo "    Fetching tarball + signing from $TARBALL_URL"

if [[ "$DRY_RUN" -eq 1 ]]; then
    SHA256="DRYRUN0000000000000000000000000000000000000000000000000000000000"
    echo "    [dry-run] would fetch tarball, sha256, and GPG-sign it"
    echo "    [dry-run] gpg --detach-sign --armor -o $ASSET_SIG <tarball>"
else
    # Download the exact bytes GitHub serves for this tag, then hash AND sign
    # that same file — the sha256 pins integrity, the detached .asc proves it
    # came from the maintainer key (verified downstream via PKGBUILD validpgpkeys).
    curl -fsSL "$TARBALL_URL" -o "$ASSET_TARBALL"
    SHA256=$(sha256sum "$ASSET_TARBALL" | awk '{print $1}')
    echo "    sha256: $SHA256"
    gpg --detach-sign --armor --yes -o "$ASSET_SIG" "$ASSET_TARBALL"
    gpg --verify "$ASSET_SIG" "$ASSET_TARBALL" >/dev/null 2>&1 \
        || { echo "ERROR: GPG signature of the release tarball failed to verify" >&2; exit 1; }
    # SHA256SUMS (+ its own detached signature) for non-makepkg consumers.
    ( cd "$RELEASE_STAGE" && sha256sum "sysforge-$NEW.tar.gz" > SHA256SUMS \
        && gpg --detach-sign --armor --yes -o SHA256SUMS.asc SHA256SUMS )
    echo "    signed: sysforge-$NEW.tar.gz.asc, SHA256SUMS.asc"
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
    # Two-element sha256sums: the tarball hash, then SKIP for the detached
    # signature source (GPG-verified against validpgpkeys, not hashed).
    # Replace the whole array (it may span multiple lines from a prior release),
    # from the sha256sums= line through its closing paren.
    awk -v sha="$SHA256" '
        /^sha256sums=/ && !done {
            printf "sha256sums=(%c%s%c\n            %cSKIP%c)\n", 39, sha, 39, 39, 39
            done = 1
            if ($0 ~ /\)[[:space:]]*$/) next
            skipping = 1; next
        }
        skipping { if ($0 ~ /\)[[:space:]]*$/) skipping = 0; next }
        { print }
    ' PKGBUILD > PKGBUILD.sha256.tmp && mv PKGBUILD.sha256.tmp PKGBUILD
    echo "    PKGBUILD sha256sums updated (tarball + SKIP for .asc)"
else
    echo "    [dry-run] would update sha256sums in PKGBUILD (tarball hash + SKIP)"
fi

# GitHub release — created BEFORE chroot validation because the stable PKGBUILD's
# detached-signature source points at this release's download URL; makepkg (run by
# makechrootpkg below) must be able to fetch sysforge-$NEW.tar.gz.asc to verify it.
echo "    Publishing GitHub release $TAG with signed artifacts"
if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "    [dry-run] gh release create $TAG --title \"sysforge $TAG\" \\"
    echo "    [dry-run]   --notes-file docs/release-notes/$TAG.md \\"
    echo "    [dry-run]   $ASSET_SIG SHA256SUMS SHA256SUMS.asc"
elif ! command -v gh >/dev/null 2>&1; then
    echo "ERROR: gh CLI not found — needed to publish the release + upload the signature." >&2
    echo "       Install: sudo pacman -S --needed github-cli && gh auth login" >&2
    exit 1
else
    if gh release view "$TAG" >/dev/null 2>&1; then
        # Idempotent resume: release already exists, just (re)upload the assets.
        echo "    release $TAG exists — uploading assets (--clobber)"
        gh release upload "$TAG" --clobber \
            "$ASSET_SIG" "$RELEASE_STAGE/SHA256SUMS" "$RELEASE_STAGE/SHA256SUMS.asc"
    else
        gh release create "$TAG" --title "sysforge $TAG" \
            --notes-file "docs/release-notes/$TAG.md" \
            "$ASSET_SIG" "$RELEASE_STAGE/SHA256SUMS" "$RELEASE_STAGE/SHA256SUMS.asc"
    fi
    echo "    release published: $TARBALL_URL → asset sysforge-$NEW.tar.gz.asc"
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
    # Clean up inline rather than via an EXIT trap — the RELEASE_STAGE trap set
    # in the sha256/sign step owns EXIT, and a second `trap ... EXIT` would clobber it.
    TMPDIR_GIT=$(mktemp -d)
    cp PKGBUILD-git "$TMPDIR_GIT/PKGBUILD"
    cp sysforge.install "$TMPDIR_GIT/"
    (cd "$TMPDIR_GIT" && makepkg --printsrcinfo > .SRCINFO)
    cp "$TMPDIR_GIT/.SRCINFO" .SRCINFO-git
    rm -rf "$TMPDIR_GIT"
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

==> Phase 4: done. The signed GitHub release $TAG is already published
    (tag, commits, and release tarball are GPG-signed). Final manual steps:

1. Push the sha256 commit:

    git push origin main

2. Push to AUR (sysforge stable):

    git clone ssh://aur@aur.archlinux.org/sysforge.git /tmp/aur-sysforge
    cp PKGBUILD .SRCINFO sysforge.install /tmp/aur-sysforge/
    cd /tmp/aur-sysforge && git add -A && git commit -m "Update to $NEW" && git push

3. Push to AUR (sysforge-git VCS):

    git clone ssh://aur@aur.archlinux.org/sysforge-git.git /tmp/aur-sysforge-git
    cp PKGBUILD-git /tmp/aur-sysforge-git/PKGBUILD
    cp .SRCINFO-git /tmp/aur-sysforge-git/.SRCINFO
    cp sysforge.install /tmp/aur-sysforge-git/
    cd /tmp/aur-sysforge-git && git add -A && git commit -m "Update to $NEW" && git push
EOF
