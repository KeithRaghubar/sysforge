# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""Tests for the end-of-run result summary renderer (F2/F32/F33/F38)."""

from sysforge.update_summary import ResultSummary, _print_result_summary


def _empty(**over) -> ResultSummary:
    base = dict(
        built_pkgs=[], failed_pkgs=[], pacman_upgrade_pkgs=[],
        installed_deps=[], pgo_skipped_pkgs=[], cleansrc_failures=[],
        install_only=False, pacman_upgrade_failed=False, skipped=0,
        versions={}, stage_owned_updates=[],
    )
    base.update(over)
    return ResultSummary(**base)


def test_emit_sink_receives_all_lines(monkeypatch):
    """The renderer routes every line through the injected `emit` sink, so
    update.py can mirror the summary into the unified log via _log.ui."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    lines: list[str] = []
    s = _empty(built_pkgs=["mesa"], versions={"mesa": ("24.0", "24.1")},
               installed_deps=["libfoo"])
    _print_result_summary(s, emit=lines.append)
    joined = "\n".join(lines)
    assert any("Update complete" in ln for ln in lines)
    assert "mesa: 24.0 → 24.1" in joined
    assert "libfoo" in joined


def test_built_renders_version_arrow(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    s = _empty(built_pkgs=["mesa"], versions={"mesa": ("24.0", "24.1")})
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "mesa: 24.0 → 24.1" in out


def test_built_missing_version_falls_back_to_bare_name(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    s = _empty(built_pkgs=["mesa"], versions={})
    _print_result_summary(s)
    assert "mesa" in capsys.readouterr().out


def test_arrow_degrades_to_ascii_under_term_linux(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "linux")
    s = _empty(built_pkgs=["mesa"], versions={"mesa": ("24.0", "24.1")})
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "24.0 -> 24.1" in out
    assert "→" not in out


def test_header_counts(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    s = _empty(built_pkgs=["a", "b"], failed_pkgs=["c"], skipped=4)
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "2 built" in out
    assert "1 failed" in out
    assert "4 skipped" in out


def test_install_only_uses_installed_label(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    s = _empty(built_pkgs=["a"], install_only=True)
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "1 installed" in out
    assert "Installed:" in out


def test_dependencies_section(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    s = _empty(built_pkgs=["a"], installed_deps=["libfoo", "libbar"])
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "Dependencies:" in out
    assert "libfoo" in out and "libbar" in out


def test_dependencies_section_omitted_when_empty(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    s = _empty(built_pkgs=["a"])
    _print_result_summary(s)
    assert "Dependencies:" not in capsys.readouterr().out


def test_stage_owned_advisory(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    s = _empty(stage_owned_updates=[("linux-custom", "6.9", "6.10", "kernel")])
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "Stage-owned updates available:" in out
    assert "linux-custom" in out
    assert "6.9 → 6.10" in out
    assert "run kernel" in out


def test_stage_owned_advisory_omitted_when_empty(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    s = _empty(built_pkgs=["a"])
    _print_result_summary(s)
    assert "Stage-owned updates available:" not in capsys.readouterr().out


def test_pacman_upgrade_failed_marker(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    s = _empty(pacman_upgrade_pkgs=["glibc"], pacman_upgrade_failed=True)
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "glibc" in out
    assert "FAILED" in out
