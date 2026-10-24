# shellcheck shell=bash
# sysforge.toml keys that stop code coming in: with the AUR turned off a
# package only the AUR has is refused before any lookup, and with sources
# frozen an AUR package is refused before it is cloned. Both runs exit
# non-zero and leave nothing behind.
# start: ready

conf=/etc/sysforge/sysforge.toml

step "with the AUR off, an AUR-only name is refused without a lookup"
sudo sed -i 's/^#enabled = true$/enabled = false/' "$conf"
grep -q '^enabled = false$' "$conf" || fail "could not turn the AUR off"
out=$(sysforge build e2e-not-in-any-repo </dev/null 2>&1) && fail "the build succeeded: $out"
grep -q 'enabled = false' <<<"$out" || fail "the refusal did not name [aur] enabled: $out"
[[ ! -e ~/src/e2e-not-in-any-repo ]] || fail "a checkout was created"
output_has tree sysforge search tree </dev/null \
    || fail "search found no repo package with the AUR off"
sudo sed -i 's/^enabled = false$/#enabled = true/' "$conf"

step "with sources frozen, an AUR package is not cloned"
sudo sed -i 's/^freeze_sources = false$/freeze_sources = true/' "$conf"
grep -q '^freeze_sources = true$' "$conf" || fail "could not freeze sources"
rm -rf ~/src/cbonsai
expect_exit 1 sysforge build cbonsai </dev/null
[[ ! -e ~/src/cbonsai ]] || fail "a frozen run cloned cbonsai"
sudo sed -i 's/^freeze_sources = true$/freeze_sources = false/' "$conf"
assert_no_sentinel
