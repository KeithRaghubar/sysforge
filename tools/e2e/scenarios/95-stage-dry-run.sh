# shellcheck shell=bash
# The long stages previewed with non-default toolchain.toml and kernel.toml:
# a dry run changes nothing, and the kernel's shows the plan it resolved from
# kernel.toml. The plan never shows yet: the runner skips the stage on a dry
# run (3.5.0-B141).
# start: ready
# xfail: 3.5.0-B141

tc=/etc/sysforge/toolchain.toml
kc=/etc/sysforge/kernel.toml

step "turn both stages on, off their defaults"
sudo sed -i 's/^enabled = false$/enabled = true/; s/^#compiler = "gcc"$/compiler = "llvm"/' "$tc"
sudo sed -i 's/^enabled = false$/enabled = true/; s/^#compiler = "gcc"$/compiler = "llvm"/;
             s/^#bootloader = "systemd-boot"$/bootloader = "none"/; s/^#source = "local"$/source = "local"/' "$kc"
grep -q '^compiler = "llvm"$' "$tc" || fail "could not set toolchain.toml compiler"
grep -q '^bootloader = "none"$' "$kc" || fail "could not set kernel.toml bootloader"
# A local kernel tree: the dry run must not need a clone.
mkdir -p ~/src/linux-sysforge
printf '%s\n' 'pkgbase=linux-sysforge' 'pkgname=(linux-sysforge linux-sysforge-headers)' \
    'pkgver=6.1' 'pkgrel=1' 'arch=(x86_64)' 'prepare() { make olddefconfig; }' \
    >~/src/linux-sysforge/PKGBUILD

profiles=$(sha256sum /etc/sysforge/profiles.toml)
state=$(ls "$STATE_DIR")

step "the toolchain dry run changes nothing"
sysforge run toolchain --dry-run --no-update </dev/null
[[ $(sha256sum /etc/sysforge/profiles.toml) == "$profiles" ]] \
    || fail "a toolchain dry run rewrote profiles.toml"
assert_no_sentinel

step "the kernel dry run changes nothing"
out=$(sysforge run kernel --dry-run --no-update </dev/null 2>&1) || fail "run kernel --dry-run failed: $out"
echo "$out"
assert_not_installed linux-sysforge
[[ $(ls "$STATE_DIR") == "$state" ]] || fail "a dry run added state: $(ls "$STATE_DIR")"
assert_no_sentinel

step "the kernel dry run shows the plan it resolved"
grep -q 'Kernel build plan' <<<"$out" || fail "run kernel --dry-run showed no plan"
grep -q 'none' <<<"$out" || fail "the plan does not show bootloader = none"
