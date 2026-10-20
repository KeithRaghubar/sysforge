#!/usr/bin/env bash
# CI job body (3.4.0-DEV1): the suite, lint and the check-* gates in a fresh
# archlinux:base-devel container, away from the workstation's clang-forced
# shell, live config and hardware profile.
#
# Usage (as root, inside the container): tools/ci.sh <checkout-dir>
#
# .github/workflows/ci.yml runs it after actions/checkout; `make ci-local`
# replays it under podman against the working tree, so a red CI run can be
# reproduced without pushing. The checkout is only read: the gates run on a
# copy owned by an unprivileged user, because makepkg refuses root and a root
# run would pass every permission-denied test for the wrong reason.
set -euo pipefail

src=${1:?usage: tools/ci.sh <checkout-dir>}
user=builder
work=/home/$user/sysforge

# Same reason as tools/container/Containerfile: without CAP_SYS_ADMIN pacman's
# per-hook network namespace fails and the hook is skipped while pacman still
# exits 0. Must go inside [options].
sed -i '/^\[options\]/a DisableSandboxNetwork' /etc/pacman.conf

# -Syu, not -Sy: the image's libraries must not skew against what the core
# tier pulls in. The core tier comes from the Makefile, the one record of it.
pacman -Syu --noconfirm --needed make
make -C "$src" dev-deps-core PACMAN_FLAGS=--noconfirm

useradd --create-home "$user"
cp -a "$src" "$work"
chown -R "$user:$user" "$work"
# Drop gitignored state (a local .venv, caches, coverage output) so a
# `make ci-local` copy is a CI checkout plus the uncommitted edits, and the
# host's leftovers can't make a gate pass.
runuser -u "$user" -- git -C "$work" clean -fdXq

# The environment the gates run in (3.5.0-DEV7). A test that depends on the
# host fails only where the environment differs, so a CI-only failure should
# read as a diff against the local machine, not a guess.
echo "== environment"
echo "packages:  $(pacman -Q 2>/dev/null | wc -l)"
for pkg in llvm clang lld python-tomlkit pyalpm; do
    printf '%-10s %s\n' "$pkg:" "$(pacman -Q "$pkg" 2>/dev/null || echo 'not installed')"
done
echo "python:    $(python3 --version 2>&1)"
echo "pytest:    $(pytest --version 2>&1 | head -1) (system; make test overlays its own)"
echo "uv:        $(uv --version 2>&1)"

# The `pre-release` gates, in their order, read from the Makefile so the two
# cannot drift (3.5.0-DEV8). Every gate runs even after one fails (make -k's
# behaviour, kept by hand so each gate can be grouped and reported); the run
# still fails.
read -ra gates <<<"$(runuser -u "$user" -- make -s --no-print-directory -C "$work" print-gates)"
in_actions=${GITHUB_ACTIONS:-false}
# A gate's full test-failure report (3.5.0-DEV6): every failure with its
# assertion, and a JUnit file Actions annotations are drawn from. -o addopts=
# drops the suite's -v, which would print one line per test.
junit=/home/$user/junit.xml
test_args="-o addopts= -q -rfE --tb=short -o junit_family=xunit1 --junitxml=$junit"
failed=()
results=()
for gate in "${gates[@]}"; do
    [[ $in_actions == true ]] && echo "::group::$gate"
    args=()
    [[ $gate == test ]] && args=("ARGS=$test_args")
    if runuser -u "$user" -- make -C "$work" --no-print-directory "$gate" "${args[@]}"; then
        results+=("| \`$gate\` | pass |")
    else
        failed+=("$gate")
        results+=("| \`$gate\` | **FAIL** |")
    fi
    [[ $in_actions == true ]] && echo "::endgroup::"
done

# Outside Actions (`make ci-local`) the output stays the gates' own.
if [[ $in_actions == true ]]; then
    [[ -f $junit ]] && python3 "$work/tools/junit_annotate.py" "$junit"
    if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then
        {
            echo "| Gate | Result |"
            echo "|------|--------|"
            printf '%s\n' "${results[@]}"
        } >>"$GITHUB_STEP_SUMMARY"
    fi
fi

if ((${#failed[@]})); then
    echo "FAILED gates: ${failed[*]}" >&2
    exit 1
fi
