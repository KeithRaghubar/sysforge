# shellcheck shell=bash
# Maintaining a package and letting it go: inspect a source-built package,
# give it a packages.toml override and take it away, stop maintaining it while
# it stays installed, then uninstall it.
# start: ready

dir=$(stage_pkg e2e-hello)
manifest=/etc/sysforge/packages.toml

step "build it"
sysforge build "$dir" -si --rebuild </dev/null
assert_installed e2e-hello
assert_recorded e2e-hello

step "state list names it, in JSON too"
output_has e2e-hello sysforge state list --no-pager </dev/null || fail "state list did not name e2e-hello"
out=$(sysforge state list --json </dev/null)
assert_json <<<"$out"
grep -q e2e-hello <<<"$out" || fail "state list --json did not name e2e-hello"

step "resolve describes it, and the build left its log"
sysforge resolve "$dir" --show-flags </dev/null >/dev/null
output_has e2e-hello sysforge log --verb build --no-pager </dev/null \
    || fail "log --verb build did not show the e2e-hello build"

step "an override is written, listed and removed"
sysforge packages add e2e-hello --no-cache </dev/null
grep -A3 'name = "e2e-hello"' "$manifest" | grep -q 'cache *= *false' \
    || fail "packages add --no-cache did not write cache = false"
output_has e2e-hello sysforge packages list </dev/null || fail "packages list did not name e2e-hello"
sysforge packages remove e2e-hello </dev/null
if grep -q 'name = "e2e-hello"' "$manifest"; then
    fail "packages remove left the entry"
fi

step "a desktop group is written without installing anything"
sysforge packages add-group xfce </dev/null
grep -q '^\[group.xfce\]' "$manifest" || fail "packages add-group wrote no [group.xfce]"
assert_not_installed xfce4-session

step "state forget stops maintaining it and leaves it installed"
sysforge state forget e2e-hello </dev/null
[[ -z $(state_get e2e-hello build_mode) ]] || fail "e2e-hello is still recorded"
assert_installed e2e-hello

step "uninstall removes the package and its record"
sysforge build "$dir" -si --rebuild </dev/null
assert_recorded e2e-hello
# pacman asks to confirm the removal; uninstall has no --yes.
answer 'remove these packages' 'y\n' -- sysforge uninstall e2e-hello
assert_not_installed e2e-hello
[[ -z $(state_get e2e-hello build_mode) ]] || fail "uninstall left e2e-hello's record"
assert_no_sentinel
