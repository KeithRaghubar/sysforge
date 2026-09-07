# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

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
  CONFIG_ARM64          = "n"    # arch-disable: host is x86_64
  CONFIG_ARCH_QCOM      = "n"    # arch-disable: arm64 SoC umbrella
  # … plus the rest of the non-host kconfig domains (RISC-V, PowerPC, MIPS, …)

  [kconfig_devices]
  CONFIG_IGC = "m"               # device-driven: modular driver for a present device

The [kconfig] and [kconfig_devices] tables are consumed by the kernel stage
when building a custom kernel ([kconfig] wins on overlap; device entries are
emitted "m" and are gated there by kernel.toml device_kconfig). Device
coverage comes from device_probe's curated table plus the kbuild
module→kconfig cache the kernel stage harvests at Gate-2 time
(<state_dir>/kbuild_module_map.json) — first run is curated-only, later runs
are near-total. Absent hardware_profile.toml is non-fatal for the kernel
stage — kconfig entries are simply skipped.
"""

import re
import subprocess
import tomllib
from pathlib import Path

from sysforge import log
_log = log.get_logger("HARDWARE")
from sysforge.pipeline.stages.base import Stage
from sysforge.pipeline.state import resolve_state_dir
# Detection + derivation live in the leaf layer (3.2.0-F1c) so the primitives
# that need live hardware facts don't import upward into this stage. Re-exported
# here: ``stages.hardware.derive_llvm_targets`` and friends stay valid paths.
from sysforge.primitives.hardware_probe import (  # noqa: F401
    derive_llvm_targets,
    derive_mesa_drivers,
    detect_host_arch,
    parse_gpu_vendors,
)
import contextlib


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
            with contextlib.suppress(ValueError):
                result["cpu_family"] = int(value)
        elif key == "model" and "cpu_model" not in result:
            with contextlib.suppress(ValueError):
                result["cpu_model"] = int(value)
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

# ---------------------------------------------------------------------------
# Architecture-aware kconfig disable
#
# Each entry in _ARCH_OWNED_KCONFIG maps a kernel architecture "domain" to
# the CONFIG_* keys that only make sense when the kernel is targeting that
# domain. The hardware stage emits `<key> = "n"` for every key whose domain
# is not the host's domain, culling unreachable subtrees from `make nconfig`.
#
# Keys are curated, not exhaustive — start with top-level umbrellas. Most
# SoC drivers are gated by `depends on ARCH_<vendor>` in the kernel's own
# Kconfig, so disabling the umbrella is enough to cull the subtree.
# ---------------------------------------------------------------------------

_ARCH_OWNED_KCONFIG = {
    "x86": frozenset({
        "CONFIG_X86", "CONFIG_X86_64", "CONFIG_X86_32",
        "CONFIG_MICROCODE_INTEL", "CONFIG_INTEL_RDT",
    }),
    "arm": frozenset({"CONFIG_ARM"}),
    "arm64": frozenset({
        "CONFIG_ARM64",
        "CONFIG_ARCH_BCM", "CONFIG_ARCH_QCOM", "CONFIG_ARCH_TEGRA",
        "CONFIG_ARCH_ROCKCHIP", "CONFIG_ARCH_SUNXI", "CONFIG_ARCH_MEDIATEK",
        "CONFIG_ARCH_RENESAS", "CONFIG_ARCH_HISILICON",
        "CONFIG_ARCH_LAYERSCAPE", "CONFIG_ARCH_MXC",
        "CONFIG_ARCH_OMAP2PLUS", "CONFIG_ARCH_EXYNOS", "CONFIG_ARCH_K3",
    }),
    "riscv":     frozenset({"CONFIG_RISCV"}),
    "powerpc":   frozenset({"CONFIG_PPC", "CONFIG_PPC32", "CONFIG_PPC64"}),
    "mips":      frozenset({"CONFIG_MIPS"}),
    "sparc":     frozenset({"CONFIG_SPARC", "CONFIG_SPARC32", "CONFIG_SPARC64"}),
    "loongarch": frozenset({"CONFIG_LOONGARCH"}),
}

_HOST_ARCH_TO_KCONFIG_DOMAIN = {
    "x86_64": "x86", "i686": "x86", "i386": "x86",
    "aarch64": "arm64",
    "armv7l": "arm", "armv6l": "arm",
    "riscv64": "riscv", "riscv32": "riscv",
    "ppc64le": "powerpc", "ppc64": "powerpc", "ppc": "powerpc",
    "mips": "mips", "mips64": "mips",
    "sparc": "sparc", "sparc64": "sparc",
    "loongarch64": "loongarch",
}


def _arch_disable_kconfig(host_arch: str) -> dict[str, str]:
    """Return {CONFIG_X: "n"} entries for every kconfig key owned by a
    domain other than the host's. Defensive: keys appearing in the host's
    own domain set are filtered out, so a key registered under multiple
    domains never gets disabled on a host whose domain owns it.
    """
    domain = _HOST_ARCH_TO_KCONFIG_DOMAIN.get(host_arch)
    if domain is None:
        _log.warn(
            f"host_arch={host_arch!r} not mapped to a kconfig domain — "
            "arch-disable skipped",
        )
        return {}
    host_owned = _ARCH_OWNED_KCONFIG.get(domain, frozenset())
    disable: dict[str, str] = {}
    for other_domain, keys in _ARCH_OWNED_KCONFIG.items():
        if other_domain == domain:
            continue
        for key in keys:
            if key in host_owned:
                continue
            disable[key] = "n"
    return disable


# ---------------------------------------------------------------------------
# hardware_profile.toml writer
# ---------------------------------------------------------------------------

def _toml_str(value: str) -> str:
    """Escape a string for a double-quoted (basic) TOML value.

    Escapes backslash/quote and the control chars that would otherwise make
    the emitted line invalid TOML (newline/CR/tab) — device descriptions come
    from external tools, so don't assume they are clean single-line text.
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


# ---------------------------------------------------------------------------
# Drift detection (F3) — report changes vs. the existing profile before
# overwriting. Pure helpers mirroring the flag_drift reporting pattern: the
# diff is computed without side effects; the stage decides how to surface it.
# Only the scalar [hardware] summary is compared — kconfig/device tables churn
# on every probe (device addresses, kbuild-map width) and aren't a stable
# drift surface.
# ---------------------------------------------------------------------------

# Ordered (key, label) pairs for the [hardware] summary drift report. Order
# determines the report order; labels are the human-facing field names.
_HARDWARE_DRIFT_FIELDS: list[tuple[str, str]] = [
    ("cpu_vendor", "cpu_vendor"),
    ("cpu_family", "cpu_family"),
    ("cpu_model", "cpu_model"),
    ("host_arch", "host_arch"),
    ("gpu_vendors", "gpu_vendors"),
    ("nvme", "nvme"),
    ("llvm_targets", "llvm_targets"),
    ("mesa_gallium_drivers", "mesa_gallium_drivers"),
    ("mesa_vulkan_drivers", "mesa_vulkan_drivers"),
]


def _load_hardware_summary(path: Path) -> dict | None:
    """Read the ``[hardware]`` table from an existing profile, or ``None``.

    Returns ``None`` when the file is absent or unparseable (a corrupt prior
    profile is treated as "no baseline" — drift reporting is advisory, never a
    hard failure that would block a refresh).
    """
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    table = data.get("hardware")
    return table if isinstance(table, dict) else None


def _fmt_drift_value(value) -> str:
    """Render a summary value for a drift line (lists as comma-joined)."""
    if isinstance(value, list):
        return "[" + ", ".join(str(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _diff_hardware_summary(old: dict, new: dict) -> list[str]:
    """Return human-readable drift lines between two ``[hardware]`` summaries.

    Compares only the fields in :data:`_HARDWARE_DRIFT_FIELDS`, in that order.
    Format per changed field: ``  <field>: <old> → <new>``. Missing keys on the
    old side (a profile written before a field existed) read as an empty value
    rather than being skipped, so a newly-tracked field still surfaces.
    """
    diffs: list[str] = []
    for key, label in _HARDWARE_DRIFT_FIELDS:
        old_val = old.get(key)
        new_val = new.get(key)
        if old_val == new_val:
            continue
        diffs.append(
            f"  {label}: {_fmt_drift_value(old_val)} → {_fmt_drift_value(new_val)}"
        )
    return diffs


def _write_hardware_profile(
    path: Path, hw: dict, kconfig: dict, dry_run: bool, devices=None,
    device_kconfig: dict | None = None,
) -> None:
    """Write hardware_profile.toml atomically.

    ``device_kconfig`` is emitted as a ``[kconfig_devices]`` table after
    ``[kconfig]`` — device-driven modular-driver symbols, already deduped
    against ``[kconfig]`` by the caller. ``devices`` (a list of
    ``device_probe.Device``) is emitted as a ``[[devices]]`` array-of-tables
    **after** the scalar tables — the full PCI/USB inventory with
    bound-driver state.
    """
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
        "mesa_gallium_drivers = [{}]".format(
            ", ".join(f'"{v}"' for v in hw.get("mesa_gallium_drivers", []))
        ),
        "mesa_vulkan_drivers = [{}]".format(
            ", ".join(f'"{v}"' for v in hw.get("mesa_vulkan_drivers", []))
        ),
        f'nvme        = {"true" if hw.get("nvme") else "false"}',
        "",
    ]

    if kconfig:
        lines += ["[kconfig]"]
        for option, value in kconfig.items():
            lines.append(f'{option} = "{value}"')
        lines.append("")

    if device_kconfig:
        lines += ["[kconfig_devices]"]
        for option, value in device_kconfig.items():
            lines.append(f'{option} = "{value}"')
        lines.append("")

    for d in devices or []:
        mods = ", ".join(f'"{m}"' for m in d.expected_modules)
        kconf = ", ".join(f'"{k}"' for k in d.suggested_kconfig)
        lines += [
            "[[devices]]",
            f'bus = "{d.bus}"',
            f'address = "{_toml_str(d.address)}"',
            f'modalias = "{_toml_str(d.modalias)}"',
            f'class = "{_toml_str(d.class_id)}"',
            f'description = "{_toml_str(d.description)}"',
            f'driver = "{_toml_str(d.driver or "")}"',
            f"expected_modules = [{mods}]",
            f"suggested_kconfig = [{kconf}]",
            "",
        ]

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
    _log.info(f"Wrote hardware_profile.toml: {path}")


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class HardwareStage(Stage):
    name = "hardware"
    description = "Hardware detection — CPU, GPU, storage"
    depends_on = ["install"]

    def run(self, config, state, options):  # noqa: ARG002
        state_dir, _ = resolve_state_dir(options.state_dir)
        output_path = state_dir / "hardware_profile.toml"

        _log.info("Probing hardware...")

        # --- CPU ---
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        except OSError as e:
            raise RuntimeError(f"[HARDWARE] Cannot read /proc/cpuinfo: {e}") from e

        cpu_info = _parse_cpuinfo(cpuinfo)
        _log.info(
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
            _log.info(f"GPU(s): {', '.join(gpu_vendors)}")
        else:
            _log.info("GPU: none detected via lspci")

        if nvme:
            _log.info("NVMe: present")

        # --- Host arch + LLVM target list ---
        host_arch = detect_host_arch()
        llvm_targets = derive_llvm_targets(host_arch, gpu_vendors)
        mesa_drivers = derive_mesa_drivers(gpu_vendors)
        _log.info(f"host_arch: {host_arch}")
        if llvm_targets:
            _log.info(f"llvm_targets: {';'.join(llvm_targets)}")
        _log.info(
            "mesa drivers: gallium={} vulkan={}".format(
                ",".join(mesa_drivers["gallium"]),
                ",".join(mesa_drivers["vulkan"]),
            )
        )

        # --- Build kconfig ---
        kconfig = {}
        kconfig.update(_cpu_kconfig(cpu_info))
        kconfig.update(_gpu_kconfig(gpu_vendors))
        if nvme:
            kconfig["CONFIG_BLK_DEV_NVME"] = "y"

        # Arch-disable runs last so future host-arch-specific =y entries can
        # never be clobbered by a non-host-domain =n. Manual [[kconfig]] in
        # kernel.toml overrides anything here — see DESIGN.md §Architecture-
        # aware kconfig disable for the cross-compile escape hatch.
        arch_disable = _arch_disable_kconfig(host_arch)
        kconfig.update(arch_disable)
        if arch_disable:
            _log.info(
                f"Arch-disable: {len(arch_disable)} kconfig entries set to 'n' "
                f"(non-{host_arch} architecture/SoC umbrellas)",
            )

        if kconfig:
            _log.info(
                f"kconfig entries: {len(kconfig)} total "
                f"({len(kconfig) - len(arch_disable)} hardware-driven, "
                f"{len(arch_disable)} arch-disable)",
            )
        else:
            _log.info("No kconfig entries generated")

        # --- Full PCI/USB device inventory + driver-coverage check ---
        from sysforge.primitives import device_probe, kbuild_map

        # Widen module→CONFIG_* beyond the curated table with the cached
        # kbuild map (harvested from the kernel stage's last build tree).
        kconfig_map = None
        cached = kbuild_map.load_map(state_dir / kbuild_map.KBUILD_MAP_FILENAME)
        if cached is not None:
            kconfig_map, map_release = cached
            _log.info(
                f"Loaded kbuild module→kconfig map: {len(kconfig_map)} modules "
                f"(from kernel {map_release or '?'})",
            )
        devices = device_probe.enumerate_devices(kconfig_map=kconfig_map)
        _log.info(f"Devices: {len(devices)} PCI/USB endpoint(s) inventoried")
        unsupported = device_probe.check_unsupported_devices(devices=devices)
        for finding in unsupported:
            _log.warn(f"{finding.message} {finding.remediation}".strip())
        if unsupported:
            _log.warn(
                f"{len(unsupported)} device(s) present with no driver bound — "
                "see `sysforge doctor --hardware`",
            )

        # --- Device-driven kconfig: modular drivers for present devices ---
        device_kconfig: dict[str, str] = {}
        for d in devices:
            for sym in d.suggested_kconfig:
                if sym not in kconfig:
                    device_kconfig.setdefault(sym, "m")
        if device_kconfig:
            _log.info(
                f"Device-driven kconfig entries: {len(device_kconfig)} "
                "(emitted =m; heuristic [kconfig] wins on overlap)",
            )

        # --- Hardware summary dict ---
        hw = {
            **cpu_info,
            "gpu_vendors": gpu_vendors,
            "nvme": nvme,
            "host_arch": host_arch,
            "llvm_targets": llvm_targets,
            "mesa_gallium_drivers": mesa_drivers["gallium"],
            "mesa_vulkan_drivers": mesa_drivers["vulkan"],
        }

        # --- Drift report (F3): advise on changes vs. the existing profile
        # before overwriting. Advisory only — never blocks the refresh.
        prior = _load_hardware_summary(output_path)
        if prior is not None:
            drift = _diff_hardware_summary(prior, hw)
            if drift:
                _log.warn(
                    f"Hardware profile drift vs. existing {output_path.name} "
                    f"({len(drift)} field(s) changed):",
                )
                for line in drift:
                    _log.warn(line)
            else:
                _log.info(f"Hardware profile unchanged vs. existing {output_path.name}")

        # --- Write output ---
        _write_hardware_profile(
            output_path, hw, kconfig, options.dry_run, devices,
            device_kconfig=device_kconfig,
        )

        _log.info("Hardware detection complete.")
