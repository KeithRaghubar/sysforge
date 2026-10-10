# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
mesa_smoke.py — does the installed mesa actually create a context? (3.2.0-F10)

The other graphics guards check build inputs or static properties of the
result (target filtering, the software-driver baseline, libgallium's LLVM
symbols). This one loads the stack: it creates and tears down an EGL context
and enumerates Vulkan devices through mesa, so a driver trimmed from the build,
a ``MESA_WHICH_LLVM`` mismatch, a ``-march`` past this CPU, or a miscompiled
entry point is reported while the user still has a working session.

Two pins keep the probe honest:

* EGL goes through glvnd, which tries ``10_nvidia.json`` before mesa on an
  Nvidia host, so ``__EGL_VENDOR_LIBRARY_FILENAMES`` names mesa's vendor file
  and ``VK_DRIVER_FILES`` lists only ICDs a mesa-family package owns —
  otherwise the check would pass on the proprietary driver.
* ``eglinfo -p surfaceless`` needs no display server, so a headless host is
  checked, not skipped. Skips are reserved for a missing tool, vendor file or
  ICD set.

Hardware drivers (radeonsi, iris, RADV, ANV) bind through ``/dev/dri`` on
their own hosts; on the Nvidia test host only llvmpipe/lavapipe are exercised.

Public API:
    check_egl(*, software=False) / check_vulkan() -> GraphicsFinding | diag.Skip | None
    run_smoke() -> diag.AxisResult
    mesa_vendor_file() -> Path | None / mesa_icds() -> list[Path]
    mesa_stack(fields=None) -> set[str]
    should_check(changed, state_dir, *, stack=None) -> bool
    post_install(changed, state_dir) -> diag.AxisResult | None
"""
from __future__ import annotations

import dataclasses
import os
import re
import subprocess
from pathlib import Path

from sysforge import log
from sysforge.primitives import diagnostics as diag
from sysforge.primitives import pacman, run
from sysforge.primitives.graphics_probe import SEV_ERROR, GraphicsFinding
from sysforge.primitives.profile import is_mesa_family

_log = log.get_logger("MESA")

SMOKE_TIMEOUT_S = 30

_EGL_VENDOR_DIR = Path("/usr/share/glvnd/egl_vendor.d")
_ICD_DIR = Path("/usr/share/vulkan/icd.d")
_LAVAPIPE_ICD = "lvp_icd.json"
_SOFTWARE_RENDERERS = ("llvmpipe", "softpipe")
_RENDERER_RE = re.compile(r"^OpenGL .*?profile renderer:\s*(?P<r>.+)$", re.MULTILINE)
_DEVICE_RE = re.compile(r"^\s*deviceName\s*=\s*(?P<n>.+)$", re.MULTILINE)

#: Returned by :func:`_probe` when the command outlived ``SMOKE_TIMEOUT_S``.
_TIMEOUT = "timeout"


def mesa_vendor_file() -> Path | None:
    """mesa's glvnd EGL vendor JSON (``50_mesa.json`` on Arch), or None."""
    if not _EGL_VENDOR_DIR.is_dir():
        return None
    found = sorted(_EGL_VENDOR_DIR.glob("*_mesa.json"))
    return found[0] if found else None


def mesa_icds() -> list[Path]:
    """Vulkan ICD manifests owned by a mesa-family package (one ``pacman -Qo``).

    An undetermined owner (the lookup failed wholesale) is treated as "not
    mesa", so a locked DB yields an empty list (a skip), never a probe of the
    proprietary ICD.
    """
    if not _ICD_DIR.is_dir():
        return []
    icds = sorted(_ICD_DIR.glob("*.json"))
    if not icds:
        return []
    owners = pacman.owners_of(icds)
    out: list[Path] = []
    for p in icds:
        owner = owners.get(p)
        if owner and is_mesa_family(pacman.get_pkgbase(owner) or owner):
            out.append(p)
    return out


def _probe(cmd: list[str], env_extra: dict[str, str]):
    """Run *cmd* with *env_extra* over the current environment.

    Returns the CompletedProcess, ``None`` when the binary is missing, or
    :data:`_TIMEOUT` when it hung — a hang creating a context is a failure.
    """
    try:
        return run.capture(cmd, env={**os.environ, **env_extra},
                           timeout=SMOKE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _TIMEOUT


def _tail(text: str | None, n: int = 3) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return " | ".join(lines[-n:]) if lines else "(no output)"


#: Hardware Vulkan ICDs whose GPU we can name from ``lspci``. nouveau is left
#: out: on an Nvidia host the proprietary driver owns the GPU, so a nouveau
#: ICD enumerating nothing is expected, not a fault.
_ICD_GPU_VENDOR = {
    "radeon_icd.json": "amd",
    "intel_icd.json": "intel",
    "intel_hasvk_icd.json": "intel",
}


def _gpu_vendors() -> list[str]:
    from sysforge.primitives.hardware_probe import parse_gpu_vendors
    r = run.capture(["lspci", "-nn"])
    return parse_gpu_vendors(r.stdout) if r is not None and r.returncode == 0 else []


def _failure(check_id: str, message: str) -> GraphicsFinding:
    """A probe failure. :func:`run_smoke` replaces the remediation with one
    naming the source-built packages to roll back."""
    return GraphicsFinding(SEV_ERROR, check_id, message,
                           "run `sysforge doctor --graphics` for the full picture")


def _remediation(culprits: list[str]) -> str:
    if culprits:
        return (f"`sysforge revert-to-stock {' '.join(sorted(culprits))}` while this "
                "session still works, then `sysforge doctor --graphics`")
    return ("no package in the mesa stack is source-built; reinstall the mesa "
            "packages from the repos (`sudo pacman -S <pkg>`), then "
            "`sysforge doctor --graphics`")


def _source_built_stack() -> list[str]:
    try:
        return sorted(mesa_stack() & _source_built(None))
    except Exception:  # noqa: BLE001 — the hint degrades, the finding stands
        return []


def check_egl(*, software: bool = False) -> GraphicsFinding | diag.Skip | None:
    """Create a surfaceless EGL context through mesa (``software`` forces llvmpipe)."""
    check_id = "mesa_egl_software" if software else "mesa_egl"
    vendor = mesa_vendor_file()
    if vendor is None:
        return diag.Skip("no mesa EGL vendor file")
    env = {"__EGL_VENDOR_LIBRARY_FILENAMES": str(vendor)}
    if software:
        env["LIBGL_ALWAYS_SOFTWARE"] = "1"
    r = _probe(["eglinfo", "-B", "-p", "surfaceless"], env)
    if r is None:
        return diag.Skip("eglinfo not installed (mesa-utils)")
    if r is _TIMEOUT:
        return _failure(check_id, f"eglinfo did not finish within {SMOKE_TIMEOUT_S}s "
                                  "(mesa hung creating an EGL context)")
    m = _RENDERER_RE.search(r.stdout or "")
    if r.returncode != 0 or m is None:
        return _failure(check_id, f"mesa could not create an EGL context "
                                  f"(eglinfo exit {r.returncode}): {_tail(r.stderr)}")
    renderer = m.group("r").strip()
    if software and not renderer.startswith(_SOFTWARE_RENDERERS):
        return _failure(check_id, f"forced software rendering gave {renderer!r}, "
                                  "not llvmpipe/softpipe")
    return None


def check_vulkan() -> GraphicsFinding | diag.Skip | None:
    """Enumerate Vulkan devices through mesa's ICDs only."""
    icds = mesa_icds()
    if not icds:
        return diag.Skip("no mesa Vulkan ICDs installed")
    r = _probe(["vulkaninfo", "--summary"],
               {"VK_DRIVER_FILES": ":".join(str(p) for p in icds)})
    if r is None:
        return diag.Skip("vulkaninfo not installed (vulkan-tools)")
    if r is _TIMEOUT:
        return _failure("mesa_vulkan", f"vulkaninfo did not finish within "
                                       f"{SMOKE_TIMEOUT_S}s (mesa hung enumerating devices)")
    devices = _DEVICE_RE.findall(r.stdout or "")
    if r.returncode == 0 and devices:
        return None
    has_lavapipe = any(p.name == _LAVAPIPE_ICD for p in icds)
    if not devices and not has_lavapipe and r.returncode >= 0:
        # Only hardware ICDs. A skip only when none of them is for a GPU this
        # host has (vulkan-radeon on Nvidia); RADV/ANV enumerating nothing on
        # their own GPU is the regression this check exists for.
        present = _gpu_vendors()
        if not any(_ICD_GPU_VENDOR.get(p.name) in present for p in icds):
            return diag.Skip("no installed mesa Vulkan driver matches this hardware")
    return _failure("mesa_vulkan", f"mesa Vulkan enumerated {len(devices)} device(s) "
                                   f"(vulkaninfo exit {r.returncode}): {_tail(r.stderr)}")


def run_smoke(*, culprits: list[str] | None = None) -> diag.AxisResult:
    """Run all three probes; findings plus the ran/skipped roster.

    ``culprits`` are the source-built mesa-stack members a failure's
    remediation names for ``revert-to-stock``; ``None`` (the ``doctor`` path)
    computes them only if a probe fails.
    """
    roster = diag.Roster()
    findings: list[GraphicsFinding] = []
    for check_id, fn in (
        ("mesa_egl", check_egl),
        ("mesa_egl_software", lambda: check_egl(software=True)),
        ("mesa_vulkan", check_vulkan),
    ):
        f = diag.record(roster, check_id, fn())
        if f is not None:
            findings.append(f)
    if findings:
        hint = _remediation(_source_built_stack() if culprits is None else culprits)
        findings = [dataclasses.replace(f, remediation=hint) for f in findings]
    return diag.AxisResult(findings, roster)


# ---------------------------------------------------------------------------
# When to run after an install
# ---------------------------------------------------------------------------

_DEP_NAME_RE = re.compile(r"[<>=]")


def _dep_name(dep: str) -> str:
    return _DEP_NAME_RE.split(dep, maxsplit=1)[0].strip()


def mesa_stack(fields: dict[str, dict[str, list[str]]] | None = None) -> set[str]:
    """Installed mesa-family packages plus their runtime ``depends`` closure.

    Virtual names resolve through ``%PROVIDES%``; ``lib32-*`` members are left
    out because a 64-bit probe cannot load them. ``fields`` is
    :func:`pacman.get_all_package_fields` output (read here when omitted).
    """
    if fields is None:
        fields = pacman.get_all_package_fields("%BASE%", "%DEPENDS%", "%PROVIDES%")

    def _base(name: str) -> str:
        base = fields[name].get("%BASE%") or []
        return base[0] if base else name

    providers: dict[str, str] = {}
    for name, f in fields.items():
        for prov in f.get("%PROVIDES%", []):
            providers.setdefault(_dep_name(prov), name)

    todo = [n for n in fields
            if not n.startswith("lib32-") and is_mesa_family(_base(n))]
    stack: set[str] = set()
    while todo:
        name = todo.pop()
        if name in stack:
            continue
        stack.add(name)
        for dep in fields[name].get("%DEPENDS%", []):
            dn = _dep_name(dep)
            target = dn if dn in fields else providers.get(dn)
            if target and target not in stack and not target.startswith("lib32-"):
                todo.append(target)
    return stack


def _source_built(state_dir) -> set[str]:
    from sysforge.primitives.build_sandbox import source_built_packages
    return source_built_packages(state_dir)


def should_check(changed: set[str] | None, state_dir, *,
                 stack: set[str] | None = None) -> bool:
    """Run the post-install probe when the mesa stack holds a source-built
    package AND this run changed a stack member (``changed=None`` = unknown,
    which errs toward running). An all-stock stack is what the distribution
    shipped and tested together, so it never triggers."""
    stack = mesa_stack() if stack is None else stack
    if not stack or not (stack & _source_built(state_dir)):
        return False
    return changed is None or bool(changed & stack)


def _post_install_plan(changed: set[str] | None, state_dir) -> list[str] | None:
    """The source-built stack members to name on failure, or ``None`` when the
    post-install check should not run (one stack walk for both answers)."""
    stack = mesa_stack()
    if not should_check(changed, state_dir, stack=stack):
        return None
    return sorted(stack & _source_built(state_dir))


def post_install(changed: set[str] | None, state_dir) -> diag.AxisResult | None:
    """Advisory post-install smoke check. Never raises; never changes the
    caller's outcome — the packages are already installed, so a failure's job
    is to name the problem and the rollback while the session still works."""
    try:
        culprits = _post_install_plan(changed, state_dir)
        if culprits is None:
            return None
        result = run_smoke(culprits=culprits)
    except Exception as exc:  # noqa: BLE001 — advisory backstop
        _log.warn(f"post-install mesa smoke check could not run: {exc}")
        return None
    roster = result.roster or diag.Roster()
    for check_id, why in roster.skipped:
        _log.info(f"post-install: {check_id} not checked: {why}")
    for f in result.findings:
        _log.error(f"post-install: {f.message}")
        if f.remediation:
            _log.error(f"post-install: → {f.remediation}")
    if not result.findings and roster.ran:
        _log.info(f"post-install: mesa smoke check passed ({', '.join(roster.ran)})")
    return result
