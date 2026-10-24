# shellcheck shell=bash
# lib.sh -- assertions for the end-to-end scenarios (tools/e2e/scenarios/).
# Sourced by run.sh's per-scenario wrapper; every scenario runs as the
# unprivileged demo user with `set -euo pipefail`, so a failed assertion or an
# unexpected non-zero exit ends it.

E2E_DIR=${E2E_DIR:-/src/sysforge/tools/e2e}
STATE_DIR=/var/lib/sysforge

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

step() {
    echo "--- $*"
}

# answer [PATTERN REPLY]... -- COMMAND...: run COMMAND on a terminal, typing
# REPLY when PATTERN shows (tools/e2e/answer.py).
answer() {
    python3 "$E2E_DIR/answer.py" "$@"
}

# expect_exit CODE COMMAND...: run COMMAND, fail unless it exits CODE.
expect_exit() {
    local want=$1 got=0
    shift
    "$@" || got=$?
    [[ $got == "$want" ]] || fail "\`$*\` exited $got, expected $want"
}

# expect_exit_in "CODES" COMMAND...: run COMMAND, fail unless its exit code is
# one of CODES (space-separated). For a report whose findings depend on the
# host, where any answer is fine but a crash is not.
expect_exit_in() {
    local want=" $1 " got=0
    shift
    "$@" || got=$?
    [[ $want == *" $got "* ]] || fail "\`$*\` exited $got, expected one of:$want"
}

# json_report "CODES" COMMAND...: run COMMAND (a --json report), fail unless it
# exits one of CODES and prints valid JSON. Not a pipe: under pipefail a
# report's exit 1 for findings would fail the pipeline.
json_report() {
    local want=" $1 " got=0 out
    shift
    out=$("$@") || got=$?
    [[ $want == *" $got "* ]] || fail "\`$*\` exited $got, expected one of:$want"
    assert_json <<<"$out"
}

# output_has PATTERN COMMAND...: run COMMAND, then succeed if its output
# matches PATTERN. Both streams: a command's answer goes through ui(), which
# writes to stderr. Captured first: `COMMAND | grep -q` can SIGPIPE COMMAND
# under pipefail once grep has its match.
output_has() {
    local pattern=$1 out
    shift
    out=$("$@" 2>&1) || fail "\`$*\` failed: $out"
    grep -q -- "$pattern" <<<"$out"
}

# assert_json: stdin parses as JSON.
assert_json() {
    python3 -c 'import json, sys; json.load(sys.stdin)' || fail "not valid JSON"
}

assert_not_installed() {
    if pacman -Qi "$1" >/dev/null 2>&1; then
        fail "$1 is still installed"
    fi
}

assert_installed() {
    pacman -Qi "$1" >/dev/null 2>&1 || fail "$1 is not installed"
}

# assert_built_here PKG: PKG's installed copy was built here by makepkg, not
# downloaded from a repo (whose packages name an Arch packager).
assert_built_here() {
    local packager
    packager=$(pacman -Qi "$1" | sed -n 's/^Packager *: //p')
    [[ $packager == "Unknown Packager" ]] || fail "$1 was not built here (Packager: $packager)"
}

# state_get PKG KEY: KEY of PKG's build_state.toml record, empty when absent.
state_get() {
    python3 - "$STATE_DIR/build_state.toml" "$1" "$2" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    print(tomllib.load(f).get(sys.argv[2], {}).get(sys.argv[3], ""))
PY
}

assert_recorded() {
    [[ -f $STATE_DIR/build_state.toml ]] || fail "no $STATE_DIR/build_state.toml"
    [[ -n $(state_get "$1" build_mode) ]] || fail "$1 has no build_state.toml record"
}

# No stage left its in-progress record behind (the next run would refuse).
assert_no_sentinel() {
    [[ ! -e $STATE_DIR/stage_in_progress.toml ]] \
        || fail "stale sentinel: $(cat "$STATE_DIR/stage_in_progress.toml")"
}

# stage_pkg NAME: copy tools/e2e/pkgs/NAME to ~/e2e/NAME (makepkg writes
# beside the PKGBUILD) and print the copy's path.
stage_pkg() {
    mkdir -p ~/e2e
    rm -rf ~/e2e/"$1"
    cp -r "$E2E_DIR/pkgs/$1" ~/e2e/"$1"
    echo ~/e2e/"$1"
}
