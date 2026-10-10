"""
test_graphics_probe.py — unit tests for sysforge's system-state graphics
probes. All filesystem and subprocess dependencies are patched at the
module boundary.
"""
import os
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from sysforge.primitives import diagnostics as diag
from sysforge.primitives import graphics_probe as gp
from sysforge.primitives import pacman as pacman_mod


@pytest.fixture(autouse=True)
def _quiet_mesa_llvm_symbols(monkeypatch):
    """`_check_mesa_llvm_symbols` reaches the real /usr libs via
    `toolchain_safety.check_installed_consumer_symbols`. Neutralise it by
    default so orchestrator tests stay deterministic and host-independent
    (this file's contract: all deps patched at the module boundary). Tests
    targeting it re-patch the same attribute explicitly."""
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.check_installed_consumer_symbols",
        lambda: [], raising=True,
    )
    from sysforge.primitives import diagnostics as _diag
    monkeypatch.setattr(
        "sysforge.primitives.mesa_smoke.run_smoke",
        lambda: _diag.AxisResult([], _diag.Roster()), raising=True,
    )


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
    _skip(gp._check_nvidia_fbdev(), "kernel older than 6.11")


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
    _skip(gp._check_nvidia_fbdev(), "driver has no fbdev parameter")


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
    _skip(gp._check_nvidia_driver_skew(), "no NVIDIA driver packages installed")


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
    _skip(gp._check_nvidia_module_loaded(["amd"]), gp.NO_NVIDIA)


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
    _skip(gp._check_multilib_enabled([]), "no NVIDIA/AMD/Intel GPU detected")


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


def test_session_type_absent_skips(monkeypatch):
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    _skip(gp._check_session_type(), "no XDG session variables set")


# ---------------------------------------------------------------------------
# xwayland_present
# ---------------------------------------------------------------------------

def test_xwayland_skipped_on_x11(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    _skip(gp._check_xwayland_present({}), "not a Wayland session")


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
    _skip(gp._check_explicit_sync_protocol(["amd"]), gp.NO_NVIDIA)


def test_explicit_sync_skipped_on_x11(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    _skip(gp._check_explicit_sync_protocol(["nvidia"]), "not a Wayland session")


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
    _skip(gp._check_explicit_sync_protocol(["nvidia"]), "wayland-info unavailable")


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
    _skip(gp._check_steam_gpu_accel(), "Steam config not found")


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

    findings = gp.check_system_graphics({}, gpu_vendors=["nvidia"]).findings
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

    findings = gp.check_system_graphics({}, gpu_vendors=["amd"]).findings
    ids = {f.check_id for f in findings}
    # None of the NVIDIA-gated checks should fire
    assert "nvidia_modeset" not in ids
    assert "nvidia_driver_skew" not in ids
    assert "explicit_sync_protocol" not in ids


# ---------------------------------------------------------------------------
# _check_mesa_llvm_symbols — reuses the toolchain post-install symbol fact so
# `doctor --graphics` self-diagnoses the "rebuilt toolchain → black screen"
# bug. The fact is monkeypatched (its own unit tests live in
# test_toolchain_safety.py); here we pin the GraphicsFinding mapping.
# ---------------------------------------------------------------------------

_CHECK = "sysforge.primitives.toolchain_safety.check_installed_consumer_symbols"


def test_mesa_llvm_symbols_clean_returns_none(monkeypatch):
    monkeypatch.setattr(_CHECK, lambda: [])
    assert gp._check_mesa_llvm_symbols() is None


def test_mesa_llvm_symbols_broken_maps_to_error(monkeypatch):
    finding = SimpleNamespace(
        message="libgallium-26.so links libLLVM.so.22.1 but the installed "
        "libLLVM does not export 6 symbol(s) — dropped LLVM target(s): AMDGPU",
        remediation="Rebuild the toolchain with AMDGPU kept.",
    )
    monkeypatch.setattr(_CHECK, lambda: [finding])
    f = gp._check_mesa_llvm_symbols()
    assert f is not None
    assert f.severity == gp.SEV_ERROR
    assert f.check_id == "mesa_llvm_symbols"
    assert "AMDGPU" in f.message
    assert f.remediation == "Rebuild the toolchain with AMDGPU kept."


def test_mesa_llvm_symbols_multiple_consumers_count(monkeypatch):
    f1 = SimpleNamespace(message="consumer one broken", remediation="r")
    f2 = SimpleNamespace(message="consumer two broken", remediation="r")
    monkeypatch.setattr(_CHECK, lambda: [f1, f2])
    f = gp._check_mesa_llvm_symbols()
    assert f is not None
    assert "(2 consumers affected)" in f.message


def test_check_system_graphics_includes_mesa_llvm(monkeypatch):
    """The orchestrator surfaces a broken mesa/libLLVM link on any GPU vendor."""
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})
    monkeypatch.setattr(
        _CHECK,
        lambda: [SimpleNamespace(message="broken AMDGPU link", remediation="fix")],
    )
    findings = gp.check_system_graphics(None, gpu_vendors=[]).findings
    assert "mesa_llvm_symbols" in {f.check_id for f in findings}


def _skip(result, fragment):
    assert isinstance(result, diag.Skip), result
    assert fragment in result.reason


def test_module_loaded_skip_when_lsmod_missing(monkeypatch):
    _patch_run(monkeypatch, {})
    _skip(gp._check_nvidia_module_loaded(["nvidia"]), "lsmod unavailable")


def test_multilib_skip_when_conf_unreadable(monkeypatch):
    _patch_read(monkeypatch, {})
    _skip(gp._check_multilib_enabled(["amd"]), "/etc/pacman.conf unreadable")


def test_fbdev_skip_when_kernel_version_unreadable(monkeypatch):
    _patch_run(monkeypatch, {})
    _skip(gp._check_nvidia_fbdev(), "kernel version unreadable")


def test_orchestrator_roster_without_nvidia(monkeypatch):
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    _patch_read(monkeypatch, {"/etc/pacman.conf": "[multilib]\n"})
    res = gp.check_system_graphics({}, gpu_vendors=["amd"])
    skipped = dict(res.roster.skipped)
    for cid in ("nvidia_module_loaded", "nvidia_modeset", "nvidia_fbdev",
                "nvidia_driver_skew", "explicit_sync_protocol"):
        assert skipped[cid] == gp.NO_NVIDIA
    assert "multilib_enabled" in res.roster.ran
    assert "mesa_llvm_symbols" in res.roster.ran
    assert skipped["session_type"] == "no XDG session variables set"
    assert skipped["xwayland_present"] == "not a Wayland session"


def test_graphics_merges_smoke_when_mesa_installed(monkeypatch):
    from sysforge.primitives import mesa_smoke
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {"mesa": "1:26.2.4-1"})
    monkeypatch.setattr(mesa_smoke, "run_smoke", lambda: diag.AxisResult(
        [gp.GraphicsFinding("error", "mesa_egl", "boom")],
        diag.Roster(ran=["mesa_egl"], skipped=[("mesa_vulkan", "no mesa Vulkan ICDs installed")])))
    res = gp.check_system_graphics({}, gpu_vendors=[])
    assert "mesa_egl" in [f.check_id for f in res.findings]
    assert "mesa_egl" in res.roster.ran
    assert ("mesa_vulkan", "no mesa Vulkan ICDs installed") in res.roster.skipped


def test_graphics_omits_smoke_without_mesa(monkeypatch):
    from sysforge.primitives import mesa_smoke
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {"lib32-mesa": "1"})
    monkeypatch.setattr(mesa_smoke, "run_smoke", lambda: pytest.fail("must not probe"))
    res = gp.check_system_graphics({}, gpu_vendors=[])
    assert "mesa_egl" not in res.roster.ran


def test_graphics_runs_smoke_for_renamed_pgo_mesa(monkeypatch):
    from sysforge.primitives import mesa_smoke
    called = []
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {"mesa-sysforge": "1"})
    monkeypatch.setattr(mesa_smoke, "run_smoke",
                        lambda: called.append(1) or diag.AxisResult([], diag.Roster()))
    gp.check_system_graphics({}, gpu_vendors=[])
    assert called == [1]
