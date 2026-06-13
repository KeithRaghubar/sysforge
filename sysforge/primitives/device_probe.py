"""
device_probe.py — full PCI/USB device inventory + driver-coverage probe

Complements the hardware stage's scalar CPU/GPU/NVMe model with a
device-level inventory: every PCI and USB device, whether a kernel driver
is bound to it *right now*, and — as far as is reliable — the module and
``CONFIG_*`` symbol a missing driver would need.

The driver-bound signal validates the *running* kernel (the ``driver``
symlink in sysfs). The device→module link is resolved against a **complete
reference kernel**'s ``modules.alias`` (the kernel's own
``MODULE_DEVICE_TABLE`` data), not the running kernel — a custom kernel that
omitted a driver cannot resolve the modalias it lacks, so resolving against
the running kernel would hide exactly the gap we want to surface.

All probes are read-only and degrade silently when an input is missing
(no reference kernel, restricted sysfs, ``lspci`` absent): a missing input
shrinks the result, it never errors. Mirrors ``graphics_probe.py``.

Public API:
    enumerate_devices(buses=("pci", "usb"), kconfig_map=None) -> list[Device]
    check_unsupported_devices(*, devices=None, ref_dir=None) -> list[DeviceFinding]
    find_reference_modules_dir() -> Path | None
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SEV_ERROR = "error"
SEV_WARN = "warn"
SEV_INFO = "info"

# Filesystem roots — module-level so tests can repoint them at a fixture tree.
_SYS_BUS = Path("/sys/bus")
_MODULES_BASE = Path("/lib/modules")


@dataclass(frozen=True)
class Device:
    bus: str                              # "pci" | "usb"
    address: str                          # BDF (pci) or sysfs path component (usb)
    modalias: str
    class_id: str                         # raw sysfs class string
    description: str
    driver: str | None                    # bound module name, or None
    expected_modules: list[str] = field(default_factory=list)
    suggested_kconfig: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DeviceFinding:
    severity: str        # SEV_ERROR | SEV_WARN | SEV_INFO
    check_id: str        # short stable id, e.g. "unsupported_device"
    message: str
    remediation: str = ""


# ---------------------------------------------------------------------------
# Low-level helpers (same idiom as graphics_probe)
# ---------------------------------------------------------------------------

def _read_text(path: Path) -> str | None:
    """Read a small file; return None on permission error or missing."""
    try:
        return path.read_text()
    except (FileNotFoundError, PermissionError, IsADirectoryError, OSError):
        return None


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    """Run a command; return None if the binary is missing."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# module → CONFIG_* curated table
#
# The vetted core mapping: the common subsystems whose absence is most likely
# to leave a desktop/server device dead. Broad coverage comes from the
# tree-derived ``kbuild_map`` cache (parsed from the kernel srcdir at build
# time) passed in via ``kconfig_map``; this table always wins on overlap and
# is the only mapping when no cache exists yet. Unknown modules degrade to
# "module name only" (empty suggested_kconfig).
# Keys are the underscore module names as they appear in modules.alias.
# ---------------------------------------------------------------------------

_MODULE_TO_KCONFIG: dict[str, str] = {
    # audio
    "snd_hda_intel": "CONFIG_SND_HDA_INTEL",
    "snd_hda_codec_hdmi": "CONFIG_SND_HDA_CODEC_HDMI",
    "snd_usb_audio": "CONFIG_SND_USB_AUDIO",
    # storage controllers
    "nvme": "CONFIG_BLK_DEV_NVME",
    "ahci": "CONFIG_SATA_AHCI",
    "xhci_pci": "CONFIG_USB_XHCI_PCI",
    "xhci_hcd": "CONFIG_USB_XHCI_HCD",
    "ehci_pci": "CONFIG_USB_EHCI_PCI",
    "uas": "CONFIG_USB_UAS",
    "usb_storage": "CONFIG_USB_STORAGE",
    # wired NICs
    "e1000e": "CONFIG_E1000E",
    "igb": "CONFIG_IGB",
    "igc": "CONFIG_IGC",
    "r8169": "CONFIG_R8169",
    "tg3": "CONFIG_TIGON3",
    "alx": "CONFIG_ALX",
    # wireless
    "iwlwifi": "CONFIG_IWLWIFI",
    "ath10k_pci": "CONFIG_ATH10K_PCI",
    "ath11k_pci": "CONFIG_ATH11K_PCI",
    "rtw88_pci": "CONFIG_RTW88_PCI",
    "rtw89_pci": "CONFIG_RTW89_PCI",
    # input / hid
    "hid_generic": "CONFIG_HID_GENERIC",
    "usbhid": "CONFIG_USB_HID",
    # gpu (open drivers — proprietary nvidia is out-of-tree, handled elsewhere)
    "amdgpu": "CONFIG_DRM_AMDGPU",
    "i915": "CONFIG_DRM_I915",
    "xe": "CONFIG_DRM_XE",
}


# PCI base-class bytes (first two hex digits of the 6-hex sysfs class) that
# legitimately have no driver bound — filtering them keeps the false-positive
# rate down. 0x06 = bridge, 0x00 = unclassified, 0xff = vendor-specific noise.
_NONFUNCTIONAL_PCI_BASE = frozenset({"06", "00", "ff"})
# USB device-class 0x09 = hub (incl. root hubs) — never user hardware.
_USB_HUB_CLASS = frozenset({"09", "9"})


def _is_functional_device(bus: str, class_id: str) -> bool:
    """True when a missing driver on this device is worth flagging.

    Bridges, hubs and unclassified functions routinely have no driver and
    would otherwise dominate the findings with noise.
    """
    cid = (class_id or "").strip().lower()
    if bus == "pci":
        base = cid[2:4] if cid.startswith("0x") else cid[:2]
        return base not in _NONFUNCTIONAL_PCI_BASE and bool(base)
    if bus == "usb":
        base = cid[2:] if cid.startswith("0x") else cid
        return base.lstrip("0").zfill(1) not in _USB_HUB_CLASS or base in ("", "0", "00")
    return True


# ---------------------------------------------------------------------------
# Reference modules.alias database
# ---------------------------------------------------------------------------

def find_reference_modules_dir() -> Path | None:
    """Pick the newest installed **stock** kernel's /lib/modules dir.

    Excludes any modules dir whose name contains ``custom`` (the broken kernel
    we are validating against must not be its own reference) and prefers a dir
    that carries a real ``modules.alias`` (a fully-installed kernel). Returns
    None when only a custom kernel is installed (Tier 2 degrades; the
    bound-driver check still works).
    """
    base = _MODULES_BASE
    try:
        candidates = [d for d in base.iterdir() if d.is_dir()]
    except (FileNotFoundError, PermissionError, OSError):
        return None

    non_custom = [d for d in candidates if "custom" not in d.name.lower()]
    if not non_custom:
        return None

    # Prefer dirs that have a modules.alias (a real, fully-installed kernel).
    usable = [d for d in non_custom if (d / "modules.alias").exists()]
    pool = usable or non_custom

    def _ver_key(d: Path):
        # Sort by the leading X.Y.Z numerically, newest first.
        head = d.name.split("-", 1)[0]
        parts = []
        for piece in head.split("."):
            parts.append(int(piece) if piece.isdigit() else 0)
        return parts

    pool.sort(key=_ver_key, reverse=True)
    return pool[0]


# Cache: ref_dir -> list[(compiled_glob_regex, module)] parsed from the alias
# tables. The patterns are compiled once (not on every match) — see below.
_alias_cache: dict[str, list[tuple[re.Pattern[str], str]]] = {}


def _parse_reference_aliases(ref_dir: Path) -> list[tuple[re.Pattern[str], str]]:
    """Parse modules.alias (+ builtin) into (compiled_glob_regex, module) pairs.

    modules.alias lines look like: ``alias pci:v00001022d...sv* snd_hda_intel``
    modules.builtin.modinfo packs ``<mod>.alias=<pattern>`` NUL-separated.

    Each glob is compiled to a regex **once** (via ``fnmatch.translate``) and
    the result cached per ref_dir. The per-device resolve loop then matches a
    modalias against the whole table without recompiling: a real modules.alias
    has ~40k entries, and ``fnmatch.fnmatchcase``'s internal 256-entry compile
    cache thrashes badly at that size — compiling on the fly made a single
    ``enumerate_devices()`` take minutes (≈2.3s/device × 64 devices).
    """
    key = str(ref_dir)
    cached = _alias_cache.get(key)
    if cached is not None:
        return cached

    pairs: list[tuple[re.Pattern[str], str]] = []

    def _add(pattern: str, module: str) -> None:
        try:
            rx = re.compile(fnmatch.translate(pattern))
        except re.error:
            return  # skip a pathological pattern rather than abort the parse
        pairs.append((rx, module.replace("-", "_")))

    alias_text = _read_text(ref_dir / "modules.alias")
    if alias_text:
        for line in alias_text.splitlines():
            line = line.strip()
            if not line.startswith("alias "):
                continue
            rest = line[len("alias "):].strip()
            sp = rest.rsplit(None, 1)
            if len(sp) == 2:
                _add(sp[0], sp[1])

    # Built-in drivers carry their device table in modules.builtin.modinfo
    # as NUL-separated key=value records (<module>.alias=<pattern>).
    builtin = ref_dir / "modules.builtin.modinfo"
    try:
        raw = builtin.read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        raw = b""
    if raw:
        for record in raw.split(b"\x00"):
            try:
                text = record.decode("utf-8", "replace")
            except Exception:
                continue
            if ".alias=" not in text:
                continue
            mod, _, pattern = text.partition(".alias=")
            if pattern:
                _add(pattern.strip(), mod)

    _alias_cache[key] = pairs
    return pairs


def resolve_expected_modules(modalias: str, ref_dir: Path | None) -> list[str]:
    """Return the module(s) whose device table matches ``modalias``.

    This is exactly modprobe's matching: the device modalias is matched against
    each ``alias`` glob (precompiled, case-sensitive — same semantics as
    ``fnmatch.fnmatchcase``). Returns a de-duplicated, order-preserving list.
    Empty when no reference DB or no match.
    """
    if not modalias or ref_dir is None:
        return []
    pairs = _parse_reference_aliases(ref_dir)
    seen: list[str] = []
    for rx, module in pairs:
        if rx.match(modalias):
            if module not in seen:
                seen.append(module)
    return seen


def _suggested_kconfig(
    modules: list[str],
    extra_map: dict[str, str] | None = None,
) -> list[str]:
    """Map resolved modules to CONFIG_* (dedup, ordered).

    The curated table wins (its entries are vetted); ``extra_map`` — typically
    a tree-derived ``kbuild_map`` cache — extends coverage to modules the
    table doesn't know.
    """
    out: list[str] = []
    for m in modules:
        opt = _MODULE_TO_KCONFIG.get(m) or (extra_map or {}).get(m)
        if opt and opt not in out:
            out.append(opt)
    return out


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

def _pci_descriptions() -> dict[str, str]:
    """Map PCI BDF → human name via ``lspci``. Empty if lspci is unavailable.

    Uses ``lspci -mm`` machine-readable output so the device name survives
    without brittle line parsing; the BDF is the short form (``0d:00.4``).
    """
    r = _run(["lspci", "-mm"])
    if r is None or r.returncode != 0:
        return {}
    out: dict[str, str] = {}
    import shlex
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        if len(fields) < 4:
            continue
        bdf = fields[0]
        # fields: slot class vendor device [svendor sdevice] — name = vendor+device
        name = f"{fields[2]} {fields[3]}".strip()
        out[bdf] = name
    return out


def _device_driver(dev_dir: Path) -> str | None:
    """Resolve the bound module name from the ``driver`` symlink, or None."""
    link = dev_dir / "driver"
    try:
        target = link.resolve()
    except (OSError, RuntimeError):
        return None
    if not link.is_symlink() and not link.exists():
        return None
    name = target.name
    return name.replace("-", "_") if name else None


def _usb_description(dev_dir: Path) -> str:
    manufacturer = (_read_text(dev_dir / "manufacturer") or "").strip()
    product = (_read_text(dev_dir / "product") or "").strip()
    joined = " ".join(p for p in (manufacturer, product) if p)
    return joined


def enumerate_devices(
    buses: tuple[str, ...] = ("pci", "usb"),
    kconfig_map: dict[str, str] | None = None,
) -> list[Device]:
    """Walk /sys/bus/<bus>/devices and build the device inventory.

    For each device: modalias, class, the bound-driver symlink, a human
    description, and (when a reference modules dir exists) the expected
    module(s) and suggested CONFIG_*. ``kconfig_map`` (a loaded ``kbuild_map``
    cache) widens the module→CONFIG_* step beyond the curated table; omitted,
    behavior is curated-only as before.
    """
    ref_dir = find_reference_modules_dir()
    pci_names = _pci_descriptions() if "pci" in buses else {}
    devices: list[Device] = []

    for bus in buses:
        bus_dir = _SYS_BUS / bus / "devices"
        try:
            entries = sorted(bus_dir.iterdir())
        except (FileNotFoundError, PermissionError, OSError):
            continue

        for dev_dir in entries:
            modalias = (_read_text(dev_dir / "modalias") or "").strip()
            if not modalias:
                continue

            if bus == "pci":
                class_id = (_read_text(dev_dir / "class") or "").strip()
                description = pci_names.get(dev_dir.name) or modalias
            else:  # usb
                class_id = (_read_text(dev_dir / "bDeviceClass") or "").strip()
                description = _usb_description(dev_dir) or modalias

            driver = _device_driver(dev_dir)
            expected = resolve_expected_modules(modalias, ref_dir)
            suggested = _suggested_kconfig(expected, kconfig_map)

            devices.append(Device(
                bus=bus,
                address=dev_dir.name,
                modalias=modalias,
                class_id=class_id,
                description=description,
                driver=driver,
                expected_modules=expected,
                suggested_kconfig=suggested,
            ))

    return devices


# ---------------------------------------------------------------------------
# Unsupported-device check
# ---------------------------------------------------------------------------

def check_unsupported_devices(
    *,
    devices: list[Device] | None = None,
) -> list[DeviceFinding]:
    """Flag functional devices that are present but have no driver bound.

    A finding is emitted only when the device is functional (not a
    bridge/hub), has no bound driver, and the reference DB names an expected
    module — i.e. the kernel *should* support it but currently doesn't. The
    suggested CONFIG_* rides along when the curated table knows it.

    Returns findings in a stable order (enumeration order).
    """
    devs = devices if devices is not None else enumerate_devices()
    findings: list[DeviceFinding] = []

    for d in devs:
        if d.driver is not None:
            continue
        if not d.expected_modules:
            continue
        if not _is_functional_device(d.bus, d.class_id):
            continue

        mods = ", ".join(d.expected_modules)
        kconf = ", ".join(d.suggested_kconfig)
        msg = (
            f"{d.bus} device {d.address} ({d.description}) has no driver bound; "
            f"reference kernel provides module {mods}."
        )
        if kconf:
            remediation = (
                f"Enable {kconf} in the kernel config (or load {mods}). "
                "If this is a custom kernel, the driver was not built."
            )
        else:
            remediation = (
                f"Build/load module {mods} (no curated CONFIG_* mapping — "
                "check the module's Kconfig symbol)."
            )
        findings.append(DeviceFinding(
            SEV_WARN, "unsupported_device", msg, remediation,
        ))

    return findings
