# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for completions_cmd.CompletionsVerb — the shell-completion data sink.

Each ``args.resource`` branch reads a source (config dirs, packages.toml, the
build state, ``makepkg --help``, or the repo+AUR universe) and prints candidate
names one per line for the ``_sysforge`` completion script. These assert the
observable stdout the completion script consumes."""
import subprocess
import types


from sysforge.completions_cmd import CompletionsVerb
from sysforge.verbs import PreCheckResult


def _lines(capsys) -> list[str]:
    return [ln for ln in capsys.readouterr().out.splitlines() if ln]


def _exec(resource: str) -> None:
    CompletionsVerb().execute(
        types.SimpleNamespace(resource=resource), PreCheckResult()
    )


def _mkpkgdir(base, name):
    d = base / name
    d.mkdir()
    (d / "PKGBUILD").write_text("# pkgbuild\n", encoding="utf-8")
    return d


def test_completions_local_lists_only_pkgbuild_dirs(monkeypatch, tmp_path, capsys):
    _mkpkgdir(tmp_path, "foo")
    _mkpkgdir(tmp_path, "bar")
    (tmp_path / "baz").mkdir()  # no PKGBUILD → excluded
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")  # not a dir → excluded
    monkeypatch.setattr(
        "sysforge.completions_cmd.load_config",
        lambda: {"paths": {"pkgbuild_src_dir": str(tmp_path)}},
    )

    _exec("local")

    assert _lines(capsys) == ["bar", "foo"]  # sorted, PKGBUILD dirs only


def test_completions_local_empty_when_dir_missing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "sysforge.completions_cmd.load_config",
        lambda: {"paths": {"pkgbuild_src_dir": str(tmp_path / "nope")}},
    )

    _exec("local")

    assert _lines(capsys) == []


def test_completions_manifest_lists_package_names(monkeypatch, tmp_path, capsys):
    manifest = tmp_path / "packages.toml"
    manifest.write_text(
        '[[package]]\nname = "mesa"\n\n[[package]]\nname = "linux-custom"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("sysforge.completions_cmd.load_config", lambda: {})
    monkeypatch.setattr(
        "sysforge.completions_cmd.resolve_packages_path", lambda _cfg: manifest
    )

    _exec("manifest")

    assert set(_lines(capsys)) == {"mesa", "linux-custom"}


def test_completions_manifest_silent_when_file_absent(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("sysforge.completions_cmd.load_config", lambda: {})
    monkeypatch.setattr(
        "sysforge.completions_cmd.resolve_packages_path",
        lambda _cfg: tmp_path / "missing.toml",
    )

    _exec("manifest")

    assert _lines(capsys) == []


def test_completions_makepkg_flags_parses_help_and_excludes_meta(monkeypatch, capsys):
    help_text = (
        "  -s, --syncdeps       Install missing dependencies with pacman\n"
        "  -f, --force          Overwrite existing package\n"
        "      --nocolor        Disable colorized output\n"
        "  -h, --help           Show this help message and exit\n"
    )
    monkeypatch.setattr("sysforge.completions_cmd.load_config", lambda: {})
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout=help_text, stderr=""
        ),
    )

    _exec("makepkg-flags")

    out = _lines(capsys)
    assert "-s:Install missing dependencies with pacman" in out
    assert "--syncdeps:Install missing dependencies with pacman" in out
    assert "-f:Overwrite existing package" in out
    # excluded meta flags never appear
    assert not any(ln.startswith(("-h:", "--help:", "--nocolor:")) for ln in out)


def test_completions_state_lists_build_state_packages(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("sysforge.completions_cmd.load_config", lambda: {})
    monkeypatch.setattr(
        "sysforge.pipeline.state.resolve_state_dir", lambda _x: (tmp_path, None)
    )

    class _FakeBuildState:
        def __init__(self, _state_dir):
            pass

        def all_packages(self):
            return {"zlib-custom", "mesa"}

    monkeypatch.setattr(
        "sysforge.primitives.build_state.BuildState", _FakeBuildState
    )

    _exec("state")

    assert _lines(capsys) == ["mesa", "zlib-custom"]  # sorted


def test_completions_default_universe_dedups_across_sources(
    monkeypatch, tmp_path, capsys
):
    # local dir contributes "foo"; pacman contributes "foo" (dup) + "bar";
    # AUR cache contributes "baz" + "foo" (dup). Expect each name once, in
    # source order: local, then pacman, then AUR.
    _mkpkgdir(tmp_path, "foo")
    monkeypatch.setattr(
        "sysforge.completions_cmd.load_config",
        lambda: {"paths": {"pkgbuild_src_dir": str(tmp_path)}},
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout="foo\nbar\n", stderr=""
        ),
    )
    aur_cache = tmp_path / "aur-cache.txt"
    aur_cache.write_text("baz\nfoo\n", encoding="utf-8")
    monkeypatch.setattr("sysforge.primitives.aur.AUR_CACHE_PATH", aur_cache)

    _exec("default")

    assert _lines(capsys) == ["foo", "bar", "baz"]


def test_completions_default_survives_pacman_failure(monkeypatch, tmp_path, capsys):
    _mkpkgdir(tmp_path, "foo")
    monkeypatch.setattr(
        "sysforge.completions_cmd.load_config",
        lambda: {"paths": {"pkgbuild_src_dir": str(tmp_path)}},
    )
    # non-zero pacman → its output is skipped, local names still emitted
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="err"),
    )
    monkeypatch.setattr(
        "sysforge.primitives.aur.AUR_CACHE_PATH", tmp_path / "no-cache.txt"
    )

    _exec("default")

    assert _lines(capsys) == ["foo"]
