#!/usr/bin/env python3
"""
Unit tests for sysforge.primitives.toolchain_preflight.

Covers:
  - collect_required_toolchains: rust:native + lib32 cross expansion;
    cmake/meson token emission; no-op for makepkg/env-only consumes
  - run_preflight + probes: monkeypatched subprocess.run + shutil.which
  - render_preflight: ok-only renders cleanly; failures emit fix lines
  - auto_remediate: runs fix_cmd only for auto_remediable, prompts when
    interactive, no-ops when non_interactive
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import subprocess

from sysforge.primitives.toolchain_preflight import (
    ToolchainCheck,
    ToolchainPreflightReport,
    auto_remediate,
    collect_required_toolchains,
    render_preflight,
    run_preflight,
)


# ---------------------------------------------------------------------------
# collect_required_toolchains
# ---------------------------------------------------------------------------

def test_collect_empty():
    assert collect_required_toolchains({}, frozenset()) == frozenset()


def test_collect_makepkg_env_only():
    """Packages with no special toolchain consumes contribute nothing."""
    per_pkg = {"htop": frozenset({"makepkg", "env"})}
    assert collect_required_toolchains(per_pkg, frozenset()) == frozenset()


def test_collect_rust_native_only():
    per_pkg = {"alacritty": frozenset({"makepkg", "rust", "env"})}
    assert collect_required_toolchains(per_pkg, frozenset()) == frozenset({"rust:native"})


def test_collect_lib32_rust_expands_to_cross():
    per_pkg = {"lib32-gstreamer": frozenset({"makepkg", "rust", "env"})}
    out = collect_required_toolchains(per_pkg, frozenset({"lib32-gstreamer"}))
    assert "rust:native" in out
    assert "rust:cross:i686-unknown-linux-gnu" in out


def test_collect_lib32_rust_with_toolchain_pin():
    """PKGBUILD pinning RUSTUP_TOOLCHAIN=stable produces a pinned token."""
    per_pkg = {"lib32-gstreamer": frozenset({"makepkg", "rust", "env"})}
    pins = {"lib32-gstreamer": "stable"}
    out = collect_required_toolchains(
        per_pkg, frozenset({"lib32-gstreamer"}), pins,
    )
    assert "rust:cross:i686-unknown-linux-gnu@stable" in out
    assert "rust:cross:i686-unknown-linux-gnu" not in out


def test_collect_cmake_meson():
    per_pkg = {
        "foo": frozenset({"makepkg", "cmake", "env"}),
        "bar": frozenset({"makepkg", "meson", "env"}),
    }
    assert collect_required_toolchains(per_pkg, frozenset()) == frozenset({"cmake", "meson"})


def test_collect_lib32_without_rust_skips_cross():
    """lib32-* without rust consume doesn't pull in a rust cross target."""
    per_pkg = {"lib32-foo": frozenset({"makepkg", "cmake", "env"})}
    out = collect_required_toolchains(per_pkg, frozenset({"lib32-foo"}))
    assert "rust:native" not in out
    assert all(not t.startswith("rust:cross:") for t in out)
    assert "cmake" in out


# ---------------------------------------------------------------------------
# Probes (monkeypatched)
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_probe_rust_native_ok(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _FakeResult(0, "rustc 1.95.0 (deadbeef 2026-04-14)\n", ""),
    )
    report = run_preflight(frozenset({"rust:native"}))
    assert all(c.ok for c in report.checks)
    assert report.checks[0].name == "rust:native"


def test_probe_rust_native_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda c: None)
    report = run_preflight(frozenset({"rust:native"}))
    assert not report.checks[0].ok
    assert "not on PATH" in report.checks[0].detail
    assert report.failed


def test_probe_rust_cross_missing_std_with_rustup(monkeypatch):
    """Reproduces the lib32-gstreamer failure: rustc present, rustup present,
    cross probe fails with E0463."""
    def fake_which(c):
        return f"/usr/bin/{c}" if c in {"rustc", "rustup"} else None
    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setenv("RUSTUP_TOOLCHAIN", "stable")

    def fake_run(cmd, *a, **kw):
        if cmd[0] == "rustc" and "--target" in cmd:
            return _FakeResult(
                1, "",
                "error[E0463]: can't find crate for `std`\n"
                "  = note: the `i686-unknown-linux-gnu` target may not be installed\n",
            )
        return _FakeResult(0, "rustc 1.95.0\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = run_preflight(frozenset({"rust:cross:i686-unknown-linux-gnu"}))
    failed = report.failed
    assert len(failed) == 1
    c = failed[0]
    assert c.name == "rust:cross:i686-unknown-linux-gnu"
    assert c.auto_remediable is True
    assert c.fix_cmd == "rustup target add --toolchain stable i686-unknown-linux-gnu"
    assert "std crate missing" in c.detail


def test_probe_rust_cross_with_pin_overrides_active(monkeypatch):
    """A pinned token (@stable) probes that toolchain regardless of the
    workstation's active default. The fix command names the pinned
    toolchain too, not the default."""
    def fake_which(c):
        return f"/usr/bin/{c}" if c in {"rustc", "rustup"} else None
    monkeypatch.setattr("shutil.which", fake_which)
    # Active default is nightly — but the build pins stable.
    monkeypatch.delenv("RUSTUP_TOOLCHAIN", raising=False)

    captured_envs: list = []

    def fake_run(cmd, *a, **kw):
        captured_envs.append((kw.get("env") or {}).get("RUSTUP_TOOLCHAIN"))
        if cmd[0] == "rustc" and "--target" in cmd:
            return _FakeResult(1, "", "error[E0463]: can't find crate for `std`\n")
        # rustup show active-toolchain (only called when no pin)
        return _FakeResult(0, "nightly-x86_64-unknown-linux-gnu (default)\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = run_preflight(frozenset({"rust:cross:i686-unknown-linux-gnu@stable"}))
    c = report.failed[0]
    # Probe ran with RUSTUP_TOOLCHAIN=stable in its env.
    assert "stable" in captured_envs
    assert c.name == "rust:cross:i686-unknown-linux-gnu@stable"
    assert c.fix_cmd == "rustup target add --toolchain stable i686-unknown-linux-gnu"
    assert c.auto_remediable is True


def test_probe_rust_cross_missing_std_no_rustup(monkeypatch):
    """System rust (no rustup): cross failure is not auto-remediable."""
    def fake_which(c):
        return f"/usr/bin/{c}" if c == "rustc" else None  # rustup missing
    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.delenv("RUSTUP_TOOLCHAIN", raising=False)

    def fake_run(cmd, *a, **kw):
        return _FakeResult(1, "", "error[E0463]: can't find crate for `std`\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = run_preflight(frozenset({"rust:cross:i686-unknown-linux-gnu"}))
    c = report.failed[0]
    assert c.auto_remediable is False
    assert "lib32-rust-libs" in (c.fix_cmd or "")


def test_probe_cmake_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda c: None)
    report = run_preflight(frozenset({"cmake"}))
    assert not report.checks[0].ok
    assert "pacman -S cmake" in (report.checks[0].fix_cmd or "")


def test_probe_meson_ok(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeResult(0, "1.11.1\n", ""))
    report = run_preflight(frozenset({"meson"}))
    assert report.checks[0].ok
    assert not report.failed


# ---------------------------------------------------------------------------
# render_preflight
# ---------------------------------------------------------------------------

def test_render_empty_report():
    assert render_preflight(ToolchainPreflightReport(checks=())) == ""


def test_render_ok_only():
    rep = ToolchainPreflightReport(checks=(
        ToolchainCheck("rust:native", True, "rustc 1.95.0", None, False),
    ))
    out = render_preflight(rep)
    assert "rust:native" in out
    assert "ok" in out
    assert "fix:" not in out


def test_render_failed_lists_fix_cmd():
    rep = ToolchainPreflightReport(checks=(
        ToolchainCheck(
            "rust:cross:i686-unknown-linux-gnu", False,
            "missing target", "rustup target add --toolchain stable i686-unknown-linux-gnu",
            True,
        ),
    ))
    out = render_preflight(rep)
    assert "FAIL" in out
    assert "fix:" in out
    assert "rustup target add" in out
    assert "(auto)" in out  # auto_remediable marker


# ---------------------------------------------------------------------------
# auto_remediate
# ---------------------------------------------------------------------------

def test_auto_remediate_non_interactive_skips(monkeypatch):
    rep = ToolchainPreflightReport(checks=(
        ToolchainCheck(
            "rust:cross:i686-unknown-linux-gnu", False, "missing",
            "rustup target add --toolchain stable i686-unknown-linux-gnu", True,
        ),
    ))
    # Even if the user is on a TTY, non_interactive=True must not run the fix.
    called = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: (called.append(a) or _FakeResult(0)))
    new_rep = auto_remediate(rep, non_interactive=True)
    assert called == []
    assert new_rep.failed  # still failed


def test_auto_remediate_non_remediable_skips(monkeypatch):
    rep = ToolchainPreflightReport(checks=(
        ToolchainCheck("cmake", False, "missing", "pacman -S cmake", False),
    ))
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_preflight.is_interactive", lambda: True,
    )
    called = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: (called.append(a) or _FakeResult(0)))
    new_rep = auto_remediate(rep, non_interactive=False)
    assert called == []
    assert new_rep.failed


def test_auto_remediate_runs_and_reprobes(monkeypatch):
    """Happy path: prompt accepted, fix succeeds, re-probe is green."""
    rep = ToolchainPreflightReport(checks=(
        ToolchainCheck(
            "rust:cross:i686-unknown-linux-gnu", False, "missing target",
            "rustup target add --toolchain stable i686-unknown-linux-gnu", True,
        ),
    ))
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_preflight.is_interactive", lambda: True,
    )
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_preflight.prompt_choice",
        lambda *a, **kw: "y",
    )
    # First call: the shell fix command; second call: the re-probe via
    # _probe_rust_cross which invokes rustc with --target. Both succeed.
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")
    monkeypatch.setenv("RUSTUP_TOOLCHAIN", "stable")

    calls: list = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd if isinstance(cmd, str) else list(cmd))
        return _FakeResult(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    new_rep = auto_remediate(rep, non_interactive=False)
    # Fix shell command must have been invoked at least once.
    assert any("rustup target add" in str(c) for c in calls)
    assert not new_rep.failed
