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
        packages=[], graphics=False, all=False, repo=False,
        shallow=False, quiet=False, suggest=False, config={},
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
    assert "Affected: brokenpkg (1)" in err
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
    assert "Affected: pkga (1), pkgb (1)" in err
    assert rc == 1


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
    assert "== cleanpkg 1.0-1 ==" in captured.err
    assert "Scanned 1 package(s)" in captured.err


# ---------------------------------------------------------------------------
# _collect_suggestions — soname extraction from depends + ABI issues
# ---------------------------------------------------------------------------

def test_collect_suggestions_depends_soname(monkeypatch):
    calls: list[tuple[str, bool]] = []

    def fake_suggest(entry, *, lib32=False, run_fn=None):
        calls.append((entry, lib32))
        return ["core/libcap"]

    monkeypatch.setattr(doctor, "suggest_for_soname", fake_suggest)

    issue = "soname not found in ldconfig: libcap.so=2"
    out = doctor._collect_suggestions("somepkg", [issue], [])

    assert out == {issue: ["core/libcap"]}
    assert calls == [("libcap.so=2", False)]


def test_collect_suggestions_lib32_context(monkeypatch):
    calls: list[tuple[str, bool]] = []

    def fake_suggest(entry, *, lib32=False, run_fn=None):
        calls.append((entry, lib32))
        return ["multilib/lib32-foo"]

    monkeypatch.setattr(doctor, "suggest_for_soname", fake_suggest)

    issue = "soname not found in ldconfig: libfoo.so=3"
    out = doctor._collect_suggestions("lib32-somepkg", [issue], [])

    assert out == {issue: ["multilib/lib32-foo"]}
    assert calls == [("libfoo.so=3", True)]


def test_collect_suggestions_abi_missing_needed(monkeypatch):
    def fake_suggest(entry, *, lib32=False, run_fn=None):
        assert entry == "libbar.so.5"
        return ["extra/bar"]

    monkeypatch.setattr(doctor, "suggest_for_soname", fake_suggest)

    issue = (
        "libsomething.so.1: NEEDED lib 'libbar.so.5' not found in "
        "ldconfig cache — may not be installed or ldconfig not yet run"
    )
    out = doctor._collect_suggestions("somepkg", [], [issue])
    assert out == {issue: ["extra/bar"]}


def test_collect_suggestions_skips_unparseable_issues(monkeypatch):
    """A plain `unsatisfied dep:` line isn't sent to pacman -F."""
    sent = []

    def fake_suggest(entry, *, lib32=False, run_fn=None):
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

    def fake_suggest(entry, *, lib32=False, run_fn=None):
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
    assert out == {issue: ["core/gcc-libs", "core/glibc"]}
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
    hit suggest_for_soname once per (soname, lib32) key.
    """
    monkeypatch.setattr(
        doctor, "needed_sonames",
        lambda path: ["libc.so.6", "libstdc++.so.6"],
    )

    calls: list[str] = []

    def fake_suggest(entry, *, lib32=False, run_fn=None):
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

    cache: dict[tuple[str, bool], list[str]] = {}
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
    assert cache[("libfoo.so.1", False)] == ["core/foo"]
    assert cache[("libc.so.6", False)] == ["core/glibc"]


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
        lambda entry, *, lib32=False, run_fn=None: ["core/missinglib"],
    )

    rc = doctor.cmd_doctor(_make_args(packages=["brokenpkg"], suggest=True))
    err = capsys.readouterr().err

    assert "soname not found in ldconfig: libmissing.so=3" in err
    assert "→ provided by: core/missinglib" in err
    assert rc == 1


def test_cmd_doctor_suggest_no_candidate_line(tmp_path, monkeypatch, capsys):
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
        lambda entry, *, lib32=False, run_fn=None: [],
    )

    doctor.cmd_doctor(_make_args(packages=["brokenpkg"], suggest=True))
    err = capsys.readouterr().err

    assert "→ provided by: no candidate in files db" in err


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
    assert "→ provided by" not in err
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
        lambda entry, *, lib32=False, run_fn=None: {
            "libshared.so=1": ["core/shared"],
            "libuniq.so=2": ["extra/uniq"],
        }.get(entry, []),
    )

    doctor.cmd_doctor(_make_args(packages=["pkga", "pkgb"], suggest=True))
    err = capsys.readouterr().err

    # Grouped per-pkg lines, preserving per-pkg order
    assert "Suggestions:" in err
    assert "  pkga: core/shared" in err
    assert "  pkgb: core/shared, extra/uniq" in err
    # Deduped global line — core/shared appears once
    assert "Suggested packages: core/shared, extra/uniq" in err


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

    err = capsys.readouterr().err
    assert "steam" in err
    assert "Affected: steam (1)" in err
    assert rc == 1


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

    doctor.cmd_doctor(_make_args(all=True))
    err = capsys.readouterr().err
    assert "== nativepkg 1.0-1 ==" in err
    assert "== foreignpkg 1.0-1 ==" in err


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
    assert "== nativepkg 1.0-1 ==" in err
    assert "foreignpkg" not in err


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
