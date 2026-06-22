# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
pacman_hooks.py — one home for installing/refreshing sysforge's libalpm hooks.

sysforge ships three pacman ``PostTransaction`` hooks plus a tiny helper
script:

    /usr/share/libalpm/hooks/sysforge-kernel.hook
    /usr/share/libalpm/hooks/sysforge-toolchain.hook
    /usr/share/libalpm/hooks/sysforge-buildstate.hook
    /usr/lib/sysforge/pacman-hook-helper.sh

They are the *input* side of ``sysforge update``'s reminder + auto-demote
logic (``update._consume_pacman_hook_sentinels`` /
``update._reconcile_external_demotions``). The PKGBUILD ``package()`` step is
the only thing that installs them — which leaves the **dev-checkout workflow**
(running sysforge from a source tree) with stale or entirely-missing hooks, and
gives no way to refresh an installed system after a hook source changes.

This primitive is the single home for locating the canonical hook/helper
*source* content, comparing it byte-for-byte against what is installed on the
system, and (when privileged) writing the missing/stale files into place. Both
``sysforge setup`` (provision) and ``doctor --pacman`` (read-only report)
consume it — no parallel logic.

Canonical source resolution (``shipped_sources``):

  1. **Repo checkout** — when sysforge runs from a source tree, the repo root
     (two parents up from this module) carries ``etc/pacman.d/hooks/*.hook`` and
     ``tools/pacman-hook-helper.sh``; those are authoritative.
  2. **Installed wheel** — otherwise the same files travel inside the wheel as
     package data under ``sysforge/_data/`` (via a ``force-include`` in
     ``pyproject.toml``), read through :mod:`importlib.resources`.

Writes go through :func:`fs_provision._run_priv` (the existing sudo-or-fail
privileged exec path); we never roll a second sudo path. A privileged failure
raises :class:`fs_provision.FsProvisionError`, which the caller turns into a
"re-run with sudo" hint rather than a crash.
"""
from __future__ import annotations

import importlib.resources as importlib_resources
from dataclasses import dataclass
from pathlib import Path

from sysforge.primitives import fs_provision

# Install destinations (fixed by the shipped hooks / PKGBUILD layout).
HOOK_DEST_DIR = Path("/usr/share/libalpm/hooks")
HELPER_DEST = Path("/usr/lib/sysforge/pacman-hook-helper.sh")

# Hook filenames shipped under etc/pacman.d/hooks/.
HOOK_NAMES = (
    "sysforge-kernel.hook",
    "sysforge-toolchain.hook",
    "sysforge-buildstate.hook",
)

HELPER_NAME = "pacman-hook-helper.sh"

# Install modes: hooks are plain data (0644); the helper is executed (0755).
_HOOK_MODE = 0o644
_HELPER_MODE = 0o755

# Comparison states.
STATE_OK = "ok"
STATE_MISSING = "missing"
STATE_STALE = "stale"


@dataclass(frozen=True)
class HookArtifact:
    """A single shipped file: its install destination, canonical bytes, mode."""
    dest: Path
    content: bytes
    mode: int

    @property
    def name(self) -> str:
        return self.dest.name


# ---------------------------------------------------------------------------
# Canonical source resolution
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """Repo root when running from a source checkout (…/sysforge/primitives/
    pacman_hooks.py → two parents up)."""
    return Path(__file__).resolve().parents[2]


def _read_repo_sources() -> list[HookArtifact] | None:
    """Read canonical content from the repo checkout, or None when not present
    (installed-wheel case)."""
    root = _repo_root()
    hooks_dir = root / "etc/pacman.d/hooks"
    helper = root / "tools" / HELPER_NAME
    if not hooks_dir.is_dir() or not helper.is_file():
        return None
    out: list[HookArtifact] = [
        HookArtifact(HOOK_DEST_DIR / name, (hooks_dir / name).read_bytes(), _HOOK_MODE)
        for name in HOOK_NAMES
    ]
    out.append(HookArtifact(HELPER_DEST, helper.read_bytes(), _HELPER_MODE))
    return out


def _read_wheel_sources() -> list[HookArtifact]:
    """Read canonical content from package data shipped inside the wheel."""
    data = importlib_resources.files("sysforge").joinpath("_data")
    out: list[HookArtifact] = [
        HookArtifact(HOOK_DEST_DIR / name,
                     data.joinpath("pacman-hooks", name).read_bytes(), _HOOK_MODE)
        for name in HOOK_NAMES
    ]
    out.append(HookArtifact(HELPER_DEST,
                            data.joinpath(HELPER_NAME).read_bytes(), _HELPER_MODE))
    return out


def shipped_sources() -> list[HookArtifact]:
    """Return the canonical hook + helper artifacts (repo checkout preferred,
    else the wheel's bundled package data)."""
    repo = _read_repo_sources()
    return repo if repo is not None else _read_wheel_sources()


# ---------------------------------------------------------------------------
# Comparison (pure read — no writes)
# ---------------------------------------------------------------------------

def _state_for(artifact: HookArtifact) -> str:
    try:
        installed = artifact.dest.read_bytes()
    except FileNotFoundError:
        return STATE_MISSING
    except OSError:
        # Unreadable (e.g. permissions) — treat as missing so setup re-installs.
        return STATE_MISSING
    return STATE_OK if installed == artifact.content else STATE_STALE


def diff_status() -> list[tuple[HookArtifact, str]]:
    """Compare each shipped artifact against what's installed. Pure read."""
    return [(art, _state_for(art)) for art in shipped_sources()]


def needs_provision(status: list[tuple[HookArtifact, str]] | None = None) -> bool:
    """True when any artifact is missing or stale."""
    rows = status if status is not None else diff_status()
    return any(state != STATE_OK for _, state in rows)


# ---------------------------------------------------------------------------
# Provisioning (privileged write via fs_provision._run_priv)
# ---------------------------------------------------------------------------

def provision(status: list[tuple[HookArtifact, str]] | None = None
              ) -> list[tuple[Path, str]]:
    """Install every missing/stale artifact via a privileged ``install``.

    Returns the list of ``(dest, state)`` that were written. Raises
    :class:`fs_provision.FsProvisionError` if a privileged step fails (caller
    prints a manual-sudo hint). Up-to-date artifacts are skipped.
    """
    rows = status if status is not None else diff_status()
    written: list[tuple[Path, str]] = []
    for art, state in rows:
        if state == STATE_OK:
            continue
        _install_artifact(art)
        written.append((art.dest, state))
    return written


def _install_artifact(art: HookArtifact) -> None:
    """Write one artifact to its destination with the right mode, privileged.

    ``install -D`` creates parent dirs and sets the mode atomically. We feed the
    canonical bytes through a temp file the privileged ``install`` reads, so we
    never depend on the source path being readable by root.
    """
    import tempfile

    with tempfile.NamedTemporaryFile("wb", delete=False) as tf:
        tf.write(art.content)
        tmp = tf.name
    try:
        fs_provision._run_priv([
            "install", "-Dm", f"{art.mode:o}", tmp, str(art.dest),
        ])
    finally:
        Path(tmp).unlink(missing_ok=True)
