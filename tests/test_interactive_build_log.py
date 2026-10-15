"""
test_interactive_build_log.py — an interactive build's failure reason reaches
the logs (3.2.0-F19).

Interactive makepkg used to inherit sysforge's stdio, so neither the unified
run log nor the per-package log recorded why it failed. It now runs under
run_with_pty's two-way proxy: the whole stream is teed into the per-package
log, and the failure tail is repeated under [BUILD] (both logs) and fed to the
build diagnosis.
"""
import subprocess

import pytest

from sysforge import log
from sysforge.primitives import build_diag, makepkg_invoke


@pytest.fixture
def logs(tmp_path):
    unified, pkg = tmp_path / "sysforge-update.log", tmp_path / "sysforge_foo.log"
    log.open_unified_log(unified, purge=True)
    log.open_pkg_log(pkg)
    yield unified, pkg
    # success=False keeps the content (success truncates).
    log.close_pkg_log(success=False)
    log.close_unified_log(success=False)


def _failing_build(monkeypatch, lines, rc=4):
    def _fake_pty(cmd, *, line_callback, **kw):
        assert kw["interactive"] is True
        for line in lines:
            line_callback(line)
        return rc
    monkeypatch.setattr(makepkg_invoke, "run_with_pty", _fake_pty)


def _invoke(tmp_path):
    pb = tmp_path / "PKGBUILD"
    pb.write_text("# fake\n")
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    makepkg_invoke.invoke_makepkg(pb, conf, {}, interactive=True)


def test_failing_interactive_build_logs_its_last_lines(tmp_path, monkeypatch, logs):
    unified, pkg = logs
    stream = [f"compile step {i}" for i in range(200)]
    stream += ["\x1b[31merror: undefined reference to `frobnicate'\x1b[0m",
               "==> ERROR: A failure occurred in build()."]
    _failing_build(monkeypatch, stream)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _invoke(tmp_path)
    assert exc.value.returncode == 4
    unified_text, pkg_text = unified.read_text(), pkg.read_text()

    # The failure reason is in both logs, ANSI-stripped, under [BUILD].
    for text in (unified_text, pkg_text):
        assert "[BUILD] error: undefined reference to `frobnicate'" in text
        assert "A failure occurred in build()." in text
        assert "interactive makepkg output (build failed, exit 4)" in text
        assert "\x1b[" not in text
    # The whole stream went to the per-package log only; the unified log
    # carries just the tail.
    assert "compile step 0\n" in pkg_text
    assert "compile step 0\n" not in unified_text


def test_tail_feeds_the_diagnosis(tmp_path, monkeypatch, logs):
    seen = {}

    def _diagnose(lines, build_dir=None):
        seen["lines"] = list(lines)
        return []
    monkeypatch.setattr(build_diag, "diagnose", _diagnose)
    _failing_build(monkeypatch, ["configure: error: C compiler cannot create executables"])
    with pytest.raises(subprocess.CalledProcessError):
        _invoke(tmp_path)
    assert seen["lines"][0] == "configure: error: C compiler cannot create executables"


def test_successful_interactive_build_logs_no_failure_tail(tmp_path, monkeypatch, logs):
    unified, pkg = logs
    _failing_build(monkeypatch, ["all good"], rc=0)
    _invoke(tmp_path)
    assert "build failed" not in unified.read_text()
    assert "all good" in pkg.read_text()
