# shellcheck shell=bash
# `update --devel` on a -git package: with no new upstream commit it reports
# the package current and rebuilds nothing; after a commit it reports the new
# pkgver and rebuilds; build_state.toml records the commit each build used.
# The upstream is a local git repository, so this needs no network.
# start: ready

upstream=~/e2e/upstream/e2e-hello
rm -rf "$upstream"
mkdir -p "$upstream"
cp "$E2E_DIR/pkgs/e2e-hello/hello.c" "$upstream/"
git -C "$upstream" init -q -b main
git -C "$upstream" -c user.name=e2e -c user.email=e2e@localhost add hello.c
git -C "$upstream" -c user.name=e2e -c user.email=e2e@localhost commit -q -m one

dir=$(stage_pkg e2e-hello-git)
sed -i "s|@UPSTREAM@|$upstream|" "$dir/PKGBUILD"

# installed_ver PKG: the installed version, as pacman reports it.
installed_ver() {
    pacman -Q "$1" | cut -d' ' -f2
}

# devel_plan: what `update --devel --dry-run` plans for e2e-hello-git, as
# "ACTION AVAILABLE" from its --json document.
devel_plan() {
    local out
    out=$(sysforge update --devel --dry-run --json </dev/null) || fail "update --devel --dry-run --json failed"
    python3 -c '
import json, sys
for p in json.load(sys.stdin)["packages"]:
    if p["pkgbase"] == "e2e-hello-git":
        print(p["action"], p["available"])
' <<<"$out"
}

step "build it"
sysforge build "$dir" -si </dev/null
assert_installed e2e-hello-git
assert_recorded e2e-hello-git
first=$(git -C "$upstream" rev-parse HEAD)
[[ $(state_get e2e-hello-git built_upstream_commit) == "$first" ]] \
    || fail "build_state.toml did not record the commit built ($first)"
ver1=$(installed_ver e2e-hello-git)
[[ $ver1 == r1.* ]] || fail "pkgver() did not run: installed $ver1"
built1=$(state_get e2e-hello-git built_at)

step "no new commit: reported current, nothing rebuilt"
plan=$(devel_plan)
[[ $plan == UP_TO_DATE* ]] || fail "update --devel --dry-run planned \`$plan\` with no new commit"
sysforge update --devel --yes </dev/null
[[ $(installed_ver e2e-hello-git) == "$ver1" ]] || fail "update --devel changed the version with no new commit"
[[ $(state_get e2e-hello-git built_at) == "$built1" ]] || fail "update --devel rebuilt e2e-hello-git with no new commit"

step "a new commit: the new pkgver reported and built"
echo '/* two */' >>"$upstream/hello.c"
git -C "$upstream" -c user.name=e2e -c user.email=e2e@localhost commit -q -am two
second=$(git -C "$upstream" rev-parse HEAD)
want="r2.${second:0:7}"
plan=$(devel_plan)
[[ $plan == "NEEDS_REBUILD $want-1" ]] || fail "update --devel --dry-run planned \`$plan\`, expected NEEDS_REBUILD $want-1"
output_has "$want" sysforge update --devel --dry-run </dev/null \
    || fail "update --devel --dry-run did not show $want"
sysforge update --devel --yes </dev/null
[[ $(installed_ver e2e-hello-git) == "$want-1" ]] \
    || fail "update --devel installed $(installed_ver e2e-hello-git), expected $want-1"
[[ $(state_get e2e-hello-git built_upstream_commit) == "$second" ]] \
    || fail "build_state.toml did not record the new commit ($second)"
assert_built_here e2e-hello-git
assert_no_sentinel
