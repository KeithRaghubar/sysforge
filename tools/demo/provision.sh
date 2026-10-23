#!/usr/bin/env bash
# provision.sh -- as root on a fresh archlinux:base-devel system: the packages
# and unprivileged `demo` user the recordings (tools/record-demo.sh) and the
# end-to-end tier (tools/e2e/) start from. The Containerfile runs it at image
# build; `tools/e2e/run.sh --here` runs it directly, as in CI.
#
# demo can sudo without a password, as a new user would right after
# installing sysforge. sysforge itself is not installed here (install.sh).
set -euo pipefail

# devtools: pkgctl, which `sysforge build` uses to check out a repo package's
# PKGBUILD. less: the pager a review's [v]iew diff opens, as on most systems.
pacman -Syu --noconfirm --needed git sudo python pyalpm uv python-installer devtools less
pacman -Scc --noconfirm

id demo >/dev/null 2>&1 || useradd -m -G wheel demo
echo 'demo ALL=(ALL:ALL) NOPASSWD: ALL' > /etc/sudoers.d/demo
chmod 0440 /etc/sudoers.d/demo
