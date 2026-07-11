# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
gfxperf_probe.py — advisory graphics runtime-degradation sweep.

Complements graphics_probe.py's binary *does-it-render* health checks with a
*checklist* of static configuration known to predispose a system to runtime
degradation (stutter, tearing, judder, frame drops). Nothing here is ever an
ERROR: no static config proves a runtime symptom, so findings are WARN (strong
misconfiguration) or INFO (env-/snapshot-dependent observation, and OK-path
checklist lines). Env-var checks are hedged — doctor reads its own process env,
not the compositor/session env, so an unset value is never asserted as broken.

All checks are read-only and safe when the target is absent.

Public API:
    check_gfxperf(config, *, gpu_vendors=None) -> list[GraphicsFinding]
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from sysforge.primitives import pacman
from sysforge.primitives.graphics_probe import (
    SEV_ERROR, SEV_INFO, SEV_WARN, GraphicsFinding,
)

__all__ = ["check_gfxperf", "GraphicsFinding", "SEV_ERROR", "SEV_WARN", "SEV_INFO"]


def _read_text(path: Path) -> str | None:
    """Read a small file; return None on permission error or missing."""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, IsADirectoryError, OSError):
        return None


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    """Run a command; return None if the binary is missing."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Cluster 3 — CPU frequency (vendor-agnostic)
# ---------------------------------------------------------------------------

_GOVERNOR_PATH = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")


def _check_cpu_governor() -> GraphicsFinding | None:
    gov = _read_text(_GOVERNOR_PATH)
    if gov is None:
        return None  # no cpufreq exposed (VM / unusual kernel) — does not apply
    gov = gov.strip()
    if gov == "powersave":
        return GraphicsFinding(
            SEV_WARN, "cpu_governor",
            "CPU scaling governor is 'powersave' — clocks stay low and can "
            "cause frame-time spikes / hitching in interactive workloads.",
            "Set a responsive governor (e.g. 'schedutil' or 'performance'). "
            "This is separate from sysforge's build throttle, which lowers "
            "priority only during builds.",
        )
    return GraphicsFinding(SEV_INFO, "cpu_governor",
                           f"CPU scaling governor: {gov}.")


# ---------------------------------------------------------------------------
# Cluster 1 — Video-decode path (NVIDIA-gated)
# ---------------------------------------------------------------------------

def _check_vaapi_driver(installed: dict[str, str]) -> GraphicsFinding | None:
    if "nvidia-vaapi-driver" in installed:
        return GraphicsFinding(
            SEV_INFO, "vaapi_driver",
            "hardware video-decode bridge present (nvidia-vaapi-driver).")
    return GraphicsFinding(
        SEV_WARN, "vaapi_driver",
        "nvidia-vaapi-driver is not installed — browsers and players fall back "
        "to CPU video decode, which drops frames on high-resolution / "
        "high-framerate video (a common cause of in-browser video stutter).",
        "Install 'nvidia-vaapi-driver', enable hardware video decode in your "
        "browser, and set LIBVA_DRIVER_NAME=nvidia in your session.",
    )


def _check_libva_env() -> GraphicsFinding | None:

    drv = os.environ.get("LIBVA_DRIVER_NAME", "")
    backend = os.environ.get("NVD_BACKEND", "")
    return GraphicsFinding(
        SEV_INFO, "libva_env",
        f"video-decode env in this shell: LIBVA_DRIVER_NAME={drv or 'unset'} "
        f"NVD_BACKEND={backend or 'unset'} — these must be set in your "
        "compositor/session to take effect; an unset value here is not proof "
        "your apps lack them.",
    )


# ---------------------------------------------------------------------------
# Cluster 2 — GPU power / clock state (NVIDIA-gated)
# ---------------------------------------------------------------------------

def _check_nvidia_persistence() -> GraphicsFinding | None:
    r = _run(["nvidia-smi", "-q"])
    if r is None or r.returncode != 0:
        return None
    m = re.search(r"Persistence Mode\s*:\s*(\w+)", r.stdout)
    if m is None:
        return None
    if m.group(1).lower() == "enabled":
        return GraphicsFinding(SEV_INFO, "nvidia_persistence",
                               "NVIDIA persistence mode enabled.")
    return GraphicsFinding(
        SEV_INFO, "nvidia_persistence",
        "NVIDIA persistence mode is disabled — the driver may unload and the "
        "GPU drop to low clocks between apps, adding latency on first use.",
        "Enable the persistence daemon: `sudo systemctl enable --now "
        "nvidia-persistenced.service`.",
    )


def _check_nvidia_powerd() -> GraphicsFinding | None:
    r = _run(["systemctl", "is-active", "nvidia-powerd.service"])
    if r is None:
        return None
    state = r.stdout.strip()
    if state == "active":
        return GraphicsFinding(SEV_INFO, "nvidia_powerd",
                               "nvidia-powerd (Dynamic Boost) is active.")
    if state == "inactive":
        return GraphicsFinding(
            SEV_INFO, "nvidia_powerd",
            "nvidia-powerd.service is present but inactive — Dynamic Boost "
            "power balancing is off.",
            "If your GPU supports Dynamic Boost, enable it: `sudo systemctl "
            "enable --now nvidia-powerd.service`.",
        )
    return None  # unknown / not-installed unit — does not apply


# ---------------------------------------------------------------------------
# Cluster 4 — Frame pacing / vsync (NVIDIA-gated, env-dependent → INFO)
# ---------------------------------------------------------------------------

def _check_gl_frame_pacing() -> GraphicsFinding | None:
    mfa = os.environ.get("__GL_MaxFramesAllowed", "")
    vsync = os.environ.get("__GL_SYNC_TO_VBLANK", "")
    return GraphicsFinding(
        SEV_INFO, "gl_frame_pacing",
        f"frame-pacing env in this shell: __GL_MaxFramesAllowed={mfa or 'unset'} "
        f"__GL_SYNC_TO_VBLANK={vsync or 'unset'} — set in your session to tune "
        "buffering/vsync; unset here is not proof your apps lack them.",
    )


# ---------------------------------------------------------------------------
# Cluster 5 (NVIDIA) — thermal snapshot
# ---------------------------------------------------------------------------

def _check_gpu_thermal() -> GraphicsFinding | None:
    r = _run(["nvidia-smi", "-q"])
    if r is None or r.returncode != 0:
        return None
    cur = re.search(r"GPU Current Temp\s*:\s*(\d+)", r.stdout)
    if cur is None:
        return None
    cur_t = int(cur.group(1))
    slow = re.search(r"GPU Slowdown Temp\s*:\s*(\d+)", r.stdout)
    slow_t = int(slow.group(1)) if slow else None
    if slow_t is not None and cur_t >= slow_t - 5:
        return GraphicsFinding(
            SEV_WARN, "gpu_thermal",
            f"GPU is {cur_t}C at this instant, within 5C of the {slow_t}C "
            "slowdown threshold — thermal throttling can cause frame drops "
            "(point-in-time snapshot).",
            "Improve case airflow, clean dust, or check the fan curve.",
        )
    tail = f", slowdown at {slow_t}C)." if slow_t is not None else ")."
    return GraphicsFinding(
        SEV_INFO, "gpu_thermal",
        f"GPU temperature OK at this instant ({cur_t}C{tail}")


# ---------------------------------------------------------------------------
# Cluster 6 — Transient pressure (snapshot)
# ---------------------------------------------------------------------------

def _check_memory_pressure() -> GraphicsFinding | None:
    text = _read_text(Path("/proc/meminfo"))
    if text is None:
        return None
    vals: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)\s*kB", line)
        if m:
            vals[m.group(1)] = int(m.group(2))
    total = vals.get("MemTotal", 0)
    if total == 0:
        return None
    avail = vals.get("MemAvailable", 0)
    swap_used = vals.get("SwapTotal", 0) - vals.get("SwapFree", 0)
    avail_pct = 100 * avail // total
    if avail_pct < 10 and swap_used > 0:
        return GraphicsFinding(
            SEV_INFO, "memory_pressure",
            f"low free memory at this instant ({avail_pct}% available) with "
            f"{swap_used // 1024} MiB swap in use — paging can cause hitches.",
            "Close memory-heavy apps or add RAM/zram; re-check when the symptom "
            "recurs (this is a point-in-time snapshot).",
        )
    return GraphicsFinding(
        SEV_INFO, "memory_pressure",
        f"memory headroom OK at this instant ({avail_pct}% available).")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def check_gfxperf(
    config,
    *,
    gpu_vendors: list[str] | None = None,
) -> list[GraphicsFinding]:
    """Run the advisory graphics-performance sweep. Vendor-agnostic checks run
    always; NVIDIA-gated clusters run when 'nvidia' in gpu_vendors. Findings are
    returned in a stable order; callers render them verbatim."""
    del config  # reserved for future user-configured skips
    gvendors = gpu_vendors or []
    installed = pacman.get_all_installed_packages()

    findings: list[GraphicsFinding] = []

    def _add(f: GraphicsFinding | None) -> None:
        if f is not None:
            findings.append(f)

    _add(_check_cpu_governor())
    _add(_check_memory_pressure())

    if "nvidia" in gvendors:
        _add(_check_vaapi_driver(installed))
        _add(_check_libva_env())
        _add(_check_nvidia_persistence())
        _add(_check_nvidia_powerd())
        _add(_check_gl_frame_pacing())
        _add(_check_gpu_thermal())

    return findings
