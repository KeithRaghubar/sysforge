# shellcheck shell=bash
# A repo package's opt-in: `build` on a package the repos also carry asks
# before building it from source, saves the answer, and records the build as a
# repo package's, so `update` keeps rebuilding it from source.
# start: ready

dir=$(stage_pkg tree)

step "without a terminal, build refuses an un-opted-in repo package"
sysforge build "$dir" -si </dev/null
pacman -Qi tree >/dev/null 2>&1 && fail "tree was installed without an opt-in"

step "at a terminal, the opt-in prompt builds it and saves the answer"
answer 'build from source\?' 'y\n' -- sysforge build "$dir" -si
assert_installed tree
assert_built_here tree
assert_recorded tree
[[ $(state_get tree source) == repo ]] || fail "tree's record is not source=repo"
grep -q 'enable_build_from_source *= *true' /etc/sysforge/packages.toml \
    || fail "the opt-in was not saved to packages.toml"
assert_no_sentinel

step "the saved opt-in answers the next build"
sysforge build "$dir" -si --rebuild </dev/null
assert_no_sentinel
