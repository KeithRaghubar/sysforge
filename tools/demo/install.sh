#!/usr/bin/env bash
# install.sh -- as the demo user: install sysforge from the checkout at
# /src/sysforge, as Quick start step 1 does from the AUR.
#
# Usage: install.sh COMMIT
#
# PKGBUILD-git is pointed at the checkout, so the package is built from COMMIT
# (uncommitted edits are not in it).
set -euo pipefail

commit=$1
rm -rf ~/pkg
mkdir -p ~/pkg
cd ~/pkg
cp /src/sysforge/PKGBUILD-git PKGBUILD
cp /src/sysforge/sysforge.install .
sed -i "s|^source=.*|source=(\"\$pkgname::git+file:///src/sysforge#commit=$commit\")|" PKGBUILD
makepkg -si --noconfirm
