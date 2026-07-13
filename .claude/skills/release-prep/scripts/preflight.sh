#!/usr/bin/env bash
# release-prep pre-flight: validate sysforge release readiness.
#
# Run from the skill via:  bash .claude/skills/release-prep/scripts/preflight.sh
#
# Exits 0 when there are no [FAIL]s (warnings allowed), non-zero otherwise.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$REPO_ROOT"

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
    warn "ruff not on PATH — install with: sudo pacman -S ruff"
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
# Section 7: coverage ratchet (opt-in — instrumented suite is slow)
# ---------------------------------------------------------------------------
echo "Coverage ratchet"
if [[ "${RUN_COVERAGE:-0}" == "1" ]]; then
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
    pass "skipped (set RUN_COVERAGE=1 to run the instrumented suite + ratchet)"
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
