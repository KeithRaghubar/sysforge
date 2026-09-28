# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_payload_layout.py — the post-build payload-layout lint (3.2.0-F15).

A package can place config one directory deeper than a non-recursive
consumer ever looks; nothing in the build, `pacman -Qkk`, or the install
notices. The lint names it at build time.
"""
import subprocess
from pathlib import Path

from sysforge.primitives import payload_layout as pl


def test_flags_the_cosmic_greeter_pam_directory():
    """AUR cosmic-greeter-git 4268a07: `install -t .../pam.d/cosmic-greeter/`
    shipped a directory where PAM resolves the flat file
    /etc/pam.d/<service>, so the greeter fell through to pam.d/other."""
    found = pl.nested_members([
        "usr/bin/cosmic-greeter",
        "etc/pam.d/cosmic-greeter/",
        "etc/pam.d/cosmic-greeter/cosmic-greeter.pam",
    ])
    assert [(f.member, f.directory, f.spec) for f in found] == [
        ("etc/pam.d/cosmic-greeter/cosmic-greeter.pam", "etc/pam.d", "pam.d(5)"),
    ]


def test_flat_files_in_non_recursive_dirs_are_fine():
    assert pl.nested_members([
        "etc/pam.d/", "etc/pam.d/cosmic-greeter",
        "usr/lib/sysusers.d/greeter.conf", "usr/lib/tmpfiles.d/greeter.conf",
        "etc/sudoers.d/wheel", "etc/ld.so.conf.d/cuda.conf",
    ]) == []


def test_every_table_directory_is_checked():
    for directory in pl.NON_RECURSIVE_DIRS:
        assert pl.nested_members([f"{directory}/sub/file"]), directory


def test_config_trees_that_legitimately_nest_are_not_flagged():
    """Deliberately not a general /etc opinion."""
    assert pl.nested_members([
        "etc/xdg/autostart/x.desktop", "etc/systemd/system/foo.service.d/o.conf",
        "usr/lib/systemd/system/foo.service",
    ]) == []


def test_check_package_layout_reads_the_archive_listing(tmp_path, monkeypatch):
    pkg = tmp_path / "cosmic-greeter-git-1-1-x86_64.pkg.tar.zst"
    pkg.write_bytes(b"")

    def fake_run(argv, **kw):
        assert argv[:2] == ["bsdtar", "-t"]
        return subprocess.CompletedProcess(
            argv, 0, "etc/pam.d/cosmic-greeter/\netc/pam.d/cosmic-greeter/x.pam\n", "")

    monkeypatch.setattr(pl.subprocess, "run", fake_run)
    [f] = pl.check_package_layout(pkg)
    assert f.member == "etc/pam.d/cosmic-greeter/x.pam"


def test_report_never_raises(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise OSError("bsdtar missing")

    monkeypatch.setattr(pl.subprocess, "run", boom)
    pl.report_payload_layout([tmp_path / "x.pkg.tar.zst"])  # must not raise


def test_standards_row_cites_every_table_directory():
    """Row 28 is the table's citation: the two must name the same set."""
    row = next(
        line for line in (Path(__file__).resolve().parent.parent
                          / "docs/design/21-standards.md").read_text().splitlines()
        if line.startswith("| 28 |")
    )
    for directory, spec in pl.NON_RECURSIVE_DIRS.items():
        assert f"/{directory}" in row and spec in row, directory


# ---------------------------------------------------------------------------
# End-of-run surfacing: the mid-run warn() is -v only, so the finding is
# carried to the summary, which is the ui() home for check results.
# ---------------------------------------------------------------------------

def test_report_records_findings_for_the_end_of_run_summary(tmp_path, monkeypatch):
    pkg = tmp_path / "cosmic-greeter-git-1-1-x86_64.pkg.tar.zst"

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(
            argv, 0, "etc/pam.d/cosmic-greeter/x.pam\n", "")

    monkeypatch.setattr(pl.subprocess, "run", fake_run)
    pl.reset_session()
    pl.report_payload_layout([pkg])
    [line] = pl.session_findings()
    assert pkg.name in line and "etc/pam.d/cosmic-greeter/x.pam" in line
    pl.reset_session()
    assert pl.session_findings() == []


def test_update_summary_renders_a_payload_layout_section():
    from sysforge.update_summary import ResultSummary, _print_result_summary

    lines = []
    _print_result_summary(
        ResultSummary(built_pkgs=["cosmic-greeter-git"],
                      layout_findings=["greeter.pkg: etc/pam.d/x/y is nested …"]),
        emit=lines.append,
    )
    text = "\n".join(lines)
    assert "Payload layout" in text and "etc/pam.d/x/y" in text


def test_build_reports_layout_findings_even_for_one_package(capsys):
    """`build` skips its totals for single-package runs — but not this."""
    from sysforge.build_cmd import _print_layout_findings
    from sysforge.build_core import BuildOutcome

    _print_layout_findings(BuildOutcome(
        built_pkgs=["cosmic-greeter-git"],
        layout_findings=["greeter.pkg: etc/pam.d/x/y is nested …"],
    ))
    assert "etc/pam.d/x/y" in "".join(capsys.readouterr())

