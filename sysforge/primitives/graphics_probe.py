# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
graphics_probe.py — system-state graphics/windowing health checks

Complements sysforge/doctor.py's per-package ABI walk with pure system
probes: kernel params, module parameters, driver-version skew, Wayland
compositor protocol advertisement, multilib, session type, Steam client
config. These are the classes of breakage that don't show up in
`ldconfig -p` / NEEDED-symbol checks but are high-likelihood causes of
black windows, XWayland render failures, and "works under X11, broken
under Wayland" bugs on NVIDIA.

All checks are read-only and safe on systems where the probe target is
absent — missing files, missing commands, and unsupported vendor
combinations short-circuit silently rather than erroring.

Public API:
    check_system_graphics(config, *, gpu_vendors=None) -> list[GraphicsFinding]
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sysforge.primitives import pacman


SEV_ERROR = "error"
SEV_WARN = "warn"
SEV_INFO = "info"


@dataclass(frozen=True)
class GraphicsFinding:
    severity: str       # SEV_ERROR | SEV_WARN | SEV_INFO
    check_id: str       # short stable id, e.g. "nvidia_modeset"
    message: str
    remediation: str = ""


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _read_text(path: Path) -> str | None:
    """Read a small file; return None on permission error or missing."""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, IsADirectoryError, OSError):
        return None


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    """Run a command; return None if binary is missing."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None


def _kernel_major_minor() -> tuple[int, int] | None:
    """Parse `uname -r` leading X.Y. Return None on failure."""
    r = _run(["uname", "-r"])
    if not r or r.returncode != 0:
        return None
    m = re.match(r"^(\d+)\.(\d+)", r.stdout.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _check_nvidia_modeset() -> GraphicsFinding | None:
    """
    Verify `nvidia-drm.modeset=1`. Either kernel cmdline OR modprobe.d must
    enable it. We probe both the runtime parameter (/sys) and the cmdline
    (fallback when sysfs is restricted, which happens on some hardened
    kernels).
    """
    sys_path = Path("/sys/module/nvidia_drm/parameters/modeset")
    sys_val = _read_text(sys_path)
    if sys_val is not None:
        if sys_val.strip() in ("Y", "1", "y"):
            return None
        return GraphicsFinding(
            SEV_ERROR, "nvidia_modeset",
            "nvidia-drm modeset disabled — Wayland compositors cannot present "
            "through NVIDIA GBM. Games render black or hang on first frame.",
            "Add `options nvidia-drm modeset=1` to /etc/modprobe.d/nvidia.conf "
            "and regenerate initramfs (e.g. `sudo mkinitcpio -P`). Reboot.",
        )

    # sysfs unreadable — fall back to /proc/cmdline.
    cmdline = _read_text(Path("/proc/cmdline")) or ""
    if "nvidia-drm.modeset=1" in cmdline or "nvidia_drm.modeset=1" in cmdline:
        return None
    return GraphicsFinding(
        SEV_WARN, "nvidia_modeset",
        "cannot confirm nvidia-drm.modeset=1: /sys unreadable and "
        "kernel cmdline does not contain the flag.",
        "Add `nvidia-drm.modeset=1` to kernel cmdline or "
        "`options nvidia-drm modeset=1` to /etc/modprobe.d/nvidia.conf.",
    )


def _check_nvidia_fbdev() -> GraphicsFinding | None:
    """
    Recommend nvidia-drm.fbdev=1 on kernel >= 6.11. Only emits a finding when
    the fbdev parameter exists in /sys (i.e. the installed driver supports
    it) and is off. Older drivers silently lack the parameter — not a
    failure.
    """
    kver = _kernel_major_minor()
    if kver is None or kver < (6, 11):
        return None
    sys_path = Path("/sys/module/nvidia_drm/parameters/fbdev")
    if not sys_path.exists():
        return None
    val = _read_text(sys_path)
    if val is None or val.strip() in ("Y", "1", "y"):
        return None
    return GraphicsFinding(
        SEV_WARN, "nvidia_fbdev",
        "nvidia-drm fbdev disabled on kernel >= 6.11 — NVIDIA recommends "
        "fbdev=1 for Wayland presentation with recent kernels.",
        "Add `options nvidia-drm fbdev=1` alongside `modeset=1` in "
        "/etc/modprobe.d/nvidia.conf and regenerate initramfs.",
    )


_NVIDIA_PKG_CANDIDATES = (
    "nvidia", "nvidia-dkms", "nvidia-open", "nvidia-open-dkms",
)


def _installed_version(pkgname: str) -> str | None:
    """Return the installed version of pkgname (stripped of any -git suffix)."""
    installed = pacman.get_all_installed_packages()
    ver = installed.get(pkgname)
    if ver is None:
        return None
    # Drop pkgrel when comparing user-visible driver versions — upstream
    # rev is what matters for skew, not Arch packaging rev.
    return ver.split("-", 1)[0]


def _check_nvidia_driver_skew() -> GraphicsFinding | None:
    """
    NVIDIA's kernel module, userspace utils, and 32-bit userspace utils must
    all be the same upstream driver version. A mismatch here is the single
    most common cause of sudden-onset "Steam worked yesterday, black today"
    reports after a partial upgrade.
    """
    kmod = None
    for name in _NVIDIA_PKG_CANDIDATES:
        v = _installed_version(name)
        if v is not None:
            kmod = (name, v)
            break
    utils = _installed_version("nvidia-utils")
    lib32 = _installed_version("lib32-nvidia-utils")

    if kmod is None and utils is None:
        return None  # no NVIDIA stack installed

    versions: list[tuple[str, str]] = []
    if kmod is not None:
        versions.append(kmod)
    if utils is not None:
        versions.append(("nvidia-utils", utils))
    if lib32 is not None:
        versions.append(("lib32-nvidia-utils", lib32))

    distinct = {v for _, v in versions}
    if len(distinct) <= 1:
        return None

    joined = ", ".join(f"{n}={v}" for n, v in versions)
    return GraphicsFinding(
        SEV_ERROR, "nvidia_driver_skew",
        f"NVIDIA driver packages are on different versions: {joined}. "
        "Kernel module and userspace must match.",
        "Run `sudo pacman -Syu` to resynchronize. If one of them is pinned, "
        "unpin it or align the rest to match.",
    )


def _check_nvidia_module_loaded(gpu_vendors: list[str]) -> GraphicsFinding | None:
    """NVIDIA GPU detected but kernel module not loaded."""
    if "nvidia" not in gpu_vendors:
        return None
    r = _run(["lsmod"])
    if r is None or r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("nvidia "):
            return None
    return GraphicsFinding(
        SEV_ERROR, "nvidia_module_loaded",
        "NVIDIA GPU present but nvidia kernel module is not loaded.",
        "Check `dmesg | grep -i nvidia` for load failures; typically a "
        "DKMS rebuild against the running kernel is needed.",
    )


def _check_multilib_enabled(gpu_vendors: list[str]) -> GraphicsFinding | None:
    """
    32-bit Steam games need lib32-* Vulkan/GL. [multilib] must be enabled in
    /etc/pacman.conf. Only flagged when an NVIDIA/AMD/Intel GPU is present
    (servers without gaming don't need this).
    """
    if not any(v in gpu_vendors for v in ("nvidia", "amd", "intel")):
        return None
    conf = _read_text(Path("/etc/pacman.conf"))
    if conf is None:
        return None
    # [multilib] section header, not commented out
    if re.search(r"^\s*\[multilib\]", conf, re.MULTILINE):
        return None
    return GraphicsFinding(
        SEV_ERROR, "multilib_enabled",
        "[multilib] is not enabled in /etc/pacman.conf. 32-bit Steam games "
        "cannot load lib32 Vulkan/GL drivers without it.",
        "Uncomment the `[multilib]` section and its `Include` line in "
        "/etc/pacman.conf, then run `sudo pacman -Sy`.",
    )


def _check_session_type() -> GraphicsFinding | None:
    """Always-info — attaches session context so the report is self-contained."""
    sess = os.environ.get("XDG_SESSION_TYPE", "")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if not sess and not desktop:
        return None
    return GraphicsFinding(
        SEV_INFO, "session_type",
        f"session: XDG_SESSION_TYPE={sess or '(unset)'} "
        f"XDG_CURRENT_DESKTOP={desktop or '(unset)'}",
    )


def _check_xwayland_present(installed: dict[str, str]) -> GraphicsFinding | None:
    """
    On a Wayland session, Steam and most games still render through
    XWayland. If xwayland is missing the black-window failure is trivial.
    """
    if os.environ.get("XDG_SESSION_TYPE") != "wayland":
        return None
    candidates = ("xwayland", "xwayland-git", "xorg-xwayland", "xorg-xwayland-git")
    if any(c in installed for c in candidates):
        return None
    return GraphicsFinding(
        SEV_ERROR, "xwayland_present",
        "session is Wayland but xwayland is not installed. X11-only games "
        "(most Steam titles) will fail to present.",
        "Install `xorg-xwayland`.",
    )


def _check_explicit_sync_protocol(
    gpu_vendors: list[str],
) -> GraphicsFinding | None:
    """
    NVIDIA + Wayland XWayland rendering requires the compositor to
    advertise the `linux-drm-syncobj-v1` protocol — the actual
    wl_registry global is `wp_linux_drm_syncobj_manager_v1` — or the
    older `zwp_linux_explicit_synchronization_v1`. Without it XWayland
    falls back to implicit sync, which is known-broken on NVIDIA —
    producing black windows for most Steam games.

    We probe via `wayland-info`, which reads the compositor's advertised
    globals. If the tool isn't installed we can't check — emit nothing
    rather than a false positive. The substring match has to be the
    *registry* global name, not the protocol-document name: a probe for
    the bare `wp_linux_drm_syncobj_v1` string never matches because no
    such global exists.
    """
    if "nvidia" not in gpu_vendors:
        return None
    if os.environ.get("XDG_SESSION_TYPE") != "wayland":
        return None
    r = _run(["wayland-info"])
    if r is None or r.returncode != 0:
        return None
    text = r.stdout
    if ("wp_linux_drm_syncobj_manager_v1" in text
            or "zwp_linux_explicit_synchronization_v1" in text):
        return None
    return GraphicsFinding(
        SEV_ERROR, "explicit_sync_protocol",
        "Wayland compositor does not advertise wp_linux_drm_syncobj_manager_v1 "
        "(nor the legacy zwp_linux_explicit_synchronization_v1). On NVIDIA, "
        "XWayland clients (Steam games) render black without explicit sync. "
        "This is the single most common cause of Steam black-window on "
        "NVIDIA+Wayland.",
        "Update the compositor to a version that supports the explicit-sync "
        "protocol, or run games via gamescope "
        "(`gamescope -w W -h H -- %command%` in Steam launch options) to "
        "bypass the compositor's XWayland path.",
    )


_STEAM_CONFIG_PATHS = (
    Path.home() / ".local/share/Steam/config/config.vdf",
    Path.home() / ".steam/steam/config/config.vdf",
)


def _check_steam_gpu_accel() -> GraphicsFinding | None:
    """
    Steam client web views (store, library, friends) render black on
    NVIDIA+Wayland in Steam 1.0.0.85+ when GPUAccelerationEnabled is on.
    Orthogonal to game rendering — games can be fine while the store is
    black, and vice versa. Informational; we don't know the user's exact
    build, but flag when enabled so it's a testable lever.
    """
    for path in _STEAM_CONFIG_PATHS:
        text = _read_text(path)
        if text is None:
            continue
        m = re.search(r'"GPUAccelerationEnabled"\s+"(\d)"', text)
        if not m:
            return None
        if m.group(1) != "1":
            return None
        return GraphicsFinding(
            SEV_WARN, "steam_gpu_accel",
            "Steam client GPU-accelerated web views are enabled. Known to "
            "render black on NVIDIA+Wayland in recent Steam builds (client "
            "store/library only — separate from game-window rendering).",
            "Steam → Settings → Interface → uncheck "
            "'Enable GPU accelerated rendering in web views', then restart "
            "Steam.",
        )
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def check_system_graphics(
    config,
    *,
    gpu_vendors: list[str] | None = None,
) -> list[GraphicsFinding]:
    """
    Run all system graphics probes. `gpu_vendors` should be passed by the
    caller (doctor.py already has the detected list from `_read_gpu_vendors`);
    if None, vendor-gated checks are skipped.

    Findings are returned in a stable order — callers render them verbatim.
    """
    del config  # reserved for future, e.g. user-configured skips
    gvendors = gpu_vendors or []
    installed = pacman.get_all_installed_packages()

    findings: list[GraphicsFinding] = []

    def _add(f: GraphicsFinding | None) -> None:
        if f is not None:
            findings.append(f)

    _add(_check_session_type())
    _add(_check_xwayland_present(installed))
    _add(_check_multilib_enabled(gvendors))

    if "nvidia" in gvendors:
        _add(_check_nvidia_module_loaded(gvendors))
        _add(_check_nvidia_modeset())
        _add(_check_nvidia_fbdev())
        _add(_check_nvidia_driver_skew())
        _add(_check_explicit_sync_protocol(gvendors))

    _add(_check_steam_gpu_accel())

    return findings
