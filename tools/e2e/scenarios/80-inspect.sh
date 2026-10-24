# shellcheck shell=bash
# The read-only commands, run against real state: each answers without
# changing anything. Reports whose findings depend on the host (doctor) may
# exit 1; anything else is a crash.
# start: ready

dir=$(stage_pkg e2e-hello)

step "build something to inspect"
sysforge build "$dir" -si --rebuild </dev/null
assert_recorded e2e-hello
before=$(sha256sum "$STATE_DIR/build_state.toml")

step "state reports"
sysforge state failed --no-pager </dev/null
sysforge state orphans --no-pager </dev/null
sysforge state profiles --no-pager </dev/null
sysforge state repair --dry-run </dev/null

step "doctor, whole system and one package"
expect_exit_in "0 1" sysforge doctor system --findings-only </dev/null
json_report "0 1" sysforge doctor system --json </dev/null
expect_exit_in "0 1" sysforge doctor pkg e2e-hello </dev/null
json_report "0 1" sysforge doctor pkg e2e-hello --json </dev/null

step "env and fetch"
sysforge env </dev/null >/dev/null
sysforge fetch "$dir" --no-update </dev/null

step "nothing changed"
[[ $(sha256sum "$STATE_DIR/build_state.toml") == "$before" ]] \
    || fail "a read-only command changed build_state.toml"
assert_no_sentinel
