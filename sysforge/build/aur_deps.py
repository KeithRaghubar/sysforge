# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
aur_deps.py — build and install resolved AUR dependencies (3.2.0-F4).

Moved out of ``primitives/aur_resolve.py``: *resolving* the dependency graph is
leaf work and stays there, but *building* it drives ``makepkg_wrapper.run`` per
dependency, which is orchestration — the same reason ``makepkg_wrapper`` itself
left the primitives layer.

Public API:
    build_resolved_deps(deps, *, profile_conf, cc_override, cxx_override,
                        ld_override, state_dir, interactive) -> list[str]
"""
from __future__ import annotations

import time
from pathlib import Path

from sysforge import log
from sysforge.build import makepkg_wrapper
from sysforge.primitives import progress_hooks
from sysforge.primitives.aur_resolve import ResolvedDep
from sysforge.primitives.build_sandbox import register_artifacts
from sysforge.primitives.makepkg_artifacts import _find_built_packages
from sysforge.primitives.pacman import get_pkgdest

_log = log.get_logger("AUR_RESOLVE")  # unchanged tag across the move


def build_resolved_deps(
    deps: list[ResolvedDep],
    *,
    profile_conf: str | None = None,
    cc_override: str | None = None,
    cxx_override: str | None = None,
    ld_override: str | None = None,
    state_dir: Path | None = None,
    interactive: bool = False,
) -> list[str]:
    """Build and install AUR deps in topological order.

    Each dep is built via makepkg_wrapper.run() and then installed by sysforge
    (``install_built_packages``) before the next one builds, so it is available
    to its dependents. makepkg never installs here (3.4.0-F1): a ``-i`` ran
    ``sudo pacman -U`` inside the build's forwarded output, where a password
    prompt could not pause or hide the progress bar. The install now goes
    through the privilege seam and the logged pacman transaction like every
    other sysforge install — and a dependency PKGDEST already holds
    (AlreadyBuilt) is installed too, instead of aborting the dependency arm.
    ``interactive`` is threaded through so a ``build --interactive`` run keeps
    live output / prompts for the dependency builds too, not only the main
    target.

    Returns list of successfully built dep names.
    """
    # Opens the "AUR dep" tracker; refuse before any build (3.3.0-F2).
    progress_hooks.hooks().require_no_tracker("build_resolved_deps")

    aur_deps = [
        (d, d.pkgbuild_path) for d in deps
        if d.source == "aur" and d.pkgbuild_path is not None
    ]
    if not aur_deps:
        return []

    _log.ui(f"Building {len(aur_deps)} AUR dependency(ies) before main package")

    built: list[str] = []
    with progress_hooks.hooks().tracker(len(aur_deps), "AUR dep") as _tick:
        for i, (dep, dep_pkgbuild) in enumerate(aur_deps):
            req = ", ".join(dep.required_by)
            _tick(dep.name)
            _log.ui(f"  [{i + 1}/{len(aur_deps)}] {dep.name} (required by {req})")

            opts = makepkg_wrapper.BuildOptions(
                profile_conf=profile_conf,
                cc_override=cc_override,
                cxx_override=cxx_override,
                ld_override=ld_override,
                state_dir=state_dir,
                init_session=(i == 0),
                pkg_log=False,
                interactive=interactive,
            )
            _dep_start = time.time()
            try:
                makepkg_wrapper.run(dep_pkgbuild, options=opts)
            except makepkg_wrapper.AlreadyBuilt:
                _log.info(f"  {dep.name}: already built — installing the existing package")
            _tick.note(f"installing {dep.name}")
            makepkg_wrapper.install_built_packages(
                dep_pkgbuild.parent, noconfirm=not interactive)
            _tick.resume()
            built.append(dep.name)
            # Under the build sandbox a dep installed on the host is invisible
            # to the next container; register its artifacts so the seam can
            # hand them back in as ``-I``. Inert while the sandbox is off.
            _dep_dest = get_pkgdest() or dep_pkgbuild.parent
            register_artifacts(
                p for p in _find_built_packages(_dep_dest)
                if p.stat().st_mtime >= _dep_start
            )

    _log.ui(f"All {len(built)} dependency(ies) built and installed")
    return built
