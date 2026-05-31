"""
test_diagnostics.py — the unified Finding framework.

Covers severity normalisation (incl. the "warning" → "warn" fold), the
adapters from the existing probe dataclasses, error-count reduction,
exception-isolated axis running, and the rendered output format.
"""
from __future__ import annotations

from types import SimpleNamespace

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
