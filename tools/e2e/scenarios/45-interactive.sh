# shellcheck shell=bash
# `--interactive` hands pacman's own questions to the user (3.3.0-B6). A
# package conflict is answered N under --noconfirm, so without the flag the
# install stops and nothing changes; with it, the question shows on the
# terminal and a yes swaps the packages. `update` first, then `build`.
# start: ready

hello=$(stage_pkg e2e-hello)
alt=$(stage_pkg e2e-hello-alt)
profiles=/etc/sysforge/profiles.toml

# conflicts_with PKG: the installed PKG's "Conflicts With" field.
conflicts_with() {
    pacman -Qi "$1" | sed -n 's/^Conflicts With *: //p'
}

step "two packages that do not conflict yet"
sed -i 's/^conflicts=.*/conflicts=()/' "$alt/PKGBUILD"
# e2e-hello by makepkg alone: no record, so the profile change below rebuilds
# only e2e-hello-alt. pacman asks "Remove e2e-hello?" only when e2e-hello is
# installed and not itself in the transaction.
(cd "$hello" && makepkg -si --noconfirm)
sysforge build "$alt" -si </dev/null
assert_installed e2e-hello
assert_installed e2e-hello-alt
assert_recorded e2e-hello-alt

step "the next e2e-hello-alt conflicts with e2e-hello, and a profile change makes it drift"
cp "$E2E_DIR/pkgs/e2e-hello-alt/PKGBUILD" "$alt/PKGBUILD"
# Another default profile, whichever one an earlier scenario left in place
# (they share the system under --here).
if grep -q '^profile = "standard"' "$profiles"; then
    sudo sed -i 's/^profile = "standard"/profile = "optimized"/' "$profiles"
else
    sudo sed -i 's/^profile = "[a-z]*"/profile = "standard"/' "$profiles"
fi

step "update without --interactive: the conflict stops the install"
expect_exit 1 sysforge update --rebuild-on-flag-drift --yes --no-sysupgrade </dev/null
assert_installed e2e-hello
[[ $(conflicts_with e2e-hello-alt) == None ]] || fail "the conflicting e2e-hello-alt was installed"
assert_no_sentinel

step "update --interactive: the question reaches the user, and yes swaps them"
answer 'Remove e2e-hello\?' 'y\n' 'Proceed with installation\?' 'y\n' -- \
    sysforge update --rebuild-on-flag-drift --yes --no-sysupgrade --interactive
assert_not_installed e2e-hello
[[ $(conflicts_with e2e-hello-alt) == e2e-hello ]] || fail "update --interactive did not install the conflicting e2e-hello-alt"
assert_built_here e2e-hello-alt
assert_no_sentinel

step "build without --interactive: the conflict stops the install"
expect_exit 1 sysforge build "$hello" -si --rebuild </dev/null
assert_not_installed e2e-hello
assert_installed e2e-hello-alt
assert_no_sentinel

step "build --interactive: the question reaches the user, and yes swaps them back"
answer 'Remove e2e-hello-alt\?' 'y\n' 'Proceed with installation\?' 'y\n' -- \
    sysforge build "$hello" -si --rebuild --interactive
assert_installed e2e-hello
assert_not_installed e2e-hello-alt
assert_built_here e2e-hello
assert_no_sentinel
