#!/usr/bin/env python3
"""
Tests for sysforge.build_cmd — the ``build`` verb's presentation layer.

The engine itself is covered by test_build_core.py; here we cover the
verb-side summary: multi-package runs end with an update-style totals block,
single-package runs stay quiet.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sysforge.build_cmd import _print_build_summary
from sysforge.build_core import BuildOutcome


def test_summary_lists_built_and_failed(capsys):
    outcome = BuildOutcome(
        built_pkgs=["vulkan-icd-loader-git", "vulkan-utility-libraries-git"],
        failed_pkgs=["vulkan-validation-layers-git"],
    )
    _print_build_summary(outcome)
    out = capsys.readouterr().err
    assert "Build complete: 2 built, 1 failed." in out
    assert "Built:       vulkan-icd-loader-git vulkan-utility-libraries-git" in out
    assert "Failed:      vulkan-validation-layers-git" in out
    assert "Skipped:" not in out
    assert "PGO-skipped:" not in out


def test_summary_marks_install_failure_and_optional_sections(capsys):
    outcome = BuildOutcome(
        built_pkgs=["foo"],
        failed_pkgs=[],
        review_skipped=["bar"],
        pgo_skipped_pkgs=["baz"],
        install_failed=True,
    )
    _print_build_summary(outcome)
    out = capsys.readouterr().err
    assert (
        "Build complete: 1 built, 0 failed, 1 skipped at review, "
        "1 pgo-skipped (install FAILED)." in out
    )
    assert "Skipped:     bar (PKGBUILD review)" in out
    assert "PGO-skipped: baz" in out


def test_summary_all_failed(capsys):
    outcome = BuildOutcome(failed_pkgs=["foo", "bar"])
    _print_build_summary(outcome)
    out = capsys.readouterr().err
    assert "Build complete: 0 built, 2 failed." in out
    assert "Built:" not in out
