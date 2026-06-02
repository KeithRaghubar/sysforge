"""
conftest.py — shared pytest configuration for SysForge tests.

Sets SYSFORGE_CONFIG_DIR to point at tests/data before any module imports,
so load_config() and other path-sensitive functions resolve test fixtures
rather than /etc/sysforge.
"""
import os
from pathlib import Path

import pytest

# Must be set before importing any sysforge module that reads CONFIG_BASE
# at import time.
TESTS_DIR = Path(__file__).parent
TEST_DATA = TESTS_DIR / "data"

os.environ.setdefault("SYSFORGE_CONFIG_DIR", str(TEST_DATA))

# Force the subprocess fallback in primitives.pacman so existing tests that
# mock subprocess.run continue to drive the query. The pyalpm fast path is
# exercised explicitly in test_pacman_pyalpm.py.
os.environ.setdefault("SYSFORGE_PACMAN_NO_PYALPM", "1")

# Show all log messages in tests so assertions against log output work.
import sysforge.log as _sf_log
_sf_log.set_verbosity(2)


@pytest.fixture(autouse=True)
def _isolate_filesystem_soname_cache(monkeypatch):
    """
    `dep_analysis.soname_available` consults the real /usr/lib (and friends)
    when the supplied ldconfig set misses. Tests assert against synthetic
    state, so default-patch the filesystem probe to an empty set. Tests
    that want to exercise the fallback explicitly override the patch.
    """
    from sysforge.primitives import dep_analysis as _da
    monkeypatch.setattr(_da, "_filesystem_soname_set",
                        lambda lib32=False: frozenset())


# ---------------------------------------------------------------------------
# Centralized external-isolation fixtures (Phase 0 — behavior-first testing).
#
# OPT-IN (not autouse): request them by name. They give behavior-level tests a
# single seam per genuine external so tests assert on *observable* effects
# (which commands were emitted, what was asked) instead of patching internal
# call sites. Existing tests that roll their own patching are unaffected.
#
# The global seams are valid because the codebase uses `subprocess.run` and
# `shutil.which` everywhere (no `from subprocess import run` / `from shutil
# import which`), so patching the module attribute intercepts every caller.
# Proven in tests/test_fixtures_smoke.py.
# ---------------------------------------------------------------------------
import shutil
import subprocess


def _make_proc(returncode=0, stdout="", stderr="", args=None):
    """Build a subprocess.CompletedProcess the way the codebase expects."""
    return subprocess.CompletedProcess(
        args=args if args is not None else [],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.fixture
def make_proc():
    """Factory fixture for CompletedProcess results."""
    return _make_proc


class _RunRecorder:
    """
    Programmable stand-in for ``subprocess.run`` installed at the global seam.

    - Records every invocation's argv in ``.calls`` (``.commands`` as strings).
    - Returns the first matching programmed response, else ``default``.
    - ``respond(match, ...)``: ``match`` is a substring (vs the joined command),
      an argv-prefix list, or a callable predicate. ``check=True`` raises
      ``CalledProcessError`` on a non-zero response, like the real call.
    """

    def __init__(self):
        self.calls = []
        self.default = _make_proc()
        self._rules = []

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(cmd)
        proc = self.default
        for predicate, response in self._rules:
            if predicate(cmd):
                proc = response
                break
        if kwargs.get("check") and proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, proc.stdout, proc.stderr)
        return proc

    @staticmethod
    def _as_predicate(match):
        if callable(match):
            return match
        if isinstance(match, (list, tuple)):
            prefix = list(match)
            return lambda cmd: list(cmd)[: len(prefix)] == prefix
        return lambda cmd: match in (
            cmd if isinstance(cmd, str) else " ".join(map(str, cmd)))

    def respond(self, match, *, returncode=0, stdout="", stderr=""):
        """Register a response rule (first match wins, in registration order)."""
        self._rules.append(
            (self._as_predicate(match), _make_proc(returncode, stdout, stderr)))
        return self

    def set_default(self, *, returncode=0, stdout="", stderr=""):
        self.default = _make_proc(returncode, stdout, stderr)
        return self

    @property
    def commands(self):
        """Each recorded call as one string, for ergonomic assertions."""
        return [c if isinstance(c, str) else " ".join(map(str, c))
                for c in self.calls]


@pytest.fixture
def fake_run(monkeypatch):
    """Install a programmable ``subprocess.run`` recorder at the global seam."""
    recorder = _RunRecorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    return recorder


class _WhichMap:
    """Programmable ``shutil.which``: only registered binaries are 'found'."""

    def __init__(self):
        self._paths = {}

    def add(self, *names, prefix="/usr/bin"):
        for name in names:
            self._paths[name] = f"{prefix}/{name}"
        return self

    def set(self, name, path):
        self._paths[name] = path
        return self

    def __call__(self, name, *args, **kwargs):
        return self._paths.get(os.path.basename(name))


@pytest.fixture
def fake_which(monkeypatch):
    """Install a programmable ``shutil.which`` (empty by default)."""
    which = _WhichMap()
    monkeypatch.setattr(shutil, "which", which)
    return which


class _InputQueue:
    """Programmable ``builtins.input``: pops queued answers, records prompts."""

    def __init__(self):
        self.answers = []
        self.prompts = []

    def push(self, *answers):
        self.answers.extend(answers)
        return self

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError("no queued input")
        return self.answers.pop(0)


@pytest.fixture
def fake_input(monkeypatch):
    """Install a programmable ``builtins.input`` (EOFError when drained)."""
    queue = _InputQueue()
    monkeypatch.setattr("builtins.input", queue)
    return queue


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """A throwaway state dir wired via ``SYSFORGE_STATE_DIR``; returns the Path."""
    d = tmp_path / "sf-state"
    d.mkdir()
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(d))
    return d


@pytest.fixture
def no_network(monkeypatch):
    """Hard-fail any accidental real network call from a behavior test."""
    import urllib.request

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "network access in a behavior test (fake the RPC/subprocess seam)")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    return _blocked
