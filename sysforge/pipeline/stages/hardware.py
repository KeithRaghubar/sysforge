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

import re
import subprocess
from pathlib import Path

import sysforge.log as _log
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
                    "[HARDWARE]",
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


def _parse_gpu_vendors(lspci_text: str) -> list[str]:
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
        "gpu_vendors = [{}]".format(
            ", ".join(f'"{v}"' for v in hw.get("gpu_vendors", []))
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
        _log.ui("[HARDWARE]", f"[dry-run] would write hardware_profile.toml to {path}:")
        for line in lines:
            _log.ui("[HARDWARE]", f"  {line}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.rename(path)
    _log.ui("[HARDWARE]", f"Wrote hardware_profile.toml: {path}")


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

        _log.ui("[HARDWARE]", "Probing hardware...")

        # --- CPU ---
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text()
        except OSError as e:
            raise RuntimeError(f"[HARDWARE] Cannot read /proc/cpuinfo: {e}")

        cpu_info = _parse_cpuinfo(cpuinfo)
        _log.ui(
            "[HARDWARE]",
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
                "[HARDWARE]",
                f"lspci failed (exit {lspci_result.returncode}) — GPU and NVMe detection skipped",
            )
            lspci_text = ""
        else:
            lspci_text = lspci_result.stdout

        gpu_vendors = _parse_gpu_vendors(lspci_text)
        nvme = _has_nvme(lspci_text)

        if gpu_vendors:
            _log.ui("[HARDWARE]", f"GPU(s): {', '.join(gpu_vendors)}")
        else:
            _log.ui("[HARDWARE]", "GPU: none detected via lspci")

        if nvme:
            _log.ui("[HARDWARE]", "NVMe: present")

        # --- Build kconfig ---
        kconfig = {}
        kconfig.update(_cpu_kconfig(cpu_info))
        kconfig.update(_gpu_kconfig(gpu_vendors))
        if nvme:
            kconfig["CONFIG_BLK_DEV_NVME"] = "y"

        if kconfig:
            _log.ui(
                "[HARDWARE]",
                f"kconfig entries: {', '.join(f'{k}={v}' for k, v in kconfig.items())}",
            )
        else:
            _log.ui("[HARDWARE]", "No kconfig entries generated")

        # --- Hardware summary dict ---
        hw = {
            **cpu_info,
            "gpu_vendors": gpu_vendors,
            "nvme": nvme,
        }

        # --- Write output ---
        _write_hardware_profile(output_path, hw, kconfig, options.dry_run)

        _log.ui("[HARDWARE]", "Hardware detection complete.")
