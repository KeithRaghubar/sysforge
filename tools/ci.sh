#!/usr/bin/env bash
# CI job body (3.4.0-DEV1): the suite, lint and the check-* gates in a fresh
# archlinux:base-devel container, away from the workstation's clang-forced
# shell, live config and hardware profile.
#
# Usage (as root, inside the container): tools/ci.sh <checkout-dir>
#
# .github/workflows/ci.yml runs it after actions/checkout; `make ci-local`
# replays it under podman against the working tree, so a red CI run can be
# reproduced without pushing. The checkout is only read: the gates run on a
# copy owned by an unprivileged user, because makepkg refuses root and a root
# run would pass every permission-denied test for the wrong reason.
set -euo pipefail

src=${1:?usage: tools/ci.sh <checkout-dir>}
user=builder
work=/home/$user/sysforge

# Same reason as tools/container/Containerfile: without CAP_SYS_ADMIN pacman's
# per-hook network namespace fails and the hook is skipped while pacman still
# exits 0. Must go inside [options].
sed -i '/^\[options\]/a DisableSandboxNetwork' /etc/pacman.conf

# -Syu, not -Sy: the image's libraries must not skew against what the core
# tier pulls in. The core tier comes from the Makefile, the one record of it.
pacman -Syu --noconfirm --needed make
make -C "$src" dev-deps-core PACMAN_FLAGS=--noconfirm

useradd --create-home "$user"
cp -a "$src" "$work"
chown -R "$user:$user" "$work"
# Drop gitignored state (a local .venv, caches, coverage output) so a
# `make ci-local` copy is a CI checkout plus the uncommitted edits, and the
# host's leftovers can't make a gate pass.
runuser -u "$user" -- git -C "$work" clean -fdXq

# The `pre-release` set, in its order. -k: one red gate must not hide the
# others; the run still fails.
# `coverage`, not `test` (3.4.0-DEV2): it is the complete run. `make test` uses
# the system pytest, so tests/test_sync_config.py only runs where
# python-tomlkit happens to be installed; coverage layers tomlkit in.
runuser -u "$user" -- make -C "$work" -k \
    lint typecheck coverage check-shipped check-personal check-design check-roadmap-table \
    check-standards
