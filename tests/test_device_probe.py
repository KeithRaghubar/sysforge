"""
test_device_probe.py — unit tests for sysforge's PCI/USB device inventory
and driver-coverage probe. Filesystem inputs are routed through fixture
trees; the module→config table and modalias matching are exercised against
a trimmed modules.alias sample.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives import device_probe as dp

_DATA = Path(__file__).parent / "data"


# ---------------------------------------------------------------------------
# Helpers — build a fake sysfs / modules tree
# ---------------------------------------------------------------------------

def _make_ref_dir(tmp_path) -> Path:
    """A /lib/modules/<ver> dir holding the sample modules.alias."""
    ref = tmp_path / "lib" / "modules" / "7.0.10-arch1-1"
    ref.mkdir(parents=True)
    (ref / "modules.alias").write_text((_DATA / "modules.alias.sample").read_text())
    return ref


def _make_pci_device(bus_root: Path, bdf, modalias, class_id, *, driver=None):
    dev = bus_root / "pci" / "devices" / bdf
    dev.mkdir(parents=True)
    (dev / "modalias").write_text(modalias + "\n")
    (dev / "class").write_text(class_id + "\n")
    if driver is not None:
        drv = bus_root / "pci" / "drivers" / driver
        drv.mkdir(parents=True, exist_ok=True)
        (dev / "driver").symlink_to(drv)
    return dev


# Real AMD Starship/Matisse HD-audio controller modalias (front-panel audio).
_AMD_HDA = "pci:v00001022d00001487sv00001022sd00001487bc04sc03i00"


# ---------------------------------------------------------------------------
# resolve_expected_modules + curated kconfig
# ---------------------------------------------------------------------------

def test_resolve_amd_hda_to_snd_hda_intel(tmp_path):
    ref = _make_ref_dir(tmp_path)
    mods = dp.resolve_expected_modules(_AMD_HDA, ref)
    assert mods == ["snd_hda_intel"]


def test_resolve_nvme_modalias(tmp_path):
    ref = _make_ref_dir(tmp_path)
    mods = dp.resolve_expected_modules(
        "pci:v0000144Dd0000A80Asv00001028sd00002003bc01sc08i02", ref)
    assert "nvme" in mods


def test_resolve_no_ref_dir_returns_empty():
    assert dp.resolve_expected_modules(_AMD_HDA, None) == []


def test_resolve_no_match_returns_empty(tmp_path):
    ref = _make_ref_dir(tmp_path)
    assert dp.resolve_expected_modules("pci:v0000FFFFd0000FFFF", ref) == []


def test_suggested_kconfig_maps_known_modules():
    assert dp._suggested_kconfig(["snd_hda_intel"]) == ["CONFIG_SND_HDA_INTEL"]
    assert dp._suggested_kconfig(["nvme", "ahci"]) == [
        "CONFIG_BLK_DEV_NVME", "CONFIG_SATA_AHCI"]


def test_suggested_kconfig_unknown_module_degrades():
    assert dp._suggested_kconfig(["some_obscure_mod"]) == []


def test_suggested_kconfig_extra_map_extends():
    extra = {"some_obscure_mod": "CONFIG_OBSCURE"}
    assert dp._suggested_kconfig(["some_obscure_mod"], extra) == ["CONFIG_OBSCURE"]


def test_suggested_kconfig_curated_wins_over_extra():
    extra = {"nvme": "CONFIG_FROM_TREE"}
    assert dp._suggested_kconfig(["nvme"], extra) == ["CONFIG_BLK_DEV_NVME"]


# ---------------------------------------------------------------------------
# functional-device filter (false-positive guard)
# ---------------------------------------------------------------------------

def test_pci_bridge_is_nonfunctional():
    assert dp._is_functional_device("pci", "0x060400") is False  # PCI bridge


def test_pci_audio_is_functional():
    assert dp._is_functional_device("pci", "0x040300") is True   # audio device


def test_usb_hub_is_nonfunctional():
    assert dp._is_functional_device("usb", "0x09") is False


# ---------------------------------------------------------------------------
# find_reference_modules_dir
# ---------------------------------------------------------------------------

def test_find_reference_excludes_custom(tmp_path, monkeypatch):
    base = tmp_path / "lib" / "modules"
    (base / "7.0.10-arch1-1").mkdir(parents=True)
    (base / "7.0.10-arch1-1" / "modules.alias").write_text("")
    (base / "7.0.9-custom").mkdir(parents=True)
    (base / "7.0.9-custom" / "modules.alias").write_text("")
    monkeypatch.setattr(dp, "_MODULES_BASE", base)
    ref = dp.find_reference_modules_dir()
    assert ref is not None
    assert ref.name == "7.0.10-arch1-1"


def test_find_reference_none_when_only_custom(tmp_path, monkeypatch):
    base = tmp_path / "lib" / "modules"
    (base / "7.0.9-custom").mkdir(parents=True)
    monkeypatch.setattr(dp, "_MODULES_BASE", base)
    assert dp.find_reference_modules_dir() is None


def test_find_reference_picks_newest(tmp_path, monkeypatch):
    base = tmp_path / "lib" / "modules"
    for ver in ("6.12.1-arch1-1", "7.0.10-arch1-1", "6.6.0-arch1-1"):
        (base / ver).mkdir(parents=True)
        (base / ver / "modules.alias").write_text("")
    monkeypatch.setattr(dp, "_MODULES_BASE", base)
    assert dp.find_reference_modules_dir().name == "7.0.10-arch1-1"


# ---------------------------------------------------------------------------
# enumerate_devices (fixture sysfs tree)
# ---------------------------------------------------------------------------

def test_enumerate_unbound_audio(tmp_path, monkeypatch):
    bus_root = tmp_path / "sys" / "bus"
    _make_pci_device(bus_root, "0000:0d:00.4", _AMD_HDA, "0x040300")  # no driver
    ref = _make_ref_dir(tmp_path)
    monkeypatch.setattr(dp, "_SYS_BUS", bus_root)
    monkeypatch.setattr(dp, "find_reference_modules_dir", lambda: ref)
    monkeypatch.setattr(dp, "_pci_descriptions", lambda: {})

    devs = dp.enumerate_devices(buses=("pci",))
    assert len(devs) == 1
    d = devs[0]
    assert d.address == "0000:0d:00.4"
    assert d.driver is None
    assert d.expected_modules == ["snd_hda_intel"]
    assert d.suggested_kconfig == ["CONFIG_SND_HDA_INTEL"]


def test_enumerate_bound_device_has_driver(tmp_path, monkeypatch):
    bus_root = tmp_path / "sys" / "bus"
    _make_pci_device(bus_root, "0000:0d:00.4", _AMD_HDA, "0x040300",
                     driver="snd_hda_intel")
    ref = _make_ref_dir(tmp_path)
    monkeypatch.setattr(dp, "_SYS_BUS", bus_root)
    monkeypatch.setattr(dp, "find_reference_modules_dir", lambda: ref)
    monkeypatch.setattr(dp, "_pci_descriptions", lambda: {})

    devs = dp.enumerate_devices(buses=("pci",))
    assert devs[0].driver == "snd_hda_intel"


def test_enumerate_threads_kconfig_map(tmp_path, monkeypatch):
    bus_root = tmp_path / "sys" / "bus"
    _make_pci_device(bus_root, "0000:0d:00.4", _AMD_HDA, "0x040300")
    ref = _make_ref_dir(tmp_path)
    monkeypatch.setattr(dp, "_SYS_BUS", bus_root)
    monkeypatch.setattr(dp, "find_reference_modules_dir", lambda: ref)
    monkeypatch.setattr(dp, "_pci_descriptions", lambda: {})
    # Empty the curated table so only the tree-derived map can answer —
    # proves the kwarg reaches the suggestion step.
    monkeypatch.setattr(dp, "_MODULE_TO_KCONFIG", {})

    devs = dp.enumerate_devices(
        buses=("pci",), kconfig_map={"snd_hda_intel": "CONFIG_SND_HDA_INTEL"})
    assert devs[0].suggested_kconfig == ["CONFIG_SND_HDA_INTEL"]


# ---------------------------------------------------------------------------
# check_unsupported_devices
# ---------------------------------------------------------------------------

def _dev(**kw):
    base = dict(bus="pci", address="0000:0d:00.4", modalias=_AMD_HDA,
                class_id="0x040300", description="AMD HD Audio", driver=None,
                expected_modules=["snd_hda_intel"],
                suggested_kconfig=["CONFIG_SND_HDA_INTEL"])
    base.update(kw)
    return dp.Device(**base)


def test_unsupported_flags_unbound_functional():
    findings = dp.check_unsupported_devices(devices=[_dev()])
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == dp.SEV_WARN
    assert f.check_id == "unsupported_device"
    assert "snd_hda_intel" in f.message
    assert "CONFIG_SND_HDA_INTEL" in f.remediation


def test_unsupported_skips_bound_device():
    assert dp.check_unsupported_devices(devices=[_dev(driver="snd_hda_intel")]) == []


def test_unsupported_skips_bridge():
    bridge = _dev(class_id="0x060400", expected_modules=["pcieport"],
                  suggested_kconfig=[])
    assert dp.check_unsupported_devices(devices=[bridge]) == []


def test_unsupported_skips_device_with_no_expected_module():
    assert dp.check_unsupported_devices(devices=[_dev(expected_modules=[])]) == []


def test_unsupported_unknown_kconfig_still_flags():
    d = _dev(expected_modules=["weird_mod"], suggested_kconfig=[])
    findings = dp.check_unsupported_devices(devices=[d])
    assert len(findings) == 1
    assert "weird_mod" in findings[0].message
    assert "no curated CONFIG_*" in findings[0].remediation
