"""
stages/hardware.py — stage 3: hardware detection

Probes the running system and emits hardware_profile.toml to the state dir.
No config file required — everything is auto-detected.

hardware_profile.toml layout:
  [hardware]
  cpu_vendor  = "AuthenticAMD"   # raw vendor string from /proc/cpuinfo
  cpu_family  = 25               # cpu family integer
  cpu_model   = 33               # model integer
  gpu_vendors = ["nvidia"]       # list: "amd" | "nvidia" | "intel" | "other"
  nvme        = true             # NVMe storage detected

  [kconfig]
  CONFIG_MZEN3          = "y"    # CPU-specific kernel optimisation
  CONFIG_X86_AMD_PSTATE = "y"    # AMD P-state driver
  CONFIG_DRM_NOUVEAU    = "n"    # disabled when NVIDIA GPU present
  CONFIG_BLK_DEV_NVME   = "y"    # NVMe present

The [kconfig] table is consumed by the kernel stage when building a custom
kernel. Absent hardware_profile.toml is non-fatal for the kernel stage —
kconfig entries are simply skipped.
"""

import os
import re
import subprocess
from pathlib import Path

from sysforge import log
_log = log.get_logger("HARDWARE")
from sysforge.pipeline.stages.base import Stage
from sysforge.pipeline.state import resolve_state_dir


# ---------------------------------------------------------------------------
# CPU detection
# ---------------------------------------------------------------------------

# Maps (cpu_family, cpu_model) → kconfig option for CPU-specific optimisation.
# Family 25 = Zen 3 / Zen 3+, Family 26 = Zen 4 / Zen 5.
# Models sourced from Linux arch/x86/include/asm/cpu_device_id.h.
_AMD_CPU_KCONFIG = {
    # Zen 3 (family 25, model 33 = Vermeer desktop, 80 = Cezanne APU, etc.)
    (25, 33): "CONFIG_MZEN3",
    (25, 80): "CONFIG_MZEN3",
    (25, 68): "CONFIG_MZEN3",
    (25, 24): "CONFIG_MZEN3",
    # Zen 4 (family 25, model 97 = Raphael desktop, 116 = Phoenix APU)
    (25, 97):  "CONFIG_MZEN4",
    (25, 116): "CONFIG_MZEN4",
    (25, 117): "CONFIG_MZEN4",
    # Zen 5 (family 26)
    (26, 32): "CONFIG_MZEN5",
    (26, 68): "CONFIG_MZEN5",
}

# Family 25+ supports AMD P-state driver.
_AMD_PSTATE_MIN_FAMILY = 25


def _parse_cpuinfo(cpuinfo_text: str) -> dict:
    """
    Parse /proc/cpuinfo and return a dict with vendor_id, cpu_family, model
    taken from the first processor block.
    """
    result = {}
    for line in cpuinfo_text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "vendor_id" and "cpu_vendor" not in result:
            result["cpu_vendor"] = value
        elif key == "cpu family" and "cpu_family" not in result:
            try:
                result["cpu_family"] = int(value)
            except ValueError:
                pass
        elif key == "model" and "cpu_model" not in result:
            try:
                result["cpu_model"] = int(value)
            except ValueError:
                pass
        if len(result) == 3:
            break
    return result


def _cpu_kconfig(cpu_info: dict) -> dict:
    """Return kconfig entries for the detected CPU."""
    entries = {}
    vendor = cpu_info.get("cpu_vendor", "")
    family = cpu_info.get("cpu_family")
    model  = cpu_info.get("cpu_model")

    if vendor == "AuthenticAMD":
        if family is not None and family >= _AMD_PSTATE_MIN_FAMILY:
            entries["CONFIG_X86_AMD_PSTATE"] = "y"
        if family is not None and model is not None:
            opt = _AMD_CPU_KCONFIG.get((family, model))
            if opt:
                entries[opt] = "y"
            else:
                _log.info(
                    f"AMD CPU family={family} model={model} — no specific kconfig mapping, "
                    "CONFIG_GENERIC_CPU will be used by default",
                )

    return entries


# ---------------------------------------------------------------------------
# GPU detection (via lspci)
# ---------------------------------------------------------------------------

_LSPCI_VGA_RE = re.compile(
    r"(?:VGA compatible controller|3D controller|Display controller).*?:\s*(.+)",
    re.IGNORECASE,
)


def parse_gpu_vendors(lspci_text: str) -> list[str]:
    """
    Extract GPU vendor names from lspci output.
    Returns a deduplicated list of lowercase vendor tags: "amd", "nvidia",
    "intel", or "other".
    """
    seen = []
    for line in lspci_text.splitlines():
        m = _LSPCI_VGA_RE.search(line)
        if not m:
            continue
        desc = m.group(1).lower()
        if "nvidia" in desc:
            tag = "nvidia"
        elif "amd" in desc or "advanced micro" in desc or "radeon" in desc:
            tag = "amd"
        elif "intel" in desc:
            tag = "intel"
        else:
            tag = "other"
        if tag not in seen:
            seen.append(tag)
    return seen


def _gpu_kconfig(gpu_vendors: list[str]) -> dict:
    """Return kconfig entries for detected GPUs."""
    entries = {}
    if "nvidia" in gpu_vendors:
        # Disable the open-source nouveau driver — nvidia proprietary takes over.
        entries["CONFIG_DRM_NOUVEAU"] = "n"
    return entries


# ---------------------------------------------------------------------------
# NVMe detection
# ---------------------------------------------------------------------------

def _has_nvme(lspci_text: str) -> bool:
    """Return True if any NVMe controller is found in lspci output."""
    return bool(re.search(r"Non-Volatile memory controller", lspci_text, re.IGNORECASE))


# ---------------------------------------------------------------------------
# LLVM target detection (used by pkgbuild_patcher when building llvm/clang/
# compiler-rt to filter LLVM_TARGETS_TO_BUILD down to what this host
# actually uses).
# ---------------------------------------------------------------------------

# Host arch → LLVM CPU backend. Unknown architectures fall through and
# yield no autodetected target list — the user must override via
# toolchain.toml [llvm] targets.
_HOST_ARCH_TO_LLVM = {
    "x86_64":  "X86",
    "amd64":   "X86",
    "i686":    "X86",
    "aarch64": "AArch64",
    "arm64":   "AArch64",
    "armv7l":  "ARM",
    "armv6l":  "ARM",
    "riscv64": "RISCV",
    "ppc64le": "PowerPC",
}

# GPU vendor (as emitted by parse_gpu_vendors) → LLVM GPU backend.
# Intel Mesa drivers (iris/anv) do not depend on an LLVM backend, so intel
# GPUs contribute no entry here.
_GPU_VENDOR_TO_LLVM = {
    "amd":    "AMDGPU",
    "nvidia": "NVPTX",
}


def detect_host_arch() -> str:
    """Return the running kernel arch as reported by uname -m."""
    return os.uname().machine


def derive_llvm_targets(host_arch: str, gpu_vendors: list[str]) -> list[str]:
    """Build the autodetected LLVM_TARGETS_TO_BUILD list for this host.

    Order: CPU backend first, then GPU backends in vendor-detection order.
    Returns an empty list when the host arch is unrecognised — callers
    treat empty as "no filtering" (i.e. preserve upstream defaults).
    """
    cpu = _HOST_ARCH_TO_LLVM.get(host_arch)
    targets: list[str] = []
    if cpu:
        targets.append(cpu)
    else:
        _log.warn(
            f"host arch {host_arch!r} has no LLVM target mapping — "
            "llvm_targets left empty (no filtering will be applied)",
        )
        return []
    for vendor in gpu_vendors:
        backend = _GPU_VENDOR_TO_LLVM.get(vendor)
        if backend and backend not in targets:
            targets.append(backend)
    return targets


# ---------------------------------------------------------------------------
# hardware_profile.toml writer
# ---------------------------------------------------------------------------

def _write_hardware_profile(path: Path, hw: dict, kconfig: dict, dry_run: bool) -> None:
    """Write hardware_profile.toml atomically."""
    lines = [
        "# Generated by SysForge hardware detection stage — do not edit manually",
        "# Re-run the hardware stage to refresh.",
        "",
        "[hardware]",
        f'cpu_vendor  = "{hw.get("cpu_vendor", "")}"',
        f'cpu_family  = {hw.get("cpu_family", 0)}',
        f'cpu_model   = {hw.get("cpu_model", 0)}',
        f'host_arch   = "{hw.get("host_arch", "")}"',
        "gpu_vendors = [{}]".format(
            ", ".join(f'"{v}"' for v in hw.get("gpu_vendors", []))
        ),
        "llvm_targets = [{}]".format(
            ", ".join(f'"{v}"' for v in hw.get("llvm_targets", []))
        ),
        f'nvme        = {"true" if hw.get("nvme") else "false"}',
        "",
    ]

    if kconfig:
        lines += ["[kconfig]"]
        for option, value in kconfig.items():
            lines.append(f'{option} = "{value}"')
        lines.append("")

    content = "\n".join(lines)

    if dry_run:
        _log.ui(f"[dry-run] would write hardware_profile.toml to {path}:")
        for line in lines:
            _log.ui(f"  {line}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.rename(path)
    _log.ui(f"Wrote hardware_profile.toml: {path}")


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class HardwareStage(Stage):
    name = "hardware"
    description = "Hardware detection — CPU, GPU, storage"
    depends_on = ["base_install"]

    def run(self, config, state, options):  # noqa: ARG002
        state_dir, _ = resolve_state_dir(options.state_dir)
        output_path = state_dir / "hardware_profile.toml"

        _log.ui("Probing hardware...")

        # --- CPU ---
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text()
        except OSError as e:
            raise RuntimeError(f"[HARDWARE] Cannot read /proc/cpuinfo: {e}")

        cpu_info = _parse_cpuinfo(cpuinfo)
        _log.ui(
            f"CPU: vendor={cpu_info.get('cpu_vendor', '?')}  "
            f"family={cpu_info.get('cpu_family', '?')}  "
            f"model={cpu_info.get('cpu_model', '?')}",
        )

        # --- GPU / NVMe via lspci ---
        lspci_result = subprocess.run(
            ["lspci"], capture_output=True, text=True
        )
        if lspci_result.returncode != 0:
            _log.warn(
                f"lspci failed (exit {lspci_result.returncode}) — GPU and NVMe detection skipped",
            )
            lspci_text = ""
        else:
            lspci_text = lspci_result.stdout

        gpu_vendors = parse_gpu_vendors(lspci_text)
        nvme = _has_nvme(lspci_text)

        if gpu_vendors:
            _log.ui(f"GPU(s): {', '.join(gpu_vendors)}")
        else:
            _log.ui("GPU: none detected via lspci")

        if nvme:
            _log.ui("NVMe: present")

        # --- Build kconfig ---
        kconfig = {}
        kconfig.update(_cpu_kconfig(cpu_info))
        kconfig.update(_gpu_kconfig(gpu_vendors))
        if nvme:
            kconfig["CONFIG_BLK_DEV_NVME"] = "y"

        if kconfig:
            _log.ui(
                f"kconfig entries: {', '.join(f'{k}={v}' for k, v in kconfig.items())}",
            )
        else:
            _log.ui("No kconfig entries generated")

        # --- Host arch + LLVM target list ---
        host_arch = detect_host_arch()
        llvm_targets = derive_llvm_targets(host_arch, gpu_vendors)
        _log.ui(f"host_arch: {host_arch}")
        if llvm_targets:
            _log.ui(f"llvm_targets: {';'.join(llvm_targets)}")

        # --- Hardware summary dict ---
        hw = {
            **cpu_info,
            "gpu_vendors": gpu_vendors,
            "nvme": nvme,
            "host_arch": host_arch,
            "llvm_targets": llvm_targets,
        }

        # --- Write output ---
        _write_hardware_profile(output_path, hw, kconfig, options.dry_run)

        _log.ui("Hardware detection complete.")
