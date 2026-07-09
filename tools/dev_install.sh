#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Keith Raghubar
# SPDX-License-Identifier: MIT
#
# dev_install.sh — install sysforge from this git checkout for a quick trial,
# WITHOUT going through the AUR. Mirrors the PKGBUILD package() layout as
# symlinks into the real system paths (sudo), plus an editable venv entry point.
# Fully reversible: `uninstall` removes only symlinks that point back into this
# checkout, never a real packaged file.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Single source of truth: checkout-relative source | absolute system target.
# Must match tools/check_shipped.py's packaged set (enforced by check-shipped).
MAPPING=(
  "man/sysforge.1|/usr/share/man/man1/sysforge.1"
  "completions/sysforge.bash|/usr/share/bash-completion/completions/sysforge"
  "completions/_sysforge|/usr/share/zsh/site-functions/_sysforge"
  "etc/sysforge/sysforge.toml|/etc/sysforge/sysforge.toml"
  "etc/sysforge/profiles.toml|/etc/sysforge/profiles.toml"
  "etc/sysforge/packages.toml|/etc/sysforge/packages.toml"
  "etc/sysforge/kernel.toml|/etc/sysforge/kernel.toml"
  "etc/sysforge/toolchain.toml|/etc/sysforge/toolchain.toml"
  "etc/pacman.d/hooks/sysforge-kernel.hook|/usr/share/libalpm/hooks/sysforge-kernel.hook"
  "etc/pacman.d/hooks/sysforge-toolchain.hook|/usr/share/libalpm/hooks/sysforge-toolchain.hook"
  "etc/pacman.d/hooks/sysforge-buildstate.hook|/usr/share/libalpm/hooks/sysforge-buildstate.hook"
  "tools/pacman-hook-helper.sh|/usr/lib/sysforge/pacman-hook-helper.sh"
)

link_one() {
  local src="$REPO/$1" tgt="$2"
  if [[ -e "$tgt" && ! -L "$tgt" ]]; then
    echo "! skip $tgt — real file exists (package-owned?); not clobbering"
    return 0
  fi
  # Only (re)link an absent target or one that is already OUR symlink (points
  # into this checkout). A symlink resolving elsewhere — a foreign or broken
  # link — is left alone rather than silently overwritten.
  if [[ -L "$tgt" && "$(readlink -f "$tgt")" != "$REPO/"* ]]; then
    echo "! skip $tgt — foreign symlink, not clobbering"
    return 0
  fi
  echo "+ symlink $tgt -> $src"
  sudo install -d "$(dirname "$tgt")"
  sudo ln -sfn "$src" "$tgt"
}

unlink_one() {
  local tgt="$2"
  if [[ -L "$tgt" && "$(readlink -f "$tgt")" == "$REPO/"* ]]; then
    echo "- remove $tgt"
    sudo rm -f "$tgt"
  fi
}

do_install() {
  echo "== dev-install from $REPO =="
  echo "-- editable venv entry point (uv pip install -e .) --"
  uv pip install -e "$REPO"
  for pair in "${MAPPING[@]}"; do link_one "${pair%%|*}" "${pair##*|}"; done
  if ! getent group sysforge >/dev/null; then
    echo "note: group 'sysforge' absent — install the package to provision"
    echo "      the group + state dirs (sysusers.d/tmpfiles.d not symlinked)."
  fi
  echo "== done =="
}

do_uninstall() {
  echo "== dev-uninstall from $REPO =="
  for pair in "${MAPPING[@]}"; do unlink_one "${pair%%|*}" "${pair##*|}"; done
  echo "-- uv pip uninstall sysforge --"
  uv pip uninstall sysforge || true
  echo "== done =="
}

case "${1:-}" in
  install) do_install ;;
  uninstall) do_uninstall ;;
  print-targets)  # used by check_shipped parity test
    for pair in "${MAPPING[@]}"; do echo "${pair##*|}"; done ;;
  *) echo "usage: $0 {install|uninstall|print-targets}" >&2; exit 2 ;;
esac
