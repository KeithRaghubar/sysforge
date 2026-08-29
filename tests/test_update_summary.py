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


def test_pacman_upgrade_shows_version_deltas(capsys, monkeypatch):
    """F3: repo packages pulled in by pacman -Syu render old → new per line,
    reusing the same version-pair machinery as the source-built section."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    s = _empty(
        pacman_upgrade_pkgs=["glibc", "nano"],
        versions={"glibc": ("2.39-1", "2.40-1"), "nano": ("7.2-1", "8.0-1")},
    )
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "glibc: 2.39-1 → 2.40-1" in out
    assert "nano: 7.2-1 → 8.0-1" in out


def test_pacman_upgrade_unknown_version_falls_back_to_bare_name(capsys, monkeypatch):
    """F3: a pacman package with no known version pair still lists by name."""
    monkeypatch.setenv("NO_COLOR", "1")
    s = _empty(pacman_upgrade_pkgs=["glibc"], versions={})
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "glibc" in out
    assert "→" not in out


# ---------------------------------------------------------------------------
# 3.0.0-F4: flag-triggered system upgrade — variant presentation
# ---------------------------------------------------------------------------

def test_system_upgrade_renders_without_package_list(capsys, monkeypatch):
    """A `--sysupgrade` run has no classified pacman-class package list, so the
    Pacman-Syu block reports the transaction itself rather than per-package
    lines."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    s = _empty(system_upgrade_ran=True)
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "system upgraded" in out
    assert "Pacman-Syu:" in out
    assert "system upgrade (pacman resolved the transaction)" in out


def test_system_upgrade_failure_label(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    s = _empty(system_upgrade_ran=True, pacman_upgrade_failed=True)
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "Pacman-Syu (transaction FAILED):" in out
    assert "(pacman -Syu FAILED)" in out


def test_classified_list_wins_over_system_upgrade_flag(capsys, monkeypatch):
    """When the walk did classify pacman-class packages, the per-package block
    is what renders — the variant presentation is the no-list case only."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    s = _empty(pacman_upgrade_pkgs=["firefox"], system_upgrade_ran=True,
               versions={"firefox": ("130.0-1", "131.0-1")})
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "1 pacman-upgraded" in out
    assert "firefox" in out
    assert "pacman resolved the transaction" not in out


# ---------------------------------------------------------------------------
# 3.0.0-F5: flag-triggered system upgrade — itemized version changes
# ---------------------------------------------------------------------------

def test_system_upgrade_itemizes_captured_version_changes(capsys, monkeypatch):
    """When the capture read the transaction back, the block renders one
    `pkg: old → new` line per change instead of the opaque fallback."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    s = _empty(system_upgrade_ran=True, sysupgrade_changes={
        "mesa": ("24.0-1", "24.1-1"),
        "firefox": ("130.0-1", "131.0-1"),
    })
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "firefox: 130.0-1 → 131.0-1" in out
    assert "mesa: 24.0-1 → 24.1-1" in out
    assert "pacman resolved the transaction" not in out
    assert "2 system-upgraded" in out


def test_system_upgrade_marks_additions_and_removals(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    s = _empty(system_upgrade_ran=True, sysupgrade_changes={
        "newdep": (None, "1.0-1"),
        "dropped": ("2.0-1", None),
    })
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "newdep: (new) 1.0-1" in out
    assert "dropped: 2.0-1 (removed)" in out


def test_system_upgrade_falls_back_when_capture_is_empty(capsys, monkeypatch):
    """No captured changes — offline, disabled, or a failed transaction with
    nothing to read back — keeps the honest single-line fallback."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    s = _empty(system_upgrade_ran=True, sysupgrade_changes={})
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "system upgrade (pacman resolved the transaction)" in out


def test_system_upgrade_block_is_capped_at_default_verbosity(capsys, monkeypatch):
    """A stock -Syu can be hundreds of packages, unlike the bounded managed
    list, so the default view caps the block and points at -v."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr("sysforge.log.get_verbosity", lambda: 0)
    changes = {f"pkg{i:03d}": (f"{i}-1", f"{i}-2") for i in range(25)}
    s = _empty(system_upgrade_ran=True, sysupgrade_changes=changes)
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "pkg000: 0-1 → 0-2" in out
    assert "pkg024" not in out
    assert "and 15 more" in out
    assert "-v" in out


def test_system_upgrade_block_is_full_at_verbose(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr("sysforge.log.get_verbosity", lambda: 1)
    changes = {f"pkg{i:03d}": (f"{i}-1", f"{i}-2") for i in range(25)}
    s = _empty(system_upgrade_ran=True, sysupgrade_changes=changes)
    _print_result_summary(s)
    out = capsys.readouterr().out
    assert "pkg024: 24-1 → 24-2" in out
    assert "more" not in out
