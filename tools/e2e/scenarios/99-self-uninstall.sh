# shellcheck shell=bash
# Removing SysForge: a repo package built from source goes back to the repo
# build, a local build stays installed, then the package, its state, caches,
# config and group are gone. Last in order: nothing runs after it. Networked:
# the stock package comes from the mirrors.
# start: ready

step "build a repo package and a local one from source"
sysforge packages add tree --enable-build-from-source </dev/null
sysforge build "$(stage_pkg tree)" -si --rebuild </dev/null
sysforge build "$(stage_pkg e2e-hello)" -si --rebuild </dev/null
assert_built_here tree
assert_built_here e2e-hello
package=$(pacman -Qqo "$(command -v sysforge)") || fail "nothing owns the sysforge command"
# What `sysforge setup` adds at a terminal, so its removal is checked too.
sudo sed -i '/^\[options\]/a IgnoreGroup = sf-build' /etc/pacman.conf

step "the dry run changes nothing"
output_has 'Revert to the repo version' sysforge self-uninstall --dry-run </dev/null \
    || fail "the plan does not revert tree"
assert_built_here tree
[[ -d /var/lib/sysforge ]] || fail "the dry run deleted the state directory"

step "without a terminal it refuses unless told"
expect_exit 2 sysforge self-uninstall </dev/null
assert_installed "$package"

step "self-uninstall"
sysforge self-uninstall --yes </dev/null

step "tree is the repo build again, the local build stays"
packager=$(pacman -Qi tree | sed -n 's/^Packager *: //p')
[[ $packager != "Unknown Packager" ]] || fail "tree is still the source-built copy"
assert_built_here e2e-hello

step "sysforge and everything it wrote are gone"
assert_not_installed "$package"
hash -r  # bash remembers where it last found sysforge
if command -v sysforge >/dev/null; then
    fail "the sysforge command is still there"
fi
for d in /etc/sysforge /var/lib/sysforge /var/cache/sysforge ~/.cache/sysforge; do
    [[ ! -e $d ]] || fail "$d is still there"
done
if getent group sysforge >/dev/null; then
    fail "the sysforge group is still there"
fi
if grep -q 'sf-build' /etc/pacman.conf; then
    fail "IgnoreGroup still names sf-build"
fi
