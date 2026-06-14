# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
makepkg_env.py — subprocess env resolution for makepkg builds

Resolves which profile keys travel to the build via the inherited process
environment rather than the temp ``makepkg.conf`` (makepkg does not export
``CC``/``CXX`` and friends from the conf to child processes), and locates the
directory makepkg actually built in for side-car log diagnosis.  Owns the
``[ENV]`` tag.

Consumed by the build orchestrator (``makepkg_wrapper._run_build`` /
``invoke_makepkg``); ``resolve_env_vars`` is re-exported from ``makepkg_wrapper``
for its existing direct-import test surface.
"""
import os
from pathlib import Path

from sysforge import log
from sysforge.primitives.profile import CONF_KEY_MAP, SYSFORGE_KEYS

_env_log = log.get_logger("ENV")


def resolve_env_vars(resolved_profile, active_consumes=None):
    """
    Extract profile keys that travel via subprocess env injection rather than
    the makepkg.conf temp file.

    Three categories are collected:
      1. Keys in the "toolchain" conf type (CC, CXX) — always injected,
         regardless of active_consumes. makepkg does not export CC/CXX from
         makepkg.conf to child processes; they must be in the inherited env.
      2. Keys in the "env" conf type — only collected when "env" is in
         active_consumes or active_consumes is None (fallback mode).
      3. Unknown keys — not in any CONF_KEY_MAP type and not in SYSFORGE_KEYS.
         Always collected and logged under [ENV] as a warning.

    Returns dict[str, str] of key -> value pairs to inject on invocation.
    Empty dict if nothing to inject.
    """
    toolchain_keys = CONF_KEY_MAP.get("toolchain", set())
    env_type_keys  = CONF_KEY_MAP.get("env", set())

    # All keys explicitly classified into any conf type
    all_conf_keys: set[str] = set()
    for keys in CONF_KEY_MAP.values():
        all_conf_keys.update(keys)

    collect_env_type = active_consumes is None or "env" in active_consumes

    result: dict[str, str] = {}
    unknown: list[str] = []

    for key, val in resolved_profile.items():
        if key in SYSFORGE_KEYS:
            continue

        if key in toolchain_keys:
            # Always delivered via env — makepkg doesn't export CC/CXX from conf
            result[key] = val
            _env_log.info(f"Injecting (toolchain): {key}={val!r}")
            continue

        if key in env_type_keys:
            if collect_env_type:
                result[key] = val
                _env_log.info(f"Injecting (env type): {key}={val!r}")
            else:
                _env_log.info(f"Skipping env-type key {key!r} (not in active_consumes)")
            continue

        if key not in all_conf_keys:
            # Unknown key — not classified; env pass with warning
            result[key] = val
            unknown.append(key)

    if unknown:
        _env_log.warn(f"Unclassified profile keys injected via env (consider adding to CONF_KEY_MAP): {sorted(unknown)}")

    return result


def _effective_build_dir(pkgbuild_path, resolved_profile) -> Path:
    """Return the directory makepkg actually built in, for side-car diagnosis.

    With ``BUILDDIR`` set in the profile, env, or system ``makepkg.conf``,
    makepkg builds under ``$BUILDDIR/<pkgbase>`` rather than in-place, so the
    meson/cmake logs live there — not under the PKGBUILD dir. Best-effort: uses
    the PKGBUILD dir name as the pkgbase (true for AUR ``-git`` checkouts) and
    falls back to the PKGBUILD dir when that candidate doesn't exist.
    """
    from sysforge.primitives.pacman import get_builddir

    pkgbuild_dir = Path(pkgbuild_path).parent
    # Per-build profile override wins; otherwise resolve from env/system
    # makepkg.conf (a user may set BUILDDIR only in /etc/makepkg.conf).
    builddir = resolved_profile.get("BUILDDIR") or get_builddir()
    if builddir:
        expanded = Path(os.path.expanduser(os.path.expandvars(str(builddir))))
        candidate = expanded / pkgbuild_dir.name
        if (candidate / "src").is_dir():
            return candidate
    return pkgbuild_dir


def _logdest_tail(pkgbuild_path, max_bytes: int = 256 * 1024) -> list[str]:
    """Return the tail lines of the newest ``LOGDEST`` build log for diagnosis.

    With ``OPTIONS+=log``, makepkg writes the full build output to
    ``$LOGDEST/<pkgbase>-<ver>-<arch>-<stage>.log`` instead of the terminal.
    In the interactive failure path makepkg's stdout was never captured, so
    that file is the only record of *why* the build failed. Best-effort:
    matches on the PKGBUILD dir name as the pkgbase, returns ``[]`` when
    ``LOGDEST`` is unset or no matching log exists.
    """
    from sysforge.primitives.pacman import get_logdest

    logdest = get_logdest()
    if logdest is None or not logdest.is_dir():
        return []
    pkgbase = Path(pkgbuild_path).parent.name
    hits = sorted(
        logdest.glob(f"{pkgbase}-*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not hits:
        return []
    newest = hits[0]
    try:
        with newest.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()
