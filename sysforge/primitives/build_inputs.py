# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
build_inputs.py — build inputs that change the artifact outside the profile.

Flag drift compares the serialized *profile* flags (``flags_string``). Several
settings change what a build produces without passing through the profile:

* ``python`` — the interpreter ``[build] python`` pins for ``build()``
  (``makepkg_env.resolve_build_python``). A package that installs into
  ``site-packages`` is tied to that interpreter's ``major.minor``.
* ``mesa_gallium`` / ``mesa_vulkan`` — the driver lists patched into a mesa
  PKGBUILD (``mesa_drivers.resolve_or_detect_mesa_drivers``).
* ``llvm_targets`` — ``LLVM_TARGETS_TO_BUILD`` patched into an LLVM PKGBUILD
  (``llvm_targets.resolve_or_detect_llvm_targets``).
* ``pgo_profile`` — the merged profile a rebuild reuses from the package's PGO
  store (``mesa_pgo.reuse_profdata``), spliced in via ``compiler_flags_extra``.

:func:`resolve_build_inputs` is the one resolver both sides call (3.2.0-F18):
``makepkg_wrapper._record_build_state`` records its result as the
``build_inputs`` field, and ``flag_drift`` re-derives it at drift time. Both
sides calling the same function is what guarantees a rebuild clears the drift
it reports (the 3.1.0-B11 rule). The value is serialized in ``flags_string``'s
own ``KEY=value`` line form, so ``flag_drift.diff_flags`` renders the diff.

Pure apart from the resolvers' own reads (config files, hardware profile, a
live ``lspci`` fallback for mesa/LLVM packages only) and one cached interpreter
probe. Never logs; never raises.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from sysforge.primitives.paths import SYSFORGE_TOML_PATH, TOOLCHAIN_PATH

_PY_NAME_RE = re.compile(r"python(\d+\.\d+)$")


def _pkgbase(pkgmeta: dict) -> str | None:
    g = pkgmeta.get("globals", {})
    name = g.get("pkgbase") or g.get("pkgname")
    if isinstance(name, list):
        name = name[0] if name else None
    return name or None


def is_python_package(pkgmeta: dict) -> bool:
    """Whether the package runtime-depends on python (``python`` / ``python-*``).

    Runtime ``depends`` only: a package installing into ``site-packages``
    depends on the interpreter, while one that runs python only during the
    build (meson, a code generator) is unaffected by a python minor bump.
    """
    deps = pkgmeta.get("globals", {}).get("depends") or []
    if isinstance(deps, str):
        deps = [deps]
    for dep in deps:
        name = re.split(r"[<>=:]", str(dep), maxsplit=1)[0].strip()
        if name == "python" or name.startswith("python-"):
            return True
    return False


@lru_cache(maxsize=8)
def _python_minor(interp: str) -> str | None:
    """``major.minor`` of ``interp``: from its resolved name, else a probe."""
    m = _PY_NAME_RE.search(Path(interp).resolve().name)
    if m:
        return m.group(1)
    from sysforge.primitives.run import capture
    cp = capture([interp, "-c",
                  "import sys; print('%d.%d' % sys.version_info[:2])"])
    if cp is None or cp.returncode != 0:
        return None
    out = cp.stdout.strip()
    return out if re.fullmatch(r"\d+\.\d+", out) else None


def _resolve_python(sysforge_cfg: dict | None) -> str | None:
    from sysforge.primitives.makepkg_env import resolve_build_python
    interp = resolve_build_python(sysforge_cfg)
    return _python_minor(str(interp)) if interp is not None else None


def resolve_build_inputs(pkgmeta: dict, *, state_dir: Path, cc: str | None,
                         owner_stage: str | None = None,
                         sysforge_cfg: dict | None = None) -> dict[str, str]:
    """The out-of-profile build inputs that apply to this package, by key.

    Only the inputs that apply are present; an absent key means "not an input
    for this package" (a non-mesa package has no ``mesa_*``, an unfiltered mesa
    has none either). ``cc`` is the build's compiler, which gates
    ``pgo_profile`` (a clang profile only feeds a clang build). ``owner_stage``
    set means a stage drives PGO itself (``pgo_managed``), so the store reuse
    does not apply. ``state_dir`` locates ``hardware_profile.toml``.
    """
    from sysforge.primitives.pkgbuild_patcher import is_llvm_pkgbase
    from sysforge.primitives.profile import is_llvm_toolchain, is_mesa_pkgbase

    inputs: dict[str, str] = {}
    pkgbase = _pkgbase(pkgmeta)
    hw_profile = Path(state_dir) / "hardware_profile.toml"

    if is_python_package(pkgmeta):
        minor = _resolve_python(sysforge_cfg)
        if minor:
            inputs["python"] = minor

    if is_mesa_pkgbase(pkgbase):
        from sysforge.primitives.mesa_drivers import resolve_or_detect_mesa_drivers
        drivers = resolve_or_detect_mesa_drivers(SYSFORGE_TOML_PATH, hw_profile)
        if drivers:
            inputs["mesa_gallium"] = ",".join(drivers["gallium"])
            inputs["mesa_vulkan"] = ",".join(drivers["vulkan"])

    # lib32-* LLVM is never target-filtered (makepkg_wrapper._maybe_patch_llvm_targets).
    if is_llvm_pkgbase(pkgbase) and not (pkgbase or "").startswith("lib32-"):
        from sysforge.primitives.llvm_targets import resolve_or_detect_llvm_targets
        targets = resolve_or_detect_llvm_targets(TOOLCHAIN_PATH, hw_profile)
        if targets:
            inputs["llvm_targets"] = ";".join(targets)

    if pkgbase and owner_stage is None and is_llvm_toolchain(cc or ""):
        from sysforge.primitives import mesa_pgo
        profile = mesa_pgo.reuse_profdata(pkgbase=pkgbase)
        if profile is not None:
            inputs["pgo_profile"] = str(profile)

    return inputs


def serialize_build_inputs(inputs: dict[str, str]) -> str:
    """``KEY=value`` lines, sorted — the ``flags_string`` form ``diff_flags`` reads."""
    return "".join(f"{k}={inputs[k]}\n" for k in sorted(inputs))
