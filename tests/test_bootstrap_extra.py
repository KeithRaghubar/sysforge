"""
test_bootstrap_extra.py — additional unit tests for bootstrap stages.

Covers pre-flight checks, subprocess-calling functions, error paths,
and edge cases not covered by test_stage_bootstrap.py.
"""
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from sysforge.pipeline.stages.base import RunOptions, BootstrapRebootRequired
from sysforge.pipeline.stages._bootstrap import BootstrapConfig
from sysforge.pipeline.stages.partition import (
    _check_tools,
    _check_device,
    _check_not_mounted,
    _is_already_mounted,
    _root_partition,
    _partition_disk,
    _format_partitions,
    _mount_partitions,
)
from sysforge.pipeline.stages.base_install import (
    _verify_target_mounted,
    _run_pacstrap,
    _generate_fstab,
)
from sysforge.pipeline.stages.configure import (
    _chroot,
    _configure_sshd,
    _write_resume_reminder,
    _set_root_password,
    _create_user,
    _find_sysforge_source,
    _create_state_dir,
    _create_sysforge_group,
    _configure_shell,
)
from sysforge.pipeline.stages.hardware import (
    _parse_cpuinfo,
    _parse_gpu_vendors,
    _has_nvme,
    HardwareStage,
)
from sysforge.pipeline.stages.reconfigure import (
    _open_in_editor,
    _parse_step_selection,
    _resolve_editor,
    _run_editor_argv,
    _probe_host,
    _step_editor,
    _STEP_KEYS,
    ReconfigureStage,
)
from sysforge.primitives.config import load_sysforge_toml
from sysforge.pipeline.runner import _validate_stages, run_pipeline
from sysforge.pipeline.state import PipelineState


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


# ===========================================================================
# Partition stage — pre-flight checks
# ===========================================================================

class TestCheckTools:
    def test_all_tools_present(self):
        with patch("sysforge.pipeline.stages.partition.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _check_tools("ext4")  # should not raise

    def test_missing_tool_raises(self):
        def fake_which(cmd, **kwargs):
            tool = cmd[1]
            if tool == "sgdisk":
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        with patch("sysforge.pipeline.stages.partition.subprocess.run", side_effect=fake_which):
            with pytest.raises(RuntimeError, match="sgdisk"):
                _check_tools("ext4")

    def test_missing_mkfs_btrfs(self):
        def fake_which(cmd, **kwargs):
            tool = cmd[1]
            if tool == "mkfs.btrfs":
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        with patch("sysforge.pipeline.stages.partition.subprocess.run", side_effect=fake_which):
            with pytest.raises(RuntimeError, match="mkfs.btrfs"):
                _check_tools("btrfs")


class TestCheckDevice:
    def test_existing_device_ok(self, tmp_path):
        device = tmp_path / "fake_device"
        device.touch()
        _check_device(str(device))  # should not raise

    def test_missing_device_raises(self):
        with pytest.raises(RuntimeError, match="not found"):
            _check_device("/dev/nonexistent_device_xyz")


class TestIsAlreadyMounted:
    def test_mounted_returns_true(self):
        with patch("sysforge.pipeline.stages.partition.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="/dev/vda2 /mnt ext4 rw,relatime 0 0\n"
            )
            assert _is_already_mounted("/dev/vda", "/mnt") is True

    def test_not_mounted_returns_false(self):
        with patch("sysforge.pipeline.stages.partition.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            assert _is_already_mounted("/dev/vda", "/mnt") is False


class TestCheckNotMounted:
    def test_device_mounted_raises(self):
        with patch("sysforge.pipeline.stages.partition.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="/dev/vda /somewhere ext4\n")
            with pytest.raises(RuntimeError, match="already mounted"):
                _check_not_mounted("/dev/vda", "/mnt")

    def test_target_mounted_raises(self):
        def fake_findmnt(cmd, **kwargs):
            if "--source" in cmd:
                return MagicMock(stdout="")
            return MagicMock(stdout="/dev/sda1 /mnt ext4\n")

        with patch("sysforge.pipeline.stages.partition.subprocess.run", side_effect=fake_findmnt):
            with pytest.raises(RuntimeError, match="already has something mounted"):
                _check_not_mounted("/dev/vda", "/mnt")

    def test_nothing_mounted_ok(self):
        with patch("sysforge.pipeline.stages.partition.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            _check_not_mounted("/dev/vda", "/mnt")  # should not raise


class TestRootPartition:
    def test_nvme(self):
        assert _root_partition("/dev/nvme0n1") == "/dev/nvme0n1p2"

    def test_sda(self):
        assert _root_partition("/dev/sda") == "/dev/sda2"

    def test_mmcblk(self):
        assert _root_partition("/dev/mmcblk0") == "/dev/mmcblk0p2"

    def test_loop(self):
        assert _root_partition("/dev/loop0") == "/dev/loop0p2"

    def test_md(self):
        assert _root_partition("/dev/md0") == "/dev/md0p2"


class TestPartitionDiskPartprobe:
    def test_partprobe_failure_warns(self):
        """partprobe failure should warn but not crash (partition paths verified separately)."""
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if cmd[0] == "partprobe":
                return MagicMock(returncode=1, stderr="device busy")
            return MagicMock(returncode=0)

        cfg = make_cfg(device="/dev/sda")
        with patch("sysforge.pipeline.stages.partition.subprocess.run", side_effect=fake_run), \
             patch("sysforge.pipeline.stages.partition.Path") as mock_path:
            # Make partition path check pass
            mock_path.return_value.exists.return_value = True
            esp, root = _partition_disk(cfg)
        assert esp == "/dev/sda1"
        assert root == "/dev/sda2"


class TestPartitionDiskPathVerification:
    def test_missing_partition_path_raises(self):
        def fake_run(cmd, **kwargs):
            return MagicMock(returncode=0, stderr="")

        cfg = make_cfg(device="/dev/sda")
        with patch("sysforge.pipeline.stages.partition.subprocess.run", side_effect=fake_run):
            # Real filesystem — /dev/sda1 won't exist
            with pytest.raises(RuntimeError, match="not found after sgdisk"):
                _partition_disk(cfg)


class TestFormatPartitions:
    def test_ext4_format(self):
        cfg = make_cfg(root_fs="ext4")
        with patch("sysforge.pipeline.stages.partition.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _format_partitions(cfg, "/dev/sda1", "/dev/sda2")
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("mkfs.fat" in c for c in cmds)
        assert any("mkfs.ext4" in c for c in cmds)

    def test_btrfs_format(self):
        cfg = make_cfg(root_fs="btrfs")
        with patch("sysforge.pipeline.stages.partition.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _format_partitions(cfg, "/dev/sda1", "/dev/sda2")
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("mkfs.btrfs" in c for c in cmds)

    def test_mkfs_failure_raises(self):
        cfg = make_cfg()
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(returncode=0)  # mkfs.fat ok
            return MagicMock(returncode=1)  # mkfs.ext4 fails

        with patch("sysforge.pipeline.stages.partition.subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="mkfs.ext4 failed"):
                _format_partitions(cfg, "/dev/sda1", "/dev/sda2")


class TestMountPartitions:
    def test_mount_calls(self, tmp_path):
        cfg = make_cfg(target=str(tmp_path))
        with patch("sysforge.pipeline.stages.partition.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _mount_partitions(cfg, "/dev/sda1", "/dev/sda2")
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert len(cmds) == 2
        assert cmds[0][0] == "mount"
        assert cmds[1][0] == "mount"

    def test_mount_failure_raises(self, tmp_path):
        cfg = make_cfg(target=str(tmp_path))
        with patch("sysforge.pipeline.stages.partition.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            with pytest.raises(RuntimeError, match="mount failed"):
                _mount_partitions(cfg, "/dev/sda1", "/dev/sda2")


# ===========================================================================
# BaseInstall stage
# ===========================================================================

class TestVerifyTargetMounted:
    def test_not_a_directory_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="does not exist"):
            _verify_target_mounted(str(tmp_path / "nonexistent"))

    def test_not_mounted_raises(self, tmp_path):
        with patch("sysforge.pipeline.stages.base_install.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            with pytest.raises(RuntimeError, match="Nothing appears to be mounted"):
                _verify_target_mounted(str(tmp_path))

    def test_mounted_ok(self, tmp_path):
        with patch("sysforge.pipeline.stages.base_install.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="/dev/sda2 /mnt ext4\n")
            _verify_target_mounted(str(tmp_path))  # should not raise


class TestRunPacstrap:
    def test_dry_run_no_subprocess(self):
        with patch("sysforge.pipeline.stages.base_install.subprocess.run") as mock_run:
            _run_pacstrap("/mnt", ["base"], dry_run=True)
        mock_run.assert_not_called()

    def test_pacstrap_failure_raises(self):
        with patch("sysforge.pipeline.stages.base_install.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            with pytest.raises(RuntimeError, match="pacstrap failed"):
                _run_pacstrap("/mnt", ["base"], dry_run=False)

    def test_pacstrap_success(self):
        with patch("sysforge.pipeline.stages.base_install.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _run_pacstrap("/mnt", ["base", "linux"], dry_run=False)
        cmd = mock_run.call_args.args[0]
        assert cmd[0] == "pacstrap"
        assert "base" in cmd
        assert "linux" in cmd


class TestGenerateFstab:
    def test_dry_run_no_write(self, tmp_path):
        with patch("sysforge.pipeline.stages.base_install.subprocess.run") as mock_run:
            _generate_fstab(str(tmp_path), dry_run=True)
        mock_run.assert_not_called()

    def test_genfstab_failure_raises(self, tmp_path):
        with patch("sysforge.pipeline.stages.base_install.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error")
            with pytest.raises(RuntimeError, match="genfstab failed"):
                _generate_fstab(str(tmp_path), dry_run=False)

    def test_writes_fstab(self, tmp_path):
        (tmp_path / "etc").mkdir()
        with patch("sysforge.pipeline.stages.base_install.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="# /dev/sda2\nUUID=abc / ext4 defaults 0 1\n"
            )
            _generate_fstab(str(tmp_path), dry_run=False)
        fstab = (tmp_path / "etc/fstab").read_text()
        assert "UUID=abc" in fstab


# ===========================================================================
# Configure stage — subprocess functions
# ===========================================================================

class TestChroot:
    def test_calls_arch_chroot(self):
        with patch("sysforge.pipeline.stages.configure.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _chroot("/mnt", ["locale-gen"])
        cmd = mock_run.call_args.args[0]
        assert cmd == ["arch-chroot", "/mnt", "locale-gen"]

    def test_check_raises_on_failure(self):
        with patch("sysforge.pipeline.stages.configure.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "arch-chroot")
            with pytest.raises(subprocess.CalledProcessError):
                _chroot("/mnt", ["false"])


class TestConfigureSshd:
    def test_uncomments_permit_root_login(self, tmp_path):
        etc = tmp_path / "etc/ssh"
        etc.mkdir(parents=True)
        sshd_config = etc / "sshd_config"
        sshd_config.write_text(
            "# Authentication:\n#PermitRootLogin prohibit-password\nUsePAM yes\n"
        )
        cfg = make_cfg(target=str(tmp_path))
        _configure_sshd(cfg)
        text = sshd_config.read_text()
        assert "PermitRootLogin yes" in text
        assert "#PermitRootLogin" not in text

    def test_appends_if_not_present(self, tmp_path):
        etc = tmp_path / "etc/ssh"
        etc.mkdir(parents=True)
        sshd_config = etc / "sshd_config"
        sshd_config.write_text("UsePAM yes\n")
        cfg = make_cfg(target=str(tmp_path))
        _configure_sshd(cfg)
        text = sshd_config.read_text()
        assert "PermitRootLogin yes" in text

    def test_missing_sshd_config_skips(self, tmp_path):
        cfg = make_cfg(target=str(tmp_path))
        _configure_sshd(cfg)  # should not raise


class TestWriteResumeReminder:
    def test_writes_file(self, tmp_path):
        cfg = make_cfg(target=str(tmp_path))
        (tmp_path / "etc/profile.d").mkdir(parents=True)
        _write_resume_reminder(cfg)
        reminder = tmp_path / "etc/profile.d/sysforge-resume.sh"
        assert reminder.exists()
        assert "sysforge" in reminder.read_text()
        assert reminder.stat().st_mode & 0o777 == 0o644


class TestSetRootPassword:
    def test_sets_password(self):
        cfg = make_cfg(root_password="secret")
        with patch("sysforge.pipeline.stages.configure.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _set_root_password(cfg)
        cmd = mock_run.call_args.args[0]
        assert "chpasswd" in cmd
        assert mock_run.call_args.kwargs["input"] == "root:secret\n"

    def test_no_password_warns(self):
        cfg = make_cfg(root_password=None)
        with patch("sysforge.pipeline.stages.configure._log") as mock_log:
            _set_root_password(cfg)
        mock_log.warn.assert_called_once()

    def test_chpasswd_failure_raises(self):
        cfg = make_cfg(root_password="secret")
        with patch("sysforge.pipeline.stages.configure.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            with pytest.raises(RuntimeError, match="chpasswd failed"):
                _set_root_password(cfg)


class TestCreateUser:
    def test_creates_user_and_sudoers(self, tmp_path):
        cfg = make_cfg(target=str(tmp_path), username="builder")
        with patch("sysforge.pipeline.stages.configure._chroot") as mock_chroot, \
             patch("sysforge.pipeline.stages.configure.subprocess.run") as mock_run, \
             patch("sysforge.pipeline.stages.configure._log"):
            mock_chroot.return_value = MagicMock(returncode=0)
            _create_user(cfg)
        sudoers = tmp_path / "etc/sudoers.d/wheel"
        assert sudoers.exists()
        assert "%wheel" in sudoers.read_text()

    def test_user_already_exists_ok(self, tmp_path):
        """useradd exit code 9 = user exists — should not raise."""
        cfg = make_cfg(target=str(tmp_path), username="builder")
        with patch("sysforge.pipeline.stages.configure._chroot") as mock_chroot, \
             patch("sysforge.pipeline.stages.configure._log"):
            mock_chroot.return_value = MagicMock(returncode=9)
            _create_user(cfg)

    def test_useradd_other_failure_raises(self, tmp_path):
        cfg = make_cfg(target=str(tmp_path), username="builder")
        with patch("sysforge.pipeline.stages.configure._chroot") as mock_chroot:
            mock_chroot.return_value = MagicMock(returncode=2)
            with pytest.raises(RuntimeError, match="useradd failed"):
                _create_user(cfg)


class TestCreateSysforgeGroup:
    def test_creates_group_and_adds_user(self):
        cfg = make_cfg(username="builder")
        with patch("sysforge.pipeline.stages.configure._chroot") as mock_chroot:
            _create_sysforge_group(cfg)
        assert mock_chroot.call_count == 2
        mock_chroot.assert_any_call(cfg.target, ["groupadd", "-f", "sysforge"])
        mock_chroot.assert_any_call(cfg.target, ["usermod", "-aG", "sysforge", "builder"])


class TestCreateStateDir:
    def test_creates_dir_with_permissions(self, tmp_path):
        cfg = make_cfg(target=str(tmp_path))
        with patch("sysforge.pipeline.stages.configure._chroot") as mock_chroot:
            _create_state_dir(cfg)
        state_dir = tmp_path / "var/lib/sysforge"
        assert state_dir.is_dir()
        assert mock_chroot.call_count == 3
        mock_chroot.assert_any_call(
            str(tmp_path), ["chown", "-R", "root:sysforge", "/var/lib/sysforge"]
        )
        mock_chroot.assert_any_call(
            str(tmp_path), ["chmod", "02775", "/var/lib/sysforge"]
        )
        # Recursive contents normalization: any existing files written
        # earlier in the pipeline get setgid dirs + g+w files.
        find_calls = [
            c for c in mock_chroot.call_args_list
            if len(c.args) >= 2 and c.args[1][:2] == ["sh", "-c"]
        ]
        assert len(find_calls) == 1
        sh_script = find_calls[0].args[1][2]
        assert "find /var/lib/sysforge -mindepth 1 -type d -exec chmod 02775" in sh_script
        assert "find /var/lib/sysforge -mindepth 1 -type f -exec chmod g+w" in sh_script


class TestConfigureShell:
    def test_writes_root_dotfiles(self, tmp_path):
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        cfg = make_cfg(target=str(tmp_path), username="builder")
        _configure_shell(cfg)
        assert (root_dir / ".bashrc").exists()
        assert (root_dir / ".zshrc").exists()

    def test_writes_user_dotfiles(self, tmp_path):
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        user_dir = tmp_path / "home/builder"
        user_dir.mkdir(parents=True)
        cfg = make_cfg(target=str(tmp_path), username="builder")
        _configure_shell(cfg)
        assert (user_dir / ".bashrc").exists()
        assert (user_dir / ".zshrc").exists()
        # User gets green prompt, root gets red
        assert "green" in (user_dir / ".zshrc").read_text()
        assert "red" in (root_dir / ".zshrc").read_text()


class TestFindSysforgeSource:
    def test_finds_via_iso_install_cache(self, tmp_path):
        """Strategy 0: a populated /var/cache/sysforge/source wins over pip metadata."""
        cache = tmp_path / "source"
        cache.mkdir()
        (cache / "pyproject.toml").write_text("[project]\nname='sysforge'\n")
        with patch("sysforge.pipeline.stages.configure._ISO_INSTALL_SOURCE_CACHE", cache):
            result = _find_sysforge_source()
        assert result == cache

    def test_finds_via_pip_metadata(self, tmp_path):
        """When pip metadata has a file:// URL, return that path."""
        src_dir = tmp_path / "sysforge-src"
        src_dir.mkdir()
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = f'{{"url": "file://{src_dir}"}}'
        empty_cache = tmp_path / "no-cache"
        with patch("sysforge.pipeline.stages.configure._ISO_INSTALL_SOURCE_CACHE", empty_cache), \
             patch("sysforge.pipeline.stages.configure.distribution",
                   return_value=mock_dist):
            result = _find_sysforge_source()
        assert result == src_dir

    def test_falls_back_to_file(self, tmp_path):
        """When pip metadata is absent, __file__ fallback runs without error."""
        from importlib.metadata import PackageNotFoundError
        empty_cache = tmp_path / "no-cache"
        with patch("sysforge.pipeline.stages.configure._ISO_INSTALL_SOURCE_CACHE", empty_cache), \
             patch("sysforge.pipeline.stages.configure.distribution",
                   side_effect=PackageNotFoundError("no metadata")):
            result = _find_sysforge_source()
        # Result depends on whether pyproject.toml is above the installed package
        assert result is None or isinstance(result, Path)

    def test_returns_none_when_nothing_found(self, tmp_path):
        """Returns None when no cache, no pip metadata, and no pyproject.toml above __file__."""
        from importlib.metadata import PackageNotFoundError
        empty_cache = tmp_path / "no-cache"
        # __file__ points to a dir with no pyproject.toml above it
        fake_init = tmp_path / "a/b/sysforge/__init__.py"
        fake_init.parent.mkdir(parents=True)
        fake_init.touch()
        import sysforge as _pkg
        with patch("sysforge.pipeline.stages.configure._ISO_INSTALL_SOURCE_CACHE", empty_cache), \
             patch("sysforge.pipeline.stages.configure.distribution",
                   side_effect=PackageNotFoundError("no metadata")), \
             patch.object(_pkg, "__file__", str(fake_init)):
            result = _find_sysforge_source()
        assert result is None


# ===========================================================================
# Hardware stage — edge cases
# ===========================================================================

class TestParseCpuinfoEdgeCases:
    def test_multi_socket(self):
        """Only first processor block fields should be used."""
        cpuinfo = textwrap.dedent("""\
            processor\t: 0
            vendor_id\t: AuthenticAMD
            cpu family\t: 25
            model\t\t: 33

            processor\t: 1
            vendor_id\t: AuthenticAMD
            cpu family\t: 25
            model\t\t: 33
        """)
        info = _parse_cpuinfo(cpuinfo)
        assert info["cpu_vendor"] == "AuthenticAMD"
        assert info["cpu_family"] == 25
        assert info["cpu_model"] == 33

    def test_whitespace_variations(self):
        """Handle extra whitespace around colons."""
        cpuinfo = (
            "vendor_id :  AuthenticAMD\n"
            "cpu family :  25\n"
            "model :  33\n"
        )
        info = _parse_cpuinfo(cpuinfo)
        assert info["cpu_vendor"] == "AuthenticAMD"
        assert info["cpu_family"] == 25
        assert info["cpu_model"] == 33

    def test_missing_model_field(self):
        cpuinfo = "vendor_id\t: GenuineIntel\ncpu family\t: 6\n"
        info = _parse_cpuinfo(cpuinfo)
        assert info["cpu_vendor"] == "GenuineIntel"
        assert info["cpu_family"] == 6
        assert "cpu_model" not in info

    def test_non_numeric_family(self):
        cpuinfo = "vendor_id\t: AuthenticAMD\ncpu family\t: unknown\nmodel\t\t: 33\n"
        info = _parse_cpuinfo(cpuinfo)
        assert "cpu_family" not in info
        assert info["cpu_model"] == 33


class TestHardwareLspciFailure:
    def test_lspci_failure_continues(self, tmp_path):
        """Stage should complete even if lspci fails."""
        stage = HardwareStage()
        options = make_options(state_dir=tmp_path)

        cpuinfo = textwrap.dedent("""\
            processor\t: 0
            vendor_id\t: GenuineIntel
            cpu family\t: 6
            model\t\t: 154
        """)

        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if cmd[0] == "lspci":
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0)

        with patch("sysforge.pipeline.stages.hardware.subprocess.run", side_effect=fake_run), \
             patch("sysforge.pipeline.stages.hardware.resolve_state_dir",
                   return_value=(tmp_path, "test")), \
             patch("pathlib.Path.read_text", return_value=cpuinfo):
            stage.run({}, MagicMock(), options)

        out = tmp_path / "hardware_profile.toml"
        assert out.exists()
        import tomllib
        with open(out, "rb") as f:
            data = tomllib.load(f)
        assert data["hardware"]["gpu_vendors"] == []
        assert data["hardware"]["nvme"] is False


class TestParseGpuVendorsEdgeCases:
    def test_display_controller(self):
        lspci = "00:02.0 Display controller: Intel Corporation UHD Graphics\n"
        assert _parse_gpu_vendors(lspci) == ["intel"]

    def test_radeon_detected_as_amd(self):
        lspci = "01:00.0 VGA compatible controller: Radeon RX 580\n"
        assert _parse_gpu_vendors(lspci) == ["amd"]

    def test_unknown_vendor(self):
        lspci = "01:00.0 VGA compatible controller: Matrox Electronics G200eW\n"
        assert _parse_gpu_vendors(lspci) == ["other"]

    def test_dedup_multiple_same_vendor(self):
        lspci = (
            "01:00.0 VGA compatible controller: NVIDIA Corporation GA102\n"
            "02:00.0 VGA compatible controller: NVIDIA Corporation GA104\n"
        )
        assert _parse_gpu_vendors(lspci) == ["nvidia"]


# ===========================================================================
# Reconfigure stage — step selection edge cases
# ===========================================================================

class TestParseStepSelectionExtra:
    """
    _parse_step_selection now returns (selected, invalid). Empty input or
    'all' selects everything; '0' / 'cancel' returns ([], []); unrecognized
    tokens are surfaced in `invalid` so the caller can warn the user
    instead of silently falling back to "run all".
    """

    def test_empty_returns_all(self):
        selected, invalid = _parse_step_selection("")
        assert selected == list(_STEP_KEYS)
        assert invalid == []

    def test_all_keyword(self):
        selected, invalid = _parse_step_selection("all")
        assert selected == list(_STEP_KEYS)
        assert invalid == []

    def test_ALL_case_insensitive(self):
        selected, invalid = _parse_step_selection("ALL")
        assert selected == list(_STEP_KEYS)
        assert invalid == []

    def test_cancel_returns_empty(self):
        selected, invalid = _parse_step_selection("cancel")
        assert selected == []
        assert invalid == []

    def test_zero_returns_empty(self):
        selected, invalid = _parse_step_selection("0")
        assert selected == []
        assert invalid == []

    def test_zero_in_middle_cancels(self):
        selected, invalid = _parse_step_selection("1 0 3")
        assert selected == []
        assert invalid == []

    def test_range(self):
        selected, invalid = _parse_step_selection("1-3")
        assert selected == _STEP_KEYS[:3]
        assert invalid == []

    def test_mixed_numbers_and_names(self):
        selected, invalid = _parse_step_selection("1 network")
        assert selected == ["editor", "network"]
        assert invalid == []

    def test_dedup(self):
        selected, invalid = _parse_step_selection("1 1 editor")
        assert selected == ["editor"]
        assert invalid == []

    def test_out_of_range_number_reported_as_invalid(self):
        selected, invalid = _parse_step_selection("99")
        assert selected == []
        assert invalid == ["99"]

    def test_invalid_name_reported_as_invalid(self):
        selected, invalid = _parse_step_selection("nonexistent")
        assert selected == []
        assert invalid == ["nonexistent"]

    def test_invalid_range_reported_as_invalid(self):
        selected, invalid = _parse_step_selection("9-1")
        assert selected == []
        assert invalid == ["9-1"]


class TestResolveEditor:
    def test_sysforge_editor_env_wins(self):
        with patch.dict("os.environ", {"SYSFORGE_EDITOR": "emacs"}, clear=False), \
             patch("sysforge.pipeline.stages.reconfigure.load_sysforge_toml",
                   return_value={}):
            editor, source = _resolve_editor()
        assert editor == "emacs"
        assert source == "SYSFORGE_EDITOR"

    def test_sysforge_toml_second(self):
        with patch.dict("os.environ", {}, clear=False), \
             patch("sysforge.pipeline.stages.reconfigure.load_sysforge_toml",
                   return_value={"ui": {"editor": "helix"}}):
            # Clear env vars that might interfere
            env = dict(SYSFORGE_EDITOR="", EDITOR="", VISUAL="")
            with patch.dict("os.environ", env):
                editor, source = _resolve_editor()
        assert editor == "helix"
        assert source == "sysforge.toml"

    def test_editor_env_fallback(self):
        with patch.dict("os.environ",
                        {"EDITOR": "nano", "SYSFORGE_EDITOR": ""},
                        clear=False), \
             patch("sysforge.pipeline.stages.reconfigure.load_sysforge_toml",
                   return_value={}):
            editor, source = _resolve_editor()
        assert editor == "nano"
        assert source == "$EDITOR"

    def test_fallback_to_detected(self):
        with patch.dict("os.environ",
                        {"SYSFORGE_EDITOR": "", "EDITOR": "", "VISUAL": ""},
                        clear=False), \
             patch("sysforge.pipeline.stages.reconfigure.load_sysforge_toml",
                   return_value={}), \
             patch("sysforge.pipeline.stages.reconfigure.shutil.which") as mock_which:
            mock_which.side_effect = lambda x: "/usr/bin/vim" if x == "vim" else None
            editor, source = _resolve_editor()
        assert editor == "vim"
        assert source == "detected"


class TestOpenInEditor:
    def test_missing_editor_warns_and_returns(self, tmp_path):
        path = tmp_path / "f.toml"
        path.write_text("")
        with patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value=None), \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run") as run, \
             patch("sysforge.pipeline.stages.reconfigure._log") as log:
            _open_in_editor(path, "ghosted-editor")
        run.assert_not_called()
        # Warns clearly so the user isn't left wondering why nothing opened.
        assert log.warn.called

    def test_passes_tty_fd_to_subprocess(self, tmp_path):
        path = tmp_path / "f.toml"
        path.write_text("")
        fake_fd = 9999
        with patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value="/usr/bin/nvim"), \
             patch("sysforge.pipeline.stages.reconfigure.os.open",
                   return_value=fake_fd) as os_open, \
             patch("sysforge.pipeline.stages.reconfigure.os.close") as os_close, \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run",
                   return_value=MagicMock(returncode=0)) as run:
            _open_in_editor(path, "nvim")
        # /dev/tty must be opened RDWR and bound to all three streams so
        # editors keep working under `sysforge ... | tee log`.
        os_open.assert_called_once()
        opened_path, flags = os_open.call_args.args
        assert opened_path == "/dev/tty"
        # Flag value matches os.O_RDWR (don't import os here just for the const).
        import os as _os
        assert flags == _os.O_RDWR
        kwargs = run.call_args.kwargs
        assert kwargs.get("stdin") == fake_fd
        assert kwargs.get("stdout") == fake_fd
        assert kwargs.get("stderr") == fake_fd
        os_close.assert_called_once_with(fake_fd)

    def test_falls_back_when_no_tty(self, tmp_path):
        path = tmp_path / "f.toml"
        path.write_text("")
        with patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value="/usr/bin/nvim"), \
             patch("sysforge.pipeline.stages.reconfigure.os.open",
                   side_effect=OSError("no tty")), \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run",
                   return_value=MagicMock(returncode=0)) as run:
            _open_in_editor(path, "nvim")
        # Still calls subprocess.run, just without the tty fd kwargs.
        kwargs = run.call_args.kwargs
        assert "stdin" not in kwargs

    def test_warns_on_nonzero_exit(self, tmp_path):
        path = tmp_path / "f.toml"
        path.write_text("")
        with patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value="/usr/bin/nvim"), \
             patch("sysforge.pipeline.stages.reconfigure.os.open",
                   side_effect=OSError("no tty")), \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run",
                   return_value=MagicMock(returncode=2)), \
             patch("sysforge.pipeline.stages.reconfigure._log") as log:
            _open_in_editor(path, "nvim")
        assert log.warn.called

    def test_run_editor_argv_returns_minus_one_on_filenotfound(self):
        with patch("sysforge.pipeline.stages.reconfigure.os.open",
                   side_effect=OSError("no tty")), \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run",
                   side_effect=FileNotFoundError):
            assert _run_editor_argv(["nope"]) == -1


class TestStepEditorRejectsUnresolvable:
    def _opts(self, dry_run=False):
        opts = MagicMock()
        opts.dry_run = dry_run
        return opts

    def test_rejects_when_install_doesnt_provide_binary(self):
        # User types nvim, install attempt runs but binary is still missing
        # (e.g. typed package name doesn't actually provide /usr/bin/nvim).
        # Function must return the original editor, not the unusable one.
        # Single-choice prompts ('change?', 'save?') go through
        # _prompt_choice; free-text prompts (editor name, pkg name) go
        # through _prompt.
        choices = iter([
            "e",        # change?
            "y",        # save?
        ])
        prompts = iter([
            "nvim",     # new editor
            "neovim",   # package to install
        ])
        with patch("sysforge.pipeline.stages.reconfigure._interactive",
                   return_value=True), \
             patch("sysforge.pipeline.stages.reconfigure._resolve_editor",
                   return_value=("vi", "default")), \
             patch("sysforge.pipeline.stages.reconfigure._prompt_choice",
                   side_effect=lambda *a, **k: next(choices)), \
             patch("sysforge.pipeline.stages.reconfigure._prompt",
                   side_effect=lambda *a, **k: next(prompts)), \
             patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value=None), \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run",
                   return_value=MagicMock(returncode=1)), \
             patch("sysforge.pipeline.stages.reconfigure._save_sysforge_toml_ui") as save:
            result = _step_editor(None, None, self._opts(), "vi")
        assert result == "vi"
        save.assert_not_called()

    def test_does_not_save_unresolvable_editor_after_skipped_install(self):
        # User types nvim, declines install (empty package name), function
        # must keep the previous editor and not save anything.
        choices = iter([
            "e",        # change?
        ])
        prompts = iter([
            "nvim",     # new editor
            "",         # package to install (skip)
        ])
        with patch("sysforge.pipeline.stages.reconfigure._interactive",
                   return_value=True), \
             patch("sysforge.pipeline.stages.reconfigure._resolve_editor",
                   return_value=("vi", "default")), \
             patch("sysforge.pipeline.stages.reconfigure._prompt_choice",
                   side_effect=lambda *a, **k: next(choices)), \
             patch("sysforge.pipeline.stages.reconfigure._prompt",
                   side_effect=lambda *a, **k: next(prompts)), \
             patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value=None), \
             patch("sysforge.pipeline.stages.reconfigure._save_sysforge_toml_ui") as save:
            result = _step_editor(None, None, self._opts(), "vi")
        assert result == "vi"
        save.assert_not_called()


class TestProbeHost:
    def test_unreachable_returns_false(self):
        # Use a non-routable address to ensure timeout
        assert _probe_host("192.0.2.1", 1, timeout=1) is False


class TestLoadSysforgeToml:
    def test_missing_file_returns_empty(self, tmp_path):
        with patch("sysforge.primitives.config.SYSFORGE_TOML_PATH",
                    tmp_path / "nonexistent.toml"):
            assert load_sysforge_toml() == {}

    def test_valid_file(self, tmp_path):
        toml_path = tmp_path / "sysforge.toml"
        toml_path.write_text('[ui]\neditor = "vim"\n')
        with patch("sysforge.primitives.config.SYSFORGE_TOML_PATH", toml_path):
            data = load_sysforge_toml()
        assert data["ui"]["editor"] == "vim"

    def test_invalid_toml_warns_returns_empty(self, tmp_path):
        toml_path = tmp_path / "sysforge.toml"
        toml_path.write_text("invalid = [broken\n")
        with patch("sysforge.primitives.config.SYSFORGE_TOML_PATH", toml_path):
            data = load_sysforge_toml()
        assert data == {}

    def test_git_section_loaded(self, tmp_path):
        toml_path = tmp_path / "sysforge.toml"
        toml_path.write_text('[git]\npull_timeout = 15\nclone_timeout = 45\n')
        with patch("sysforge.primitives.config.SYSFORGE_TOML_PATH", toml_path):
            data = load_sysforge_toml()
        assert data["git"]["pull_timeout"] == 15
        assert data["git"]["clone_timeout"] == 45


class TestReconfigureRebootDetection:
    def test_archiso_raises_bootstrap_reboot(self, tmp_path):
        stage = ReconfigureStage()
        options = make_options(state_dir=tmp_path)

        mock_state = MagicMock()
        mock_state.path = tmp_path / "pipeline.json"
        mock_state.path.touch()

        cfg = make_cfg(target=str(tmp_path))
        (tmp_path / "var/lib/sysforge").mkdir(parents=True)

        with patch("sysforge.pipeline.stages.reconfigure.Path") as mock_path_cls, \
             patch("sysforge.pipeline.stages.reconfigure.shutil.copy2"), \
             patch("sysforge.pipeline.stages._bootstrap.load_bootstrap", return_value=cfg):

            # Make Path("/run/archiso").exists() return True
            real_path = Path

            def path_side_effect(arg):
                if str(arg) == "/run/archiso":
                    m = MagicMock()
                    m.exists.return_value = True
                    return m
                return real_path(arg)

            mock_path_cls.side_effect = path_side_effect

            with pytest.raises(BootstrapRebootRequired, match="Reboot"):
                stage.run({}, mock_state, options)


class TestReconfigureReminderRemoval:
    def test_permission_error_warns(self, tmp_path):
        """PermissionError on reminder unlink should warn, not crash."""
        stage = ReconfigureStage()
        options = make_options(state_dir=tmp_path, dry_run=True)

        mock_state = MagicMock()
        mock_state.stage_status.return_value = None

        with patch("sysforge.pipeline.stages.reconfigure.Path") as mock_path_cls, \
             patch("sysforge.pipeline.stages.reconfigure._interactive", return_value=False), \
             patch("sysforge.pipeline.stages.reconfigure._show_stage_summary"), \
             patch("sysforge.pipeline.stages.reconfigure._run_selected_steps"):

            real_path = Path

            def path_side_effect(arg):
                if str(arg) == "/run/archiso":
                    m = MagicMock()
                    m.exists.return_value = False
                    return m
                if str(arg) == "/etc/profile.d/sysforge-resume.sh":
                    m = MagicMock()
                    m.exists.return_value = True
                    m.unlink.side_effect = PermissionError("denied")
                    m.__str__ = lambda self: "/etc/profile.d/sysforge-resume.sh"
                    return m
                return real_path(arg)

            mock_path_cls.side_effect = path_side_effect
            # Should not raise
            stage.run({}, mock_state, options)


# ===========================================================================
# Pipeline runner — dependency validation
# ===========================================================================

class TestValidateStagesCycleDetection:
    """_validate_stages checks that depends_on references exist."""

    def test_valid_dependencies(self):
        from sysforge.pipeline.stages.base import Stage

        class A(Stage):
            name = "a"
            depends_on = []

        class B(Stage):
            name = "b"
            depends_on = ["a"]

        _validate_stages([A(), B()])  # should not raise

    def test_invalid_dependency_raises(self):
        from sysforge.pipeline.stages.base import Stage

        class A(Stage):
            name = "a"
            depends_on = ["nonexistent"]

        with pytest.raises(ValueError, match="nonexistent"):
            _validate_stages([A()])


class TestBootstrapRebootInRunner:
    def test_bootstrap_reboot_saves_state_and_exits_zero(self, tmp_path):
        from sysforge.pipeline.stages.base import Stage

        class RebootStage(Stage):
            name = "reboot_test"
            description = "test"
            depends_on = []

            def run(self, config, state, options):
                raise BootstrapRebootRequired("reboot now")

        with pytest.raises(SystemExit) as exc_info:
            run_pipeline(
                {}, make_options(state_dir=tmp_path),
                stages=[RebootStage()],
            )
        assert exc_info.value.code == 0
