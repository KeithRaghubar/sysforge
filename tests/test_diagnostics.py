"""
test_diagnostics.py — the unified Finding framework.

Covers severity normalisation (incl. the "warning" → "warn" fold), the
adapters from the existing probe dataclasses, error-count reduction,
exception-isolated axis running, and the rendered output format.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


from sysforge import log
from sysforge.primitives import diagnostics as diag


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

def test_normalize_severity_folds_warning_alias():
    assert diag.normalize_severity("warning") == diag.SEV_WARN
    assert diag.normalize_severity("warn") == diag.SEV_WARN
    assert diag.normalize_severity("error") == diag.SEV_ERROR
    assert diag.normalize_severity("info") == diag.SEV_INFO


def test_normalize_severity_unknown_and_none_default_to_warn():
    assert diag.normalize_severity(None) == diag.SEV_WARN
    assert diag.normalize_severity("") == diag.SEV_WARN
    assert diag.normalize_severity("bogus") == diag.SEV_WARN


def test_severity_rank_ordering():
    assert diag.severity_rank("error") > diag.severity_rank("warn")
    assert diag.severity_rank("warn") > diag.severity_rank("info")


def test_finding_is_error_includes_brick():
    assert diag.Finding("x", diag.SEV_ERROR, "id", "m").is_error
    assert not diag.Finding("x", diag.SEV_WARN, "id", "m").is_error
    # A brick warning still counts as an error for the exit code.
    assert diag.Finding("x", diag.SEV_WARN, "id", "m", is_brick=True).is_error


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

def test_adapt_generic_finding_shape():
    src = SimpleNamespace(severity="error", check_id="cid", message="boom",
                          remediation="fix it")
    f = diag.adapt("hardware", src)
    assert (f.category, f.severity, f.check_id, f.message, f.remediation) == \
        ("hardware", diag.SEV_ERROR, "cid", "boom", "fix it")
    assert not f.is_brick


def test_adapt_carries_is_brick_and_folds_warning():
    src = SimpleNamespace(severity="warning", check_id="cid", message="m",
                          remediation="", is_brick=True)
    f = diag.adapt("kernel", src)
    assert f.severity == diag.SEV_WARN
    assert f.is_brick


def test_from_toolchain_check_failing_is_error_with_fix():
    chk = SimpleNamespace(name="cc:clang", ok=False, detail="clang cannot run",
                          fix_cmd="sudo pacman -S clang", auto_remediable=False)
    f = diag.from_toolchain_check(chk)
    assert f.severity == diag.SEV_ERROR
    assert f.check_id == "cc:clang"
    assert f.fix_cmd == "sudo pacman -S clang"
    assert f.remediation == "sudo pacman -S clang"


def test_from_toolchain_check_passing_is_info():
    chk = SimpleNamespace(name="cmake", ok=True, detail="ok",
                          fix_cmd=None, auto_remediable=False)
    assert diag.from_toolchain_check(chk).severity == diag.SEV_INFO


def test_from_fix_suggestion_is_warn_with_fix():
    s = SimpleNamespace(signature="rust:E0463", message="missing std",
                        fix_cmd="rustup target add ...")
    f = diag.from_fix_suggestion(s)
    assert f.severity == diag.SEV_WARN
    assert f.check_id == "rust:E0463"
    assert f.fix_cmd == "rustup target add ..."


# ---------------------------------------------------------------------------
# Reductions
# ---------------------------------------------------------------------------

def test_error_count_counts_error_and_brick_only():
    findings = [
        diag.Finding("a", diag.SEV_ERROR, "1", "m"),
        diag.Finding("a", diag.SEV_WARN, "2", "m"),
        diag.Finding("a", diag.SEV_WARN, "3", "m", is_brick=True),
        diag.Finding("a", diag.SEV_INFO, "4", "m"),
    ]
    assert diag.error_count(findings) == 2


# ---------------------------------------------------------------------------
# Axis running
# ---------------------------------------------------------------------------

def test_run_axes_isolates_exceptions():
    def boom() -> list[diag.Finding]:
        raise RuntimeError("probe blew up")

    ok_axis = diag.Axis("ok", "ok checks",
                        lambda: [diag.Finding("ok", diag.SEV_INFO, "x", "fine")])
    bad_axis = diag.Axis("bad", "bad checks", boom)

    results = diag.run_axes([ok_axis, bad_axis])
    assert [f.check_id for f in results["ok"]] == ["x"]
    # The raising axis degrades to a single WARN, never propagating.
    assert len(results["bad"]) == 1
    assert results["bad"][0].severity == diag.SEV_WARN
    assert results["bad"][0].check_id == "bad:probe_error"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_render_axis_clean(capsys):
    logger = log.get_logger("TEST")
    rc = diag.render_axis(logger, "hardware checks", [],
                          clean_msg="all good")
    out = capsys.readouterr().err
    assert "== hardware checks ==" in out
    assert "all good" in out
    assert rc == 0


def test_render_axis_clean_quiet_suppresses_message(capsys):
    logger = log.get_logger("TEST")
    diag.render_axis(logger, "hardware checks", [], clean_msg="all good",
                     quiet=True)
    out = capsys.readouterr().err
    assert "== hardware checks ==" in out
    assert "all good" not in out


def test_render_axis_emits_findings_and_counts_errors(capsys):
    logger = log.get_logger("TEST")
    findings = [
        diag.Finding("hw", diag.SEV_WARN, "warn_id", "a warning", "do x"),
        diag.Finding("hw", diag.SEV_ERROR, "err_id", "an error", "do y"),
    ]
    rc = diag.render_axis(logger, "hardware checks", findings)
    out = capsys.readouterr().err
    assert "[ERROR] err_id: an error" in out
    assert "→ do y" in out
    assert "[WARN] warn_id: a warning" in out
    assert "hardware checks: 2 finding(s), 1 error(s)." in out
    # Most-severe first: the error line precedes the warning line.
    assert out.index("err_id") < out.index("warn_id")
    assert rc == 1


def test_render_axis_colours_severity_when_enabled(capsys, monkeypatch):
    monkeypatch.setattr(log, "_COLOR_MODE", "always")
    logger = log.get_logger("TEST")
    findings = [
        diag.Finding("hw", diag.SEV_ERROR, "err_id", "an error", "do y"),
        diag.Finding("hw", diag.SEV_WARN, "warn_id", "a warning"),
    ]
    diag.render_axis(logger, "hardware checks", findings)
    out = capsys.readouterr().err
    # ERROR token wrapped in red, WARN in yellow; the remediation arrow in green.
    assert f"{log._ANSI_RED}ERROR{log._ANSI_RESET}" in out
    assert f"{log._ANSI_YELLOW}WARN{log._ANSI_RESET}" in out
    assert f"{log._ANSI_GREEN}→{log._ANSI_RESET}" in out


def test_color_severity_plain_when_disabled(monkeypatch):
    monkeypatch.setattr(log, "_COLOR_MODE", "never")
    assert diag._color_severity(diag.SEV_ERROR) == "ERROR"


# ---------------------------------------------------------------------------
# Finding.subject and grouped rendering
# ---------------------------------------------------------------------------


def test_finding_subject_defaults_to_empty():
    f = diag.Finding("abi", diag.SEV_WARN, "abi:x", "msg")
    assert f.subject == ""


def test_render_axis_grouped_groups_by_subject(capsys):
    logger = log.get_logger("TEST")
    findings = [
        diag.Finding("abi", diag.SEV_INFO, "abi:a", "info a",
                     subject="mesa 25.1"),
        diag.Finding("abi", diag.SEV_ERROR, "abi:b", "err b",
                     subject="cosmic-comp-git 1.0"),
        diag.Finding("abi", diag.SEV_WARN, "abi:c", "warn c",
                     subject="mesa 25.1"),
    ]
    errors = diag.render_axis(logger, "ABI & depends",
                              findings, grouped=True)
    out = capsys.readouterr().err
    assert errors == 1
    # Groups ordered by worst severity: cosmic-comp-git (ERROR) before mesa (WARN).
    assert out.index("cosmic-comp-git 1.0") < out.index("mesa 25.1")
    # Within a group, severity descending: warn c before info a.
    assert out.index("warn c") < out.index("info a")
    # Each finding sits under its own group header, not re-sorted globally.
    assert out.index("cosmic-comp-git 1.0") < out.index("err b") < out.index("mesa 25.1")


def test_render_axis_ungrouped_is_unchanged(capsys):
    logger = log.get_logger("TEST")
    findings = [
        diag.Finding("abi", diag.SEV_INFO, "abi:a", "info a",
                     subject="mesa 25.1"),
        diag.Finding("abi", diag.SEV_ERROR, "abi:b", "err b",
                     subject="cosmic-comp-git 1.0"),
    ]
    diag.render_axis(logger, "ABI", findings)
    out = capsys.readouterr().err
    # Default mode ignores subject entirely — flat, severity-sorted.
    assert "mesa 25.1" not in out
    assert out.index("err b") < out.index("info a")


# ---------------------------------------------------------------------------
# Roster (3.1.0-F1)
# ---------------------------------------------------------------------------

@pytest.fixture
def at_verbosity():
    saved = log.get_verbosity()
    yield log.set_verbosity
    log.set_verbosity(saved)


def test_record_files_skip_ran_and_finding():
    roster = diag.Roster()
    finding = diag.Finding("g", diag.SEV_WARN, "b", "bad")
    assert diag.record(roster, "a", None) is None
    assert diag.record(roster, "b", finding) is finding
    assert diag.record(roster, "c", diag.Skip("tool missing")) is None
    assert roster.ran == ["a", "b"]
    assert roster.skipped == [("c", "tool missing")]


def test_roster_merge_appends_both_lists():
    a = diag.Roster(ran=["x"], skipped=[("y", "why")])
    a.merge(diag.Roster(ran=["z"], skipped=[("w", "because")]))
    assert a.ran == ["x", "z"]
    assert a.skipped == [("y", "why"), ("w", "because")]


def test_run_axis_normalises_list_and_axisresult():
    roster = diag.Roster(ran=["k"])
    f = diag.Finding("ok", diag.SEV_INFO, "x", "fine")
    bare = diag.run_axis(diag.Axis("bare", "bare", lambda: [f]))
    full = diag.run_axis(diag.Axis("full", "full", lambda: diag.AxisResult([f], roster)))
    assert bare.findings == [f] and bare.roster is None
    assert full.findings == [f] and full.roster is roster


def test_run_axis_raising_axis_has_no_roster():
    def boom():
        raise RuntimeError("x")
    res = diag.run_axis(diag.Axis("bad", "bad", boom))
    assert [x.check_id for x in res.findings] == ["bad:probe_error"]
    assert res.roster is None


def test_run_axes_still_returns_finding_lists():
    f = diag.Finding("ok", diag.SEV_INFO, "x", "fine")
    out = diag.run_axes([diag.Axis("a", "a", lambda: diag.AxisResult([f], diag.Roster()))])
    assert out == {"a": [f]}


def test_render_axis_roster_hidden_below_vv(capsys, at_verbosity):
    at_verbosity(1)
    roster = diag.Roster(ran=["a"], skipped=[("b", "no gpu")])
    diag.render_axis(log.get_logger("TEST"), "g", [], clean_msg="ok", roster=roster)
    out = capsys.readouterr().err
    assert "ok" in out
    assert "ran:" not in out and "skipped:" not in out


def test_render_axis_roster_shown_at_vv_on_clean_axis(capsys, at_verbosity):
    at_verbosity(2)
    roster = diag.Roster(ran=["a", "c"], skipped=[("b", "no gpu")])
    diag.render_axis(log.get_logger("TEST"), "g", [], clean_msg="ok", roster=roster)
    out = capsys.readouterr().err
    assert "ran:     a, c" in out
    assert "skipped: b (no gpu)" in out


def test_render_axis_roster_shown_at_vv_with_findings(capsys, at_verbosity):
    at_verbosity(2)
    f = diag.Finding("g", diag.SEV_WARN, "a", "bad")
    diag.render_axis(log.get_logger("TEST"), "g", [f],
                     roster=diag.Roster(ran=["a"]))
    out = capsys.readouterr().err
    assert "[WARN] a: bad" in out or "a: bad" in out
    assert "ran:     a" in out
    assert "skipped:" not in out  # empty list omitted


def test_render_axis_unreported_roster_only_at_vvv(capsys, at_verbosity):
    at_verbosity(2)
    diag.render_axis(log.get_logger("TEST"), "g", [], clean_msg="ok", roster=None)
    assert "not reported" not in capsys.readouterr().err
    at_verbosity(3)
    diag.render_axis(log.get_logger("TEST"), "g", [], clean_msg="ok", roster=None)
    assert "roster: not reported by this axis" in capsys.readouterr().err


def test_render_axis_roster_is_quiet_at_default(quiet_at_default):
    roster = diag.Roster(ran=["a"], skipped=[("b", "no gpu")])
    err_v0, err_v2 = quiet_at_default(
        lambda: diag.render_axis(log.get_logger("TEST"), "g", [], clean_msg="ok",
                                 roster=roster))
    assert "ran:" in err_v2
