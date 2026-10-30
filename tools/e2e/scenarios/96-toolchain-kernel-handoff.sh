# shellcheck shell=bash
# What `run toolchain` leaves for `run kernel`: the compiler it registers in
# pipeline_state.toml is the one the kernel plans with when kernel.toml names
# none. skip_build registers the compiler without building it, so this takes
# seconds; the kernel side is a dry run.
# start: ready

tc=/etc/sysforge/toolchain.toml
kc=/etc/sysforge/kernel.toml

# toolchain_result KEY: KEY of the toolchain stage's saved result.
toolchain_result() {
    python3 - "$STATE_DIR/pipeline_state.toml" "$1" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    print(tomllib.load(f).get("stages", {}).get("toolchain", {}).get("result", {}).get(sys.argv[2], ""))
PY
}

# set_compiler NAME: toolchain.toml compiler = NAME, whatever the line holds.
set_compiler() {
    sudo sed -i "s/^#\?compiler = \"[a-z]*\"$/compiler = \"$1\"/" "$tc"
    grep -q "^compiler = \"$1\"$" "$tc" || fail "could not set toolchain.toml compiler = $1"
}

# kernel_plan: the plan `run kernel --dry-run` prints.
kernel_plan() {
    local out
    out=$(sysforge run kernel --dry-run --no-update </dev/null 2>&1) || fail "run kernel --dry-run failed: $out"
    echo "$out"
}

step "the toolchain registers its compiler without building; the kernel names none"
# Set outright, not from the shipped lines: under --here 95-stage-dry-run has
# already edited both files.
sudo sed -i '0,/^#\?enabled = [a-z]*$/s//enabled = true/; s/^#\?skip_build = [a-z]*$/skip_build = true/' "$tc"
sudo sed -i '0,/^#\?enabled = [a-z]*$/s//enabled = true/; s/^compiler = .*/#compiler = "gcc"/;
             s/^#\?bootloader = "[a-z-]*"$/bootloader = "none"/; s/^#\?source = "[a-z]*"$/source = "local"/' "$kc"
grep -q '^skip_build = true$' "$tc" || fail "could not set toolchain.toml skip_build"
if grep -q '^compiler = ' "$kc"; then
    fail "kernel.toml still names a compiler"
fi
mkdir -p ~/src/linux-sysforge
printf '%s\n' 'pkgbase=linux-sysforge' 'pkgname=(linux-sysforge linux-sysforge-headers)' \
    'pkgver=6.1' 'pkgrel=1' 'arch=(x86_64)' 'prepare() { make olddefconfig; }' \
    >~/src/linux-sysforge/PKGBUILD

step "run toolchain with llvm saves clang"
set_compiler llvm
sysforge run toolchain --no-update </dev/null
assert_no_sentinel
[[ $(toolchain_result cc) == /usr/bin/clang ]] || fail "toolchain result cc is $(toolchain_result cc), expected /usr/bin/clang"
[[ $(toolchain_result cxx) == /usr/bin/clang++ ]] || fail "toolchain result cxx is $(toolchain_result cxx)"
[[ $(toolchain_result variant) == stock_llvm ]] || fail "toolchain result variant is $(toolchain_result variant), expected stock_llvm"
grep -q '^toolchain = "llvm"' /etc/sysforge/profiles.toml || fail "profiles.toml [defaults] toolchain is not llvm"

step "the kernel plans with it"
plan=$(kernel_plan)
grep -q 'compiler: .*(from the last .run toolchain.; cc=/usr/bin/clang cxx=/usr/bin/clang++)' <<<"$plan" \
    || fail "the kernel plan does not inherit clang from run toolchain"
grep -q 'variant: *stock_llvm$' <<<"$plan" || fail "the kernel plan does not show variant stock_llvm"
assert_not_installed linux-sysforge

step "run toolchain with gcc saves gcc, and the kernel follows"
set_compiler gcc
sysforge run toolchain --no-update </dev/null
assert_no_sentinel
[[ $(toolchain_result cc) == /usr/bin/gcc ]] || fail "toolchain result cc is $(toolchain_result cc), expected /usr/bin/gcc"
[[ $(toolchain_result variant) == gcc ]] || fail "toolchain result variant is $(toolchain_result variant), expected gcc"
plan=$(kernel_plan)
grep -q 'compiler: .*(from the last .run toolchain.; cc=/usr/bin/gcc cxx=/usr/bin/g++)' <<<"$plan" \
    || fail "the kernel plan does not inherit gcc from run toolchain"
grep -q 'variant: *gcc$' <<<"$plan" || fail "the kernel plan does not show variant gcc"
assert_not_installed linux-sysforge
