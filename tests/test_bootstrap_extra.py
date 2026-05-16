"""
test_bootstrap_extra.py — additional unit tests for bootstrap stages.

Covers pre-flight checks, subprocess-calling functions, error paths,
and edge cases not covered by test_stage_bootstrap.py.
"""
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    _install_sysforge,
    _read_pkgver,
)
from sysforge.pipeline.stages.hardware import (
    _parse_cpuinfo,
    parse_gpu_vendors,
    HardwareStage,
)
from sysforge.pipeline.stages.reconfigure import (
    _open_in_editor,
    _parse_step_selection,
    _resolve_editor,
    _review_config_file,
    _run_editor_argv,
    _probe_host,
    _step_editor,
    _STEP_KEYS,
    ReconfigureStage,
)
from sysforge.primitives.config import load_sysforge_toml
from sysforge.pipeline.runner import _validate_stages, run_pipeline


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
             patch("sysforge.pipeline.stages.configure.subprocess.run"), \
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


class TestReadPkgver:
    def test_simple(self, tmp_path):
        pkgbuild = tmp_path / "PKGBUILD"
        pkgbuild.write_text("pkgname=sysforge\npkgver=1.2.3\npkgrel=1\n")
        assert _read_pkgver(pkgbuild) == "1.2.3"

    def test_quoted(self, tmp_path):
        pkgbuild = tmp_path / "PKGBUILD"
        pkgbuild.write_text('pkgname=sysforge\npkgver="0.4.0"\npkgrel=1\n')
        assert _read_pkgver(pkgbuild) == "0.4.0"

    def test_missing_raises(self, tmp_path):
        pkgbuild = tmp_path / "PKGBUILD"
        pkgbuild.write_text("pkgname=sysforge\n# no pkgver\n")
        with pytest.raises(RuntimeError, match="could not parse pkgver"):
            _read_pkgver(pkgbuild)


class TestInstallSysforge:
    """The install step must end up pacman-tracked, which means makepkg + pacman -U
    rather than `uv pip install`. These tests exercise the orchestration without
    actually invoking makepkg/tar/arch-chroot."""

    def _stage_source(self, tmp_path):
        """Lay out a fake source tree with a PKGBUILD that the install step
        will copy into the target build dir."""
        src = tmp_path / "src/sysforge"
        src.mkdir(parents=True)
        (src / "pyproject.toml").write_text("[project]\nname='sysforge'\n")
        (src / "PKGBUILD").write_text("pkgname=sysforge\npkgver=1.0.0\npkgrel=1\n")
        return src

    def _make_target(self, tmp_path):
        target = tmp_path / "target"
        # Pre-create the dirs the function writes into
        (target / "root").mkdir(parents=True)
        (target / "home/builder").mkdir(parents=True)
        (target / "etc/sudoers.d").mkdir(parents=True)
        return target

    def test_writes_temp_sudoers_and_runs_makepkg_via_user(self, tmp_path):
        src = self._stage_source(tmp_path)
        target = self._make_target(tmp_path)
        cfg = make_cfg(target=str(target), username="builder")

        # The build step is mocked, so makepkg never actually produces a
        # .pkg.tar.zst. Stage one ourselves so the host-side glob finds it
        # when the install step runs.
        build_dir = target / "home/builder/sysforge-pkg"

        def chroot_side_effect(target_arg, cmd, check=True):
            if cmd[:3] == ["sudo", "-u", "builder"]:
                # Simulate makepkg producing a built package.
                build_dir.mkdir(parents=True, exist_ok=True)
                (build_dir / "sysforge-1.0.0-1-any.pkg.tar.zst").touch()
            return MagicMock(returncode=0)

        # _chroot is mocked but subprocess.run is real — the tarball staging
        # uses real `tar` (universally available) so we verify the actual file.
        with patch("sysforge.pipeline.stages.configure._find_sysforge_source",
                   return_value=src), \
             patch("sysforge.pipeline.stages.configure._chroot",
                   side_effect=chroot_side_effect) as mock_chroot:
            _install_sysforge(cfg)

        # Source copied into target
        assert (target / "root/sysforge/PKGBUILD").exists()
        # Tarball staged for makepkg
        assert (target / "home/builder/sysforge-pkg/sysforge-1.0.0.tar.gz").exists()
        assert (target / "home/builder/sysforge-pkg/PKGBUILD").exists()
        chroot_cmds = [c.args[1] for c in mock_chroot.call_args_list]
        # Build step: makepkg -s (no -i) under sudo -u builder
        makepkg_call = next(
            (c for c in chroot_cmds if c[:3] == ["sudo", "-u", "builder"]),
            None,
        )
        assert makepkg_call is not None, f"no sudo -u builder call found: {chroot_cmds}"
        assert "makepkg -s --skipchecksums --skipinteg --noconfirm --needed" \
            in makepkg_call[-1]
        assert " -si " not in makepkg_call[-1] and not makepkg_call[-1].endswith(" -si"), \
            f"makepkg should not use -i (split build/install): {makepkg_call[-1]}"
        # Install step: pacman -U with --overwrite for /etc/sysforge/*
        pacman_call = next(
            (c for c in chroot_cmds if c[:2] == ["pacman", "-U"]),
            None,
        )
        assert pacman_call is not None, f"no pacman -U call found: {chroot_cmds}"
        assert "--overwrite=/etc/sysforge/*" in pacman_call, \
            f"pacman -U missing --overwrite glob: {pacman_call}"
        assert "--noconfirm" in pacman_call
        assert any(p.endswith(".pkg.tar.zst") for p in pacman_call), \
            f"no built package path passed to pacman -U: {pacman_call}"
        # Temporary sudoers drop-in is removed at the end
        assert not (target / "etc/sudoers.d/99-sysforge-bootstrap-build").exists()

    def test_makepkg_failure_raises_and_cleans_sudoers(self, tmp_path):
        src = self._stage_source(tmp_path)
        target = self._make_target(tmp_path)
        cfg = make_cfg(target=str(target), username="builder")

        def chroot_side_effect(target_arg, cmd, check=True):
            # Chown calls succeed; the sudo -u makepkg call fails.
            if cmd[:3] == ["sudo", "-u", "builder"]:
                return MagicMock(returncode=2)
            return MagicMock(returncode=0)

        with patch("sysforge.pipeline.stages.configure._find_sysforge_source",
                   return_value=src), \
             patch("sysforge.pipeline.stages.configure._chroot",
                   side_effect=chroot_side_effect):
            with pytest.raises(RuntimeError, match="makepkg build of sysforge failed"):
                _install_sysforge(cfg)

        # Sudoers drop-in is removed even on failure
        assert not (target / "etc/sudoers.d/99-sysforge-bootstrap-build").exists()

    def test_pacman_install_failure_raises_and_cleans_sudoers(self, tmp_path):
        src = self._stage_source(tmp_path)
        target = self._make_target(tmp_path)
        cfg = make_cfg(target=str(target), username="builder")
        build_dir = target / "home/builder/sysforge-pkg"

        def chroot_side_effect(target_arg, cmd, check=True):
            # Build step succeeds and stages a built package; pacman -U fails.
            if cmd[:3] == ["sudo", "-u", "builder"]:
                build_dir.mkdir(parents=True, exist_ok=True)
                (build_dir / "sysforge-1.0.0-1-any.pkg.tar.zst").touch()
                return MagicMock(returncode=0)
            if cmd[:2] == ["pacman", "-U"]:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        with patch("sysforge.pipeline.stages.configure._find_sysforge_source",
                   return_value=src), \
             patch("sysforge.pipeline.stages.configure._chroot",
                   side_effect=chroot_side_effect):
            with pytest.raises(RuntimeError, match="pacman -U install of sysforge failed"):
                _install_sysforge(cfg)

        assert not (target / "etc/sudoers.d/99-sysforge-bootstrap-build").exists()

    def test_missing_pkgbuild_in_source_raises(self, tmp_path):
        src = tmp_path / "src/sysforge"
        src.mkdir(parents=True)
        (src / "pyproject.toml").write_text("[project]\nname='sysforge'\n")
        # No PKGBUILD
        target = self._make_target(tmp_path)
        cfg = make_cfg(target=str(target), username="builder")

        with patch("sysforge.pipeline.stages.configure._find_sysforge_source",
                   return_value=src), \
             patch("sysforge.pipeline.stages.configure._chroot",
                   return_value=MagicMock(returncode=0)):
            with pytest.raises(RuntimeError, match="no PKGBUILD found"):
                _install_sysforge(cfg)


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
        assert parse_gpu_vendors(lspci) == ["intel"]

    def test_radeon_detected_as_amd(self):
        lspci = "01:00.0 VGA compatible controller: Radeon RX 580\n"
        assert parse_gpu_vendors(lspci) == ["amd"]

    def test_unknown_vendor(self):
        lspci = "01:00.0 VGA compatible controller: Matrox Electronics G200eW\n"
        assert parse_gpu_vendors(lspci) == ["other"]

    def test_dedup_multiple_same_vendor(self):
        lspci = (
            "01:00.0 VGA compatible controller: NVIDIA Corporation GA102\n"
            "02:00.0 VGA compatible controller: NVIDIA Corporation GA104\n"
        )
        assert parse_gpu_vendors(lspci) == ["nvidia"]


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
                   return_value={}), \
             patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value="/usr/bin/emacs"):
            editor, source = _resolve_editor()
        assert editor == "emacs"
        assert source == "SYSFORGE_EDITOR"

    def test_sysforge_toml_second(self):
        with patch.dict("os.environ", {}, clear=False), \
             patch("sysforge.pipeline.stages.reconfigure.load_sysforge_toml",
                   return_value={"ui": {"editor": "helix"}}):
            # Clear env vars that might interfere
            env = dict(SYSFORGE_EDITOR="", EDITOR="", VISUAL="")
            with patch.dict("os.environ", env), \
                 patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                       return_value="/usr/bin/helix"):
                editor, source = _resolve_editor()
        assert editor == "helix"
        assert source == "sysforge.toml"

    def test_editor_env_fallback(self):
        with patch.dict("os.environ",
                        {"EDITOR": "nano", "SYSFORGE_EDITOR": ""},
                        clear=False), \
             patch("sysforge.pipeline.stages.reconfigure.load_sysforge_toml",
                   return_value={}), \
             patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value="/usr/bin/nano"):
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

    def test_env_candidate_not_on_path_falls_through(self):
        # $EDITOR points at an editor that isn't installed. Without
        # a PATH check, this would have propagated as "the editor"
        # and every later [e]dit prompt would silently fail. Must
        # fall through to the next valid candidate.
        with patch.dict("os.environ",
                        {"SYSFORGE_EDITOR": "ghosted",
                         "EDITOR": "", "VISUAL": ""},
                        clear=False), \
             patch("sysforge.pipeline.stages.reconfigure.load_sysforge_toml",
                   return_value={}), \
             patch("sysforge.pipeline.stages.reconfigure.shutil.which") as mock_which:
            mock_which.side_effect = lambda x: "/usr/bin/nano" if x == "nano" else None
            editor, source = _resolve_editor()
        assert editor == "nano"
        assert source == "detected"

    def test_no_editor_anywhere_returns_none_sentinel(self):
        # When *no* editor is on PATH (no env var, no sysforge.toml entry,
        # no vim/nano/vi fallback) the function used to hard-return
        # ("vi", "default") even though /usr/bin/vi didn't exist. That lie
        # then propagated through the editor prompt and the config-review
        # step. The empty-string + "none" return lets callers detect the
        # situation and force the user to pick one.
        with patch.dict("os.environ",
                        {"SYSFORGE_EDITOR": "", "EDITOR": "", "VISUAL": ""},
                        clear=False), \
             patch("sysforge.pipeline.stages.reconfigure.load_sysforge_toml",
                   return_value={}), \
             patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value=None):
            editor, source = _resolve_editor()
        assert editor == ""
        assert source == "none"


class TestOpenInEditor:
    def test_missing_editor_warns_and_returns_false(self, tmp_path):
        path = tmp_path / "f.toml"
        path.write_text("")
        with patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value=None), \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run") as run, \
             patch("sysforge.pipeline.stages.reconfigure._log") as log:
            ok = _open_in_editor(path, "ghosted-editor")
        run.assert_not_called()
        # Must surface at UI level so it's visible at default verbosity —
        # warn() is gated at -v and would leave the user wondering why
        # nothing opened.
        assert log.ui.called
        # Return value lets the caller skip the validation pass that would
        # otherwise print a misleading "✓" on a file that was never opened.
        assert ok is False

    def test_empty_editor_returns_false(self, tmp_path):
        # _resolve_editor returns ("", "none") when no editor is available;
        # _open_in_editor must handle that explicitly rather than letting
        # shutil.which("") propagate into a confusing "Editor '' is not on
        # PATH" message.
        path = tmp_path / "f.toml"
        path.write_text("")
        with patch("sysforge.pipeline.stages.reconfigure.subprocess.run") as run, \
             patch("sysforge.pipeline.stages.reconfigure._log") as log:
            ok = _open_in_editor(path, "")
        run.assert_not_called()
        assert log.ui.called
        assert ok is False

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
            ok = _open_in_editor(path, "nvim")
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
        assert ok is True

    def test_falls_back_when_no_tty(self, tmp_path):
        path = tmp_path / "f.toml"
        path.write_text("")
        with patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value="/usr/bin/nvim"), \
             patch("sysforge.pipeline.stages.reconfigure.os.open",
                   side_effect=OSError("no tty")), \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run",
                   return_value=MagicMock(returncode=0)) as run:
            ok = _open_in_editor(path, "nvim")
        # Still calls subprocess.run, just without the tty fd kwargs.
        kwargs = run.call_args.kwargs
        assert "stdin" not in kwargs
        assert ok is True

    def test_nonzero_exit_still_counts_as_ran(self, tmp_path):
        # The user may have edited the file and quit with :cq, or the editor
        # may have shown a non-fatal warning. Either way the file was open;
        # we want validation to run, so _open_in_editor returns True.
        path = tmp_path / "f.toml"
        path.write_text("")
        with patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value="/usr/bin/nvim"), \
             patch("sysforge.pipeline.stages.reconfigure.os.open",
                   side_effect=OSError("no tty")), \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run",
                   return_value=MagicMock(returncode=2)), \
             patch("sysforge.pipeline.stages.reconfigure._log") as log:
            ok = _open_in_editor(path, "nvim")
        assert log.ui.called
        assert ok is True

    def test_filenotfound_returns_false(self, tmp_path):
        # subprocess.run raises FileNotFoundError when the binary is gone
        # between shutil.which() and execve(). _run_editor_argv returns -1;
        # _open_in_editor must propagate that as a launch failure.
        path = tmp_path / "f.toml"
        path.write_text("")
        with patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value="/usr/bin/nvim"), \
             patch("sysforge.pipeline.stages.reconfigure.os.open",
                   side_effect=OSError("no tty")), \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run",
                   side_effect=FileNotFoundError):
            ok = _open_in_editor(path, "nvim")
        assert ok is False

    def test_run_editor_argv_returns_minus_one_on_filenotfound(self):
        with patch("sysforge.pipeline.stages.reconfigure.os.open",
                   side_effect=OSError("no tty")), \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run",
                   side_effect=FileNotFoundError):
            assert _run_editor_argv(["nope"]) == -1


class TestStepEditorRejectsUnresolvable:
    """
    The editor step has three failure modes that must never silently leave
    the stage with an unusable editor: the user types a missing editor and
    cancels, picks an invalid pacman package, or the install runs but
    doesn't produce the binary on PATH. In every case the function must
    return the previous (resolvable) editor and skip the save prompt.
    Single-choice prompts (``change?``, ``[i/r/cancel]``, ``save?``) go
    through ``_prompt_choice``; free-text prompts (editor name, pkg name)
    go through ``_prompt``.
    """

    def _opts(self, dry_run=False):
        opts = MagicMock()
        opts.dry_run = dry_run
        return opts

    def test_rejects_when_install_doesnt_provide_binary(self):
        # User types nvim, picks install, the install runs but the binary
        # is still missing (e.g. typed package name doesn't actually provide
        # /usr/bin/nvim). The retry loop falls back to the editor-name
        # prompt; the user hits Enter to keep the previous editor.
        choices = iter([
            "e",        # change?
            "i",        # [i]nstall after 'nvim' not on PATH
        ])
        prompts = iter([
            "nvim",     # new editor
            "neovim",   # package to install
            "",         # second pass: ↵ keeps previous editor
        ])

        def fake_run(argv, *a, **k):
            # The pacman -Si precheck must succeed so that the install
            # path is exercised; the install itself then fails to
            # produce the binary on PATH.
            if argv[:2] == ["pacman", "-Si"]:
                return MagicMock(returncode=0)
            return MagicMock(returncode=1)

        with patch("sysforge.pipeline.stages.reconfigure._interactive",
                   return_value=True), \
             patch("sysforge.pipeline.stages.reconfigure._resolve_editor",
                   return_value=("vi", "default")), \
             patch("sysforge.pipeline.stages.reconfigure._prompt_choice",
                   side_effect=lambda *a, **k: next(choices)), \
             patch("sysforge.pipeline.stages.reconfigure._prompt",
                   side_effect=lambda *a, **k: next(prompts)), \
             patch("sysforge.pipeline.stages.reconfigure.shutil.which",
                   return_value="/usr/bin/vi"), \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run",
                   side_effect=fake_run), \
             patch("sysforge.pipeline.stages.reconfigure._save_sysforge_toml_ui") as save:
            # Re-stub which() to return the previous editor's path but None
            # for nvim. shutil.which is called many times; differentiate.
            from sysforge.pipeline.stages import reconfigure as _r
            with patch.object(_r.shutil, "which",
                              side_effect=lambda x: "/usr/bin/vi" if x == "vi" else None):
                result = _step_editor(None, None, self._opts(), "vi")
        assert result == "vi"
        save.assert_not_called()

    def test_pacman_precheck_rejects_unknown_package(self):
        # User types nvim, picks install, then a package name that pacman -Si
        # doesn't recognise. We must reject early without calling
        # `sudo pacman -S` and let the retry loop drop the user back at the
        # editor prompt; ↵ keeps the previous editor.
        choices = iter([
            "e",        # change?
            "i",        # install
        ])
        prompts = iter([
            "nvim",        # new editor
            "neoooovim",   # bogus package name
            "",            # ↵ keeps prev
        ])
        calls = []

        def fake_run(argv, *a, **k):
            calls.append(argv)
            if argv[:2] == ["pacman", "-Si"]:
                return MagicMock(returncode=1)  # not in repos
            return MagicMock(returncode=0)

        from sysforge.pipeline.stages import reconfigure as _r
        with patch("sysforge.pipeline.stages.reconfigure._interactive",
                   return_value=True), \
             patch("sysforge.pipeline.stages.reconfigure._resolve_editor",
                   return_value=("vi", "default")), \
             patch("sysforge.pipeline.stages.reconfigure._prompt_choice",
                   side_effect=lambda *a, **k: next(choices)), \
             patch("sysforge.pipeline.stages.reconfigure._prompt",
                   side_effect=lambda *a, **k: next(prompts)), \
             patch.object(_r.shutil, "which",
                          side_effect=lambda x: "/usr/bin/vi" if x == "vi" else None), \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run",
                   side_effect=fake_run), \
             patch("sysforge.pipeline.stages.reconfigure._save_sysforge_toml_ui") as save:
            result = _step_editor(None, None, self._opts(), "vi")
        assert result == "vi"
        save.assert_not_called()
        # Only the precheck ran — no `sudo pacman -S` attempt.
        assert calls == [["pacman", "-Si", "neoooovim"]]

    def test_does_not_save_unresolvable_editor_after_cancel(self):
        # User types nvim and presses ↵ at the install/retry/cancel prompt.
        # Function must keep the previous editor and skip the save prompt.
        choices = iter([
            "e",        # change?
            "",         # ↵ cancel after 'nvim' not on PATH
        ])
        prompts = iter([
            "nvim",     # new editor
        ])

        from sysforge.pipeline.stages import reconfigure as _r
        with patch("sysforge.pipeline.stages.reconfigure._interactive",
                   return_value=True), \
             patch("sysforge.pipeline.stages.reconfigure._resolve_editor",
                   return_value=("vi", "default")), \
             patch("sysforge.pipeline.stages.reconfigure._prompt_choice",
                   side_effect=lambda *a, **k: next(choices)), \
             patch("sysforge.pipeline.stages.reconfigure._prompt",
                   side_effect=lambda *a, **k: next(prompts)), \
             patch.object(_r.shutil, "which",
                          side_effect=lambda x: "/usr/bin/vi" if x == "vi" else None), \
             patch("sysforge.pipeline.stages.reconfigure._save_sysforge_toml_ui") as save:
            result = _step_editor(None, None, self._opts(), "vi")
        assert result == "vi"
        save.assert_not_called()


class TestStepEditorNoEditorOnPath:
    """
    When _resolve_editor returns ``("", "none")`` (no editor on PATH at
    all), the step must not offer a "[↵] keep" branch — there's nothing
    valid to keep, and propagating an empty editor through _open_in_editor
    later would print a misleading "vi not on PATH" warning at every
    config-review prompt. The user is forced to pick one (or cancel out
    with the empty-editor sentinel).
    """

    def _opts(self, dry_run=False):
        opts = MagicMock()
        opts.dry_run = dry_run
        return opts

    def test_no_keep_prompt_when_no_editor(self):
        # The "Change? [e]dit / [↵] keep" prompt must be skipped entirely
        # when there's no editor — we go straight to the editor-name prompt.
        prompts = iter([
            "vim",      # picks vim
        ])
        choices_called = []

        def fake_choice(msg, *a, **k):
            choices_called.append(msg)
            return "n"  # decline save prompt

        from sysforge.pipeline.stages import reconfigure as _r
        with patch("sysforge.pipeline.stages.reconfigure._interactive",
                   return_value=True), \
             patch("sysforge.pipeline.stages.reconfigure._resolve_editor",
                   return_value=("", "none")), \
             patch("sysforge.pipeline.stages.reconfigure._prompt_choice",
                   side_effect=fake_choice), \
             patch("sysforge.pipeline.stages.reconfigure._prompt",
                   side_effect=lambda *a, **k: next(prompts)), \
             patch.object(_r.shutil, "which",
                          side_effect=lambda x: "/usr/bin/vim" if x == "vim" else None), \
             patch("sysforge.pipeline.stages.reconfigure._save_sysforge_toml_ui"):
            result = _step_editor(None, None, self._opts(), "")
        assert result == "vim"
        # Only the save prompt should have been shown — no "Change?" prompt.
        assert all("Change?" not in m for m in choices_called)

    def test_cancel_returns_empty_string(self):
        # When no editor exists and the user presses ↵ at the editor-name
        # prompt, the function returns "" so downstream config-review steps
        # know there's no editor to launch.
        prompts = iter([
            "",         # ↵ skips
        ])
        from sysforge.pipeline.stages import reconfigure as _r
        with patch("sysforge.pipeline.stages.reconfigure._interactive",
                   return_value=True), \
             patch("sysforge.pipeline.stages.reconfigure._resolve_editor",
                   return_value=("", "none")), \
             patch("sysforge.pipeline.stages.reconfigure._prompt_choice",
                   return_value="n"), \
             patch("sysforge.pipeline.stages.reconfigure._prompt",
                   side_effect=lambda *a, **k: next(prompts)), \
             patch.object(_r.shutil, "which", return_value=None), \
             patch("sysforge.pipeline.stages.reconfigure._save_sysforge_toml_ui") as save:
            result = _step_editor(None, None, self._opts(), "")
        assert result == ""
        save.assert_not_called()


class TestStepEditorRetryFlow:
    """The [r]e-enter editor option lets a user fix a typo without having
    to abort and re-run the whole step."""

    def _opts(self, dry_run=False):
        opts = MagicMock()
        opts.dry_run = dry_run
        return opts

    def test_re_enter_after_typo(self):
        # Typo "vimm" → choose [r]e-enter → type "vim" (which exists).
        choices = iter([
            "e",        # change?
            "r",        # re-enter after 'vimm' missing
            "n",        # decline save
        ])
        prompts = iter([
            "vimm",     # typo
            "vim",      # corrected
        ])
        from sysforge.pipeline.stages import reconfigure as _r
        with patch("sysforge.pipeline.stages.reconfigure._interactive",
                   return_value=True), \
             patch("sysforge.pipeline.stages.reconfigure._resolve_editor",
                   return_value=("nano", "$EDITOR")), \
             patch("sysforge.pipeline.stages.reconfigure._prompt_choice",
                   side_effect=lambda *a, **k: next(choices)), \
             patch("sysforge.pipeline.stages.reconfigure._prompt",
                   side_effect=lambda *a, **k: next(prompts)), \
             patch.object(_r.shutil, "which",
                          side_effect=lambda x: f"/usr/bin/{x}" if x in {"nano", "vim"} else None), \
             patch("sysforge.pipeline.stages.reconfigure._save_sysforge_toml_ui"):
            result = _step_editor(None, None, self._opts(), "nano")
        assert result == "vim"

    def test_install_succeeds_returns_new_editor(self):
        # Editor not on PATH → pacman -F auto-detects 'neovim' provides nvim →
        # user confirms install → binary now present → return new editor.
        # The user never types a package name: that's the UX win this asserts.
        choices = iter([
            "e",        # change?
            "i",        # [i]nstall via pacman
            "y",        # confirm install of auto-detected 'neovim'
            "n",        # decline save
        ])
        prompts = iter([
            "nvim",     # new editor (not on PATH initially)
        ])
        installed = {"nvim": False}

        def fake_which(x):
            if x == "vi":
                return "/usr/bin/vi"
            if x == "pacman":
                return "/usr/bin/pacman"
            if x == "nvim" and installed["nvim"]:
                return "/usr/bin/nvim"
            return None

        def fake_run(argv, *a, **k):
            if argv[:2] == ["pacman", "-Fq"]:
                return MagicMock(returncode=0, stdout="extra/neovim\n", stderr="")
            if argv[:3] == ["sudo", "pacman", "-S"]:
                installed["nvim"] = True
                return MagicMock(returncode=0)
            return MagicMock(returncode=1)

        from sysforge.pipeline.stages import reconfigure as _r
        with patch("sysforge.pipeline.stages.reconfigure._interactive",
                   return_value=True), \
             patch("sysforge.pipeline.stages.reconfigure._resolve_editor",
                   return_value=("vi", "default")), \
             patch("sysforge.pipeline.stages.reconfigure._prompt_choice",
                   side_effect=lambda *a, **k: next(choices)), \
             patch("sysforge.pipeline.stages.reconfigure._prompt",
                   side_effect=lambda *a, **k: next(prompts)), \
             patch.object(_r.shutil, "which", side_effect=fake_which), \
             patch("sysforge.pipeline.stages.reconfigure.subprocess.run",
                   side_effect=fake_run):
            result = _step_editor(None, None, self._opts(), "vi")
        assert result == "nvim"


class TestReviewConfigFile:
    """
    _review_config_file used to validate the config file even when the
    editor failed to launch, printing a misleading "✓" on a file the
    user never actually edited. The fix: when _open_in_editor returns
    False, skip the validation pass entirely so the user knows the edit
    didn't happen.
    """

    def test_validation_skipped_when_editor_fails_to_launch(self, tmp_path):
        path = tmp_path / "profiles.toml"
        path.write_text("garbage = [unclosed\n")

        validate_calls = []
        def fake_validate(p):
            validate_calls.append(p)
            return True, "ok"  # would mask the failure if it ran

        # User picks [e]dit; the editor doesn't exist; validation must NOT run.
        with patch("sysforge.pipeline.stages.reconfigure._interactive",
                   return_value=True), \
             patch("sysforge.pipeline.stages.reconfigure._prompt_choice",
                   return_value="e"), \
             patch("sysforge.pipeline.stages.reconfigure._open_in_editor",
                   return_value=False) as open_mock:
            _review_config_file(
                "profiles.toml", path, editor="ghosted",
                dry_run=False, validate_fn=fake_validate,
            )
        open_mock.assert_called_once()
        assert validate_calls == []

    def test_validation_runs_when_editor_succeeds(self, tmp_path):
        path = tmp_path / "profiles.toml"
        path.write_text("")

        validate_calls = []
        def fake_validate(p):
            validate_calls.append(p)
            return True, "ok"

        with patch("sysforge.pipeline.stages.reconfigure._interactive",
                   return_value=True), \
             patch("sysforge.pipeline.stages.reconfigure._prompt_choice",
                   return_value="e"), \
             patch("sysforge.pipeline.stages.reconfigure._open_in_editor",
                   return_value=True):
            _review_config_file(
                "profiles.toml", path, editor="vim",
                dry_run=False, validate_fn=fake_validate,
            )
        assert validate_calls == [path]


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
