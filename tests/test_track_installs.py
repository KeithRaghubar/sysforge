"""
test_track_installs.py — coverage for `sysforge build -i` auto-tracking.

Verifies that an install-flag build (`-i` / `--install`) appends the main
package(s) to packages.toml, mirroring the behavior of `sysforge packages
add`. Also covers the shared `_classify_and_build_entries` helper that
backs both `cmd_packages_add` and `append_explicit_entries`.

Regression note: `sysforge update` and `sysforge converge` strip
`-i`/`--install` via `pacman.BATCH_STRIP_FLAGS` before invoking the build
runner, so the auto-track behavior is reachable only from `_cmd_build`.
Test `test_batch_strip_flags_includes_install` guards against accidental
removal.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from sysforge.packages_cmd import (
    _classify_and_build_entries,
    append_explicit_entries,
    cmd_packages_add,
)
from sysforge.primitives.makepkg_wrapper import INSTALL_FLAGS
from sysforge.primitives.pacman import BATCH_STRIP_FLAGS


# ---------------------------------------------------------------------------
# _classify_and_build_entries (shared by add + auto-track)
# ---------------------------------------------------------------------------

def test_classify_repo_and_aur(monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.aur.repo_packages",
        lambda _names: {"htop"},
    )
    monkeypatch.setattr(
        "sysforge.primitives.aur.aur_info",
        lambda _names: {"yay-bin": {}},
    )
    entries, unknown = _classify_and_build_entries(["htop", "yay-bin"], {})
    assert unknown == []
    by_name = {e["name"]: e for e in entries}
    assert by_name["htop"]["source"] == "repo"
    assert by_name["yay-bin"]["source"] == "aur"
    assert "reason" not in by_name["htop"]
    assert "reason" not in by_name["yay-bin"]


def test_classify_unknown_returned_separately(monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.aur.repo_packages", lambda _names: set()
    )
    monkeypatch.setattr(
        "sysforge.primitives.aur.aur_info", lambda _names: {}
    )
    entries, unknown = _classify_and_build_entries(["nonexistent"], {})
    assert entries == []
    assert unknown == ["nonexistent"]


def test_classify_reason_kwarg_propagates(monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.aur.repo_packages", lambda _names: {"foo"}
    )
    monkeypatch.setattr(
        "sysforge.primitives.aur.aur_info", lambda _names: {}
    )
    entries, _ = _classify_and_build_entries(["foo"], {}, reason="dependency")
    assert entries[0]["reason"] == "dependency"


def test_classify_pkgbuild_patch_inferred(monkeypatch, tmp_path):
    pkg_dir = tmp_path / "myaur"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(
        "pkgname=myaur\npkgver=1.0\npkgrel=1\narch=(x86_64)\n"
    )
    monkeypatch.setattr(
        "sysforge.primitives.aur.repo_packages", lambda _names: set()
    )
    monkeypatch.setattr(
        "sysforge.primitives.aur.aur_info", lambda _names: {"myaur": {}}
    )
    monkeypatch.setattr(
        "sysforge.primitives.pkgbuild_patcher.extract_pkgbuild_profile",
        lambda meta, path: {"flags": "..."},
    )
    entries, _ = _classify_and_build_entries(
        ["myaur"], {"pkgbuild_src_dir": str(tmp_path)}
    )
    assert entries[0]["pkgbuild_patch"] is True


# ---------------------------------------------------------------------------
# append_explicit_entries — the new public helper
# ---------------------------------------------------------------------------

def _patch_classifier(monkeypatch, sources: dict[str, str]):
    """Patch the aur module so the classifier returns deterministic results."""
    repo_set = {n for n, s in sources.items() if s == "repo"}
    aur_dict = {n: {} for n, s in sources.items() if s == "aur"}
    monkeypatch.setattr(
        "sysforge.primitives.aur.repo_packages", lambda names: repo_set & set(names)
    )
    monkeypatch.setattr(
        "sysforge.primitives.aur.aur_info",
        lambda names: {k: v for k, v in aur_dict.items() if k in names},
    )


def _read_packages(path: Path) -> list[dict]:
    import tomllib
    with open(path, "rb") as f:
        return tomllib.load(f).get("package", [])


def test_append_explicit_writes_entry(tmp_path, monkeypatch):
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text("[build]\npkgbuild_src_dir = \"~/src\"\n")
    _patch_classifier(monkeypatch, {"htop": "repo"})

    added = append_explicit_entries(["htop"], packages_file=str(pkg_file))
    assert added == ["htop"]
    entries = _read_packages(pkg_file)
    assert len(entries) == 1
    assert entries[0] == {"name": "htop", "source": "repo"}
    # Confirm no `reason` key was written.
    assert "reason" not in pkg_file.read_text().splitlines()[-1]


def test_append_explicit_idempotent(tmp_path, monkeypatch):
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text(
        "[build]\npkgbuild_src_dir = \"~/src\"\n\n"
        "[[package]]\nname = \"htop\"\nsource = \"repo\"\n"
    )
    _patch_classifier(monkeypatch, {"htop": "repo"})

    added = append_explicit_entries(["htop"], packages_file=str(pkg_file))
    assert added == []
    assert len(_read_packages(pkg_file)) == 1


def test_append_explicit_skips_existing_dependency_entry(tmp_path, monkeypatch):
    """A name already tracked as a dep is treated as present — no duplicate."""
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text(
        "[[package]]\nname = \"libfoo\"\nsource = \"aur\"\nreason = \"dependency\"\n"
    )
    _patch_classifier(monkeypatch, {"libfoo": "aur"})

    added = append_explicit_entries(["libfoo"], packages_file=str(pkg_file))
    assert added == []
    entries = _read_packages(pkg_file)
    assert len(entries) == 1
    assert entries[0]["reason"] == "dependency"


def test_append_explicit_unknown_name_warns_and_skips(tmp_path, monkeypatch):
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text("[build]\npkgbuild_src_dir = \"~/src\"\n")
    _patch_classifier(monkeypatch, {})

    added = append_explicit_entries(["ghost-pkg"], packages_file=str(pkg_file))
    assert added == []
    assert _read_packages(pkg_file) == []


def test_append_explicit_multi_pkg(tmp_path, monkeypatch):
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text("[build]\npkgbuild_src_dir = \"~/src\"\n")
    _patch_classifier(monkeypatch, {"a": "repo", "b": "aur", "c": "repo"})

    added = append_explicit_entries(["a", "b", "c"], packages_file=str(pkg_file))
    assert sorted(added) == ["a", "b", "c"]
    by_name = {e["name"]: e for e in _read_packages(pkg_file)}
    assert by_name["a"]["source"] == "repo"
    assert by_name["b"]["source"] == "aur"
    assert by_name["c"]["source"] == "repo"


def test_append_explicit_creates_file_when_missing(tmp_path, monkeypatch):
    pkg_file = tmp_path / "packages.toml"
    assert not pkg_file.exists()
    _patch_classifier(monkeypatch, {"htop": "repo"})

    added = append_explicit_entries(["htop"], packages_file=str(pkg_file))
    assert added == ["htop"]
    assert pkg_file.exists()
    assert _read_packages(pkg_file)[0]["name"] == "htop"


def test_append_explicit_skips_when_parent_dir_missing(tmp_path, monkeypatch):
    """Implicit bookkeeping must not materialise new directories."""
    pkg_file = tmp_path / "nope" / "packages.toml"
    _patch_classifier(monkeypatch, {"htop": "repo"})

    added = append_explicit_entries(["htop"], packages_file=str(pkg_file))
    assert added == []
    assert not pkg_file.parent.exists()


def test_append_explicit_swallows_write_errors(tmp_path, monkeypatch):
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text("[build]\npkgbuild_src_dir = \"~/src\"\n")
    _patch_classifier(monkeypatch, {"htop": "repo"})

    def boom(*a, **kw):
        raise PermissionError("read-only")
    monkeypatch.setattr("builtins.open", boom)

    # Must not raise — best-effort bookkeeping.
    added = append_explicit_entries(["htop"], packages_file=str(pkg_file))
    assert added == []


# ---------------------------------------------------------------------------
# cmd_packages_add still works after refactor (regression guard)
# ---------------------------------------------------------------------------

def test_cmd_packages_add_after_refactor(tmp_path, monkeypatch, capsys):
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text("[build]\npkgbuild_src_dir = \"~/src\"\n")
    _patch_classifier(monkeypatch, {"htop": "repo"})

    args = SimpleNamespace(pkgs=["htop"], packages=str(pkg_file))
    cmd_packages_add(args)
    entries = _read_packages(pkg_file)
    assert len(entries) == 1
    assert entries[0]["name"] == "htop"
    assert entries[0]["source"] == "repo"


def test_cmd_packages_add_unknown_name_exits_nonzero(tmp_path, monkeypatch):
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text("[build]\npkgbuild_src_dir = \"~/src\"\n")
    _patch_classifier(monkeypatch, {})

    args = SimpleNamespace(pkgs=["ghost"], packages=str(pkg_file))
    with pytest.raises(SystemExit) as ei:
        cmd_packages_add(args)
    assert ei.value.code == 1
    assert _read_packages(pkg_file) == []


def test_classify_helper_matches_packages_add_output(tmp_path, monkeypatch):
    """The shared helper must produce the same entry shape as the path
    `cmd_packages_add` writes — guards against drift between call sites."""
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text("[build]\npkgbuild_src_dir = \"~/src\"\n")
    _patch_classifier(monkeypatch, {"htop": "repo", "yay-bin": "aur"})

    args = SimpleNamespace(pkgs=["htop", "yay-bin"], packages=str(pkg_file))
    cmd_packages_add(args)
    via_add = sorted(_read_packages(pkg_file), key=lambda e: e["name"])

    pkg_file2 = tmp_path / "packages2.toml"
    pkg_file2.write_text("[build]\npkgbuild_src_dir = \"~/src\"\n")
    append_explicit_entries(["htop", "yay-bin"], packages_file=str(pkg_file2))
    via_explicit = sorted(_read_packages(pkg_file2), key=lambda e: e["name"])

    assert via_add == via_explicit


# ---------------------------------------------------------------------------
# Cross-module guards: install-flag detection + batch-strip set
# ---------------------------------------------------------------------------

def test_install_flags_constant_contents():
    assert INSTALL_FLAGS == frozenset({"-i", "--install"})


def test_batch_strip_flags_includes_install():
    """Update/converge must strip install flags so the auto-track behavior
    only fires from `sysforge build`. Regression guard."""
    assert "-i" in BATCH_STRIP_FLAGS
    assert "--install" in BATCH_STRIP_FLAGS
    assert INSTALL_FLAGS <= BATCH_STRIP_FLAGS


# ---------------------------------------------------------------------------
# _cmd_build integration — install flag triggers append; absence does not
# ---------------------------------------------------------------------------

def _build_args(pkg: str, makepkg_flags: str | None, packages_file: str | None):
    return SimpleNamespace(
        pkgbuilds=[pkg],
        makepkg=makepkg_flags,
        no_pkg_log=False, log_dir=None,
        interactive=False, persist_log=False,
        profile_conf=None, cc=None, cxx=None, ld=None,
        cache_report=False, abi_check=False,
        no_update=True, cleansrc=False,
        state_dir=None, track_deps=False,
        packages=packages_file,
    )


def test_cmd_build_with_install_flag_appends_entry(tmp_path, monkeypatch):
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text("[build]\npkgbuild_src_dir = \"~/src\"\n")
    pkgbuild = tmp_path / "src" / "htop" / "PKGBUILD"
    pkgbuild.parent.mkdir(parents=True)
    pkgbuild.write_text("pkgname=htop\npkgver=1\npkgrel=1\narch=(x86_64)\n")

    monkeypatch.setattr("sysforge.cli.find_pkgbuild", lambda pkg, cfg: pkgbuild)
    monkeypatch.setattr("sysforge.cli.load_config", lambda: {})
    monkeypatch.setattr("sysforge.cli.resolve_aur_deps", lambda *a, **kw: [], raising=False)
    monkeypatch.setattr(
        "sysforge.primitives.aur_resolve.resolve_aur_deps", lambda *a, **kw: []
    )
    monkeypatch.setattr("sysforge.cli.run", lambda *a, **kw: None)
    _patch_classifier(monkeypatch, {"htop": "repo"})

    from sysforge.cli import _cmd_build
    args = _build_args("htop", "-i", str(pkg_file))
    _cmd_build(args)

    entries = _read_packages(pkg_file)
    assert len(entries) == 1
    assert entries[0]["name"] == "htop"


def test_cmd_build_without_install_flag_does_not_append(tmp_path, monkeypatch):
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text("[build]\npkgbuild_src_dir = \"~/src\"\n")
    pkgbuild = tmp_path / "src" / "htop" / "PKGBUILD"
    pkgbuild.parent.mkdir(parents=True)
    pkgbuild.write_text("pkgname=htop\npkgver=1\npkgrel=1\narch=(x86_64)\n")

    monkeypatch.setattr("sysforge.cli.find_pkgbuild", lambda pkg, cfg: pkgbuild)
    monkeypatch.setattr("sysforge.cli.load_config", lambda: {})
    monkeypatch.setattr(
        "sysforge.primitives.aur_resolve.resolve_aur_deps", lambda *a, **kw: []
    )
    monkeypatch.setattr("sysforge.cli.run", lambda *a, **kw: None)

    # Failsafe: even if classifier is consulted, no entry should be written.
    sentinel_called = []
    monkeypatch.setattr(
        "sysforge.packages_cmd.append_explicit_entries",
        lambda *a, **kw: sentinel_called.append(True) or [],
    )

    from sysforge.cli import _cmd_build
    args = _build_args("htop", None, str(pkg_file))
    _cmd_build(args)

    assert sentinel_called == []
    assert _read_packages(pkg_file) == []


def test_cmd_build_failure_skips_append(tmp_path, monkeypatch):
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text("[build]\npkgbuild_src_dir = \"~/src\"\n")
    pkgbuild = tmp_path / "src" / "htop" / "PKGBUILD"
    pkgbuild.parent.mkdir(parents=True)
    pkgbuild.write_text("pkgname=htop\npkgver=1\npkgrel=1\narch=(x86_64)\n")

    monkeypatch.setattr("sysforge.cli.find_pkgbuild", lambda pkg, cfg: pkgbuild)
    monkeypatch.setattr("sysforge.cli.load_config", lambda: {})
    monkeypatch.setattr(
        "sysforge.primitives.aur_resolve.resolve_aur_deps", lambda *a, **kw: []
    )

    def boom(*a, **kw):
        raise RuntimeError("build failed")
    monkeypatch.setattr("sysforge.cli.run", boom)

    sentinel_called = []
    monkeypatch.setattr(
        "sysforge.packages_cmd.append_explicit_entries",
        lambda *a, **kw: sentinel_called.append(True) or [],
    )

    from sysforge.cli import _cmd_build
    args = _build_args("htop", "-i", str(pkg_file))
    # _cmd_build's RuntimeError handler calls _log.fatal which calls sys.exit
    with pytest.raises(SystemExit):
        _cmd_build(args)
    assert sentinel_called == []
