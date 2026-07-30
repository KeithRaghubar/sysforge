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
# Version-change rows (2.6.1-F9) — mirror the post-update summary table
# ---------------------------------------------------------------------------

def test_render_emits_version_pair_rows(monkeypatch):
    """A check carrying version rows renders them as `label: old → new`."""
    monkeypatch.setenv("TERM", "xterm")
    rep = ToolchainPreflightReport(checks=(
        ToolchainCheck(
            "cc:clang", False, "LLVM suite version skew", "pacman -Syu", False,
            versions=(("clang/lld", "22.1.5", "22.1.6"),
                      ("llvm/llvm-libs", "22.1.6", "22.1.6")),
        ),
    ))
    out = render_preflight(rep)
    assert "clang/lld: 22.1.5 → 22.1.6" in out
    # The already-current group collapses to the equal marker, as in the
    # LLVM source pre-flight.
    assert "llvm/llvm-libs: 22.1.6 (=)" in out


def test_render_version_rows_degrade_under_term_linux(monkeypatch):
    monkeypatch.setenv("TERM", "linux")
    rep = ToolchainPreflightReport(checks=(
        ToolchainCheck(
            "cc:clang", False, "skew", None, False,
            versions=(("clang", "22.1.5", "22.1.6"),),
        ),
    ))
    out = render_preflight(rep)
    assert "clang: 22.1.5 -> 22.1.6" in out
    assert "→" not in out


def test_render_without_version_rows_is_unchanged():
    """Checks that carry no version data keep the plain detail line."""
    rep = ToolchainPreflightReport(checks=(
        ToolchainCheck("cmake", True, "cmake version 3.31.0", None, False),
    ))
    out = render_preflight(rep)
    assert "cmake version 3.31.0" in out
    assert ":" not in out.split("cmake version")[1]


def test_skew_check_carries_structured_version_rows(monkeypatch):
    """_probe_cc populates `versions` so the renderer, not the probe, formats.

    clang/lld lag behind llvm/llvm-libs; every lagging group targets the newest
    installed pkgver.
    """
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["clang", "--version"]:
            return _FakeResult(0, "clang version 22.1.5\n", "")
        if cmd[:2] == ["pacman", "-Q"]:
            ver = "22.1.5-1" if cmd[2] in ("clang", "lld") else "22.1.6-1"
            return _FakeResult(0, f"{cmd[2]} {ver}\n", "")
        return _FakeResult(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    c = run_preflight(frozenset({"cc:clang"})).failed[0]
    rows = dict((label, (old, new)) for label, old, new in c.versions)
    assert rows["clang/lld"] == ("22.1.5", "22.1.6")
    # The newest group is its own target.
    assert rows["compiler-rt/llvm/llvm-libs/openmp/polly"] == ("22.1.6", "22.1.6")


def test_ok_check_carries_no_version_rows(monkeypatch):
    """A healthy suite reports one detail line, not seven version rows."""
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["clang", "--version"]:
            return _FakeResult(0, "clang version 22.1.6\n", "")
        if cmd[:2] == ["pacman", "-Q"]:
            return _FakeResult(0, f"{cmd[2]} 22.1.6-1\n", "")
        return _FakeResult(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_preflight(frozenset({"cc:clang"})).checks[0].versions == ()


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
    # First call: the fix command as an argv list (no shell); second call: the
    # re-probe via _probe_rust_cross which invokes rustc with --target. Both succeed.
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")
    monkeypatch.setenv("RUSTUP_TOOLCHAIN", "stable")

    calls: list = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd if isinstance(cmd, str) else list(cmd))
        return _FakeResult(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    new_rep = auto_remediate(rep, non_interactive=False)
    # Fix command must have been invoked as an argv list (never a shell string),
    # so a PKGBUILD-supplied toolchain pin can't inject shell metacharacters.
    assert any(
        isinstance(c, list) and c[:3] == ["rustup", "target", "add"]
        for c in calls
    )
    assert not new_rep.failed


def test_auto_remediate_does_not_shell_inject_from_pin(monkeypatch):
    """A metacharacter-bearing toolchain pin (as could be extracted from an
    untrusted PKGBUILD's RUSTUP_TOOLCHAIN) must reach subprocess.run as a single
    literal argv token, never a shell string — no shell=True, no interpretation.

    The payload is space-free because the extraction regex (update.py) captures
    ``[^\\s"';#]+`` — excluding whitespace/quotes/`;`/`#` but still admitting the
    command-substitution characters ``$``, ``(``, ``)``, which is the real vector."""
    injected = "$(reboot)"
    rep = ToolchainPreflightReport(checks=(
        ToolchainCheck(
            "rust:cross:i686-unknown-linux-gnu@" + injected, False, "missing target",
            f"rustup target add --toolchain {injected} i686-unknown-linux-gnu", True,
        ),
    ))
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_preflight.is_interactive", lambda: True,
    )
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_preflight.prompt_choice",
        lambda *a, **kw: "y",
    )
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")
    monkeypatch.setenv("RUSTUP_TOOLCHAIN", "stable")

    seen: list = []

    def fake_run(cmd, *a, **kw):
        seen.append((cmd, kw))
        return _FakeResult(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    auto_remediate(rep, non_interactive=False)

    fix_calls = [(cmd, kw) for cmd, kw in seen
                 if isinstance(cmd, list) and cmd[:3] == ["rustup", "target", "add"]]
    assert fix_calls, seen
    for cmd, kw in fix_calls:
        # Never dispatched through a shell...
        assert kw.get("shell") is not True
        # ...and the injected payload survives intact as ONE literal argv element,
        # proving it was neither word-split nor command-substituted.
        assert injected in cmd


# ---------------------------------------------------------------------------
# Compiler-health probe (cc:<name>) — the broken/half-installed clang case
# ---------------------------------------------------------------------------

def test_collect_emits_cc_tokens():
    out = collect_required_toolchains(
        {}, frozenset(), None, frozenset({"clang", "clang++", "gcc"})
    )
    assert out == frozenset({"cc:clang", "cc:clang++", "cc:gcc"})


def test_collect_cc_basenames_paths():
    out = collect_required_toolchains(
        {}, frozenset(), None, frozenset({"/usr/bin/clang"})
    )
    assert out == frozenset({"cc:clang"})


def test_probe_cc_ok(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["gcc", "--version"]:
            return _FakeResult(0, "gcc (GCC) 16.1.1\n", "")
        return _FakeResult(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = run_preflight(frozenset({"cc:gcc"}))
    assert report.checks[0].ok
    assert report.checks[0].name == "cc:gcc"


def test_probe_cc_clang_cannot_run(monkeypatch):
    """The live failure: clang on PATH but `clang --version` dies with a
    libLLVM symbol-lookup error → preflight blocks the batch."""
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["clang", "--version"]:
            return _FakeResult(
                127, "",
                "/usr/bin/clang: symbol lookup error: /usr/bin/clang: "
                "undefined symbol: LLVMInitializeBPFTarget, version LLVM_22.1\n",
            )
        return _FakeResult(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = run_preflight(frozenset({"cc:clang"}))
    c = report.failed[0]
    assert c.name == "cc:clang"
    assert "cannot run" in c.detail
    assert "symbol lookup error" in c.detail
    assert "pacman -Syu" in (c.fix_cmd or "")
    assert c.auto_remediable is False


def test_probe_cc_clang_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda c: None)
    report = run_preflight(frozenset({"cc:clang"}))
    c = report.failed[0]
    assert "not on PATH" in c.detail
    assert "pacman -Syu" in (c.fix_cmd or "")


def test_probe_cc_clang_llvm_libs_skew(monkeypatch):
    """clang --version succeeds but clang and llvm-libs disagree → skew flagged."""
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["clang", "--version"]:
            return _FakeResult(0, "clang version 22.1.5\n", "")
        if cmd[:2] == ["pacman", "-Q"]:
            ver = "22.1.5-1" if cmd[2] == "clang" else "22.1.6-1"
            return _FakeResult(0, f"{cmd[2]} {ver}\n", "")
        return _FakeResult(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = run_preflight(frozenset({"cc:clang"}))
    c = report.failed[0]
    assert "version skew" in c.detail
    assert "22.1.5" in c.detail and "22.1.6" in c.detail


def test_probe_cc_clang_consistent_no_skew(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["clang", "--version"]:
            return _FakeResult(0, "clang version 22.1.6\n", "")
        if cmd[:2] == ["pacman", "-Q"]:
            return _FakeResult(0, f"{cmd[2]} 22.1.6-1\n", "")
        return _FakeResult(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = run_preflight(frozenset({"cc:clang"}))
    assert report.checks[0].ok
    assert not report.failed


def test_probe_cc_skew_in_wider_suite(monkeypatch):
    """compiler-rt lags while clang/llvm-libs match → the suite-wide check still
    flags it (the old clang-vs-llvm-libs-only check would have missed this)."""
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["clang", "--version"]:
            return _FakeResult(0, "clang version 22.1.6\n", "")
        if cmd[:2] == ["pacman", "-Q"]:
            ver = "22.1.5-1" if cmd[2] == "compiler-rt" else "22.1.6-1"
            return _FakeResult(0, f"{cmd[2]} {ver}\n", "")
        return _FakeResult(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = run_preflight(frozenset({"cc:clang"}))
    c = report.failed[0]
    assert "version skew" in c.detail
    assert "compiler-rt" in c.detail
    # The fix lists every installed member so `pacman -Syu` resyncs them all —
    # crucially including the previously-stranded compiler-rt/polly/openmp.
    for pkg in ("llvm", "llvm-libs", "clang", "lld", "compiler-rt", "polly", "openmp"):
        assert pkg in (c.fix_cmd or "")


def test_probe_cc_pkgrel_only_difference_is_not_skew(monkeypatch):
    """lld at -3 while the rest are -1 (all same pkgver) is a packaging bump,
    not an upstream skew — the comparison strips pkgrel."""
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["clang", "--version"]:
            return _FakeResult(0, "clang version 22.1.5\n", "")
        if cmd[:2] == ["pacman", "-Q"]:
            rel = "3" if cmd[2] == "lld" else "1"
            return _FakeResult(0, f"{cmd[2]} 22.1.5-{rel}\n", "")
        return _FakeResult(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = run_preflight(frozenset({"cc:clang"}))
    assert report.checks[0].ok
    assert not report.failed


def test_probe_cc_independent_lineages_ignored(monkeypatch):
    """spirv-llvm-translator / lib32-* are not in the lockstep set, so their
    unrelated versions never trigger a skew."""
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")
    queried: list[str] = []

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["clang", "--version"]:
            return _FakeResult(0, "clang version 22.1.6\n", "")
        if cmd[:2] == ["pacman", "-Q"]:
            queried.append(cmd[2])
            # If something erroneously queried these, hand back skewed versions.
            if cmd[2] == "spirv-llvm-translator":
                return _FakeResult(0, f"{cmd[2]} 22.1.2-1\n", "")
            if cmd[2].startswith("lib32-"):
                return _FakeResult(0, f"{cmd[2]} 1:22.1.6-1\n", "")
            return _FakeResult(0, f"{cmd[2]} 22.1.6-1\n", "")
        return _FakeResult(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = run_preflight(frozenset({"cc:clang"}))
    assert report.checks[0].ok
    assert not report.failed
    assert "spirv-llvm-translator" not in queried
    assert not any(p.startswith("lib32-") for p in queried)
