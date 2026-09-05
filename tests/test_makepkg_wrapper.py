# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for makepkg_wrapper.install_built_packages — the split build/install
path that installs .pkg.tar* artifacts via ``pacman -U`` through the F10
privilege seam (2.3.0-F10)."""
from pathlib import Path

import pytest

from sysforge.primitives import makepkg_wrapper


@pytest.fixture(autouse=True)
def _force_non_root(monkeypatch):
    """Pin euid non-root so the seam's ``sudo`` prefix is deterministic
    regardless of the test runner's uid."""
    monkeypatch.setattr("sysforge.primitives.privilege.os.geteuid", lambda: 1000)


@pytest.fixture
def _capture_run(monkeypatch):
    """Replace the module's subprocess.run with a capturing stub.

    Returns a dict the test reads: ``cmd`` (the argv passed) and a settable
    ``rc`` (the returncode the stub reports, default 0)."""
    box = {"cmd": None, "rc": 0, "calls": 0}

    class _Result:
        def __init__(self, rc):
            self.returncode = rc

    def _fake_run(cmd, *a, **kw):
        box["cmd"] = cmd
        box["calls"] += 1
        return _Result(box["rc"])

    monkeypatch.setattr(makepkg_wrapper.subprocess, "run", _fake_run)
    return box


def _fake_artifacts(monkeypatch, pkgs):
    monkeypatch.setattr(
        makepkg_wrapper, "_artifacts_for_pkgbuild", lambda _d: list(pkgs)
    )


def test_install_built_packages_escalates_via_seam(monkeypatch, _capture_run, tmp_path):
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    result = makepkg_wrapper.install_built_packages(tmp_path)

    assert result == [pkg]
    # non-root → sudo-prefixed; noconfirm default True → --noconfirm present
    assert _capture_run["cmd"] == [
        "sudo", "pacman", "-U", "--noconfirm", str(pkg),
    ]


def test_install_built_packages_no_noconfirm(monkeypatch, _capture_run, tmp_path):
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    makepkg_wrapper.install_built_packages(tmp_path, noconfirm=False)

    assert _capture_run["cmd"] == ["sudo", "pacman", "-U", str(pkg)]


def test_install_built_packages_bare_when_root(monkeypatch, _capture_run, tmp_path):
    monkeypatch.setattr("sysforge.primitives.privilege.os.geteuid", lambda: 0)
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    makepkg_wrapper.install_built_packages(tmp_path)

    # already root → no sudo prefix
    assert _capture_run["cmd"] == ["pacman", "-U", "--noconfirm", str(pkg)]


def test_install_built_packages_no_artifacts_raises(monkeypatch, _capture_run, tmp_path):
    _fake_artifacts(monkeypatch, [])

    with pytest.raises(RuntimeError, match="nothing to install"):
        makepkg_wrapper.install_built_packages(tmp_path)

    # nothing was executed
    assert _capture_run["calls"] == 0


def test_install_built_packages_pacman_failure_raises(monkeypatch, _capture_run, tmp_path):
    _capture_run["rc"] = 1
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    with pytest.raises(RuntimeError, match="pacman -U failed"):
        makepkg_wrapper.install_built_packages(tmp_path)


def test_install_built_packages_drops_manifest_on_success(
    monkeypatch, _capture_run, tmp_path
):
    # a stale build-time manifest must be removed after a successful install so
    # a later unrelated build in the same dir can't read it (B9).
    manifest = tmp_path / makepkg_wrapper._BUILT_MANIFEST_NAME
    manifest.write_text("foo-1-1-x86_64.pkg.tar.zst\n", encoding="utf-8")
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    makepkg_wrapper.install_built_packages(tmp_path)

    assert not manifest.exists()


def test_install_built_packages_failure_names_artifacts(
        monkeypatch, _capture_run, tmp_path):
    """B7: the failure names what it tried to install — the exit code alone
    is undiagnosable after the fact."""
    _capture_run["rc"] = 1
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    with pytest.raises(RuntimeError, match="foo-1-1-x86_64"):
        makepkg_wrapper.install_built_packages(tmp_path)


def test_install_built_packages_interactive_failure_notes_tty(
        monkeypatch, _capture_run, tmp_path):
    """B7: interactive installs (noconfirm=False) inherit stdio, so pacman's
    output was never captured — the error must say where it went and that a
    declined prompt also exits 1."""
    _capture_run["rc"] = 1
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    with pytest.raises(RuntimeError, match="terminal"):
        makepkg_wrapper.install_built_packages(tmp_path, noconfirm=False)


# --- Failure-tail selection (3.2.0-B14) -------------------------------------

from sysforge.primitives.makepkg_invoke import select_failure_tail  # noqa: E402


def test_failure_tail_anchors_on_the_last_error_block():
    """3.2.0-B14. mesa's PGO run emitted hundreds of `-Wbackend-plugin` skew
    warnings after ninja's `FAILED:` block, so a blind last-80 window held
    nothing but warnings and the real error never reached the log — the whole
    point of persisting a tail is post-hoc diagnosis without a -vvv re-run."""
    lines = (["FAILED: libmesa_rust_gen.rlib", "error[E0605]: non-primitive cast"]
             + [f"warning: hash mismatch {i} [-Wbackend-plugin]" for i in range(200)])
    tail = select_failure_tail(lines, limit=20)
    assert any("FAILED:" in ln for ln in tail), tail
    assert any("E0605" in ln for ln in tail), tail


def test_failure_tail_keeps_the_final_lines_too():
    """makepkg's own verdict (`==> ERROR: A failure occurred in build()`) is
    always last, and it names the failing phase — re-anchoring must not drop
    it."""
    lines = (["FAILED: target.o", "error: boom"]
             + [f"warning: {i}" for i in range(200)]
             + ["==> ERROR: A failure occurred in build()."])
    tail = select_failure_tail(lines, limit=20)
    assert any("FAILED:" in ln for ln in tail)
    assert any("A failure occurred" in ln for ln in tail)


def test_failure_tail_is_unchanged_when_the_error_is_already_in_window():
    """The common case must keep today's behaviour exactly: a contiguous last-N
    slice, no elision marker, no reordering."""
    lines = [f"line {i}" for i in range(50)] + ["FAILED: x", "error: boom"]
    tail = select_failure_tail(lines, limit=20)
    assert tail == lines[-20:]


def test_failure_tail_without_any_marker_falls_back_to_the_last_lines():
    lines = [f"line {i}" for i in range(100)]
    assert select_failure_tail(lines, limit=10) == lines[-10:]


def test_failure_tail_never_exceeds_the_limit():
    lines = (["FAILED: x"] + [f"warning: {i}" for i in range(500)]
             + ["==> ERROR: done"])
    assert len(select_failure_tail(lines, limit=30)) <= 30


def test_failure_tail_marks_the_elision():
    """A reader must be able to tell the two segments are not contiguous."""
    lines = (["FAILED: x", "error: boom"] + [f"warning: {i}" for i in range(200)]
             + ["==> ERROR: A failure occurred in build()."])
    tail = select_failure_tail(lines, limit=20)
    assert any("omitted" in ln for ln in tail), tail


def test_failure_tail_recognizes_the_common_markers():
    for marker in ("FAILED: x.o", "error: boom", "error[E0605]: cast",
                   "x.c:1:1: fatal error: no.h", "==> ERROR: build failed"):
        lines = [marker] + [f"warning: {i}" for i in range(100)]
        tail = select_failure_tail(lines, limit=10)
        assert any(marker in ln for ln in tail), marker


def test_failure_tail_handles_short_input():
    assert select_failure_tail(["a", "b"], limit=80) == ["a", "b"]
    assert select_failure_tail([], limit=80) == []
