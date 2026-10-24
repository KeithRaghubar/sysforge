# shellcheck shell=bash
# The long stages previewed with non-default toolchain.toml and kernel.toml:
# a dry run changes nothing, and the kernel's shows the plan it resolved from
# kernel.toml (3.5.0-B141: the runner used to skip the stage on a dry run).
# start: ready

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

# The records a run changes. Caches (source_meta.toml) may fill in.
records() {
    sha256sum /etc/sysforge/profiles.toml
    sha256sum "$STATE_DIR"/pipeline_state.toml "$STATE_DIR"/build_state.toml 2>/dev/null || true
}
before=$(records)

step "the toolchain dry run changes nothing"
sysforge run toolchain --dry-run --no-update </dev/null
[[ $(records) == "$before" ]] || fail "a toolchain dry run changed a record"
[[ ! -e ~/src/llvm ]] || fail "a toolchain dry run cloned the llvm PKGBUILD"
assert_no_sentinel

step "the kernel dry run changes nothing"
out=$(sysforge run kernel --dry-run --no-update </dev/null 2>&1) || fail "run kernel --dry-run failed: $out"
echo "$out"
assert_not_installed linux-sysforge
[[ $(records) == "$before" ]] || fail "a kernel dry run changed a record"
assert_no_sentinel

step "the kernel dry run shows the plan it resolved"
grep -q 'Kernel build plan' <<<"$out" || fail "run kernel --dry-run showed no plan"
grep -q 'none' <<<"$out" || fail "the plan does not show bootloader = none"
