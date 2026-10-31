# shellcheck shell=bash
# Adopting a shipped config change: a package upgrade leaves the new default
# file beside an edited one as a .sfnew, and `config merge` runs the merge tool
# sysforge.toml [ui] merge names on the pair, then removes the .sfnew. The
# tool here is a script that takes the new file's added key and keeps the
# user's edit, as a person in a merge tool would.
# start: ready

conf=/etc/sysforge/sysforge.toml
tool=~/e2e/merge-tool

step "an edited config, and a .sfnew whose shipped file gained a key"
sudo sed -i 's/^fetch_timeout = [0-9]*$/fetch_timeout = 77/' "$conf"
grep -q '^fetch_timeout = 77$' "$conf" || fail "could not edit fetch_timeout"
# The shipped file plus one key. Unknown to the schema, so `config check`
# shows whether the merge carried it into the live file; removed at the end.
sed '/^\[git\]/a e2e_merged_key = 1' "$conf" | sed 's/^fetch_timeout = 77$/fetch_timeout = 30/' \
    | sudo tee "$conf.sfnew" >/dev/null
grep -q '^e2e_merged_key = 1$' "$conf.sfnew" || fail "could not stage the .sfnew"

step "[ui] merge names a script"
mkdir -p ~/e2e
cat >"$tool" <<'SH'
#!/usr/bin/env bash
# merge-tool LIVE NEW: copy NEW's lines missing from LIVE into it, under [git].
set -euo pipefail
line=$(grep '^e2e_merged_key' "$2")
sudo sed -i "/^\[git\]/a $line" "$1"
SH
chmod +x "$tool"
sudo sed -i "s|^#merge = .*|merge = \"$tool\"|" "$conf"
grep -q "^merge = \"$tool\"$" "$conf" || fail "could not set [ui] merge"

step "config merge: [m]erge, then [r]emove the .sfnew"
answer 'a\[b\]ort\? ' 'm' 'a\[b\]ort\? ' 'r' 'Remove sysforge.toml.sfnew\?' 'y\n' -- sysforge config merge --no-pager
[[ ! -e $conf.sfnew ]] || fail "config merge left the .sfnew"
grep -q '^e2e_merged_key = 1$' "$conf" || fail "the merge tool's change is not in the live file"
grep -q '^fetch_timeout = 77$' "$conf" || fail "the merge lost the user's edit"

step "the merged file checks clean once the test key is gone"
expect_exit 1 sysforge config check </dev/null
sudo sed -i '/^e2e_merged_key = 1$/d; s|^merge = .*|#merge = "vimdiff"|' "$conf"
sysforge config check </dev/null
sysforge config merge --dry-run </dev/null
