# shellcheck shell=bash
# A build that fails before installing anything leaves no sentinel, so the next
# run starts normally: a repo package's checkout needs pkgctl, and here it is
# missing (as on a system without devtools).
# start: ready

sudo pacman -Rdd --noconfirm devtools

step "the build fails on the missing pkgctl"
expect_exit 1 sysforge build cowsay --force </dev/null
pacman -Qi cowsay >/dev/null 2>&1 && fail "cowsay was installed"
assert_no_sentinel

step "the next run is not refused"
sysforge update --dry-run </dev/null
