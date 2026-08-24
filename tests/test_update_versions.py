# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_update_versions.py — the ``sysforge update --versions`` report (3.1.0-F6).

Covers the renderer in isolation (pure: results in, lines out) plus the CLI
wiring that reaches it. The report answers "what newer versions are available"
without entering a build loop or a pipeline stage, and it always includes
stage-owned (toolchain/kernel) packages, which the normal walk skips.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.update_result import _UpdateResult
from sysforge.update_summary import render_versions_report


def _r(pkgbase, action, installed=None, available=None):
    return _UpdateResult(
        pkgbase=pkgbase,
        pkgnames=[pkgbase],
        action=action,
        installed_ver=installed,
        pkgbuild_ver=available,
        pkgbuild_path=None,
    )


def _render(results, stage_owned=(), **kw):
    lines = []
    render_versions_report(
        results, list(stage_owned), emit=lines.append, **kw
    )
    return lines


_NON_ROW_MARKERS = ("PACKAGE", "checked", "Nothing to update")


def _body(lines):
    """Just the package rows — no header, no footer, no hint, no blanks."""
    return [
        ln for ln in lines
        if ln.strip()
        and not any(m in ln for m in _NON_ROW_MARKERS)
        and not ln.strip().startswith("(")
    ]


# ---------------------------------------------------------------------------
# Row selection: actionable only
# ---------------------------------------------------------------------------

def test_rebuild_row_shows_both_versions():
    lines = _render([_r("mesa", "NEEDS_REBUILD", "1.0-1", "1.1-1")])
    row = next(ln for ln in lines if "mesa" in ln)
    assert "1.0-1" in row
    assert "1.1-1" in row


def test_up_to_date_packages_are_not_listed():
    """The whole point of the flag: no wall of unchanged packages."""
    results = [
        _r("mesa", "NEEDS_REBUILD", "1.0-1", "1.1-1"),
        _r("zlib", "UP_TO_DATE", "1.3-1", "1.3-1"),
    ]
    assert not any("zlib" in ln for ln in _body(_render(results)))


def test_up_to_date_still_counted_in_footer():
    results = [
        _r("mesa", "NEEDS_REBUILD", "1.0-1", "1.1-1"),
        _r("zlib", "UP_TO_DATE", "1.3-1", "1.3-1"),
    ]
    footer = next(ln for ln in _render(results) if "checked" in ln)
    assert "2 checked" in footer
    assert "1 up to date" in footer


def test_pacman_upgrade_is_actionable():
    lines = _body(_render([_r("curl", "NEEDS_PACMAN_UPGRADE", "8.0-1", "8.1-1")]))
    assert any("curl" in ln for ln in lines)


def test_downgrade_is_listed_and_marked():
    lines = _render([_r("jack_capture", "DOWNGRADE", "0.9.73-8", "0.9.73post1-1")])
    row = next(ln for ln in lines if "jack_capture" in ln)
    assert "downgrade" in row.lower()


@pytest.mark.parametrize("action", ["PULL_FAILED", "RATE_LIMITED", "PURGE_REFUSED"])
def test_check_failures_are_not_reported_as_available(action):
    """A failed check is not an available version — it must not fake a row."""
    lines = _body(_render([_r("brave-bin", action, "1.0-1", None)]))
    assert not any("brave-bin" in ln for ln in lines)


# ---------------------------------------------------------------------------
# Devel packages: unresolved without --devel
# ---------------------------------------------------------------------------

def test_devel_unresolved_gets_no_row():
    """Without --devel there is no upstream version, so there is nothing to
    report. On a devel-heavy system these outnumber real answers ~80:1 and
    would recreate the wall of noise the flag exists to replace."""
    lines = _render([_r("cosmic-comp-git", "DEVEL", "1.0.r12-1", None)])
    assert not _body(lines)


def test_devel_unresolved_never_fabricates_a_version():
    lines = _render([_r("cosmic-comp-git", "DEVEL", "1.0.r12-1", None)])
    assert not any("None" in ln for ln in lines)


def test_devel_unresolved_not_counted_as_available():
    footer = next(
        ln for ln in _render([_r("c-git", "DEVEL", "1-1", None)]) if "checked" in ln
    )
    assert "0 available" in footer


def test_devel_hint_emitted_when_unresolved():
    lines = _render([_r("cosmic-comp-git", "DEVEL", "1.0.r12-1", None)])
    assert any("--devel" in ln for ln in lines)


def test_devel_hint_emitted_even_when_nothing_else_is_available():
    """The hint is the only trace of devel packages, so the empty-table
    branch must carry it too."""
    lines = _render([
        _r("zlib", "UP_TO_DATE", "1.3-1", "1.3-1"),
        _r("c-git", "DEVEL", "1-1", None),
    ])
    assert any("--devel" in ln for ln in lines)


def test_devel_hint_absent_when_no_devel_packages():
    lines = _render([_r("mesa", "NEEDS_REBUILD", "1.0-1", "1.1-1")])
    assert not any("--devel" in ln for ln in lines)


def test_resolved_devel_reports_a_real_version():
    lines = _render(
        [_r("cosmic-comp-git", "NEEDS_REBUILD", "1.0.r12-1", "1.0.r19-1")],
        devel_resolved=True,
    )
    row = next(ln for ln in lines if "cosmic-comp-git" in ln)
    assert "1.0.r19-1" in row


# ---------------------------------------------------------------------------
# Stage-owned packages — the original question this flag exists to answer
# ---------------------------------------------------------------------------

def test_stage_owned_rows_are_included():
    lines = _body(_render([], stage_owned=[("llvm", "21.1.3-1", "21.1.4-1", "toolchain")]))
    assert any("llvm" in ln for ln in lines)


def test_stage_owned_row_is_annotated_with_its_stage():
    lines = _render([], stage_owned=[("llvm", "21.1.3-1", "21.1.4-1", "toolchain")])
    row = next(ln for ln in lines if "llvm" in ln)
    assert "toolchain" in row


def test_kernel_and_toolchain_both_annotated():
    lines = _render([], stage_owned=[
        ("llvm", "21.1.3-1", "21.1.4-1", "toolchain"),
        ("linux-sysforge", "6.17.2-1", "6.17.4-1", "kernel"),
    ])
    assert any("toolchain" in ln for ln in lines if "llvm" in ln)
    assert any("kernel" in ln for ln in lines if "linux-sysforge" in ln)


def test_stage_owned_counted_as_available():
    footer = next(
        ln for ln in _render([], stage_owned=[("llvm", "1-1", "2-1", "toolchain")])
        if "checked" in ln
    )
    assert "1 available" in footer


# ---------------------------------------------------------------------------
# Empty / clean state
# ---------------------------------------------------------------------------

def test_nothing_available_says_so_explicitly():
    lines = _render([_r("zlib", "UP_TO_DATE", "1.3-1", "1.3-1")])
    assert any("up to date" in ln.lower() for ln in lines)
    assert not _body(lines)


def test_empty_input_does_not_crash():
    assert _render([]) is not None


# ---------------------------------------------------------------------------
# Column alignment
# ---------------------------------------------------------------------------

def test_columns_align_across_varied_name_lengths():
    results = [
        _r("a", "NEEDS_REBUILD", "1-1", "2-1"),
        _r("a-very-long-package-name-indeed", "NEEDS_REBUILD", "1-1", "2-1"),
    ]
    rows = _body(_render(results))
    starts = [ln.index("1-1") for ln in rows]
    assert len(set(starts)) == 1, f"INSTALLED column not aligned: {rows}"


# ---------------------------------------------------------------------------
# CLI wiring — the report is reachable and exits before any build
#
# The renderer tests above are pure; these drive the real cmd_update so a
# regression in the early-exit seam (wrong argument, wrong order, build loop
# still entered) is caught rather than only the formatting.
# ---------------------------------------------------------------------------

from types import SimpleNamespace


def _versions_args(**kw):
    defaults = dict(
        state_dir=None,
        dry_run=False,
        devel=False,
        offline=True,        # pure local version check
        no_pkg_log=True,
        persist_log=False,
        log_dir=None,
        profile_conf=None,
        cache_report=False,
        packages=None,
        versions=True,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


_PKGBUILD = """
pkgname={name}
pkgver={ver}
pkgrel=1
arch=('x86_64')
"""


def test_versions_exits_before_building_anything(update_scenario, capsys):
    """--versions is a report: an out-of-date package must NOT be built."""
    update_scenario.add_pkg("mesa", _PKGBUILD.format(name="mesa", ver="2.0"))
    update_scenario.record("mesa", "1.0", "1")
    update_scenario.run(
        _versions_args(), installed={"mesa": "1.0-1"}, foreign={"mesa": "1.0-1"}
    )
    assert update_scenario.builds == [], "--versions must not enter the build loop"


def test_versions_reports_the_available_version(update_scenario, capsys):
    update_scenario.add_pkg("mesa", _PKGBUILD.format(name="mesa", ver="2.0"))
    update_scenario.record("mesa", "1.0", "1")
    update_scenario.run(
        _versions_args(), installed={"mesa": "1.0-1"}, foreign={"mesa": "1.0-1"}
    )
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "mesa" in combined
    assert "2.0-1" in combined


def test_versions_includes_stage_owned_packages(update_scenario, capsys):
    """The question the flag exists for: a toolchain package the normal walk
    skips still shows up here, annotated with its owning stage."""
    update_scenario.add_pkg("llvm", _PKGBUILD.format(name="llvm", ver="21.1.4"))
    update_scenario.record("llvm", "21.1.3", "1", owner_stage="toolchain")
    # offline=False: the stage-owned advisory is deliberately skipped offline.
    update_scenario.run(
        _versions_args(offline=False),
        installed={"llvm": "21.1.3-1"}, foreign={"llvm": "21.1.3-1"},
    )
    combined = "".join(capsys.readouterr())
    # Assert on a real table row, not incidental narration mentioning either word.
    row = [ln for ln in combined.splitlines()
           if "llvm" in ln and "toolchain" in ln and "21.1.4-1" in ln]
    assert row, f"no annotated stage-owned row in:\n{combined}"
    assert update_scenario.builds == []
