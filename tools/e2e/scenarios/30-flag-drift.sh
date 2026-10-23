# shellcheck shell=bash
# Flag drift after a profile change: a package built with the default profile
# is reported as drifted once the default changes, and rebuilt with the new
# flags by `update --rebuild-on-flag-drift`.
# start: ready

dir=$(stage_pkg e2e-hello)
profiles=/etc/sysforge/profiles.toml

step "build with the shipped default profile"
sysforge build "$dir" -si </dev/null
assert_installed e2e-hello
assert_recorded e2e-hello
before=$(state_get e2e-hello flags_string)
[[ -n $before ]] || fail "e2e-hello has no recorded flags"

step "switch the default profile"
grep -q '^profile = "standard"' "$profiles" || fail "the shipped default profile is not standard"
sudo sed -i 's/^profile = "standard"/profile = "optimized"/' "$profiles"

step "update reports the drift"
out=$(sysforge update --dry-run --explain-drift </dev/null 2>&1) || fail "update --dry-run failed: $out"
echo "$out"
grep -q e2e-hello <<<"$out" || fail "update --dry-run did not report e2e-hello"

step "update rebuilds it with the new flags"
sysforge update --rebuild-on-flag-drift --yes </dev/null
after=$(state_get e2e-hello flags_string)
[[ $after != "$before" ]] || fail "e2e-hello's recorded flags did not change"
assert_no_sentinel
