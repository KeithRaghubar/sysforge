"""
test_host_fence.py — the conftest fence on host pacman queries (3.5.0-DEV4).

A test whose code path ran a real ``pacman`` passed or failed by what the
machine had installed (3.5.0-DEV3: green on a workstation with LLVM, red in
the CI container). ``conftest._fence_host_pacman_queries`` turns that into a
named failure everywhere. These tests pin its contract: an unstubbed query
fails naming the seam, a stub at any layer above ``subprocess.run`` wins, a
fixture DB passes, the marker opts out, and a swallowed hit still fails.
"""
import os
import subprocess
import sys
import textwrap

import pytest

from conftest import REPO_ROOT, HostStateError, _targets_host_db
from sysforge.primitives import pacman, run


def test_unstubbed_query_fails_naming_the_seam(_fence_host_pacman_queries):
    with pytest.raises(HostStateError) as exc:
        pacman.get_installed_version("lldb")
    msg = str(exc.value)
    assert "via primitives/pacman.py:" in msg
    assert "get_installed_version (pacman -Q lldb)" in msg
    assert "@pytest.mark.host_pacman" in msg
    _fence_host_pacman_queries.clear()  # provoked on purpose


def test_best_effort_except_exception_cannot_swallow_it(_fence_host_pacman_queries):
    """sysforge probes fall back on ``except Exception``; the fence is not one."""
    assert not issubclass(HostStateError, Exception)
    with pytest.raises(HostStateError):
        try:
            run.probe(["pacman", "-Qm"])
        except Exception:  # noqa: BLE001 — the shape being guarded against
            pytest.fail("the fence was swallowed by except Exception")
    _fence_host_pacman_queries.clear()


def test_stub_above_the_fence_wins(monkeypatch):
    monkeypatch.setattr(run, "probe", lambda argv, **_kw: subprocess.CompletedProcess(
        argv, 0, "pacman 7.0.0-1\n", ""))
    assert pacman.get_installed_version("pacman") == "7.0.0-1"


def test_non_pacman_commands_pass_through():
    assert run.probe([sys.executable, "-c", "print('ok')"]).stdout.strip() == "ok"


@pytest.mark.parametrize("argv, host", [
    (["pacman", "-Q", "llvm"], True),
    (["pacman", "--dbpath", "/var/lib/pacman/", "-Q"], True),
    (["pacman", "--dbpath", "/tmp/fixture/db", "-Sp", "llvm-libs"], False),
    (["pacman", "-b", "/tmp/fixture/db", "-Q"], False),
    (["pacman", "--dbpath=/tmp/fixture/db", "-Q"], False),
])
def test_only_the_host_db_is_fenced(argv, host):
    assert _targets_host_db(argv) is host


@pytest.mark.host_pacman
def test_marker_opts_out():
    assert subprocess.run.__module__ == "subprocess"


def test_swallowed_hit_fails_at_teardown(tmp_path):
    """Code that catches even ``BaseException`` cannot hide a hit."""
    # A plugin, not an inner conftest.py, which would shadow the one it imports.
    (tmp_path / "fence_plugin.py").write_text(
        "from conftest import _fence_host_pacman_queries  # noqa: F401\n"
        "from conftest import pytest_runtest_makereport  # noqa: F401\n")
    (tmp_path / "test_inner.py").write_text(textwrap.dedent("""
        from sysforge.primitives import run

        def test_swallows():
            try:
                run.probe(["pacman", "-Qm"])
            except BaseException:
                pass
    """))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(tmp_path), str(REPO_ROOT / "tests"), str(REPO_ROOT)]))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "fence_plugin",
         "--rootdir", str(tmp_path), "-c", os.devnull, str(tmp_path)],
        capture_output=True, text=True, env=env, cwd=tmp_path, check=False)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "swallowed by the code under test" in out
    assert "1 passed, 1 error" in out
