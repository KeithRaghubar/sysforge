# shellcheck shell=bash
# The README's Quick start as one login session, from sysforge just installed:
# the one-time setup, an AUR build (the review gate accepted), then `update`.
# The setup adds the user to the sysforge group, which only applies at the next
# login, so every step after it runs without that membership (3.5.0-B134).
# The tier's one networked scenario: the AUR, and pacman for build deps.
# start: installed

step "one-time setup"
sudo EDITOR=true sysforge run reconfigure </dev/null
sudo sysforge run hardware </dev/null
assert_no_sentinel
id -nG | grep -qw sysforge && fail "the sysforge group applied before the next login"

step "build an AUR package"
answer '\[a\]ccept' a -- sysforge build cbonsai -si
assert_installed cbonsai
assert_built_here cbonsai
assert_recorded cbonsai
[[ $(state_get cbonsai build_mode) == source_built ]] || fail "cbonsai build_mode is not source_built"
assert_no_sentinel
cbonsai -p >/dev/null

step "update"
sysforge update </dev/null
assert_no_sentinel
assert_recorded cbonsai
