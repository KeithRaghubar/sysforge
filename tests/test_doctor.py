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
        packages=[], graphics=False, all=False,
        shallow=False, quiet=False, config={},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_cmd_doctor_no_targets_prints_usage_exits_2(capsys):
    rc = doctor.cmd_doctor(_make_args())
    assert rc == 2
    err = capsys.readouterr().err
    assert "no packages to check" in err


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
    out = capsys.readouterr().out
    assert "cleanpkg 1.0-1" in out
    assert "clean" in out
    # No findings → no Affected: line
    assert "Affected:" not in out
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

    out = capsys.readouterr().out
    assert "brokenpkg" in out
    assert "[DEPENDS]" in out
    assert "missinglib" in out
    assert "Affected: brokenpkg (1)" in out
    assert rc == 1


def test_cmd_doctor_not_installed(tmp_path, monkeypatch, capsys):
    db = tmp_path / "local"
    db.mkdir()
    monkeypatch.setattr(pacman_mod, "_LOCAL_DB_ROOT", db)
    monkeypatch.setattr(pacman_mod, "get_all_installed_packages", lambda: {})
    monkeypatch.setattr(pacman_mod, "get_foreign_packages", lambda: {})
    monkeypatch.setattr(doctor, "_default_ldconfig_fn", lambda: "")

    rc = doctor.cmd_doctor(_make_args(packages=["ghost"]))
    out = capsys.readouterr().out
    assert "ghost" in out
    assert "not installed" in out
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
    out = capsys.readouterr().out
    # Clean package header suppressed
    assert "cleanpkg 1.0-1" not in out
    # Summary line still prints
    assert "Scanned" in out
    # No findings → no Affected: line even in quiet mode
    assert "Affected:" not in out
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

    out = capsys.readouterr().out
    assert "Affected: pkga (1), pkgb (1)" in out
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

    with patch("sysforge.doctor.subprocess.run",
               side_effect=_pacman_t_mock(["missinglib>=1"], 127)):
        rc = doctor.cmd_doctor(_make_args(all=True))

    out = capsys.readouterr().out
    assert "steam" in out
    assert "Affected: steam (1)" in out
    assert rc == 1


# ---------------------------------------------------------------------------
# pacman local-db helpers used by doctor
# ---------------------------------------------------------------------------

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
