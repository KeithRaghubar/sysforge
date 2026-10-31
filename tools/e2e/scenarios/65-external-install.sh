# shellcheck shell=bash
# A user's own pacman: a package sysforge built from source and then
# reinstalled from the repos with `pacman -S` is demoted on the next `update`,
# not rebuilt (sysforge-buildstate.hook); a toolchain package changed outside
# sysforge leaves one reminder (sysforge-toolchain.hook). Networked: both
# packages come from the mirrors.
# start: ready

dir=$(stage_pkg tree)
sentinels=$STATE_DIR/sentinels
reminder='Toolchain package(s) (llvm/clang/gcc) changed'

# Built with --force, not a packages.toml opt-in, which would pull tree back
# into update's scope by itself. Under --here, earlier scenarios opted it in.
if python3 -c 'import sys, tomllib
sys.exit(not any(p.get("name") == "tree" for p in tomllib.load(open(sys.argv[1], "rb")).get("package", [])))' \
    /etc/sysforge/packages.toml; then
    sysforge packages remove tree </dev/null
fi

step "build the repo package from source"
sysforge build "$dir" -si --force --rebuild </dev/null
assert_built_here tree
[[ $(state_get tree build_mode) == source_built ]] || fail "tree's record is not source_built"

step "the user reinstalls it from the repos"
# The sentinels stamp whole seconds and a tie is sysforge's own install, so
# the user's transaction must not share sysforge's second, as no one at a
# prompt could.
sleep 2
sudo pacman -S --noconfirm tree
cat "$sentinels"/buildstate "$sentinels"/self-install 2>/dev/null || true
[[ -s $sentinels/buildstate ]] || fail "sysforge-buildstate.hook left no sentinel"
packager=$(pacman -Qi tree | sed -n 's/^Packager *: //p')
[[ $packager != "Unknown Packager" ]] || fail "pacman -S did not install the repo's tree"

step "update demotes it instead of rebuilding it"
sysforge update --yes --no-sysupgrade </dev/null
[[ $(state_get tree build_mode) == pacman ]] || fail "update did not demote tree (build_mode: $(state_get tree build_mode))"
[[ $(pacman -Qi tree | sed -n 's/^Packager *: //p') == "$packager" ]] || fail "update rebuilt tree"
[[ ! -e $sentinels/buildstate ]] || fail "update left the buildstate sentinel"
assert_no_sentinel

step "a toolchain package from the repos leaves one reminder"
sudo pacman -S --noconfirm lld
[[ -e $sentinels/toolchain ]] || fail "sysforge-toolchain.hook left no sentinel"
out=$(sysforge update -v --yes --no-sysupgrade </dev/null 2>&1) || fail "update failed: $out"
echo "$out"
[[ $(grep -c -F "$reminder" <<<"$out") == 1 ]] || fail "update did not show the toolchain reminder once"
[[ ! -e $sentinels/toolchain ]] || fail "update left the toolchain sentinel"
out=$(sysforge update -v --yes --no-sysupgrade </dev/null 2>&1) || fail "update failed: $out"
if grep -q -F "$reminder" <<<"$out"; then
    fail "the toolchain reminder showed again"
fi
