"""
test_doctor.py — unit tests for sysforge doctor.

Uses a fake /var/lib/pacman/local/ tree under tmp_path and injects
it via the `root=` parameter on pacman local-db helpers. Subprocess
calls to pacman / ldconfig are patched at the module boundary.

Covers:
    _expand_graphics_targets — vendor expansion, installed filter,
                               missing hardware overlay tolerated
    _walk_closure            — BFS order, cycle dedup, --shallow cutoff
    _check_depends           — soname satisfied/missing, pacman -T path
    cmd_doctor               — clean pkg, missing depends, unsatisfied
                               ABI symbol, pkg-not-installed, exit codes
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge import doctor
from sysforge.primitives import pacman as pacman_mod


# ---------------------------------------------------------------------------
# Fake pacman local-db helpers
# ---------------------------------------------------------------------------

def _write_desc(entry: Path, depends: list[str]) -> None:
    content = ""
    if depends:
        content += "%DEPENDS%\n" + "\n".join(depends) + "\n\n"
    content += "%NAME%\n" + entry.name.rsplit("-", 2)[0] + "\n"
    (entry / "desc").write_text(content)


def _write_files(entry: Path, paths: list[str]) -> None:
    content = "%FILES%\n" + "\n".join(paths) + "\n"
    (entry / "files").write_text(content)


def _mk_pkg(db_root: Path, name: str, version: str,
            depends: list[str] | None = None,
            files: list[str] | None = None) -> Path:
    entry = db_root / f"{name}-{version}"
    entry.mkdir()
    _write_desc(entry, depends or [])
    _write_files(entry, files or [])
    return entry


# ---------------------------------------------------------------------------
# _expand_graphics_targets
# ---------------------------------------------------------------------------

def test_expand_graphics_no_hardware_profile():
    """No hardware_profile → base stack, no vendor extras."""
    installed = {"mesa": "25.0", "vulkan-icd-loader": "1.3", "libglvnd": "1.7"}
    targets = doctor._expand_graphics_targets({}, installed)
    # only base stack packages that are installed
    assert "mesa" in targets
    assert "vulkan-icd-loader" in targets
    assert "libglvnd" in targets
    # not installed → not in targets
    assert "mesa-git" not in targets
    # vendor-specific drivers shouldn't appear without gpu_vendors
    assert "nvidia-open-dkms" not in targets


def test_expand_graphics_nvidia_overlay(tmp_path):
    """gpu_vendors=['nvidia'] adds installed nvidia driver to targets."""
    hw = tmp_path / "hardware_profile.toml"
    hw.write_text("[hardware]\ngpu_vendors = [\"nvidia\"]\n")
    installed = {
        "mesa-git": "25.0", "nvidia-open-dkms": "565.0", "lib32-nvidia-utils": "565.0",
    }
    targets = doctor._expand_graphics_targets(
        {"hardware_profile": str(hw)}, installed,
    )
    assert "mesa-git" in targets
    assert "nvidia-open-dkms" in targets
    assert "lib32-nvidia-utils" in targets


def test_expand_graphics_amd_overlay(tmp_path):
    hw = tmp_path / "hardware_profile.toml"
    hw.write_text("[hardware]\ngpu_vendors = [\"amd\"]\n")
    installed = {"mesa": "25.0", "vulkan-radeon": "25.0"}
    targets = doctor._expand_graphics_targets(
        {"hardware_profile": str(hw)}, installed,
    )
    assert "vulkan-radeon" in targets
    assert "nvidia-open-dkms" not in targets


def test_expand_graphics_filters_uninstalled():
    """Reference list contains -git variants; only installed ones surface."""
    installed = {"mesa": "25.0"}  # neither mesa-git nor libglvnd installed
    targets = doctor._expand_graphics_targets({}, installed)
    assert targets == ["mesa"]


# ---------------------------------------------------------------------------
# _walk_closure
# ---------------------------------------------------------------------------

def test_walk_closure_bfs_order(tmp_path, monkeypatch):
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "rootpkg", "1.0-1", depends=["childa", "childb"])
    _mk_pkg(db, "childa", "1.0-1", depends=["grandchild"])
    _mk_pkg(db, "childb", "1.0-1", depends=[])
    _mk_pkg(db, "grandchild", "1.0-1", depends=[])
    installed = {n: "1.0-1" for n in ("rootpkg", "childa", "childb", "grandchild")}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)

    order = doctor._walk_closure(["rootpkg"], shallow=False)
    # BFS: root, direct, grand
    assert order[0] == "rootpkg"
    assert set(order[1:3]) == {"childa", "childb"}
    assert order[3] == "grandchild"


def test_walk_closure_shallow_skips_grandchildren(tmp_path, monkeypatch):
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "rootpkg", "1.0-1", depends=["childa"])
    _mk_pkg(db, "childa", "1.0-1", depends=["grandchild"])
    _mk_pkg(db, "grandchild", "1.0-1", depends=[])
    installed = {"rootpkg": "1.0-1", "childa": "1.0-1", "grandchild": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)

    order = doctor._walk_closure(["rootpkg"], shallow=True)
    assert "rootpkg" in order
    assert "childa" in order
    assert "grandchild" not in order


def test_walk_closure_handles_cycle(tmp_path, monkeypatch):
    """A→B→A cycle must not hang; each package visited once."""
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "pkga", "1.0-1", depends=["pkgb"])
    _mk_pkg(db, "pkgb", "1.0-1", depends=["pkga"])
    installed = {"pkga": "1.0-1", "pkgb": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)

    order = doctor._walk_closure(["pkga"], shallow=False)
    assert sorted(order) == ["pkga", "pkgb"]


def test_walk_closure_unknown_root_included(tmp_path, monkeypatch):
    """A root that isn't installed still appears in the order (to be reported)."""
    db = tmp_path / "local"
    db.mkdir()
    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})

    order = doctor._walk_closure(["ghostpkg"], shallow=False)
    assert order == ["ghostpkg"]


# ---------------------------------------------------------------------------
# _check_depends
# ---------------------------------------------------------------------------

def _pacman_t_mock(missing_lines: list[str], rc: int):
    def run(cmd, **_kw):
        r = MagicMock()
        r.stdout = "\n".join(missing_lines) + ("\n" if missing_lines else "")
        r.stderr = ""
        r.returncode = rc
        return r
    return run


def test_check_depends_soname_satisfied():
    issues = doctor._check_depends(
        ["libcap.so=2"],
        {"libcap.so.2", "libcap.so.2.69"},
    )
    assert issues == []


def test_check_depends_soname_missing():
    issues = doctor._check_depends(
        ["libmissing.so=3"],
        {"libcap.so.2"},
    )
    assert len(issues) == 1
    assert "libmissing.so=3" in issues[0]


def test_check_depends_pacman_t_reports_missing():
    with patch("sysforge.doctor.subprocess.run",
               side_effect=_pacman_t_mock(["glibc>=2.40"], 127)):
        issues = doctor._check_depends(["glibc>=2.40"], set())
    assert len(issues) == 1
    assert "glibc>=2.40" in issues[0]


def test_check_depends_pacman_t_all_satisfied():
    with patch("sysforge.doctor.subprocess.run",
               side_effect=_pacman_t_mock([], 0)):
        issues = doctor._check_depends(["glibc", "ncurses"], set())
    assert issues == []


# ---------------------------------------------------------------------------
# cmd_doctor — end-to-end (mocking abi_check + subprocess for pacman -T)
# ---------------------------------------------------------------------------

def _make_args(**overrides) -> SimpleNamespace:
    defaults = dict(
        packages=[], graphics=False, hardware=False, toolchain=False,
        pacman=False, state=False, boot=False, services=False, audio=False,
        all=False, repo=False,
        shallow=False, quiet=False, suggest=False, config={},
        apply=False, no_confirm=False, dry_run=False, state_dir=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _patch_axes_clean(monkeypatch):
    """Neutralise the system-state axes so package-walk tests stay fast and
    deterministic (they otherwise probe real hardware/toolchain/graphics/
    pacman/state/boot/services)."""
    monkeypatch.setattr(doctor, "_collect_toolchain_findings", lambda config: [])
    monkeypatch.setattr(doctor, "_collect_hardware_findings", lambda: [])
    monkeypatch.setattr(doctor, "_collect_graphics_findings", lambda config: [])
    monkeypatch.setattr(doctor, "_collect_pacman_findings", lambda: [])
    monkeypatch.setattr(doctor, "_collect_state_findings", lambda args: [])
    monkeypatch.setattr(doctor, "_collect_services_findings", lambda: [])
    monkeypatch.setattr(doctor, "_collect_audio_findings", lambda: [])
    monkeypatch.setattr(doctor, "_collect_boot_findings", lambda: [])


def test_cmd_doctor_bare_runs_full_system_sweep(monkeypatch, capsys):
    """Bare `doctor` (no args) runs every system axis — not a usage error."""
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    _patch_axes_clean(monkeypatch)

    rc = doctor.cmd_doctor(_make_args())
    err = capsys.readouterr().err
    assert "nothing to check" not in err
    # Every system-axis section renders (clean) — the full sweep.
    for label in ("toolchain checks", "hardware checks", "system graphics checks",
                  "pacman / system integrity", "sysforge state integrity",
                  "boot / kernel runtime", "services / runtime health",
                  "audio / sound stack"):
        assert label in err, label
    assert rc == 0


def test_cmd_doctor_repo_with_nothing_installed_exits_2(monkeypatch, capsys):
    """--repo with no installed packages selects no roots and no axes → usage."""
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    rc = doctor.cmd_doctor(_make_args(repo=True))
    assert rc == 2
    err = capsys.readouterr().err
    assert "nothing to check" in err


def test_resolve_axis_names_single_new_flag():
    """A single new axis flag selects exactly that axis (no full sweep)."""
    assert doctor._resolve_axis_names(_make_args(state=True)) == ["state"]
    assert doctor._resolve_axis_names(_make_args(pacman=True)) == ["pacman"]
    # Two flags → both, in canonical order (boot precedes services).
    assert doctor._resolve_axis_names(
        _make_args(services=True, boot=True)) == ["boot", "services"]


def test_resolve_axis_names_bare_includes_new_axes():
    names = doctor._resolve_axis_names(_make_args())
    for n in ("toolchain", "hardware", "graphics", "pacman", "state",
              "boot", "services", "audio"):
        assert n in names


def _patch_kernel_safety(monkeypatch, *, kernels, verify=None, space=None,
                         dkms=None):
    from sysforge.primitives import kernel_safety
    monkeypatch.setattr(kernel_safety, "find_fallback_kernels",
                        lambda *a, **k: list(kernels))
    monkeypatch.setattr(kernel_safety, "verify_boot_artifacts",
                        lambda suffix, *a, **k: (verify or {}).get(suffix, []))
    monkeypatch.setattr(kernel_safety, "check_boot_mount_space",
                        lambda *a, **k: space)
    monkeypatch.setattr(kernel_safety, "check_dkms_for_kernel",
                        lambda *a, **k: dkms or [])
    monkeypatch.setattr(kernel_safety, "running_kernel_release",
                        lambda: "6.0.0-test")


def test_collect_boot_findings_adapts_brick_finding(monkeypatch):
    from sysforge.primitives import diagnostics as diag
    from sysforge.primitives import kernel_safety
    brick = kernel_safety.KernelFinding(
        diag.SEV_ERROR, "boot_vmlinuz_missing", "image gone", "reinstall",
        is_brick=True)
    _patch_kernel_safety(monkeypatch, kernels=["linux", "linux-lts"],
                         verify={"linux": [brick]})
    findings = doctor._collect_boot_findings()
    hit = [f for f in findings if f.check_id == "boot_vmlinuz_missing"]
    assert len(hit) == 1
    assert hit[0].is_brick and hit[0].is_error
    assert hit[0].category == "boot"


def test_collect_boot_findings_warns_on_single_kernel(monkeypatch):
    _patch_kernel_safety(monkeypatch, kernels=["linux"])
    findings = doctor._collect_boot_findings()
    assert any(f.check_id == "boot_no_fallback" for f in findings)


def test_collect_boot_findings_clean_with_fallback(monkeypatch):
    _patch_kernel_safety(monkeypatch, kernels=["linux", "linux-lts"])
    assert doctor._collect_boot_findings() == []


def test_with_reboot_hint_appends_and_selects():
    from sysforge.primitives import diagnostics as diag
    keep = diag.Finding("boot", diag.SEV_WARN, "boot_vmlinuz_missing",
                        "image gone", remediation="reinstall")
    tag = diag.Finding("boot", diag.SEV_WARN, "dkms:nvidia",
                       "not built", remediation="dkms install")
    out = doctor._with_reboot_hint([keep, tag],
                                   only=lambda f: f.check_id.startswith("dkms:"))
    by_id = {f.check_id: f for f in out}
    # Selected finding keeps its remediation and gains the caveat.
    assert by_id["dkms:nvidia"].remediation.startswith("dkms install — ")
    assert "reboot" in by_id["dkms:nvidia"].remediation
    # Non-matching finding is untouched.
    assert by_id["boot_vmlinuz_missing"].remediation == "reinstall"


def test_with_reboot_hint_handles_empty_remediation():
    from sysforge.primitives import diagnostics as diag
    f = diag.Finding("hardware", diag.SEV_WARN, "missing_driver", "no driver")
    (out,) = doctor._with_reboot_hint([f])
    assert out.remediation == doctor._REBOOT_HINT


def test_collect_boot_findings_dkms_carries_reboot_hint(monkeypatch):
    from sysforge.primitives import kernel_safety
    dkms = kernel_safety.KernelFinding(
        kernel_safety.SEV_WARN, "dkms:nvidia",
        "DKMS module 'nvidia' is not built", "dkms install", is_brick=False)
    _patch_kernel_safety(monkeypatch, kernels=["linux", "linux-lts"], dkms=[dkms])
    findings = doctor._collect_boot_findings()
    hit = [f for f in findings if f.check_id == "dkms:nvidia"]
    assert len(hit) == 1
    assert "reboot" in hit[0].remediation


def test_collect_services_findings_firmware_carries_reboot_hint(monkeypatch):
    from sysforge.primitives import diagnostics as diag
    from sysforge.primitives import runtime_probe
    fw = diag.Finding("services", diag.SEV_WARN, "missing_firmware",
                      "firmware not loaded", remediation="install firmware")
    unit = diag.Finding("services", diag.SEV_ERROR, "failed_unit:foo.service",
                        "failed", remediation="inspect")
    monkeypatch.setattr(runtime_probe, "collect_runtime_findings",
                        lambda: [fw, unit])
    out = {f.check_id: f for f in doctor._collect_services_findings()}
    assert "reboot" in out["missing_firmware"].remediation
    assert out["failed_unit:foo.service"].remediation == "inspect"


# ---------------------------------------------------------------------------
# cmd_doctor --hardware
# ---------------------------------------------------------------------------

def _patch_hw(monkeypatch, *, devices=None, unsupported=None,
              running_cfg=None, audit=None):
    from sysforge.primitives import device_probe, kernel_safety
    from sysforge.primitives import dep_analysis
    monkeypatch.setattr(device_probe, "enumerate_devices",
                        lambda *a, **k: devices or [])
    monkeypatch.setattr(device_probe, "check_unsupported_devices",
                        lambda *a, **k: unsupported or [])
    monkeypatch.setattr(dep_analysis, "_parse_kernel_config",
                        lambda: running_cfg)
    monkeypatch.setattr(kernel_safety, "audit_resolved_config",
                        lambda *a, **k: audit or [])


def test_cmd_doctor_hardware_standalone_bypasses_empty_roots(monkeypatch, capsys):
    from sysforge.primitives.device_probe import DeviceFinding, SEV_WARN
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    _patch_hw(monkeypatch, unsupported=[
        DeviceFinding(SEV_WARN, "unsupported_device",
                      "pci device 0000:0d:00.4 has no driver", "Enable CONFIG_X")])

    rc = doctor.cmd_doctor(_make_args(hardware=True))
    err = capsys.readouterr().err
    assert "no packages to check" not in err
    assert "hardware checks" in err
    assert "unsupported_device" in err
    # device-coverage findings are warnings → clean exit
    assert rc == 0


def test_cmd_doctor_hardware_brick_finding_nonzero_exit(monkeypatch, capsys):
    from sysforge.primitives.kernel_safety import KernelFinding, SEV_ERROR
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    _patch_hw(monkeypatch, running_cfg={"CONFIG_MODULES": "y"}, audit=[
        KernelFinding(SEV_ERROR, "boot_kconfig:CONFIG_EXT4_FS",
                      "CONFIG_EXT4_FS is not enabled", "Set CONFIG_EXT4_FS=y",
                      is_brick=True)])

    rc = doctor.cmd_doctor(_make_args(hardware=True))
    err = capsys.readouterr().err
    assert "boot_kconfig:CONFIG_EXT4_FS" in err
    assert rc == 1


def test_cmd_doctor_hardware_clean_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    _patch_hw(monkeypatch, running_cfg=None)
    rc = doctor.cmd_doctor(_make_args(hardware=True))
    err = capsys.readouterr().err
    assert "no unsupported devices or boot-config gaps detected" in err
    assert rc == 0


def test_cmd_doctor_clean_package(tmp_path, monkeypatch, capsys):
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "cleanpkg", "1.0-1", depends=[], files=["usr/bin/cleanpkg"])
    installed = {"cleanpkg": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    # No .so in files → check_so_files never walks; no ldconfig/subprocess needed.
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")

    rc = doctor.cmd_doctor(_make_args(packages=["cleanpkg"]))
    err = capsys.readouterr().err
    assert "cleanpkg 1.0-1" in err
    assert "clean" in err
    # No findings → no Affected: line
    assert "Affected:" not in err
    assert rc == 0


def test_cmd_doctor_reports_missing_dep(tmp_path, monkeypatch, capsys):
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "brokenpkg", "1.0-1",
            depends=["missinglib>=2.0"], files=[])
    installed = {"brokenpkg": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")

    with patch("sysforge.doctor.subprocess.run",
               side_effect=_pacman_t_mock(["missinglib>=2.0"], 127)):
        rc = doctor.cmd_doctor(_make_args(packages=["brokenpkg"]))

    err = capsys.readouterr().err
    assert "brokenpkg" in err
    assert "[DEPENDS]" in err
    assert "missinglib" in err
    assert "Affected: brokenpkg [repo] (1)" in err
    assert rc == 1


def test_cmd_doctor_not_installed(tmp_path, monkeypatch, capsys):
    db = tmp_path / "local"
    db.mkdir()
    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")

    rc = doctor.cmd_doctor(_make_args(packages=["ghost"]))
    err = capsys.readouterr().err
    assert "ghost" in err
    assert "not installed" in err
    assert rc == 1


def test_cmd_doctor_quiet_hides_clean(tmp_path, monkeypatch, capsys):
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "cleanpkg", "1.0-1", depends=[], files=[])
    installed = {"cleanpkg": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")

    rc = doctor.cmd_doctor(_make_args(packages=["cleanpkg"], quiet=True))
    err = capsys.readouterr().err
    # Clean package header suppressed
    assert "cleanpkg 1.0-1" not in err
    # Summary line still prints
    assert "Scanned" in err
    # No findings → no Affected: line even in quiet mode
    assert "Affected:" not in err
    assert rc == 0


def test_cmd_doctor_affected_line_lists_multiple_packages(tmp_path, monkeypatch, capsys):
    """Two broken targets → summary lists both with per-package counts."""
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "pkga", "1.0-1", depends=["missinga>=1"], files=[])
    _mk_pkg(db, "pkgb", "1.0-1", depends=["missingb>=1"], files=[])
    installed = {"pkga": "1.0-1", "pkgb": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")

    def fake_pacman_t(cmd, **_kw):
        # cmd = ["pacman", "-T", *pkg_specs] — echo the spec back as "missing"
        r = MagicMock()
        r.stdout = "\n".join(cmd[2:]) + "\n"
        r.stderr = ""
        r.returncode = 127
        return r

    with patch("sysforge.doctor.subprocess.run", side_effect=fake_pacman_t):
        rc = doctor.cmd_doctor(_make_args(packages=["pkga", "pkgb"]))

    err = capsys.readouterr().err
    assert "Affected: pkga [repo] (1), pkgb [repo] (1)" in err
    assert rc == 1


def test_cmd_doctor_affected_line_tags_mixed_origins(tmp_path, monkeypatch, capsys):
    """
    Affected summary tags [aur] for foreign and [repo] for native packages.
    Foreign packages with build_state entries get [aur]; foreign without an
    entry get [aur][untracked] (C3).
    """
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "nativepkg", "1.0-1", depends=["missinga>=1"], files=[])
    _mk_pkg(db, "foreignpkg", "1.0-1", depends=["missingb>=1"], files=[])
    installed = {"nativepkg": "1.0-1", "foreignpkg": "1.0-1"}
    foreign = {"foreignpkg": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: foreign)
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")
    # Treat foreignpkg as build_state-tracked so the [untracked] tag is suppressed.
    from sysforge.primitives import build_state as _bs_mod
    monkeypatch.setattr(_bs_mod.BuildState, "all_packages",
                        lambda self: {"foreignpkg": {}})

    def fake_pacman_t(cmd, **_kw):
        r = MagicMock()
        r.stdout = "\n".join(cmd[2:]) + "\n"
        r.returncode = 127
        return r

    with patch("sysforge.doctor.subprocess.run", side_effect=fake_pacman_t):
        doctor.cmd_doctor(_make_args(packages=["nativepkg", "foreignpkg"]))

    err = capsys.readouterr().err
    assert "== nativepkg 1.0-1 [repo] ==" in err
    assert "== foreignpkg 1.0-1 [aur] ==" in err
    assert "Affected: nativepkg [repo] (1), foreignpkg [aur] (1)" in err


def test_cmd_doctor_not_installed_header_has_no_tag(tmp_path, monkeypatch, capsys):
    """A not-installed root reads '(not installed)' with no origin tag."""
    db = tmp_path / "local"
    db.mkdir()
    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")

    doctor.cmd_doctor(_make_args(packages=["ghost"]))
    err = capsys.readouterr().err
    assert "== ghost (not installed) ==" in err
    assert "[repo]" not in err
    assert "[aur]" not in err


def test_cmd_doctor_output_goes_through_log_ui(tmp_path, monkeypatch, capsys):
    """Doctor report lines flow through log.ui → stderr, not stdout."""
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "cleanpkg", "1.0-1", depends=[], files=[])
    installed = {"cleanpkg": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")

    doctor.cmd_doctor(_make_args(packages=["cleanpkg"]))
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "== cleanpkg 1.0-1 [repo] ==" in captured.err
    assert "Scanned 1 package(s)" in captured.err


# ---------------------------------------------------------------------------
# _collect_suggestions — soname extraction from depends + ABI issues
# ---------------------------------------------------------------------------

def test_collect_suggestions_depends_soname(monkeypatch):
    calls: list[tuple[str, bool]] = []

    def fake_suggest(entry, *, lib32=False, run_fn=None, installed_names=None):
        calls.append((entry, lib32))
        return ["core/libcap"]

    monkeypatch.setattr(doctor, "suggest_for_soname", fake_suggest)

    issue = "soname not found in ldconfig: libcap.so=2"
    out = doctor._collect_suggestions("somepkg", [issue], [])

    assert out == {issue: (doctor.SUGGEST_KIND_INSTALL, ["core/libcap"])}
    assert calls == [("libcap.so=2", False)]


def test_collect_suggestions_lib32_context(monkeypatch):
    calls: list[tuple[str, bool]] = []

    def fake_suggest(entry, *, lib32=False, run_fn=None, installed_names=None):
        calls.append((entry, lib32))
        return ["multilib/lib32-foo"]

    monkeypatch.setattr(doctor, "suggest_for_soname", fake_suggest)

    issue = "soname not found in ldconfig: libfoo.so=3"
    out = doctor._collect_suggestions("lib32-somepkg", [issue], [])

    assert out == {issue: (doctor.SUGGEST_KIND_INSTALL, ["multilib/lib32-foo"])}
    assert calls == [("libfoo.so=3", True)]


def test_collect_suggestions_abi_missing_needed(monkeypatch):
    def fake_suggest(entry, *, lib32=False, run_fn=None, installed_names=None):
        assert entry == "libbar.so.5"
        return ["extra/bar"]

    monkeypatch.setattr(doctor, "suggest_for_soname", fake_suggest)

    issue = (
        "libsomething.so.1: NEEDED lib 'libbar.so.5' not found in "
        "ldconfig cache — may not be installed or ldconfig not yet run"
    )
    out = doctor._collect_suggestions("somepkg", [], [issue])
    assert out == {issue: (doctor.SUGGEST_KIND_INSTALL, ["extra/bar"])}


def test_collect_suggestions_skips_unparseable_issues(monkeypatch):
    """A plain `unsatisfied dep:` line isn't sent to pacman -F."""
    sent = []

    def fake_suggest(entry, *, lib32=False, run_fn=None, installed_names=None):
        sent.append(entry)
        return []

    monkeypatch.setattr(doctor, "suggest_for_soname", fake_suggest)

    dep_issue = "unsatisfied dep: glibc>=2.40"
    out = doctor._collect_suggestions("somepkg", [dep_issue], [])

    assert out == {}
    assert sent == []


def test_collect_suggestions_abi_undef_versioned_symbol(monkeypatch):
    """
    For an `undefined versioned symbol` issue, enumerate the broken .so's
    NEEDED sonames and return the packages owning them (deduped).
    """
    monkeypatch.setattr(
        doctor, "needed_sonames",
        lambda path: ["libstdc++.so.6", "libc.so.6"],
    )

    calls: list[tuple[str, bool]] = []

    def fake_suggest(entry, *, lib32=False, run_fn=None, installed_names=None):
        calls.append((entry, lib32))
        return {"libstdc++.so.6": ["core/gcc-libs"],
                "libc.so.6": ["core/glibc"]}[entry]

    monkeypatch.setattr(doctor, "suggest_for_soname", fake_suggest)

    issue = (
        "libbroken.so.1: undefined versioned symbol not found in any "
        "NEEDED lib: sym@FOO_2.0"
    )
    so_path = Path("/usr/lib/libbroken.so.1")
    out = doctor._collect_suggestions(
        "somepkg", [], [issue], so_paths=[so_path],
    )
    assert out == {
        issue: (doctor.SUGGEST_KIND_ABI_DRIFT, ["core/gcc-libs", "core/glibc"])
    }
    assert calls == [("libstdc++.so.6", False), ("libc.so.6", False)]


def test_collect_suggestions_abi_undef_no_so_path_skipped(monkeypatch):
    """If the broken .so isn't in so_paths we can't enumerate NEEDED libs."""
    monkeypatch.setattr(
        doctor, "needed_sonames",
        lambda path: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    issue = (
        "libbroken.so.1: undefined versioned symbol not found in any "
        "NEEDED lib: sym@FOO_2.0"
    )
    out = doctor._collect_suggestions("somepkg", [], [issue], so_paths=[])
    assert out == {}


def test_collect_suggestions_cache_dedupes_repeat_sonames(monkeypatch):
    """
    When the same soname is referenced by multiple issues (and across
    multiple _collect_suggestions calls sharing a cache) it should only
    hit suggest_for_soname once per (soname, lib32, filter_installed) key.
    """
    monkeypatch.setattr(
        doctor, "needed_sonames",
        lambda path: ["libc.so.6", "libstdc++.so.6"],
    )

    calls: list[str] = []

    def fake_suggest(entry, *, lib32=False, run_fn=None, installed_names=None):
        calls.append(entry)
        return {
            "libfoo.so.1": ["core/foo"],
            "libc.so.6": ["core/glibc"],
            "libstdc++.so.6": ["core/gcc-libs"],
        }[entry]

    monkeypatch.setattr(doctor, "suggest_for_soname", fake_suggest)

    dep_issue = "libfoo.so.1: soname not found in ldconfig: libfoo.so.1"
    abi_needed = (
        "/usr/lib/libbroken.so.1: NEEDED lib 'libfoo.so.1' "
        "not found in ldconfig cache (…)"
    )
    abi_undef = (
        "libbroken.so.1: undefined versioned symbol not found in any "
        "NEEDED lib: sym@FOO_2.0"
    )
    so_path = Path("/usr/lib/libbroken.so.1")

    cache: dict[tuple[str, bool, bool], list[str]] = {}
    doctor._collect_suggestions(
        "pkg-a", [dep_issue], [abi_needed, abi_undef],
        so_paths=[so_path], cache=cache,
    )
    # Second package surfaces the same sonames — must not re-query.
    doctor._collect_suggestions(
        "pkg-b", [dep_issue], [abi_needed, abi_undef],
        so_paths=[so_path], cache=cache,
    )

    assert sorted(calls) == ["libc.so.6", "libfoo.so.1", "libstdc++.so.6"]
    # Install-kind lookups (dep_issue, abi_needed) get filter_installed=True;
    # abi_undef enumerates NEEDED libs with filter_installed=False.
    assert cache[("libfoo.so.1", False, True)] == ["core/foo"]
    assert cache[("libc.so.6", False, False)] == ["core/glibc"]
    assert cache[("libstdc++.so.6", False, False)] == ["core/gcc-libs"]


# ---------------------------------------------------------------------------
# A1: installed_names filter + abi_drift partition + FS fallback
# ---------------------------------------------------------------------------

def test_collect_suggestions_filters_installed_for_install_kind(monkeypatch):
    """An install candidate that's already installed should be filtered out."""
    seen_kwargs: dict[str, set[str] | None] = {}

    def fake_suggest(entry, *, lib32=False, run_fn=None, installed_names=None):
        seen_kwargs["installed_names"] = installed_names
        # Real suggest_for_soname would filter; mimic it here.
        cands = ["core/glib2"]
        if installed_names:
            cands = [c for c in cands
                     if c.split("/", 1)[-1] not in installed_names]
        return cands

    monkeypatch.setattr(doctor, "suggest_for_soname", fake_suggest)

    issue = "soname not found in ldconfig: libglib-2.0.so=0"
    out = doctor._collect_suggestions(
        "somepkg", [issue], [],
        installed_names={"glib2"},
    )

    assert seen_kwargs["installed_names"] == {"glib2"}
    assert out == {issue: (doctor.SUGGEST_KIND_INSTALL, [])}


def test_collect_suggestions_partitions_drift_into_rebuild(monkeypatch):
    """
    For an undefined-versioned-symbol finding, candidates that are already
    installed get the REBUILD kind; candidates not installed retain INSTALL
    kind on a separate dict key.
    """
    monkeypatch.setattr(
        doctor, "needed_sonames",
        lambda path: ["libfoo.so.1", "libnew.so.2"],
    )

    def fake_suggest(entry, *, lib32=False, run_fn=None, installed_names=None):
        return {
            "libfoo.so.1": ["core/foo"],   # installed → rebuild
            "libnew.so.2": ["extra/new"],  # not installed → install
        }[entry]

    monkeypatch.setattr(doctor, "suggest_for_soname", fake_suggest)

    issue = (
        "libbroken.so.1: undefined versioned symbol not found in any "
        "NEEDED lib: sym@FOO_2.0"
    )
    so_path = Path("/usr/lib/libbroken.so.1")
    out = doctor._collect_suggestions(
        "somepkg", [], [issue], so_paths=[so_path],
        installed_names={"foo"},
    )

    rebuild_key = issue
    install_key = f"{issue} [missing source]"
    assert out[rebuild_key] == (doctor.SUGGEST_KIND_REBUILD, ["core/foo"])
    assert out[install_key] == (doctor.SUGGEST_KIND_INSTALL, ["extra/new"])


def test_collect_suggestions_drift_all_installed_no_install_entry(monkeypatch):
    """When every drift candidate is installed, only a rebuild entry appears."""
    monkeypatch.setattr(doctor, "needed_sonames", lambda path: ["libfoo.so.1"])

    def fake_suggest(entry, *, lib32=False, run_fn=None, installed_names=None):
        return ["core/foo"]

    monkeypatch.setattr(doctor, "suggest_for_soname", fake_suggest)

    issue = (
        "libbroken.so.1: undefined versioned symbol not found in any "
        "NEEDED lib: sym@FOO_2.0"
    )
    out = doctor._collect_suggestions(
        "somepkg", [], [issue], so_paths=[Path("/usr/lib/libbroken.so.1")],
        installed_names={"foo"},
    )
    assert out == {issue: (doctor.SUGGEST_KIND_REBUILD, ["core/foo"])}


def test_check_depends_filesystem_fallback_when_ldconfig_stale(monkeypatch):
    """
    Soname missing from the ldconfig set but present in the filesystem set
    is treated as satisfied — masks /etc/ld.so.cache lag immediately after
    install. (Overrides conftest's empty-set patch.)
    """
    from sysforge.primitives import dep_analysis as da
    monkeypatch.setattr(
        da, "_filesystem_soname_set",
        lambda lib32=False: frozenset({"libfresh.so.1"}),
    )
    issues = doctor._check_depends(["libfresh.so=1"], set())
    assert issues == []


# ---------------------------------------------------------------------------
# cmd_doctor --suggest — end-to-end rendering + stale-db path
# ---------------------------------------------------------------------------

def test_cmd_doctor_suggest_renders_candidate_line(tmp_path, monkeypatch, capsys):
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "brokenpkg", "1.0-1",
            depends=["libmissing.so=3"], files=[])
    installed = {"brokenpkg": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")
    monkeypatch.setattr(doctor, "files_db_present", lambda: True)
    monkeypatch.setattr(
        doctor, "suggest_for_soname",
        lambda entry, *, lib32=False, run_fn=None, installed_names=None: ["core/missinglib"],
    )

    rc = doctor.cmd_doctor(_make_args(packages=["brokenpkg"], suggest=True))
    err = capsys.readouterr().err

    assert "soname not found in ldconfig: libmissing.so=3" in err
    assert "→ install candidate: core/missinglib" in err
    assert rc == 1


def test_cmd_doctor_suggest_no_candidate_line(tmp_path, monkeypatch, capsys):
    """
    Empty install candidate list — every plausible owner was filtered out
    by the installed_names check (the reported bug: doctor used to
    re-recommend installing already-installed packages). The user-facing
    line points at ldconfig instead of repeating the install suggestion.
    """
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "brokenpkg", "1.0-1",
            depends=["libghost.so=99"], files=[])
    installed = {"brokenpkg": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")
    monkeypatch.setattr(doctor, "files_db_present", lambda: True)
    monkeypatch.setattr(
        doctor, "suggest_for_soname",
        lambda entry, *, lib32=False, run_fn=None, installed_names=None: [],
    )

    doctor.cmd_doctor(_make_args(packages=["brokenpkg"], suggest=True))
    err = capsys.readouterr().err

    assert "all owning packages already installed" in err
    assert "sudo ldconfig" in err


def test_cmd_doctor_suggest_warns_on_stale_files_db(tmp_path, monkeypatch, capsys):
    """--suggest without a synced files db warns and skips lookups."""
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "brokenpkg", "1.0-1",
            depends=["libmissing.so=3"], files=[])
    installed = {"brokenpkg": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")
    monkeypatch.setattr(doctor, "files_db_present", lambda: False)

    # Assert the lookup primitive is never called.
    def fail_if_called(*_a, **_kw):
        raise AssertionError("suggest_for_soname must not run when db is stale")
    monkeypatch.setattr(doctor, "suggest_for_soname", fail_if_called)

    rc = doctor.cmd_doctor(_make_args(packages=["brokenpkg"], suggest=True))
    err = capsys.readouterr().err

    # Issue still reported; no candidate line; exit code unchanged.
    assert "soname not found in ldconfig: libmissing.so=3" in err
    assert "→ install candidate" not in err
    assert "→ ABI-drift candidate" not in err
    assert rc == 1


def test_cmd_doctor_suggest_summary_rollup(tmp_path, monkeypatch, capsys):
    """--suggest emits a per-pkg group *and* a deduped global line at the end."""
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "pkga", "1.0-1", depends=["libshared.so=1"], files=[])
    _mk_pkg(db, "pkgb", "1.0-1", depends=["libshared.so=1", "libuniq.so=2"],
            files=[])
    installed = {"pkga": "1.0-1", "pkgb": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")
    monkeypatch.setattr(doctor, "files_db_present", lambda: True)
    monkeypatch.setattr(
        doctor, "suggest_for_soname",
        lambda entry, *, lib32=False, run_fn=None, installed_names=None: {
            "libshared.so=1": ["core/shared"],
            "libuniq.so=2": ["extra/uniq"],
        }.get(entry, []),
    )

    doctor.cmd_doctor(_make_args(packages=["pkga", "pkgb"], suggest=True))
    err = capsys.readouterr().err

    # Grouped per-pkg lines, preserving per-pkg order
    assert "Suggestions:" in err
    assert "  pkga: install: core/shared" in err
    assert "  pkgb: install: core/shared, extra/uniq" in err
    # Deduped global install line — core/shared appears once
    assert "Install candidates: core/shared, extra/uniq" in err
    # No ABI-drift findings in this test
    assert "ABI-drift candidates" not in err


def test_cmd_doctor_suggest_abi_drift_summary(tmp_path, monkeypatch, capsys):
    """
    An `undefined versioned symbol` finding whose candidate owner is an
    installed *repo* package lands in the REPO_REBUILD bucket — both per-issue
    and in the end-of-run summary — kept separate from install candidates and
    from the actionable foreign-rebuild list.
    """
    db = tmp_path / "local"
    db.mkdir()
    so_rel = "usr/lib/libbroken.so.1"
    _mk_pkg(db, "driftpkg", "1.0-1", depends=[], files=[so_rel])
    # glibc is installed — every real system has it. Since glibc is a repo
    # package (not foreign), the partition routes it to REPO_REBUILD.
    installed = {"driftpkg": "1.0-1", "glibc": "2.40-1"}

    abs_so = tmp_path / so_rel
    abs_so.parent.mkdir(parents=True, exist_ok=True)
    abs_so.write_bytes(b"")

    abi_issue = (
        "libbroken.so.1: undefined versioned symbol not found in any "
        "NEEDED lib: sym@FOO_2.0"
    )

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")
    monkeypatch.setattr(doctor, "files_db_present", lambda: True)
    monkeypatch.setattr(doctor, "check_so_files", lambda so_paths, **_kw: [abi_issue])
    monkeypatch.setattr(doctor, "needed_sonames", lambda p: ["libc.so.6"])
    monkeypatch.setattr(
        doctor, "suggest_for_soname",
        lambda entry, *, lib32=False, run_fn=None, installed_names=None: ["core/glibc"],
    )
    # doctor uses file_root = Path("/"), so point _so_paths_for_pkg at our fake.
    monkeypatch.setattr(
        doctor, "_so_paths_for_pkg",
        lambda pkgname, file_root: [abs_so],
    )

    rc = doctor.cmd_doctor(_make_args(packages=["driftpkg"], suggest=True))
    err = capsys.readouterr().err

    assert "→ repo rebuild candidate (await repo update): core/glibc" in err
    # End-of-run summary
    assert "driftpkg: repo-rebuild: core/glibc" in err
    assert (
        "Repo packages with ABI drift "
        "(await repo update or `sudo pacman -S` to reinstall): core/glibc"
        in err
    )
    # Repo packages must not show up in the actionable foreign-rebuild list.
    assert "Rebuild candidates (foreign; ABI drift):" not in err
    # No install line because the candidate was reclassified as repo-rebuild.
    assert "Install candidates:" not in err
    assert "ABI-drift candidates" not in err
    assert rc == 1


def test_cmd_doctor_suggest_abi_drift_foreign_stays_actionable(
    tmp_path, monkeypatch, capsys,
):
    """
    An `undefined versioned symbol` finding whose candidate owner is an
    installed *foreign* (locally built) package lands in the actionable
    REBUILD bucket — distinct from the REPO_REBUILD informational bucket.
    """
    db = tmp_path / "local"
    db.mkdir()
    so_rel = "usr/lib/libbroken.so.1"
    _mk_pkg(db, "driftpkg", "1.0-1", depends=[], files=[so_rel])
    _mk_pkg(db, "libfoo-git", "1.0-1", depends=[], files=[])
    installed = {"driftpkg": "1.0-1", "libfoo-git": "1.0-1"}

    abs_so = tmp_path / so_rel
    abs_so.parent.mkdir(parents=True, exist_ok=True)
    abs_so.write_bytes(b"")

    abi_issue = (
        "libbroken.so.1: undefined versioned symbol not found in any "
        "NEEDED lib: sym@FOO_2.0"
    )

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(
        pacman_mod, "get_foreign_packages",
        lambda: {"libfoo-git": "1.0-1"},
    )
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")
    monkeypatch.setattr(doctor, "files_db_present", lambda: True)
    monkeypatch.setattr(doctor, "check_so_files", lambda so_paths, **_kw: [abi_issue])
    monkeypatch.setattr(doctor, "needed_sonames", lambda p: ["libfoo.so.1"])
    monkeypatch.setattr(
        doctor, "suggest_for_soname",
        lambda entry, *, lib32=False, run_fn=None, installed_names=None: ["libfoo-git"],
    )
    monkeypatch.setattr(
        doctor, "_so_paths_for_pkg",
        lambda pkgname, file_root: [abs_so],
    )

    rc = doctor.cmd_doctor(_make_args(packages=["driftpkg"], suggest=True))
    err = capsys.readouterr().err

    assert "→ rebuild candidate: libfoo-git" in err
    assert "driftpkg: rebuild: libfoo-git" in err
    assert "Rebuild candidates (foreign; ABI drift): libfoo-git" in err
    # The repo line must not appear when nothing is in that bucket.
    assert "Repo packages with ABI drift" not in err
    assert rc == 1


def test_collect_suggestions_partitions_foreign_vs_repo(monkeypatch):
    """
    When `foreign` is supplied, an undefined-symbol finding splits into:
      * installed AND foreign  → SUGGEST_KIND_REBUILD
      * installed AND non-foreign → SUGGEST_KIND_REPO_REBUILD
      * not installed → SUGGEST_KIND_INSTALL
    Each lands on a distinct dict key so all three render in the per-package
    suggestions output.
    """
    monkeypatch.setattr(
        doctor, "needed_sonames",
        lambda path: ["libfoo.so.1", "libglibc.so.6", "libnew.so.2"],
    )

    def fake_suggest(entry, *, lib32=False, run_fn=None, installed_names=None):
        return {
            "libfoo.so.1": ["aur/libfoo-git"],   # installed + foreign
            "libglibc.so.6": ["core/glibc"],     # installed + repo
            "libnew.so.2": ["extra/new"],        # not installed
        }[entry]

    monkeypatch.setattr(doctor, "suggest_for_soname", fake_suggest)

    issue = (
        "libbroken.so.1: undefined versioned symbol not found in any "
        "NEEDED lib: sym@FOO_2.0"
    )
    out = doctor._collect_suggestions(
        "somepkg", [], [issue], so_paths=[Path("/usr/lib/libbroken.so.1")],
        installed_names={"libfoo-git", "glibc"},
        foreign={"libfoo-git"},
    )

    rebuild_key = issue
    repo_key = f"{issue} [repo]"
    install_key = f"{issue} [missing source]"
    assert out[rebuild_key] == (
        doctor.SUGGEST_KIND_REBUILD, ["aur/libfoo-git"]
    )
    assert out[repo_key] == (
        doctor.SUGGEST_KIND_REPO_REBUILD, ["core/glibc"]
    )
    assert out[install_key] == (
        doctor.SUGGEST_KIND_INSTALL, ["extra/new"]
    )


def test_cmd_doctor_abi_check_skipped_for_vendored_package(
    tmp_path, monkeypatch, capsys,
):
    """
    Packages in abi_check's bundled-binary skip list (`steam` etc.) have
    their ABI pass suppressed with an explanatory one-liner. Depends check
    still runs, and a failing depends is still reported.
    """
    db = tmp_path / "local"
    db.mkdir()
    # Steam with a broken depends — depends issue must still surface.
    _mk_pkg(db, "steam", "1.0-1",
            depends=["libmissing.so=9"], files=["usr/lib/steam/libvendored.so"])
    installed = {"steam": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")

    # check_so_files must NOT be invoked for steam.
    def fail_if_called(*_a, **_kw):
        raise AssertionError("check_so_files must be skipped for steam")
    monkeypatch.setattr(doctor, "check_so_files", fail_if_called)

    rc = doctor.cmd_doctor(_make_args(packages=["steam"]))
    err = capsys.readouterr().err

    assert "[ABI] skipped: vendored prebuilt binaries" in err
    assert "[DEPENDS] 1 issue(s):" in err
    assert "libmissing.so=9" in err
    assert rc == 1


def test_cmd_doctor_all_covers_repo_packages(tmp_path, monkeypatch, capsys):
    """--all scans every installed package (pacman -Q), not just foreign (pacman -Qm)."""
    db = tmp_path / "local"
    db.mkdir()
    # A repo package (not foreign) with a broken dep — must be scanned under --all.
    _mk_pkg(db, "steam", "1.0-1", depends=["missinglib>=1"], files=[])
    installed = {"steam": "1.0-1"}
    foreign: dict[str, str] = {}  # empty — steam is a repo package

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: foreign)
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")
    _patch_axes_clean(monkeypatch)  # --all also runs system axes; keep them quiet

    with patch("sysforge.doctor.subprocess.run",
               side_effect=_pacman_t_mock(["missinglib>=1"], 127)):
        rc = doctor.cmd_doctor(_make_args(all=True))

    err = capsys.readouterr().err
    assert "steam" in err
    assert "Affected: steam [repo] (1)" in err
    assert rc == 1


def test_cmd_doctor_untracked_foreign_gets_untracked_tag(tmp_path, monkeypatch, capsys):
    """C3: foreign package with no build_state entry → [aur][untracked] tag."""
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "wildpkg", "1.0-1", depends=[], files=[])
    installed = {"wildpkg": "1.0-1"}
    foreign = {"wildpkg": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: foreign)
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")
    from sysforge.primitives import build_state as _bs_mod
    monkeypatch.setattr(_bs_mod.BuildState, "all_packages", lambda self: {})

    doctor.cmd_doctor(_make_args(packages=["wildpkg"]))
    err = capsys.readouterr().err
    assert "== wildpkg 1.0-1 [aur][untracked] ==" in err


def test_cmd_doctor_all_includes_foreign_and_native(tmp_path, monkeypatch, capsys):
    """--all includes both foreign and non-foreign packages."""
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "nativepkg", "1.0-1", depends=[], files=[])
    _mk_pkg(db, "foreignpkg", "1.0-1", depends=[], files=[])
    installed = {"nativepkg": "1.0-1", "foreignpkg": "1.0-1"}
    foreign = {"foreignpkg": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: foreign)
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")
    _patch_axes_clean(monkeypatch)  # --all also runs system axes; keep them quiet
    # Treat foreignpkg as build_state-tracked so the bare [aur] tag survives.
    from sysforge.primitives import build_state as _bs_mod
    monkeypatch.setattr(_bs_mod.BuildState, "all_packages",
                        lambda self: {"foreignpkg": {}})

    doctor.cmd_doctor(_make_args(all=True))
    err = capsys.readouterr().err
    assert "== nativepkg 1.0-1 [repo] ==" in err
    assert "== foreignpkg 1.0-1 [aur] ==" in err


def test_cmd_doctor_repo_excludes_foreign(tmp_path, monkeypatch, capsys):
    """--repo walks only non-foreign packages; foreign pkgs must not appear."""
    db = tmp_path / "local"
    db.mkdir()
    _mk_pkg(db, "nativepkg", "1.0-1", depends=[], files=[])
    _mk_pkg(db, "foreignpkg", "1.0-1", depends=[], files=[])
    installed = {"nativepkg": "1.0-1", "foreignpkg": "1.0-1"}
    foreign = {"foreignpkg": "1.0-1"}

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: foreign)
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")

    doctor.cmd_doctor(_make_args(repo=True))
    err = capsys.readouterr().err
    assert "== nativepkg 1.0-1 [repo] ==" in err
    assert "foreignpkg" not in err


# ---------------------------------------------------------------------------
# pacman local-db helpers used by doctor
# ---------------------------------------------------------------------------

def test_collect_hook_findings_reports_missing_and_stale(monkeypatch):
    from sysforge.primitives import pacman_hooks

    a_missing = pacman_hooks.HookArtifact(
        pacman_hooks.HOOK_DEST_DIR / "sysforge-kernel.hook", b"k", 0o644)
    a_stale = pacman_hooks.HookArtifact(
        pacman_hooks.HOOK_DEST_DIR / "sysforge-buildstate.hook", b"b", 0o644)
    a_ok = pacman_hooks.HookArtifact(
        pacman_hooks.HOOK_DEST_DIR / "sysforge-toolchain.hook", b"t", 0o644)
    monkeypatch.setattr(pacman_hooks, "diff_status", lambda: [
        (a_missing, pacman_hooks.STATE_MISSING),
        (a_stale, pacman_hooks.STATE_STALE),
        (a_ok, pacman_hooks.STATE_OK),
    ])

    findings = doctor._collect_hook_findings()
    ids = {f.check_id for f in findings}
    assert ids == {"hook_missing:sysforge-kernel.hook",
                   "hook_stale:sysforge-buildstate.hook"}
    assert all(f.severity == "warn" for f in findings)
    assert all("sysforge setup" in f.remediation for f in findings)


def test_collect_hook_findings_clean(monkeypatch):
    from sysforge.primitives import pacman_hooks

    a_ok = pacman_hooks.HookArtifact(
        pacman_hooks.HOOK_DEST_DIR / "sysforge-kernel.hook", b"k", 0o644)
    monkeypatch.setattr(pacman_hooks, "diff_status",
                        lambda: [(a_ok, pacman_hooks.STATE_OK)])
    assert doctor._collect_hook_findings() == []


def test_pacman_get_package_files_filters_dirs(tmp_path):
    _mk_pkg(tmp_path, "foo", "1.0-1",
            files=["usr/", "usr/bin/", "usr/bin/foo", "usr/lib/libfoo.so.1"])
    files = pacman_mod.get_package_files("foo", root=tmp_path)
    assert "usr/bin/foo" in files
    assert "usr/lib/libfoo.so.1" in files
    # Directory entries dropped
    assert "usr/" not in files
    assert "usr/bin/" not in files


def test_pacman_get_package_depends_parses_section(tmp_path):
    _mk_pkg(tmp_path, "foo", "1.0-1",
            depends=["glibc>=2.40", "libcap.so=2"])
    deps = pacman_mod.get_package_depends("foo", root=tmp_path)
    assert deps == ["glibc>=2.40", "libcap.so=2"]


def test_pacman_get_local_db_entry_exact_match(tmp_path):
    """`llvm` must not match `llvm-libs-22-1`."""
    _mk_pkg(tmp_path, "llvm", "22.1-1")
    _mk_pkg(tmp_path, "llvm-libs", "22.1-1")
    entry = pacman_mod.get_local_db_entry("llvm", root=tmp_path)
    assert entry is not None
    assert entry.name == "llvm-22.1-1"


def test_pacman_get_local_db_entry_missing_returns_none(tmp_path):
    assert pacman_mod.get_local_db_entry("ghost", root=tmp_path) is None


# ---------------------------------------------------------------------------
# B1: doctor --apply bridge
# ---------------------------------------------------------------------------

def _apply_doctor_setup(tmp_path, monkeypatch, *, foreign=True):
    """Stage a single drift finding whose REBUILD candidate is a foreign pkg."""
    db = tmp_path / "local"
    db.mkdir()
    so_rel = "usr/lib/libbroken.so.1"
    _mk_pkg(db, "driftpkg", "1.0-1", depends=[], files=[so_rel])

    abs_so = tmp_path / so_rel
    abs_so.parent.mkdir(parents=True, exist_ok=True)
    abs_so.write_bytes(b"")

    target_pkg = "tracked-foreign"
    installed = {"driftpkg": "1.0-1", target_pkg: "5.0-1"}
    foreign_set = {target_pkg: "5.0-1"} if foreign else {}

    abi_issue = (
        "libbroken.so.1: undefined versioned symbol not found in any "
        "NEEDED lib: sym@FOO_2.0"
    )

    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: foreign_set)
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")
    monkeypatch.setattr(doctor, "files_db_present", lambda: True)
    monkeypatch.setattr(doctor, "check_so_files", lambda so_paths, **_kw: [abi_issue])
    monkeypatch.setattr(doctor, "needed_sonames", lambda p: ["libtarget.so.5"])
    monkeypatch.setattr(
        doctor, "suggest_for_soname",
        lambda entry, *, lib32=False, run_fn=None, installed_names=None: [
            f"core/{target_pkg}",
        ],
    )
    monkeypatch.setattr(
        doctor, "_so_paths_for_pkg",
        lambda pkgname, file_root: [abs_so],
    )
    return target_pkg


def test_apply_dry_run_does_not_invoke_update(tmp_path, monkeypatch, capsys):
    target = _apply_doctor_setup(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr("sysforge.update.cmd_update",
                        lambda args: called.append(args))

    rc = doctor.cmd_doctor(_make_args(
        packages=["driftpkg"], apply=True, dry_run=True, no_confirm=True,
    ))

    assert called == []
    assert rc == 0
    err = capsys.readouterr().err
    assert "would rebuild 1 package(s)" in err
    assert target in err
    assert "(--dry-run: nothing rebuilt)" in err


def test_apply_invokes_cmd_update_with_eligible_pkgnames(tmp_path, monkeypatch, capsys):
    target = _apply_doctor_setup(tmp_path, monkeypatch)
    captured = []
    monkeypatch.setattr("sysforge.update.cmd_update",
                        lambda args: captured.append(args))

    rc = doctor.cmd_doctor(_make_args(
        packages=["driftpkg"], apply=True, no_confirm=True,
    ))

    assert rc == 0
    assert len(captured) == 1
    assert captured[0].pkgnames == [target]
    assert captured[0].dry_run is False
    err = capsys.readouterr().err
    assert "would rebuild 1 package(s)" in err


def test_apply_repo_candidate_only_suggests_pacman(tmp_path, monkeypatch, capsys):
    """
    A drift candidate that is NOT a foreign package is classified as
    REPO_REBUILD upstream — it never reaches the --apply rebuild bridge.
    The user gets the package name and the `sudo pacman -S` hint via the
    upstream "Repo packages with ABI drift" summary line.
    """
    target = _apply_doctor_setup(tmp_path, monkeypatch, foreign=False)
    called = []
    monkeypatch.setattr("sysforge.update.cmd_update",
                        lambda args: called.append(args))

    rc = doctor.cmd_doctor(_make_args(
        packages=["driftpkg"], apply=True, no_confirm=True,
    ))

    # Update is never invoked — candidate is repo-not-foreign so it lands in
    # REPO_REBUILD, not the actionable REBUILD bucket --apply consumes.
    assert called == []
    assert rc == 0
    err = capsys.readouterr().err
    # Upstream summary names the package and points at pacman -S.
    assert (
        "Repo packages with ABI drift "
        "(await repo update or `sudo pacman -S` to reinstall): "
        f"{target}" in err
        or f"core/{target}" in err  # candidate may carry repo/ prefix
    )
    assert "sudo pacman -S" in err
    assert "No eligible rebuild candidates" in err


def test_apply_prompt_decline_skips_rebuild(tmp_path, monkeypatch, capsys):
    _apply_doctor_setup(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr("sysforge.update.cmd_update",
                        lambda args: called.append(args))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")

    rc = doctor.cmd_doctor(_make_args(
        packages=["driftpkg"], apply=True, no_confirm=False,
    ))

    assert called == []
    assert rc == 0
    err = capsys.readouterr().err
    assert "Aborted" in err


def test_apply_prompt_y_invokes_update(tmp_path, monkeypatch, capsys):
    _apply_doctor_setup(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr("sysforge.update.cmd_update",
                        lambda args: called.append(args))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    rc = doctor.cmd_doctor(_make_args(
        packages=["driftpkg"], apply=True, no_confirm=False,
    ))

    assert rc == 0
    assert len(called) == 1


def test_apply_implies_suggest(tmp_path, monkeypatch, capsys):
    """--apply without --suggest still surfaces classified candidates."""
    _apply_doctor_setup(tmp_path, monkeypatch)
    captured = []
    monkeypatch.setattr("sysforge.update.cmd_update",
                        lambda args: captured.append(args))

    rc = doctor.cmd_doctor(_make_args(
        packages=["driftpkg"], apply=True, no_confirm=True, suggest=False,
    ))

    err = capsys.readouterr().err
    # The "rebuild candidate" line proves --suggest was effectively on.
    assert "rebuild candidate" in err
    assert rc == 0
    assert len(captured) == 1


# ---------------------------------------------------------------------------
# cmd_doctor --toolchain
# ---------------------------------------------------------------------------

def test_cmd_doctor_toolchain_mismatch_nonzero_exit(monkeypatch, capsys):
    from sysforge.primitives.llvm_state import ToolchainMismatchFinding
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(
        "sysforge.primitives.llvm_state.detect_toolchain_config_mismatch",
        lambda *a, **k: (
            ToolchainMismatchFinding(
                "toolchain_stock_install", "error",
                "stock repo LLVM is installed", "run sysforge run toolchain"),
        ))

    rc = doctor.cmd_doctor(_make_args(toolchain=True))
    err = capsys.readouterr().err
    assert "no packages to check" not in err
    assert "toolchain checks" in err
    assert "toolchain_stock_install" in err
    assert rc == 1


def test_cmd_doctor_toolchain_clean_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(
        "sysforge.primitives.llvm_state.detect_toolchain_config_mismatch",
        lambda *a, **k: ())

    rc = doctor.cmd_doctor(_make_args(toolchain=True))
    err = capsys.readouterr().err
    assert "toolchain config matches" in err
    assert rc == 0
