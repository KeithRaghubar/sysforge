#!/usr/bin/env bash
# prepare.sh -- inside the demo container, as the demo user: install sysforge
# from the checkout mounted at /src/sysforge and run the Quick start's one-time
# setup, leaving the system where a new user is after steps 1-3.
#
# Usage: prepare.sh BRANCH
#
# PKGBUILD-git is pointed at the mounted checkout, so the package is built
# from BRANCH's committed HEAD (uncommitted edits are not in it). The setup
# runs with EDITOR=true, which accepts every shipped config file unchanged.
set -euo pipefail

branch=$1
mkdir -p ~/pkg
cd ~/pkg
cp /src/sysforge/PKGBUILD-git PKGBUILD
cp /src/sysforge/sysforge.install .
sed -i "s|^source=.*|source=(\"\$pkgname::git+file:///src/sysforge#branch=$branch\")|" PKGBUILD
makepkg -si --noconfirm

cd ~
sudo sysforge run hardware </dev/null
sudo EDITOR=true sysforge run reconfigure </dev/null
