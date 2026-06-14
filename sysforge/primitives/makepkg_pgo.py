# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
makepkg_pgo.py — PGO profdata state resolution

Pure helpers that answer "is a saved clang.profdata present and compatible with
the LLVM PKGBUILD about to be built?" for the ``pgo_llvm_toolchain`` build mode.
Reads ``toolchain.toml`` (``pgo_store``) and the profdata version sidecar; no
subprocess, no logging.  The PGO *emission* sites (``[PGO]`` tag) still live in
the build orchestrator's conf/run paths and migrate here when those split out.

Consumed by the build orchestrator (``makepkg_wrapper.run``) and the toolchain
provenance check (``llvm_state``); ``PGOBuildSkipped`` is re-raised up through
``run`` and caught by ``build_core``/``update``.
"""
import os
import re
import tomllib
from pathlib import Path

from sysforge.primitives.paths import TOOLCHAIN_PATH

# Regenerable PGO profdata cache. FHS: /var/cache is for regenerable cached
# data; the profraw/profdata here is always reproducible by re-running the
# toolchain stage, so it belongs under /var/cache rather than /var/tmp.
_DEFAULT_PGO_STORE = "/var/cache/sysforge/llvm-pgo"


def resolve_pgo_store(tcfg: dict | None) -> Path:
    """Resolve the PGO profdata store directory (single source of truth).

    Precedence: ``toolchain.toml [pgo_store]`` (explicit config wins) →
    ``SYSFORGE_PGO_STORE`` env override → the FHS default ``_DEFAULT_PGO_STORE``.
    """
    configured = (tcfg or {}).get("pgo_store")
    if configured:
        return Path(configured)
    env = os.environ.get("SYSFORGE_PGO_STORE")
    if env:
        return Path(env)
    return Path(_DEFAULT_PGO_STORE)


def _try_load_toml(path: Path) -> dict | None:
    """Load a TOML file, returning None on any error."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError):
        return None


class PGOBuildSkipped(Exception):
    """
    Raised by run() when build_mode is pgo_llvm_toolchain but profdata is
    absent or version-incompatible and the user chose to skip (or input is
    non-interactive).  Callers (e.g. update.py) should treat this as a
    deliberate skip rather than a build failure.
    """


def _resolve_pgo_state(pkgbuild_path: Path) -> tuple[str, str]:
    """
    Check whether a saved clang.profdata is present and compatible with the
    PKGBUILD being built.

    Returns one of:
      ("ready",    str(profdata_path))  — profdata exists, major version matches
      ("mismatch", reason_str)          — profdata exists but major version differs
      ("absent",   reason_str)          — profdata or sidecar missing / toolchain.toml absent
    """
    toolchain_path = TOOLCHAIN_PATH
    if not toolchain_path.exists():
        return ("absent", "toolchain.toml not found — no pgo_store configured")
    try:
        with open(toolchain_path, "rb") as f:
            tcfg = tomllib.load(f)
    except Exception as e:
        return ("absent", f"cannot read toolchain.toml: {e}")

    pgo_store = resolve_pgo_store(tcfg)
    profdata_path = pgo_store / "clang.profdata"
    version_path = pgo_store / "clang.profdata.version"

    if not profdata_path.exists():
        return ("absent", f"no profdata at {profdata_path}")
    if not version_path.exists():
        return ("absent", f"profdata version sidecar missing at {version_path}")

    saved_major = version_path.read_text().strip()

    # Extract the target LLVM major version from the PKGBUILD's pkgver line.
    try:
        content = pkgbuild_path.read_text(encoding="utf-8")
        m = re.search(r"^pkgver=([^\s\n]+)", content, re.MULTILINE)
        if not m:
            return ("absent", "cannot determine pkgver from PKGBUILD")
        target_major = m.group(1).split(".")[0]
    except OSError as e:
        return ("absent", f"cannot read PKGBUILD: {e}")

    if saved_major != target_major:
        return (
            "mismatch",
            f"profdata is from LLVM {saved_major}, building LLVM {target_major}",
        )

    return ("ready", str(profdata_path))
