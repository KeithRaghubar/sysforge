"""
test_stage_bootstrap.py — unit tests for bootstrap stages 1-4.

Covers pure-logic functions. Subprocess calls (sgdisk, mkfs, pacstrap,
arch-chroot, etc.) are mocked — nothing real runs.
"""
import textwrap
from unittest.mock import MagicMock, patch

import pytest

from sysforge.pipeline.stages.base import RunOptions
from sysforge.pipeline.stages._bootstrap import BootstrapConfig, load_bootstrap
from sysforge.pipeline.stages.hardware import (
    _cpu_kconfig,
    _gpu_kconfig,
    _has_nvme,
    _parse_cpuinfo,
    parse_gpu_vendors,
    _write_hardware_profile,
    HardwareStage,
)
from sysforge.pipeline.stages.configure import (
    ConfigureStage,
    _set_hostname,
    _set_locale,
    _set_timezone,
    _set_keymap,
    _set_pacman_parallel_downloads,
)
from sysforge.pipeline.stages.base_install import BaseInstallStage, _BASE_PACKAGES
from sysforge.pipeline.stages.partition import PartitionStage, _partition_disk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_options(**kwargs):
    defaults = dict(
        resume=False, start_from=None, force_retry=False,
        dry_run=False, state_dir=None,
        no_unified_log=False, no_pkg_logs=False,
        log_dir=None, purge_log=False, persist_log=False,
    )
    defaults.update(kwargs)
    return RunOptions(**defaults)


def make_cfg(**kwargs) -> BootstrapConfig:
    defaults = dict(
        target="/mnt",
        device="/dev/vda",
        esp_size_mib=512,
        root_fs="ext4",
        hostname="testhost",
        locale="en_US.UTF-8",
        timezone="UTC",
        keymap="us",
        parallel_downloads=5,
        mirror_countries=[],
        mirror_protocol="https",
        mirror_age=12,
    )
    defaults.update(kwargs)
    return BootstrapConfig(**defaults)


# ---------------------------------------------------------------------------
# load_bootstrap
# ---------------------------------------------------------------------------

class TestLoadBootstrap:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="bootstrap.toml not found"):
            load_bootstrap(tmp_path / "nonexistent.toml")

    def test_minimal_valid(self, tmp_path):
        f = tmp_path / "bootstrap.toml"
        f.write_text(textwrap.dedent("""\
            target = "/mnt"
            [partition]
            device = "/dev/sda"
            [system]
            hostname = "myhost"
            locale   = "en_US.UTF-8"
            timezone = "UTC"
        """))
        cfg = load_bootstrap(f)
        assert cfg.target == "/mnt"
        assert cfg.device == "/dev/sda"
        assert cfg.hostname == "myhost"
        assert cfg.locale == "en_US.UTF-8"
        assert cfg.timezone == "UTC"
        assert cfg.keymap == "us"            # default
        assert cfg.esp_size_mib == 512       # default
        assert cfg.root_fs == "ext4"         # default
        assert cfg.parallel_downloads == 5   # default
        assert cfg.mirror_protocol == "https"
        assert cfg.mirror_age == 12

    def test_full_config(self, tmp_path):
        f = tmp_path / "bootstrap.toml"
        f.write_text(textwrap.dedent("""\
            target = "/mnt"
            [partition]
            device       = "/dev/nvme0n1"
            esp_size_mib = 1024
            root_fs      = "btrfs"
            [system]
            hostname           = "mybox"
            locale             = "de_DE.UTF-8"
            timezone           = "Europe/Berlin"
            keymap             = "de"
            parallel_downloads = 10
            [mirror]
            countries = ["Germany", "Austria"]
            protocol  = "https"
            age       = 6
        """))
        cfg = load_bootstrap(f)
        assert cfg.device == "/dev/nvme0n1"
        assert cfg.esp_size_mib == 1024
        assert cfg.root_fs == "btrfs"
        assert cfg.hostname == "mybox"
        assert cfg.keymap == "de"
        assert cfg.parallel_downloads == 10
        assert cfg.mirror_countries == ["Germany", "Austria"]
        assert cfg.mirror_age == 6

    def test_invalid_root_fs(self, tmp_path):
        f = tmp_path / "bootstrap.toml"
        f.write_text(textwrap.dedent("""\
            target = "/mnt"
            [partition]
            device  = "/dev/sda"
            root_fs = "xfs"
            [system]
            hostname = "h"
            locale   = "en_US.UTF-8"
            timezone = "UTC"
        """))
        with pytest.raises(RuntimeError, match="root_fs"):
            load_bootstrap(f)

    def test_missing_required_raises(self, tmp_path):
        f = tmp_path / "bootstrap.toml"
        f.write_text(textwrap.dedent("""\
            target = "/mnt"
            [partition]
            device = "/dev/sda"
            [system]
            locale   = "en_US.UTF-8"
            timezone = "UTC"
        """))
        with pytest.raises(RuntimeError, match="hostname"):
            load_bootstrap(f)

    def test_toml_syntax_error(self, tmp_path):
        f = tmp_path / "bootstrap.toml"
        f.write_text("target = [broken\n")
        with pytest.raises(RuntimeError, match="parse error"):
            load_bootstrap(f)


# ---------------------------------------------------------------------------
# Hardware stage — CPU detection
# ---------------------------------------------------------------------------

_AMD_ZEN3_CPUINFO = textwrap.dedent("""\
    processor\t: 0
    vendor_id\t: AuthenticAMD
    cpu family\t: 25
    model\t\t: 33
    model name\t: AMD Ryzen 7 5800X3D
""")

_AMD_ZEN4_CPUINFO = textwrap.dedent("""\
    processor\t: 0
    vendor_id\t: AuthenticAMD
    cpu family\t: 25
    model\t\t: 97
    model name\t: AMD Ryzen 9 7950X
""")

_INTEL_CPUINFO = textwrap.dedent("""\
    processor\t: 0
    vendor_id\t: GenuineIntel
    cpu family\t: 6
    model\t\t: 154
    model name\t: 13th Gen Intel Core i9-13900K
""")

_AMD_UNKNOWN_CPUINFO = textwrap.dedent("""\
    processor\t: 0
    vendor_id\t: AuthenticAMD
    cpu family\t: 25
    model\t\t: 255
    model name\t: AMD Unknown Processor
""")


class TestParseCpuinfo:
    def test_amd_zen3(self):
        info = _parse_cpuinfo(_AMD_ZEN3_CPUINFO)
        assert info["cpu_vendor"] == "AuthenticAMD"
        assert info["cpu_family"] == 25
        assert info["cpu_model"] == 33

    def test_intel(self):
        info = _parse_cpuinfo(_INTEL_CPUINFO)
        assert info["cpu_vendor"] == "GenuineIntel"
        assert info["cpu_family"] == 6

    def test_empty(self):
        info = _parse_cpuinfo("")
        assert info == {}


class TestCpuKconfig:
    def test_zen3_gets_mzen3_and_pstate(self):
        info = _parse_cpuinfo(_AMD_ZEN3_CPUINFO)
        kconfig = _cpu_kconfig(info)
        assert kconfig.get("CONFIG_MZEN3") == "y"
        assert kconfig.get("CONFIG_X86_AMD_PSTATE") == "y"

    def test_zen4_gets_mzen4_and_pstate(self):
        info = _parse_cpuinfo(_AMD_ZEN4_CPUINFO)
        kconfig = _cpu_kconfig(info)
        assert kconfig.get("CONFIG_MZEN4") == "y"
        assert kconfig.get("CONFIG_X86_AMD_PSTATE") == "y"

    def test_intel_no_amd_entries(self):
        info = _parse_cpuinfo(_INTEL_CPUINFO)
        kconfig = _cpu_kconfig(info)
        assert "CONFIG_MZEN3" not in kconfig
        assert "CONFIG_X86_AMD_PSTATE" not in kconfig

    def test_amd_unknown_model_gets_pstate_only(self):
        info = _parse_cpuinfo(_AMD_UNKNOWN_CPUINFO)
        kconfig = _cpu_kconfig(info)
        assert "CONFIG_X86_AMD_PSTATE" in kconfig
        # No specific Mzen* entry for unknown model
        assert "CONFIG_MZEN3" not in kconfig
        assert "CONFIG_MZEN4" not in kconfig

    def test_empty_info_returns_empty(self):
        assert _cpu_kconfig({}) == {}


# ---------------------------------------------------------------------------
# Hardware stage — GPU detection
# ---------------------------------------------------------------------------

_LSPCI_NVIDIA = """\
00:01.0 PCI bridge: Intel Corporation 12th Gen Core Processor PCI Express x16 Controller
01:00.0 VGA compatible controller: NVIDIA Corporation GA102 [GeForce RTX 3080] (rev a1)
01:00.1 Audio device: NVIDIA Corporation GA102 High Definition Audio Controller
"""

_LSPCI_AMD = """\
00:02.0 VGA compatible controller: Advanced Micro Devices, Inc. [AMD/ATI] Navi 21 [Radeon RX 6800 XT]
"""

_LSPCI_INTEL = """\
00:02.0 VGA compatible controller: Intel Corporation Alder Lake-P GT2 [Iris Xe Graphics]
"""

_LSPCI_MULTI = """\
00:02.0 VGA compatible controller: Intel Corporation UHD Graphics 630
01:00.0 3D controller: NVIDIA Corporation TU117M [GeForce GTX 1650 Mobile]
"""

_LSPCI_NVME = """\
00:1d.0 PCI bridge: Intel Corporation
05:00.0 Non-Volatile memory controller: Samsung Electronics Co Ltd NVMe SSD Controller PM9A1/PM9A3/980PRO
"""


class TestParseGpuVendors:
    def test_nvidia(self):
        assert parse_gpu_vendors(_LSPCI_NVIDIA) == ["nvidia"]

    def test_amd(self):
        assert parse_gpu_vendors(_LSPCI_AMD) == ["amd"]

    def test_intel(self):
        assert parse_gpu_vendors(_LSPCI_INTEL) == ["intel"]

    def test_multi(self):
        vendors = parse_gpu_vendors(_LSPCI_MULTI)
        assert "intel" in vendors
        assert "nvidia" in vendors

    def test_empty(self):
        assert parse_gpu_vendors("") == []


class TestGpuKconfig:
    def test_nvidia_disables_nouveau(self):
        kconfig = _gpu_kconfig(["nvidia"])
        assert kconfig.get("CONFIG_DRM_NOUVEAU") == "n"

    def test_amd_no_nouveau(self):
        kconfig = _gpu_kconfig(["amd"])
        assert "CONFIG_DRM_NOUVEAU" not in kconfig

    def test_no_gpu_empty(self):
        assert _gpu_kconfig([]) == {}


class TestHasNvme:
    def test_nvme_present(self):
        assert _has_nvme(_LSPCI_NVME) is True

    def test_no_nvme(self):
        assert _has_nvme(_LSPCI_NVIDIA) is False

    def test_empty(self):
        assert _has_nvme("") is False


# ---------------------------------------------------------------------------
# Hardware stage — write hardware_profile.toml
# ---------------------------------------------------------------------------

class TestWriteHardwareProfile:
    def test_writes_toml(self, tmp_path):
        out = tmp_path / "hardware_profile.toml"
        hw = {"cpu_vendor": "AuthenticAMD", "cpu_family": 25, "cpu_model": 33,
              "gpu_vendors": ["nvidia"], "nvme": True}
        kconfig = {"CONFIG_MZEN3": "y", "CONFIG_DRM_NOUVEAU": "n"}
        _write_hardware_profile(out, hw, kconfig, dry_run=False)

        assert out.exists()
        import tomllib
        with open(out, "rb") as f:
            data = tomllib.load(f)

        assert data["hardware"]["cpu_vendor"] == "AuthenticAMD"
        assert data["hardware"]["gpu_vendors"] == ["nvidia"]
        assert data["hardware"]["nvme"] is True
        assert data["kconfig"]["CONFIG_MZEN3"] == "y"
        assert data["kconfig"]["CONFIG_DRM_NOUVEAU"] == "n"

    def test_dry_run_no_write(self, tmp_path):
        out = tmp_path / "hardware_profile.toml"
        _write_hardware_profile(out, {}, {}, dry_run=True)
        assert not out.exists()

    def test_no_kconfig_omits_section(self, tmp_path):
        out = tmp_path / "hardware_profile.toml"
        hw = {"cpu_vendor": "GenuineIntel", "cpu_family": 6, "cpu_model": 154,
              "gpu_vendors": [], "nvme": False}
        _write_hardware_profile(out, hw, {}, dry_run=False)
        import tomllib
        with open(out, "rb") as f:
            data = tomllib.load(f)
        assert "kconfig" not in data

    def test_devices_array_of_tables(self, tmp_path):
        from sysforge.primitives.device_probe import Device
        out = tmp_path / "hardware_profile.toml"
        hw = {"cpu_vendor": "AuthenticAMD", "cpu_family": 25, "cpu_model": 33,
              "gpu_vendors": [], "nvme": True}
        devices = [
            Device(bus="pci", address="0000:0d:00.4",
                   modalias='pci:v00001022d00001487 with "quote"\nand newline',
                   class_id="0x040300", description="AMD HD Audio", driver=None,
                   expected_modules=["snd_hda_intel"],
                   suggested_kconfig=["CONFIG_SND_HDA_INTEL"]),
        ]
        _write_hardware_profile(out, hw, {"CONFIG_MZEN3": "y"},
                                dry_run=False, devices=devices)
        import tomllib
        with open(out, "rb") as f:
            data = tomllib.load(f)
        # Control chars in the modalias must not break TOML parsing.
        assert len(data["devices"]) == 1
        d = data["devices"][0]
        assert d["bus"] == "pci"
        assert d["address"] == "0000:0d:00.4"
        assert d["driver"] == ""
        assert d["expected_modules"] == ["snd_hda_intel"]
        assert d["suggested_kconfig"] == ["CONFIG_SND_HDA_INTEL"]
        assert data["kconfig"]["CONFIG_MZEN3"] == "y"

    def test_device_kconfig_table_emitted(self, tmp_path):
        out = tmp_path / "hardware_profile.toml"
        hw = {"cpu_vendor": "AuthenticAMD", "cpu_family": 25, "cpu_model": 33,
              "gpu_vendors": [], "nvme": True}
        _write_hardware_profile(out, hw, {"CONFIG_BLK_DEV_NVME": "y"},
                                dry_run=False,
                                device_kconfig={"CONFIG_IGC": "m"})
        import tomllib
        with open(out, "rb") as f:
            data = tomllib.load(f)
        assert data["kconfig_devices"] == {"CONFIG_IGC": "m"}
        assert data["kconfig"]["CONFIG_BLK_DEV_NVME"] == "y"

    def test_no_device_kconfig_omits_section(self, tmp_path):
        out = tmp_path / "hardware_profile.toml"
        hw = {"cpu_vendor": "AuthenticAMD", "cpu_family": 25, "cpu_model": 33,
              "gpu_vendors": [], "nvme": False}
        _write_hardware_profile(out, hw, {}, dry_run=False, device_kconfig={})
        import tomllib
        with open(out, "rb") as f:
            data = tomllib.load(f)
        assert "kconfig_devices" not in data


# ---------------------------------------------------------------------------
# Hardware stage — architecture-aware kconfig disable
# ---------------------------------------------------------------------------

from sysforge.pipeline.stages.hardware import (  # noqa: E402
    _ARCH_OWNED_KCONFIG,
    _HOST_ARCH_TO_KCONFIG_DOMAIN,
    _arch_disable_kconfig,
)


class TestArchDisableKconfig:
    def test_x86_64_disables_non_x86_arches(self):
        disable = _arch_disable_kconfig("x86_64")
        # Top-level non-x86 architecture umbrellas are disabled
        assert disable.get("CONFIG_ARM64") == "n"
        assert disable.get("CONFIG_ARM") == "n"
        assert disable.get("CONFIG_RISCV") == "n"
        assert disable.get("CONFIG_PPC") == "n"
        assert disable.get("CONFIG_MIPS") == "n"
        assert disable.get("CONFIG_SPARC") == "n"
        assert disable.get("CONFIG_LOONGARCH") == "n"
        # Curated arm64 SoC umbrellas are disabled
        assert disable.get("CONFIG_ARCH_QCOM") == "n"
        assert disable.get("CONFIG_ARCH_TEGRA") == "n"
        assert disable.get("CONFIG_ARCH_ROCKCHIP") == "n"
        # x86 keys are NOT in the disable set
        assert "CONFIG_X86" not in disable
        assert "CONFIG_X86_64" not in disable
        assert "CONFIG_MICROCODE_INTEL" not in disable

    def test_aarch64_disables_x86_arm32_riscv_ppc_mips(self):
        disable = _arch_disable_kconfig("aarch64")
        assert disable.get("CONFIG_X86") == "n"
        assert disable.get("CONFIG_X86_64") == "n"
        assert disable.get("CONFIG_ARM") == "n"   # 32-bit ARM
        assert disable.get("CONFIG_RISCV") == "n"
        assert disable.get("CONFIG_PPC") == "n"
        assert disable.get("CONFIG_MIPS") == "n"
        # arm64 keys (including SoC umbrellas) are NOT disabled
        assert "CONFIG_ARM64" not in disable
        assert "CONFIG_ARCH_QCOM" not in disable
        assert "CONFIG_ARCH_TEGRA" not in disable

    def test_riscv64_disables_everything_except_riscv(self):
        disable = _arch_disable_kconfig("riscv64")
        assert disable.get("CONFIG_X86") == "n"
        assert disable.get("CONFIG_ARM64") == "n"
        assert disable.get("CONFIG_PPC") == "n"
        assert "CONFIG_RISCV" not in disable

    def test_unknown_host_arch_returns_empty(self, capsys):
        disable = _arch_disable_kconfig("weirdarch")
        assert disable == {}
        captured = capsys.readouterr()
        assert "weirdarch" in captured.err
        assert "arch-disable skipped" in captured.err

    def test_all_known_host_archs_map_to_a_registry_domain(self):
        # Every entry in _HOST_ARCH_TO_KCONFIG_DOMAIN must point at a domain
        # that actually exists in _ARCH_OWNED_KCONFIG, otherwise that host
        # gets no disable entries at all (and no WARN, since the map lookup
        # succeeds but the .get(domain) miss is silent).
        for host_arch, domain in _HOST_ARCH_TO_KCONFIG_DOMAIN.items():
            assert domain in _ARCH_OWNED_KCONFIG, (
                f"host_arch {host_arch!r} maps to domain {domain!r} "
                f"which is not in _ARCH_OWNED_KCONFIG"
            )

    def test_host_owned_keys_never_disabled(self, monkeypatch):
        # Synthetic: register the same key in both the host and a non-host
        # domain. The defensive filter must skip it on the host.
        custom_owned = dict(_ARCH_OWNED_KCONFIG)
        custom_owned["x86"] = frozenset(custom_owned["x86"] | {"CONFIG_SHARED_KEY"})
        custom_owned["arm64"] = frozenset(custom_owned["arm64"] | {"CONFIG_SHARED_KEY"})
        monkeypatch.setattr(
            "sysforge.pipeline.stages.hardware._ARCH_OWNED_KCONFIG",
            custom_owned,
        )
        disable = _arch_disable_kconfig("x86_64")
        assert "CONFIG_SHARED_KEY" not in disable


# ---------------------------------------------------------------------------
# Hardware stage — full run
# ---------------------------------------------------------------------------

class TestHardwareStageRun:
    def test_run_detects_and_writes(self, tmp_path):
        stage = HardwareStage()
        options = make_options(state_dir=tmp_path)

        with patch("sysforge.pipeline.stages.hardware.Path") as mock_path, \
             patch("sysforge.pipeline.stages.hardware.subprocess.run") as mock_run, \
             patch("sysforge.pipeline.stages.hardware.resolve_state_dir",
                   return_value=(tmp_path, "test")):

            # /proc/cpuinfo
            mock_path.return_value.read_text.return_value = _AMD_ZEN3_CPUINFO
            mock_path.return_value.__truediv__ = lambda self, other: tmp_path / other

            mock_run.return_value = MagicMock(
                returncode=0, stdout=_LSPCI_NVIDIA + _LSPCI_NVME
            )

            # Re-implement: patch at a higher level to avoid Path complexity
            with patch("builtins.open", side_effect=lambda p, *a, **kw:
                       open(p, *a, **kw) if str(p) != "/proc/cpuinfo" else None):
                pass  # tested via direct unit tests above

        # Direct integration test using real functions
        from sysforge.primitives.device_probe import Device
        fake_devices = [
            Device(bus="pci", address="0000:0d:00.4",
                   modalias="pci:v00001022d00001487", class_id="0x040300",
                   description="AMD HD Audio", driver=None,
                   expected_modules=["snd_hda_intel"],
                   suggested_kconfig=["CONFIG_SND_HDA_INTEL"]),
            Device(bus="pci", address="0000:01:00.0",
                   modalias="pci:v0000144Dd0000A80A", class_id="0x010802",
                   description="Samsung NVMe", driver="nvme",
                   expected_modules=["nvme"], suggested_kconfig=["CONFIG_BLK_DEV_NVME"]),
        ]
        with patch("sysforge.pipeline.stages.hardware.subprocess.run") as mock_run, \
             patch("sysforge.pipeline.stages.hardware.resolve_state_dir",
                   return_value=(tmp_path, "test")), \
             patch("sysforge.primitives.device_probe.enumerate_devices",
                   return_value=fake_devices), \
             patch("pathlib.Path.read_text", return_value=_AMD_ZEN3_CPUINFO):

            mock_run.return_value = MagicMock(
                returncode=0, stdout=_LSPCI_NVIDIA + "\n" + _LSPCI_NVME
            )
            stage.run({}, MagicMock(), options)

        out = tmp_path / "hardware_profile.toml"
        assert out.exists()
        import tomllib
        with open(out, "rb") as f:
            data = tomllib.load(f)
        assert data["hardware"]["cpu_vendor"] == "AuthenticAMD"
        assert data["hardware"]["host_arch"]
        assert data["hardware"]["llvm_targets"]
        assert "NVPTX" in data["hardware"]["llvm_targets"]
        assert "AMDGPU" not in data["hardware"]["llvm_targets"]
        # [[devices]] inventory written, with the unbound audio controller.
        assert len(data["devices"]) == 2
        audio = next(d for d in data["devices"] if d["address"] == "0000:0d:00.4")
        assert audio["driver"] == ""
        assert audio["suggested_kconfig"] == ["CONFIG_SND_HDA_INTEL"]
        assert data["kconfig"]["CONFIG_MZEN3"] == "y"
        assert data["kconfig"]["CONFIG_DRM_NOUVEAU"] == "n"
        assert data["kconfig"]["CONFIG_BLK_DEV_NVME"] == "y"

    def test_dry_run_no_write(self, tmp_path):
        stage = HardwareStage()
        options = make_options(state_dir=tmp_path, dry_run=True)

        with patch("sysforge.pipeline.stages.hardware.subprocess.run") as mock_run, \
             patch("sysforge.pipeline.stages.hardware.resolve_state_dir",
                   return_value=(tmp_path, "test")), \
             patch("sysforge.primitives.device_probe.enumerate_devices",
                   return_value=[]), \
             patch("pathlib.Path.read_text", return_value=_AMD_ZEN3_CPUINFO):

            mock_run.return_value = MagicMock(returncode=0, stdout=_LSPCI_NVIDIA)
            stage.run({}, MagicMock(), options)

        assert not (tmp_path / "hardware_profile.toml").exists()

    def test_run_emits_device_kconfig_deduped(self, tmp_path):
        """Device suggested_kconfig is folded into [kconfig_devices] as =m,
        minus anything the heuristic [kconfig] already owns (the NVMe symbol
        here, present as =y via lspci detection)."""
        stage = HardwareStage()
        options = make_options(state_dir=tmp_path)
        from sysforge.primitives.device_probe import Device
        fake_devices = [
            Device(bus="pci", address="0000:0d:00.4",
                   modalias="pci:v00001022d00001487", class_id="0x040300",
                   description="AMD HD Audio", driver=None,
                   expected_modules=["snd_hda_intel"],
                   suggested_kconfig=["CONFIG_SND_HDA_INTEL"]),
            Device(bus="pci", address="0000:01:00.0",
                   modalias="pci:v0000144Dd0000A80A", class_id="0x010802",
                   description="Samsung NVMe", driver="nvme",
                   expected_modules=["nvme"],
                   suggested_kconfig=["CONFIG_BLK_DEV_NVME"]),
        ]
        with patch("sysforge.pipeline.stages.hardware.subprocess.run") as mock_run, \
             patch("sysforge.pipeline.stages.hardware.resolve_state_dir",
                   return_value=(tmp_path, "test")), \
             patch("sysforge.primitives.device_probe.enumerate_devices",
                   return_value=fake_devices), \
             patch("pathlib.Path.read_text", return_value=_AMD_ZEN3_CPUINFO):

            mock_run.return_value = MagicMock(
                returncode=0, stdout=_LSPCI_NVIDIA + "\n" + _LSPCI_NVME
            )
            stage.run({}, MagicMock(), options)

        import tomllib
        with open(tmp_path / "hardware_profile.toml", "rb") as f:
            data = tomllib.load(f)
        assert data["kconfig"]["CONFIG_BLK_DEV_NVME"] == "y"
        assert data["kconfig_devices"] == {"CONFIG_SND_HDA_INTEL": "m"}

    def test_run_loads_kbuild_cache_into_probe(self, tmp_path):
        """A cached kbuild map in the state dir is handed to the device probe
        so module→CONFIG_* coverage widens beyond the curated table."""
        from sysforge.primitives import kbuild_map
        stage = HardwareStage()
        options = make_options(state_dir=tmp_path)
        kbuild_map.save_map(tmp_path / kbuild_map.KBUILD_MAP_FILENAME,
                            {"igc": "CONFIG_IGC"}, "6.10.0-test")

        captured = {}
        def fake_enumerate(*a, **k):
            captured.update(k)
            return []
        # No Path.read_text patch here — load_map must read the real cache
        # file; /proc/cpuinfo is read for real (any host cpuinfo parses).
        with patch("sysforge.pipeline.stages.hardware.subprocess.run") as mock_run, \
             patch("sysforge.pipeline.stages.hardware.resolve_state_dir",
                   return_value=(tmp_path, "test")), \
             patch("sysforge.primitives.device_probe.enumerate_devices",
                   side_effect=fake_enumerate):

            mock_run.return_value = MagicMock(returncode=0, stdout="")
            stage.run({}, MagicMock(), options)

        assert captured.get("kconfig_map") == {"igc": "CONFIG_IGC"}


# ---------------------------------------------------------------------------
# Configure stage helpers
# ---------------------------------------------------------------------------

class TestSetHostname:
    def test_writes_hostname(self, tmp_path):
        etc = tmp_path / "etc"
        etc.mkdir()
        cfg = make_cfg(target=str(tmp_path))
        _set_hostname(cfg)
        assert (etc / "hostname").read_text() == "testhost\n"


class TestSetLocale:
    def test_uncomments_locale(self, tmp_path):
        etc = tmp_path / "etc"
        etc.mkdir()
        locale_gen = etc / "locale.gen"
        locale_gen.write_text(
            "# en_US.UTF-8 UTF-8\n# de_DE.UTF-8 UTF-8\n"
        )
        cfg = make_cfg(target=str(tmp_path))

        with patch("sysforge.pipeline.stages.configure._chroot"):
            _set_locale(cfg)

        text = locale_gen.read_text()
        assert text.startswith("en_US.UTF-8 UTF-8")
        assert "# de_DE.UTF-8" in text
        assert (etc / "locale.conf").read_text() == "LANG=en_US.UTF-8\n"

    def test_missing_locale_gen_warns(self, tmp_path):
        etc = tmp_path / "etc"
        etc.mkdir()
        cfg = make_cfg(target=str(tmp_path))
        with patch("sysforge.pipeline.stages.configure._chroot"):
            # Should not raise even if locale.gen missing
            _set_locale(cfg)


class TestSetTimezone:
    def test_calls_chroot(self, tmp_path):
        cfg = make_cfg(target=str(tmp_path), timezone="America/New_York")
        with patch("sysforge.pipeline.stages.configure._chroot") as mock_chroot:
            _set_timezone(cfg)
        calls = [c.args[1] for c in mock_chroot.call_args_list]
        assert any("America/New_York" in " ".join(c) for c in calls)
        assert any("hwclock" in c for c in calls)


class TestSetKeymap:
    def test_default_us_skipped(self, tmp_path):
        cfg = make_cfg(target=str(tmp_path), keymap="us")
        _set_keymap(cfg)
        assert not (tmp_path / "etc/vconsole.conf").exists()

    def test_non_default_writes_file(self, tmp_path):
        etc = tmp_path / "etc"
        etc.mkdir()
        cfg = make_cfg(target=str(tmp_path), keymap="de")
        _set_keymap(cfg)
        assert (etc / "vconsole.conf").read_text() == "KEYMAP=de\n"


class TestSetPacmanParallelDownloads:
    def test_sets_value(self, tmp_path):
        etc = tmp_path / "etc"
        etc.mkdir()
        conf = etc / "pacman.conf"
        conf.write_text("[options]\n#ParallelDownloads = 5\nColor\n")
        cfg = make_cfg(target=str(tmp_path), parallel_downloads=10)
        _set_pacman_parallel_downloads(cfg)
        assert "ParallelDownloads = 10" in conf.read_text()

    def test_inserts_if_absent(self, tmp_path):
        etc = tmp_path / "etc"
        etc.mkdir()
        conf = etc / "pacman.conf"
        conf.write_text("[options]\nColor\n")
        cfg = make_cfg(target=str(tmp_path), parallel_downloads=8)
        _set_pacman_parallel_downloads(cfg)
        assert "ParallelDownloads = 8" in conf.read_text()


# ---------------------------------------------------------------------------
# Configure stage — full run (dry_run)
# ---------------------------------------------------------------------------

class TestConfigureStageDryRun:
    def test_dry_run_no_writes(self, tmp_path):
        stage = ConfigureStage()
        options = make_options(dry_run=True)

        f = tmp_path / "bootstrap.toml"
        f.write_text(textwrap.dedent(f"""\
            target = "{tmp_path}"
            [partition]
            device = "/dev/vda"
            [system]
            hostname = "h"
            locale   = "en_US.UTF-8"
            timezone = "UTC"
        """))

        with patch("sysforge.pipeline.stages.configure.load_bootstrap",
                   return_value=make_cfg(target=str(tmp_path))):
            stage.run({}, MagicMock(), options)

        # No files should have been written
        assert not (tmp_path / "etc/hostname").exists()


# ---------------------------------------------------------------------------
# BaseInstall stage
# ---------------------------------------------------------------------------

class TestBaseInstallStage:
    def test_dry_run_no_subprocess(self, tmp_path):
        stage = BaseInstallStage()
        options = make_options(dry_run=True)
        with patch("sysforge.pipeline.stages.base_install.load_bootstrap",
                   return_value=make_cfg(target=str(tmp_path))), \
             patch("sysforge.pipeline.stages.base_install.subprocess.run") as mock_run:
            stage.run({}, MagicMock(), options)
        mock_run.assert_not_called()

    def test_base_packages_non_empty(self):
        assert len(_BASE_PACKAGES) >= 5
        assert "base" in _BASE_PACKAGES
        assert "linux" in _BASE_PACKAGES

    def test_calls_pacstrap_and_genfstab(self, tmp_path):
        stage = BaseInstallStage()
        options = make_options()
        target = str(tmp_path)

        with patch("sysforge.pipeline.stages.base_install.load_bootstrap",
                   return_value=make_cfg(target=target)), \
             patch("sysforge.pipeline.stages.base_install._verify_target_mounted"), \
             patch("sysforge.pipeline.stages.base_install.subprocess.run") as mock_run:

            mock_run.return_value = MagicMock(returncode=0, stdout="# fstab\n")
            stage.run({}, MagicMock(), options)

        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any(c[0] == "pacstrap" for c in cmds)
        assert any(c[0] == "genfstab" for c in cmds)


# ---------------------------------------------------------------------------
# Partition stage
# ---------------------------------------------------------------------------

class TestPartitionStageDryRun:
    def test_dry_run_no_subprocess(self, tmp_path):
        stage = PartitionStage()
        options = make_options(dry_run=True)
        with patch("sysforge.pipeline.stages.partition.load_bootstrap",
                   return_value=make_cfg()), \
             patch("sysforge.pipeline.stages.partition.subprocess.run") as mock_run:
            stage.run({}, MagicMock(), options)
        mock_run.assert_not_called()


class TestPartitionDevicePaths:
    """Partition name derivation for different device types."""

    @pytest.mark.parametrize("device,expected_esp,expected_root", [
        ("/dev/sda",       "/dev/sda1",        "/dev/sda2"),
        ("/dev/vda",       "/dev/vda1",         "/dev/vda2"),
        ("/dev/nvme0n1",   "/dev/nvme0n1p1",    "/dev/nvme0n1p2"),
        ("/dev/mmcblk0",   "/dev/mmcblk0p1",    "/dev/mmcblk0p2"),
    ])
    def test_partition_names(self, device, expected_esp, expected_root):
        cfg = make_cfg(device=device)
        with patch("sysforge.pipeline.stages.partition.subprocess.run") as mock_run, \
             patch("sysforge.pipeline.stages.partition.Path") as mock_path:
            mock_run.return_value = MagicMock(returncode=0)
            mock_path.return_value.exists.return_value = True
            esp, root = _partition_disk(cfg)
        assert esp  == expected_esp
        assert root == expected_root
