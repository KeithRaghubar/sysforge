"""
test_graphics_probe.py — unit tests for sysforge's system-state graphics
probes. All filesystem and subprocess dependencies are patched at the
module boundary.
"""
import os
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives import graphics_probe as gp
from sysforge.primitives import pacman as pacman_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


def _patch_run(monkeypatch, mapping):
    """
    Replace gp._run with a fake that matches on argv[0]. `mapping` maps
    binary basename → CompletedProcess or None (raises FileNotFoundError).
    """
    def fake(cmd):
        key = cmd[0]
        if key not in mapping:
            return None
        return mapping[key]
    monkeypatch.setattr(gp, "_run", fake)


def _patch_read(monkeypatch, mapping):
    """Replace gp._read_text with a fake keyed on Path."""
    m = {Path(k): v for k, v in mapping.items()}
    def fake(path):
        return m.get(Path(path))
    monkeypatch.setattr(gp, "_read_text", fake)


# ---------------------------------------------------------------------------
# nvidia_modeset
# ---------------------------------------------------------------------------

def test_modeset_clean_when_sysfs_Y(monkeypatch):
    _patch_read(monkeypatch, {"/sys/module/nvidia_drm/parameters/modeset": "Y\n"})
    assert gp._check_nvidia_modeset() is None


def test_modeset_error_when_sysfs_N(monkeypatch):
    _patch_read(monkeypatch, {"/sys/module/nvidia_drm/parameters/modeset": "N\n"})
    f = gp._check_nvidia_modeset()
    assert f is not None
    assert f.severity == gp.SEV_ERROR
    assert f.check_id == "nvidia_modeset"


def test_modeset_cmdline_fallback_finds_flag(monkeypatch):
    _patch_read(monkeypatch, {
        "/sys/module/nvidia_drm/parameters/modeset": None,  # unreadable
        "/proc/cmdline": "BOOT_IMAGE=/vmlinuz root=UUID=x nvidia-drm.modeset=1 rw\n",
    })
    assert gp._check_nvidia_modeset() is None


def test_modeset_cmdline_fallback_missing_flag_warns(monkeypatch):
    _patch_read(monkeypatch, {
        "/sys/module/nvidia_drm/parameters/modeset": None,
        "/proc/cmdline": "BOOT_IMAGE=/vmlinuz root=UUID=x rw\n",
    })
    f = gp._check_nvidia_modeset()
    assert f is not None
    assert f.severity == gp.SEV_WARN
    assert f.check_id == "nvidia_modeset"


# ---------------------------------------------------------------------------
# nvidia_fbdev
# ---------------------------------------------------------------------------

def test_fbdev_skipped_on_old_kernel(monkeypatch):
    _patch_run(monkeypatch, {"uname": _completed("6.10.5-arch1-1\n")})
    assert gp._check_nvidia_fbdev() is None


def test_fbdev_skipped_when_param_absent(monkeypatch, tmp_path):
    _patch_run(monkeypatch, {"uname": _completed("6.12.0-arch1-1\n")})
    # Point at a guaranteed-missing path
    monkeypatch.setattr(gp, "Path", Path)  # no-op: sanity
    # Stub Path("/sys/...") existence
    orig_exists = Path.exists
    def fake_exists(self):
        if str(self).endswith("parameters/fbdev"):
            return False
        return orig_exists(self)
    monkeypatch.setattr(Path, "exists", fake_exists, raising=True)
    assert gp._check_nvidia_fbdev() is None


def test_fbdev_warn_when_disabled(monkeypatch):
    _patch_run(monkeypatch, {"uname": _completed("6.14.0-arch1-1\n")})
    orig_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists",
        lambda self: True if str(self).endswith("parameters/fbdev") else orig_exists(self),
    )
    _patch_read(monkeypatch, {"/sys/module/nvidia_drm/parameters/fbdev": "N\n"})
    f = gp._check_nvidia_fbdev()
    assert f is not None
    assert f.severity == gp.SEV_WARN
    assert f.check_id == "nvidia_fbdev"


# ---------------------------------------------------------------------------
# nvidia_driver_skew
# ---------------------------------------------------------------------------

def test_driver_skew_clean(monkeypatch):
    monkeypatch.setattr(
        pacman_mod, "get_all_installed_packages",
        lambda: {
            "nvidia-open-dkms": "595.58.03-2",
            "nvidia-utils": "595.58.03-2",
            "lib32-nvidia-utils": "595.58.03-1",
        },
    )
    assert gp._check_nvidia_driver_skew() is None


def test_driver_skew_detected(monkeypatch):
    monkeypatch.setattr(
        pacman_mod, "get_all_installed_packages",
        lambda: {
            "nvidia-open-dkms": "595.58.03-2",
            "nvidia-utils": "590.26-4",   # lagging
            "lib32-nvidia-utils": "595.58.03-1",
        },
    )
    f = gp._check_nvidia_driver_skew()
    assert f is not None
    assert f.severity == gp.SEV_ERROR
    assert f.check_id == "nvidia_driver_skew"
    assert "590.26" in f.message and "595.58.03" in f.message


def test_driver_skew_no_nvidia_installed(monkeypatch):
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})
    assert gp._check_nvidia_driver_skew() is None


# ---------------------------------------------------------------------------
# nvidia_module_loaded
# ---------------------------------------------------------------------------

def test_module_loaded_clean(monkeypatch):
    _patch_run(monkeypatch, {"lsmod": _completed(
        "Module                  Size  Used by\n"
        "nvidia               15093760  896 nvidia_uvm,nvidia_modeset\n"
    )})
    assert gp._check_nvidia_module_loaded(["nvidia"]) is None


def test_module_loaded_missing_when_nvidia_vendor_present(monkeypatch):
    _patch_run(monkeypatch, {"lsmod": _completed(
        "Module                  Size  Used by\n"
        "snd_hda_intel         60416  2\n"
    )})
    f = gp._check_nvidia_module_loaded(["nvidia"])
    assert f is not None
    assert f.severity == gp.SEV_ERROR
    assert f.check_id == "nvidia_module_loaded"


def test_module_loaded_skipped_when_no_nvidia_vendor(monkeypatch):
    assert gp._check_nvidia_module_loaded(["amd"]) is None


# ---------------------------------------------------------------------------
# multilib_enabled
# ---------------------------------------------------------------------------

def test_multilib_enabled_clean(monkeypatch):
    _patch_read(monkeypatch, {"/etc/pacman.conf":
        "[options]\nArchitecture = auto\n\n[multilib]\nInclude = /etc/pacman.d/mirrorlist\n"})
    assert gp._check_multilib_enabled(["nvidia"]) is None


def test_multilib_disabled_detected(monkeypatch):
    _patch_read(monkeypatch, {"/etc/pacman.conf":
        "[options]\nArchitecture = auto\n\n#[multilib]\n#Include = /etc/pacman.d/mirrorlist\n"})
    f = gp._check_multilib_enabled(["nvidia"])
    assert f is not None
    assert f.severity == gp.SEV_ERROR


def test_multilib_skipped_without_gpu(monkeypatch):
    # headless — no 32-bit libs needed
    assert gp._check_multilib_enabled([]) is None


# ---------------------------------------------------------------------------
# session_type
# ---------------------------------------------------------------------------

def test_session_type_info(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "COSMIC")
    f = gp._check_session_type()
    assert f is not None
    assert f.severity == gp.SEV_INFO
    assert "wayland" in f.message and "COSMIC" in f.message


def test_session_type_absent_returns_none(monkeypatch):
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    assert gp._check_session_type() is None


# ---------------------------------------------------------------------------
# xwayland_present
# ---------------------------------------------------------------------------

def test_xwayland_skipped_on_x11(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert gp._check_xwayland_present({}) is None


def test_xwayland_missing_on_wayland(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    f = gp._check_xwayland_present({})
    assert f is not None
    assert f.severity == gp.SEV_ERROR


def test_xwayland_present_via_git_variant(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    installed = {"xorg-xwayland-git": "24.1.9"}
    assert gp._check_xwayland_present(installed) is None


# ---------------------------------------------------------------------------
# explicit_sync_protocol (the Steam-black-window Wayland gap on NVIDIA)
# ---------------------------------------------------------------------------

def test_explicit_sync_skipped_without_nvidia(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert gp._check_explicit_sync_protocol(["amd"]) is None


def test_explicit_sync_skipped_on_x11(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert gp._check_explicit_sync_protocol(["nvidia"]) is None


def test_explicit_sync_present_clean(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    _patch_run(monkeypatch, {"wayland-info": _completed(
        "interface: 'wp_viewporter', version: 1, name: 20\n"
        "interface: 'wp_linux_drm_syncobj_manager_v1', version: 1, name: 42\n"
    )})
    assert gp._check_explicit_sync_protocol(["nvidia"]) is None


def test_explicit_sync_legacy_synchronization_clean(monkeypatch):
    """Older compositors advertise the deprecated explicit-sync protocol."""
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    _patch_run(monkeypatch, {"wayland-info": _completed(
        "interface: 'zwp_linux_explicit_synchronization_v1', "
        "version: 2, name: 42\n"
    )})
    assert gp._check_explicit_sync_protocol(["nvidia"]) is None


def test_explicit_sync_protocol_doc_name_does_not_match(monkeypatch):
    """
    The substring `wp_linux_drm_syncobj_v1` is the protocol-document name and
    never appears as a wl_registry global. A compositor advertising the real
    `_manager_v1` global must still be detected; the bare doc-name string in
    isolation must not satisfy the check.
    """
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    _patch_run(monkeypatch, {"wayland-info": _completed(
        # Stand-alone bare name (no _manager_) — must NOT match.
        "interface: 'wp_linux_drm_syncobj_v1', version: 1, name: 42\n"
    )})
    f = gp._check_explicit_sync_protocol(["nvidia"])
    assert f is not None and f.severity == gp.SEV_ERROR


def test_explicit_sync_absent_error(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    _patch_run(monkeypatch, {"wayland-info": _completed(
        "interface: 'wp_viewporter', version: 1, name: 20\n"
        "interface: 'zwp_linux_dmabuf_v1', version: 5, name: 52\n"
    )})
    f = gp._check_explicit_sync_protocol(["nvidia"])
    assert f is not None
    assert f.severity == gp.SEV_ERROR
    assert f.check_id == "explicit_sync_protocol"


def test_explicit_sync_tool_missing_skipped(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    _patch_run(monkeypatch, {})  # wayland-info not installed
    # No false-positive when we can't probe
    assert gp._check_explicit_sync_protocol(["nvidia"]) is None


# ---------------------------------------------------------------------------
# steam_gpu_accel
# ---------------------------------------------------------------------------

def test_steam_gpu_accel_enabled_warns(monkeypatch, tmp_path):
    fake_home = tmp_path
    monkeypatch.setenv("HOME", str(fake_home))
    cfg = fake_home / ".local/share/Steam/config/config.vdf"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('"InstallConfigStore"\n{\n  "GPUAccelerationEnabled" "1"\n}\n')
    # graphics_probe caches the path tuple at import time — replace it
    monkeypatch.setattr(gp, "_STEAM_CONFIG_PATHS", (cfg,))
    f = gp._check_steam_gpu_accel()
    assert f is not None
    assert f.severity == gp.SEV_WARN
    assert f.check_id == "steam_gpu_accel"


def test_steam_gpu_accel_disabled_clean(monkeypatch, tmp_path):
    cfg = tmp_path / "config.vdf"
    cfg.write_text('"GPUAccelerationEnabled" "0"\n')
    monkeypatch.setattr(gp, "_STEAM_CONFIG_PATHS", (cfg,))
    assert gp._check_steam_gpu_accel() is None


def test_steam_gpu_accel_no_config_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(gp, "_STEAM_CONFIG_PATHS", (tmp_path / "missing.vdf",))
    assert gp._check_steam_gpu_accel() is None


# ---------------------------------------------------------------------------
# check_system_graphics orchestrator
# ---------------------------------------------------------------------------

def test_orchestrator_aggregates_findings(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "COSMIC")
    # Make every probe clean except explicit_sync, so we get one ERROR finding.
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages",
                        lambda: {"xorg-xwayland": "24.1"})
    _patch_read(monkeypatch, {
        "/sys/module/nvidia_drm/parameters/modeset": "Y\n",
        "/etc/pacman.conf": "[multilib]\n",
    })
    _patch_run(monkeypatch, {
        "uname": _completed("6.12.0-arch1-1\n"),
        "lsmod": _completed("nvidia               1 x\n"),
        "wayland-info": _completed("interface: 'wp_viewporter'\n"),  # no explicit-sync
    })

    findings = gp.check_system_graphics({}, gpu_vendors=["nvidia"])
    ids = [f.check_id for f in findings]
    # session_type always present with env set; explicit_sync_protocol should fire.
    assert "session_type" in ids
    assert "explicit_sync_protocol" in ids
    errs = [f for f in findings if f.severity == gp.SEV_ERROR]
    assert any(f.check_id == "explicit_sync_protocol" for f in errs)


def test_orchestrator_nvidia_checks_skipped_without_nvidia(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages",
                        lambda: {"xorg-xwayland": "24.1"})
    _patch_read(monkeypatch, {"/etc/pacman.conf": "[multilib]\n"})
    _patch_run(monkeypatch, {})

    findings = gp.check_system_graphics({}, gpu_vendors=["amd"])
    ids = {f.check_id for f in findings}
    # None of the NVIDIA-gated checks should fire
    assert "nvidia_modeset" not in ids
    assert "nvidia_driver_skew" not in ids
    assert "explicit_sync_protocol" not in ids
