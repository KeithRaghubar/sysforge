"""
test_stage_bootstrap.py — unit tests for bootstrap stages 1-4.

Covers pure-logic functions. Subprocess calls (sgdisk, mkfs, pacstrap,
arch-chroot, etc.) are mocked — nothing real runs.
"""
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from sysforge.pipeline.stages.base import RunOptions
from sysforge.pipeline.stages._bootstrap import BootstrapConfig, load_bootstrap
from sysforge.pipeline.stages.hardware import (
    _cpu_kconfig,
    _gpu_kconfig,
    _has_nvme,
    _parse_cpuinfo,
    _parse_gpu_vendors,
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
        assert _parse_gpu_vendors(_LSPCI_NVIDIA) == ["nvidia"]

    def test_amd(self):
        assert _parse_gpu_vendors(_LSPCI_AMD) == ["amd"]

    def test_intel(self):
        assert _parse_gpu_vendors(_LSPCI_INTEL) == ["intel"]

    def test_multi(self):
        vendors = _parse_gpu_vendors(_LSPCI_MULTI)
        assert "intel" in vendors
        assert "nvidia" in vendors

    def test_empty(self):
        assert _parse_gpu_vendors("") == []


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
        with patch("sysforge.pipeline.stages.hardware.subprocess.run") as mock_run, \
             patch("sysforge.pipeline.stages.hardware.resolve_state_dir",
                   return_value=(tmp_path, "test")), \
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
        assert data["kconfig"]["CONFIG_MZEN3"] == "y"
        assert data["kconfig"]["CONFIG_DRM_NOUVEAU"] == "n"
        assert data["kconfig"]["CONFIG_BLK_DEV_NVME"] == "y"

    def test_dry_run_no_write(self, tmp_path):
        stage = HardwareStage()
        options = make_options(state_dir=tmp_path, dry_run=True)

        with patch("sysforge.pipeline.stages.hardware.subprocess.run") as mock_run, \
             patch("sysforge.pipeline.stages.hardware.resolve_state_dir",
                   return_value=(tmp_path, "test")), \
             patch("pathlib.Path.read_text", return_value=_AMD_ZEN3_CPUINFO):

            mock_run.return_value = MagicMock(returncode=0, stdout=_LSPCI_NVIDIA)
            stage.run({}, MagicMock(), options)

        assert not (tmp_path / "hardware_profile.toml").exists()


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
