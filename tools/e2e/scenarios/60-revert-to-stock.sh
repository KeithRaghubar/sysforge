# shellcheck shell=bash
# Going back to the repo package: a repo package built from source is
# replaced by the stock one, and sysforge stops maintaining it. Networked:
# the stock package comes from the mirrors.
# start: ready

dir=$(stage_pkg tree)

step "build the repo package from source"
sysforge packages add tree --enable-build-from-source </dev/null
sysforge build "$dir" -si --rebuild </dev/null
assert_built_here tree
assert_recorded tree

step "the dry run changes nothing"
sysforge revert-to-stock tree --dry-run </dev/null
assert_built_here tree
assert_recorded tree

step "revert installs the repo's tree"
sysforge revert-to-stock tree --yes </dev/null
assert_installed tree
packager=$(pacman -Qi tree | sed -n 's/^Packager *: //p')
[[ $packager != "Unknown Packager" ]] || fail "tree is still the source-built copy"
[[ -z $(state_get tree build_mode) ]] || fail "revert-to-stock left tree's record"
assert_no_sentinel
