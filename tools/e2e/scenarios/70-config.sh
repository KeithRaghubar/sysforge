# shellcheck shell=bash
# Keeping the live config healthy: the shipped config checks clean, an unknown
# key is reported and cleaned out, a changed key is reset to its shipped
# default, and a setup preset's changes are reset the same way. The files are
# root's; sysforge escalates to write them.
# start: ready

conf=/etc/sysforge/sysforge.toml

step "the shipped config checks clean"
sysforge config check </dev/null
json_report 0 sysforge config check --json </dev/null

step "an unknown key is reported, then cleaned out"
sudo sed -i '/^\[git\]/a e2e_unknown_key = 1' "$conf"
expect_exit 1 sysforge config check </dev/null
sysforge config clean --unknown --yes </dev/null
if grep -q e2e_unknown_key "$conf"; then
    fail "config clean left the unknown key"
fi
sysforge config check </dev/null

step "a changed key is reset to the shipped default"
sudo sed -i 's/^fetch_timeout = 30$/fetch_timeout = 99/' "$conf"
grep -q '^fetch_timeout = 99$' "$conf" || fail "could not change fetch_timeout"
sysforge config reset sysforge.toml git.fetch_timeout --dry-run </dev/null
grep -q '^fetch_timeout = 99$' "$conf" || fail "config reset --dry-run wrote the file"
sysforge config reset sysforge.toml git.fetch_timeout --yes </dev/null
grep -q '^fetch_timeout = 30$' "$conf" || fail "config reset did not restore fetch_timeout"

step "a setup preset changes two files, and config reset undoes it"
sysforge setup --preset secure </dev/null
grep -q '^profile = "hardened"' /etc/sysforge/profiles.toml || fail "the secure preset did not pick the hardened profile"
grep -q '^hardening = true' /etc/sysforge/kernel.toml || fail "the secure preset did not turn kernel hardening on"
sysforge config check </dev/null
sysforge config reset profiles.toml defaults.profile --yes </dev/null
sysforge config reset kernel.toml hardening --yes </dev/null
grep -q '^profile = "standard"' /etc/sysforge/profiles.toml || fail "config reset did not restore the default profile"
if grep -q '^hardening = true' /etc/sysforge/kernel.toml; then
    fail "config reset did not restore kernel hardening"
fi
