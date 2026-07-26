#!/usr/bin/env bash
# release-prep pre-flight: validate sysforge release readiness.
#
# Run from the skill via:  bash .claude/skills/release-prep/scripts/preflight.sh
#
# Exits 0 when there are no [FAIL]s (warnings allowed), non-zero otherwise.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
# `|| exit`: with set -u but no -e, a failed cd would silently run every check
# against the caller's directory and report a green preflight for the wrong tree.
cd "$REPO_ROOT" || exit 1

PASS=0
WARN=0
FAIL=0

pass() { printf '  [PASS] %s\n' "$1"; PASS=$((PASS+1)); }
warn() { printf '  [WARN] %s\n' "$1"; WARN=$((WARN+1)); }
fail() { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL+1)); }

echo "==> sysforge release pre-flight ($(pwd))"
echo

# ---------------------------------------------------------------------------
# Section 1: branch + working tree
# ---------------------------------------------------------------------------
echo "Branch + working tree"

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
if [[ "$BRANCH" == "main" ]]; then
    pass "on main branch"
else
    fail "not on main branch (currently on '$BRANCH')"
fi

if git diff --quiet 2>/dev/null && git diff --cached --quiet 2>/dev/null; then
    pass "working tree clean"
else
    fail "working tree not clean (commit or stash before releasing)"
fi

echo

# ---------------------------------------------------------------------------
# Section 2: version-marker consistency
# ---------------------------------------------------------------------------
echo "Version markers"

PYVER=""
if [[ -f pyproject.toml ]]; then
    PYVER="$(python3 -c "
import tomllib
with open('pyproject.toml','rb') as f: d=tomllib.load(f)
print(d['project']['version'])
" 2>/dev/null || true)"
fi

if [[ -z "$PYVER" ]]; then
    fail "could not read version from pyproject.toml"
else
    pass "pyproject.toml version: $PYVER"
fi

if [[ -n "$PYVER" ]]; then
    PKGVER="$(awk -F= '/^pkgver=/ {print $2; exit}' PKGBUILD 2>/dev/null || true)"
    if [[ "$PKGVER" == "$PYVER" ]]; then
        pass "PKGBUILD pkgver matches ($PKGVER)"
    else
        fail "PKGBUILD pkgver mismatch (PKGBUILD has '$PKGVER', pyproject.toml has '$PYVER')"
    fi

    PKGGITVER="$(awk -F= '/^pkgver=/ {print $2; exit}' PKGBUILD-git 2>/dev/null || true)"
    PKGGIT_PREFIX="${PKGGITVER%%.r*}"
    if [[ "$PKGGIT_PREFIX" == "$PYVER" ]]; then
        pass "PKGBUILD-git pkgver prefix matches ($PKGGITVER)"
    else
        fail "PKGBUILD-git pkgver prefix mismatch (has '$PKGGITVER', expected '$PYVER.r…')"
    fi

    for f in README.md DESIGN.md; do
        if [[ ! -f "$f" ]]; then
            fail "$f missing"
            continue
        fi
        if ! grep -q '<!--version-->v[0-9.]\+<!--/version-->' "$f"; then
            fail "$f has no <!--version-->...<!--/version--> marker"
            continue
        fi
        BAD=$(grep -oE '<!--version-->v[0-9.]+<!--/version-->' "$f" | grep -v "<!--version-->v$PYVER<!--/version-->" || true)
        if [[ -z "$BAD" ]]; then
            COUNT=$(grep -c '<!--version-->v[0-9.]\+<!--/version-->' "$f")
            pass "$f version marker(s) match ($COUNT occurrence(s) of v$PYVER)"
        else
            fail "$f version marker mismatch (expected v$PYVER, found: $(echo "$BAD" | tr '\n' ' '))"
        fi
    done
fi

echo

# ---------------------------------------------------------------------------
# Section 3: lint
# ---------------------------------------------------------------------------
echo "Lint"
if command -v ruff >/dev/null 2>&1; then
    if ruff check sysforge/ >/dev/null 2>&1; then
        pass "ruff check sysforge/ clean"
    else
        fail "ruff check failed (run: make lint)"
    fi
else
    warn "ruff not on PATH — install with: make dev-deps-core"
fi
if command -v shellcheck >/dev/null 2>&1; then
    # The shell tooling IS the release path (release.sh, build-pkg.sh, smoke.sh),
    # so a quoting bug here fails a release or, worse, verifies the wrong tree.
    if make -s lint-sh >/dev/null 2>&1; then
        pass "shellcheck clean (all tracked scripts + bash completion)"
    else
        fail "shellcheck failed (run: make lint-sh)"
    fi
else
    warn "shellcheck not on PATH — install with: make dev-deps-core"
fi
echo

# ---------------------------------------------------------------------------
# Section 4: completions/_sysforge in sync with CLI
# ---------------------------------------------------------------------------
echo "Completions sync"
COMP_FILE=completions/_sysforge
if [[ ! -f "$COMP_FILE" ]]; then
    fail "$COMP_FILE missing"
else
    VERBS="$(python3 -c "
import sys
sys.path.insert(0, '.')
try:
    from sysforge.cli import _build_parser
    p = _build_parser()
    seen = set()
    import argparse
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction):
            for v in action.choices:
                seen.add(v)
    print(' '.join(sorted(seen)))
except Exception as e:
    print('ERR:' + str(e))
" 2>/dev/null || true)"
    if [[ -z "$VERBS" || "$VERBS" == ERR:* ]]; then
        warn "could not extract CLI verbs (${VERBS:-empty}); skipping completion check"
    else
        MISSING=()
        for v in $VERBS; do
            if ! grep -qE "(^|[[:space:]])${v}\)" "$COMP_FILE"; then
                MISSING+=("$v")
            fi
        done
        if [[ ${#MISSING[@]} -eq 0 ]]; then
            pass "all $(echo "$VERBS" | wc -w) CLI verbs present in $COMP_FILE"
        else
            for v in "${MISSING[@]}"; do
                warn "completions missing CLI verb '$v' (no '$v)' case in $COMP_FILE)"
            done
        fi
    fi
fi
echo

# ---------------------------------------------------------------------------
# Section 5: build artifacts that release.sh regenerates
# ---------------------------------------------------------------------------
echo "Build artifacts"
if [[ -f uv.lock ]]; then
    pass "uv.lock present (release.sh will regenerate)"
else
    warn "uv.lock missing (release.sh will create it via 'uv lock')"
fi
if [[ -f man/sysforge.1 ]]; then
    pass "man/sysforge.1 present (release.sh will regenerate)"
else
    warn "man/sysforge.1 missing (release.sh will create it via 'make man')"
fi
echo

# ---------------------------------------------------------------------------
# Section 6: chroot infrastructure
# ---------------------------------------------------------------------------
echo "Chroot infrastructure"
CHROOT_ROOT="${SYSFORGE_CHROOT:-/var/lib/archbuild/extra-x86_64}"
if command -v makechrootpkg >/dev/null 2>&1; then
    pass "makechrootpkg on PATH"
else
    warn "makechrootpkg not found (sudo pacman -S --needed devtools); release.sh will refuse without --skip-chroot"
fi
if [[ -d "$CHROOT_ROOT/root" ]]; then
    pass "chroot root exists at $CHROOT_ROOT/root"
else
    warn "chroot $CHROOT_ROOT/root missing — create once: sudo mkarchroot $CHROOT_ROOT/root base-devel"
fi
echo

# ---------------------------------------------------------------------------
# Section 7: coverage ratchet (on by default; RUN_COVERAGE=0 skips the slow suite)
# ---------------------------------------------------------------------------
echo "Coverage ratchet"
if [[ "${RUN_COVERAGE:-1}" == "1" ]]; then
    if make --no-print-directory coverage >/dev/null 2>&1; then
        RATCHET_OUT="$(python3 tools/coverage_ratchet.py --check 2>&1)"
        RATCHET_RC=$?
        if [[ $RATCHET_RC -eq 3 ]]; then
            warn "$RATCHET_OUT"
        else
            pass "$RATCHET_OUT"
        fi
    else
        warn "make coverage failed — could not compute the ratchet"
    fi
else
    pass "skipped (RUN_COVERAGE=0 set; instrumented suite + ratchet not run)"
fi
echo

# ---------------------------------------------------------------------------
# Section 8: VM packaging smoke (advisory; RUN_VM_SMOKE=0 skips)
#
# tools/smoke.sh asserts the post-install packaging invariants a real pacman
# install must produce (hooks, completions, tmpfiles.d sentinel dir, -Qi
# registration). Nothing else in this preflight covers them: check-shipped
# validates the *sources* in the repo, not that an installed package lays them
# down. It is surfaced here as a checklist line rather than skill prose because
# the skill reports this script's output verbatim — prose gets skimmed.
#
# Never a [FAIL] on absence: the VM is optional infrastructure and a missing VM
# must not block a release. A *failing* smoke run is a real packaging break and
# does fail.
# ---------------------------------------------------------------------------
echo "VM packaging smoke"
if [[ "${RUN_VM_SMOKE:-1}" != "1" ]]; then
    pass "skipped (RUN_VM_SMOKE=0 set)"
elif ! timeout 3 bash -c "</dev/tcp/localhost/10022" 2>/dev/null; then
    warn "VM not running — post-install packaging invariants unverified. Boot it with 'make vm-snapshot', then 'make vm-test' (build + install + smoke)."
else
    SMOKE_OUT="$(bash tools/smoke.sh 2>&1)" && SMOKE_RC=0 || SMOKE_RC=$?
    if [[ $SMOKE_RC -ne 0 ]]; then
        printf '%s\n' "$SMOKE_OUT" | sed 's/^/      /'
        fail "vm-smoke failed — an install is missing expected artifacts (see above)"
    # smoke.sh's version gate is liveness-only: it accepts ANY version, so a VM
    # holding a build of an older release still passes every check. Compare the
    # reported version against pyproject to catch that staleness.
    elif printf '%s\n' "$SMOKE_OUT" | grep -q "sysforge --version runs (.*$PYVER"; then
        pass "vm-smoke passed against an installed v$PYVER"
    else
        warn "vm-smoke passed, but the VM's installed sysforge is not v$PYVER — the result covers an older build. Re-run 'make vm-test' to build and install the current tree first."
    fi
fi
echo

# ---------------------------------------------------------------------------
# Section 9: derivative portability (advisory; RUN_DISTRO_SMOKE=0 skips)
#
# Runs the container tier's derivative arm — the only check that exercises
# Standards row 23 against a real Arch derivative rather than a fixture: extra
# sync DBs ahead of core/extra, a -march=x86-64-v3 + LTO makepkg.conf baseline,
# and bumped pkgrels on core packages. tests/test_distro_portability.py covers
# the same invariants against synthetic input; only this reaches a real one.
#
# Policy mirrors section 8 exactly: never a [FAIL] on absence (the container
# harness is optional infrastructure — no podman, no network, or an unpullable
# base image must not block a release), always a [FAIL] on a real break.
# harness.sh exits 3 for "unavailable" precisely so the two are distinguishable
# here; any other non-zero exit is a genuine portability failure.
#
# The per-minor cadence is NOT enforced here — a WARN cannot enforce it. For a
# minor or major bump this section must be *green*, which SKILL.md's checklist
# states; a yellow section 9 is acceptable for a patch release only. Keeping the
# version-sensitivity in the skill leaves this script's policy uniform across
# every section.
# ---------------------------------------------------------------------------
echo "Derivative portability (container tier)"
if [[ "${RUN_DISTRO_SMOKE:-1}" != "1" ]]; then
    pass "skipped (RUN_DISTRO_SMOKE=0 set)"
elif ! command -v podman >/dev/null 2>&1; then
    warn "podman not installed — derivative portability unverified. Install it with 'make dev-deps-container', then 'make vm-pkg-stable && make container-smoke-cachyos'."
else
    DISTRO_OUT="$(bash tools/container/harness.sh smoke --distro=cachyos 2>&1)" \
        && DISTRO_RC=0 || DISTRO_RC=$?
    if [[ $DISTRO_RC -eq 3 ]]; then
        warn "container harness unavailable (no network, or the derivative base image could not be pulled) — derivative portability unverified"
    elif [[ $DISTRO_RC -ne 0 ]]; then
        printf '%s\n' "$DISTRO_OUT" | sed 's/^/      /'
        fail "derivative portability failed — a Standards row 23 invariant does not hold on the derivative (see above)"
    elif printf '%s\n' "$DISTRO_OUT" | grep -q "sysforge --version runs (.*$PYVER"; then
        pass "derivative portability passed against an installed v$PYVER"
    else
        warn "derivative portability passed, but the container's installed sysforge is not v$PYVER — the result covers an older build. Re-run 'make vm-pkg-stable' first."
    fi
fi
echo

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "==> Summary: $PASS pass, $WARN warn, $FAIL fail"
if [[ $FAIL -gt 0 ]]; then
    echo "Release is NOT ready. Resolve the [FAIL]s above before running 'make release-*'."
    exit 1
fi
echo "Release is ready (modulo any warnings above)."
echo "Reminder: run 'make test' separately if you haven't already; this skill does not run the suite."
exit 0
