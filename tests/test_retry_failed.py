# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
Retrying what a run could not finish (3.5.0-F23).

A build failure was already recorded in build_state.toml's ``[failures]``
table. A package refused at the review gate (an unreviewed source change on a
run that could not ask, usually unattended) is now recorded there too, with
``kind = "review"``, so it survives the run. ``sysforge build --failed``
rebuilds every recorded entry; a successful build clears it, as before.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_args import cli_args

import sysforge.build_cmd as build_cmd
import sysforge.build_core as build_core
from sysforge.primitives.build_state import BuildState
from sysforge.state_cmd import cmd_state_failed


def _seed(state_dir: Path, pkgbase: str, **fields):
    state_dir.mkdir(parents=True, exist_ok=True)
    bs = BuildState(state_dir)
    bs.record_failure(pkgbase, **fields)
    bs.save()


# ---------------------------------------------------------------------------
# build_state: review refusals are failure entries with kind = "review"
# ---------------------------------------------------------------------------

def test_review_kind_survives_a_save_and_reload(tmp_path):
    _seed(tmp_path, "foo-git", error="unreviewed source change", kind="review")
    entry = BuildState(tmp_path).all_failures()["foo-git"]
    assert entry["kind"] == "review"
    assert entry["error"] == "unreviewed source change"


def test_a_build_failure_carries_no_kind(tmp_path):
    _seed(tmp_path, "foo-git", error="[build_failed] exit 4")
    assert "kind" not in BuildState(tmp_path).all_failures()["foo-git"]


def test_record_review_refusal_persists_it(tmp_path):
    build_core._record_review_refusal(tmp_path, "foo-git", "PKGBUILD changed")
    entry = BuildState(tmp_path).all_failures()["foo-git"]
    assert entry["kind"] == "review"
    assert "PKGBUILD changed" in entry["error"]


# ---------------------------------------------------------------------------
# state failed lists both kinds, telling them apart
# ---------------------------------------------------------------------------

def _state_args(state_dir: Path, **kw):
    base = dict(state_dir=str(state_dir), no_pager=True, clear=None,
                clear_all=False, names=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_state_failed_marks_a_review_refusal(tmp_path, capsys):
    _seed(tmp_path, "foo-git", error="PKGBUILD changed", kind="review")
    _seed(tmp_path, "bar-git", error="[build_failed] exit 4")
    cmd_state_failed(_state_args(tmp_path))
    out = capsys.readouterr().out
    assert "foo-git" in out and "refused at review" in out
    assert "bar-git" in out
    assert "sysforge build --failed" in out


# ---------------------------------------------------------------------------
# build --failed
# ---------------------------------------------------------------------------

def test_build_takes_failed_without_package_names():
    args = cli_args("build", "--failed")
    assert args.failed is True
    assert args.pkgbuilds == []


def test_build_needs_packages_or_failed():
    verb = build_cmd.BuildVerb()
    pre = verb.pre_check(cli_args("build"))
    assert pre.blocker and "--failed" in pre.blocker
    assert pre.exit_code == 2


def test_failed_adds_every_recorded_entry(tmp_path):
    _seed(tmp_path, "foo-git", error="PKGBUILD changed", kind="review")
    _seed(tmp_path, "bar-git", error="[build_failed] exit 4")
    args = cli_args("build", "--failed", "--state-dir", str(tmp_path))
    build_cmd._add_recorded_failures(args)
    assert args.pkgbuilds == ["bar-git", "foo-git"]


def test_failed_keeps_named_packages_and_dedups(tmp_path):
    _seed(tmp_path, "foo-git", error="[build_failed] exit 4")
    args = cli_args("build", "foo-git", "baz", "--failed", "--state-dir", str(tmp_path))
    build_cmd._add_recorded_failures(args)
    assert args.pkgbuilds == ["foo-git", "baz"]


def test_failed_with_nothing_recorded_is_a_clean_no_op(tmp_path, capsys):
    verb = build_cmd.BuildVerb()
    pre = verb.pre_check(cli_args("build", "--failed", "--state-dir", str(tmp_path)))
    assert pre.blocker is None
    assert pre.skip_reason is not None
    cap = capsys.readouterr()
    assert "nothing to retry" in cap.out + cap.err


# ---------------------------------------------------------------------------
# The update summary points at it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["failed_pkgs", "review_refused"])
def test_update_summary_suggests_build_failed(field):
    from sysforge.update_summary import ResultSummary, _print_result_summary
    summary = ResultSummary()
    if field == "failed_pkgs":
        summary.failed_pkgs = ["foo-git"]
    else:
        summary.review_refused = {"foo-git": "PKGBUILD changed"}
    lines = []
    _print_result_summary(summary, emit=lines.append)
    assert "sysforge build --failed" in "\n".join(lines)
