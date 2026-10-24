# shellcheck shell=bash
# User-authored scripts under management: discovered, adopted (by name, and
# by review), edited in the managed copy, deployed back to the live file, then
# removed. In its own container it is the user's first session with nothing
# built, so the first registry write provisions the state directory itself
# (3.5.0-B142).
# start: ready

live=~/.local/bin/e2e-tool
other=~/.local/bin/e2e-other
mkdir -p ~/.local/bin
printf '#!/bin/sh\necho e2e\n' | tee "$live" >"$other"
chmod +x "$live" "$other"

step "the unmanaged script is discovered"
output_has e2e-tool sysforge artifact list --unmanaged </dev/null \
    || fail "artifact list --unmanaged did not find e2e-tool"

step "adopt it"
sysforge artifact adopt "$live" --class script </dev/null
output_has e2e-tool sysforge artifact list </dev/null || fail "e2e-tool is not managed"

step "review adopts what is left"
sysforge artifact review --all </dev/null
output_has e2e-other sysforge artifact list </dev/null || fail "artifact review --all did not adopt e2e-other"

step "edit the managed copy, then deploy it"
editor=~/e2e/append-line.sh
mkdir -p ~/e2e
printf '#!/bin/sh\necho "# edited" >>"$1"\n' >"$editor"
chmod +x "$editor"
EDITOR=$editor sysforge artifact edit e2e-tool </dev/null
if grep -q '# edited' "$live"; then
    fail "edit changed the live file before deploy"
fi
sysforge artifact deploy e2e-tool </dev/null
grep -q '# edited' "$live" || fail "deploy did not write the edit to the live file"

step "remove it from the live system and from management"
sysforge artifact remove e2e-tool --purge </dev/null
[[ ! -e $live ]] || fail "artifact remove left the live file"
if output_has e2e-tool sysforge artifact list </dev/null; then
    fail "e2e-tool is still managed"
fi
sysforge artifact remove e2e-other --purge </dev/null
assert_no_sentinel
