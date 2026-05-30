"""
toolchain_preflight.py — batch-time toolchain availability check.

Given the active ``consumes`` set across a batch (resolved by
:func:`sysforge.primitives.profile.resolve_consumes`) plus the subset of
packages whose pkgname starts with ``lib32-``, derives the toolchains the
batch needs and probes the host for them before any ``makepkg`` runs.

Catches the common "build wastes 5 minutes then fails because rustup didn't
have the i686 target" class of error and either auto-remediates (when the
fix is a clean ``rustup target add``) or hard-fails with the exact command
to run.

Public API:
    collect_required_toolchains(per_pkg_consumes, lib32_pkgs) -> frozenset[str]
    run_preflight(required)                                   -> ToolchainPreflightReport
    render_preflight(report)                                  -> str
    auto_remediate(report, *, non_interactive=False)          -> ToolchainPreflightReport

Token grammar for required toolchains:
    "rust:native"                       — host rustc must run
    "rust:cross:<target>"               — rustc must build for <target>
                                          using the workstation's default
                                          rustup toolchain
    "rust:cross:<target>@<toolchain>"   — rustc must build for <target> using
                                          the named rustup toolchain (the
                                          PKGBUILD's own RUSTUP_TOOLCHAIN
                                          pin in build()/check())
    "cmake"                             — cmake binary on PATH
    "meson"                             — meson binary on PATH
    "cc:<name>"                         — the resolved compiler (e.g. clang,
                                          gcc) must run; for clang, also checks
                                          clang↔llvm-libs version consistency

The active rustup toolchain (when no per-PKGBUILD pin is supplied) is read
from ``$RUSTUP_TOOLCHAIN``, falling back to ``rustup show active-toolchain``.
When a PKGBUILD's build()/check() function exports its own RUSTUP_TOOLCHAIN
(``lib32-gstreamer`` does — it pins ``stable``), the caller passes that
through via ``rust_toolchain_pins`` so the probe runs against the actual
toolchain the build will use, not the workstation default.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sysforge import log
from sysforge.primitives.prompt import is_interactive, prompt_choice

_log = log.get_logger("PREFLIGHT")

_TAG = "PREFLIGHT"
_LIB32_RUST_TARGET = "i686-unknown-linux-gnu"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolchainCheck:
    name: str
    ok: bool
    detail: str
    fix_cmd: str | None
    auto_remediable: bool


@dataclass(frozen=True)
class ToolchainPreflightReport:
    checks: tuple[ToolchainCheck, ...]

    @property
    def failed(self) -> tuple[ToolchainCheck, ...]:
        return tuple(c for c in self.checks if not c.ok)


# ---------------------------------------------------------------------------
# Requirement collection
# ---------------------------------------------------------------------------

def collect_required_toolchains(
    per_pkg_consumes: Mapping[str, frozenset[str]],
    lib32_pkgs: frozenset[str],
    rust_toolchain_pins: Mapping[str, str] | None = None,
    compilers: frozenset[str] | None = None,
) -> frozenset[str]:
    """Reduce per-package consumes + lib32-ness to a required-toolchain set.

    Only consume types that imply a separate toolchain (rust, cmake, meson)
    contribute. ``makepkg`` / ``env`` are no-ops — those are always present
    when sysforge itself runs.

    ``rust_toolchain_pins`` maps ``pkgname → "stable"|"nightly"|...`` for
    packages whose PKGBUILD exports its own ``RUSTUP_TOOLCHAIN``. When a
    pin is supplied, the resulting cross-target token is suffixed with
    ``@<toolchain>`` so the probe runs against the pinned toolchain rather
    than the workstation default.
    """
    pins = rust_toolchain_pins or {}
    required: set[str] = set()
    for pkg, consumes in per_pkg_consumes.items():
        if "rust" in consumes:
            required.add("rust:native")
            if pkg in lib32_pkgs:
                pin = pins.get(pkg)
                suffix = f"@{pin}" if pin else ""
                required.add(f"rust:cross:{_LIB32_RUST_TARGET}{suffix}")
        if "cmake" in consumes:
            required.add("cmake")
        if "meson" in consumes:
            required.add("meson")
    # The resolved compiler(s) across the batch — probed for executability so a
    # broken/half-installed toolchain (e.g. clang that can't run) aborts the
    # batch up front rather than failing every package at compiler detection.
    for compiler in (compilers or frozenset()):
        base = Path(compiler).name
        if base:
            required.add(f"cc:{base}")
    return frozenset(required)


# ---------------------------------------------------------------------------
# Active rustup toolchain
# ---------------------------------------------------------------------------

def _active_rustup_toolchain() -> str | None:
    """Return the rustup toolchain that will be active for a build.

    Mirrors the resolution order rustup itself uses: ``RUSTUP_TOOLCHAIN``
    env var wins, then ``rustup show active-toolchain``. Returns ``None``
    if rustup is not installed (system rust install — no per-toolchain
    target management).
    """
    env_tc = os.environ.get("RUSTUP_TOOLCHAIN")
    if env_tc:
        return env_tc
    if not shutil.which("rustup"):
        return None
    r = subprocess.run(
        ["rustup", "show", "active-toolchain"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    # Output is e.g. "stable-x86_64-unknown-linux-gnu (default)"
    return line.split()[0] if line else None


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def _probe_command(name: str, cmd: list[str], install_hint: str) -> ToolchainCheck:
    """Generic ``<tool> --version`` probe."""
    if not shutil.which(cmd[0]):
        return ToolchainCheck(
            name=name, ok=False,
            detail=f"{cmd[0]} not on PATH",
            fix_cmd=install_hint, auto_remediable=False,
        )
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return ToolchainCheck(
            name=name, ok=False,
            detail=f"{cmd[0]} {cmd[1]} failed (exit {r.returncode})",
            fix_cmd=install_hint, auto_remediable=False,
        )
    first = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else ""
    return ToolchainCheck(
        name=name, ok=True, detail=first or f"{cmd[0]} OK",
        fix_cmd=None, auto_remediable=False,
    )


def _probe_rust_native() -> ToolchainCheck:
    return _probe_command(
        "rust:native", ["rustc", "--version"],
        install_hint="pacman -S rustup  # then: rustup default stable",
    )


def _probe_rust_cross(target: str, pin: str | None = None) -> ToolchainCheck:
    """Compile a trivial crate for ``target`` with ``--emit=metadata``.

    ``--emit=metadata`` skips codegen/linking — it still requires the std
    crate for ``target`` to be discoverable, so it catches the
    ``error[E0463]: can't find crate for 'std'`` case without paying for a
    full link. The probe mirrors the same hurdle meson clears in its rust
    sanity check.

    When ``pin`` is supplied the probe runs with ``RUSTUP_TOOLCHAIN=<pin>``
    overlayed on the env so the test exercises the same toolchain the
    PKGBUILD itself selects in build()/check(). Without a pin the probe
    uses the workstation's active rustup toolchain.
    """
    token_name = f"rust:cross:{target}" + (f"@{pin}" if pin else "")
    if not shutil.which("rustc"):
        return ToolchainCheck(
            name=token_name, ok=False,
            detail="rustc not on PATH",
            fix_cmd="pacman -S rustup  # then: rustup default stable",
            auto_remediable=False,
        )

    probe_env = os.environ.copy()
    if pin:
        probe_env["RUSTUP_TOOLCHAIN"] = pin
    effective = pin or _active_rustup_toolchain()
    rustup_managed = shutil.which("rustup") is not None and effective is not None

    with tempfile.TemporaryDirectory(prefix="sysforge-rs-probe-") as td:
        src = Path(td) / "probe.rs"
        src.write_text("fn main(){}\n")
        r = subprocess.run(
            [
                "rustc",
                "--edition", "2021",
                "--target", target,
                "--crate-type", "bin",
                "--emit", "metadata",
                "--out-dir", td,
                str(src),
            ],
            capture_output=True, text=True, env=probe_env,
        )

    if r.returncode == 0:
        suffix = f" (toolchain={effective})" if effective else ""
        return ToolchainCheck(
            name=token_name, ok=True,
            detail=f"rustc can build for {target}{suffix}",
            fix_cmd=None, auto_remediable=False,
        )

    stderr = r.stderr or ""
    if rustup_managed:
        fix = f"rustup target add --toolchain {effective} {target}"
        auto = True
        detail = f"rustup toolchain {effective!r} lacks target {target}"
    else:
        # System rust install (no rustup) — installing the target is a
        # pacman action (lib32-rust-libs for i686), not a clean auto-fix.
        fix = (
            f"pacman -S lib32-rust-libs  # provides std for {target}"
            if target == _LIB32_RUST_TARGET else
            f"# install rust std for {target} (no rustup detected)"
        )
        auto = False
        detail = f"rustc cannot build for {target} (no rustup-managed toolchain)"

    if "E0463" in stderr or "can't find crate for `std`" in stderr:
        detail += " — std crate missing"

    return ToolchainCheck(
        name=token_name, ok=False,
        detail=detail, fix_cmd=fix, auto_remediable=auto,
    )


def _probe_cmake() -> ToolchainCheck:
    return _probe_command(
        "cmake", ["cmake", "--version"], install_hint="pacman -S cmake",
    )


def _probe_meson() -> ToolchainCheck:
    return _probe_command(
        "meson", ["meson", "--version"], install_hint="pacman -S meson",
    )


# LLVM packages that ship from one upstream release and must share an exact
# pkgver. A partial upgrade (one of these newer/older than the rest) is the
# canonical half-installed-toolchain symptom. This is the single source of truth
# for "the lockstep LLVM suite" — the pipeline-layer install verifier
# (_LLVM_VERSION_MATCH_SET) imports it too. ``spirv-llvm-translator`` (its own
# version scheme) and ``lib32-*`` (separate multilib lineage, may carry an
# epoch) are deliberately excluded: they are not pkgver-locked to this set.
LLVM_LOCKSTEP_SUITE: tuple[str, ...] = (
    "llvm", "llvm-libs", "clang", "lld", "compiler-rt", "polly", "openmp",
)


def _reinstall_hint(pkgs) -> str:
    """Build the toolchain reinstall suggestion (`pacman -Syu …`) for ``pkgs``."""
    return (
        "sudo pacman -Syu " + " ".join(pkgs)
        + "   # or rebuild via `sysforge run toolchain`"
    )


# Default hint (whole suite) for the not-installed / won't-run cases.
_TOOLCHAIN_REINSTALL_HINT = _reinstall_hint(LLVM_LOCKSTEP_SUITE)


def _installed_pkgver(pkg: str) -> str | None:
    """Return the installed ``pkgver-pkgrel`` for ``pkg`` (``pacman -Q``), or None."""
    r = subprocess.run(["pacman", "-Q", pkg], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    parts = r.stdout.split()
    return parts[1] if len(parts) >= 2 else None


def _pkgver_no_rel(ver: str | None) -> str | None:
    """Drop the trailing ``-pkgrel`` from a ``[epoch:]pkgver-pkgrel`` string.

    A packaging bump (e.g. ``lld 22.1.5-3`` next to ``clang 22.1.5-1``) is not an
    upstream-version skew, so the lockstep comparison is on pkgver only. pkgver
    itself never contains a hyphen (PKGBUILD(5)), so a single right-split is
    exact; the ``epoch:`` prefix is kept so two epochs of the same pkgver still
    compare distinct.
    """
    if not ver:
        return ver
    return ver.rsplit("-", 1)[0]


def _llvm_suite_skew() -> tuple[str, list[str]] | None:
    """Detect a pkgver skew across the *installed* members of the LLVM suite.

    Returns ``(detail, resync_pkgs)`` when installed members disagree on pkgver
    (``resync_pkgs`` is every installed member, so ``pacman -Syu`` converges them
    all), or ``None`` when they agree / fewer than two are installed.
    """
    installed = {
        p: v for p in LLVM_LOCKSTEP_SUITE
        if (v := _pkgver_no_rel(_installed_pkgver(p)))
    }
    if len(set(installed.values())) <= 1:
        return None
    by_ver: dict[str, list[str]] = {}
    for pkg, ver in sorted(installed.items()):
        by_ver.setdefault(ver, []).append(pkg)
    groups = " vs ".join(
        f"{'/'.join(pkgs)} {ver}" for ver, pkgs in sorted(by_ver.items())
    )
    detail = f"LLVM suite version skew ({groups}) — toolchain is half-installed"
    return detail, sorted(installed)


def _probe_cc(compiler: str) -> ToolchainCheck:
    """Verify the resolved compiler actually runs — and, for clang, that the
    whole LLVM lockstep suite shares one pkgver.

    A half-installed / mismatched LLVM toolchain (clang built against a libLLVM
    that no longer exports a symbol it needs, or a partial upgrade that leaves
    some suite members behind) makes clang fail to even start with a dynamic-link
    symbol error. Without this probe that surfaces only as N separate per-package
    "Unknown compiler(s): [['clang']]" build failures with no captured cause. The
    skew arm checks every installed member of :data:`LLVM_LOCKSTEP_SUITE` (not
    just ``clang``↔``llvm-libs``) so e.g. a stranded ``compiler-rt`` is caught,
    and the suggested fix lists every member that needs resyncing.
    """
    base = Path(compiler).name
    name = f"cc:{base}"
    is_clang = base.startswith("clang")
    install_hint = _TOOLCHAIN_REINSTALL_HINT if is_clang else f"pacman -S {base}"
    if not shutil.which(base):
        return ToolchainCheck(
            name=name, ok=False, detail=f"{base} not on PATH",
            fix_cmd=install_hint, auto_remediable=False,
        )
    r = subprocess.run([base, "--version"], capture_output=True, text=True)
    if r.returncode != 0:
        lines = (r.stderr or r.stdout or "").strip().splitlines()
        first = lines[0] if lines else f"{base} --version exited {r.returncode}"
        return ToolchainCheck(
            name=name, ok=False, detail=f"{base} cannot run: {first}",
            fix_cmd=install_hint, auto_remediable=False,
        )
    # The LLVM suite is built from one upstream release and is normally lockstep;
    # a skew (e.g. clang/lld/compiler-rt 22.1.5 against llvm/llvm-libs 22.1.6) can
    # break the ABI even when `clang --version` happens to succeed.
    if is_clang:
        skew = _llvm_suite_skew()
        if skew is not None:
            detail, resync = skew
            return ToolchainCheck(
                name=name, ok=False, detail=detail,
                fix_cmd=_reinstall_hint(resync), auto_remediable=False,
            )
    first = (r.stdout or r.stderr).strip().splitlines()
    return ToolchainCheck(
        name=name, ok=True, detail=first[0] if first else f"{base} OK",
        fix_cmd=None, auto_remediable=False,
    )


def _probe_one(token: str) -> ToolchainCheck:
    if token.startswith("cc:"):
        return _probe_cc(token[len("cc:") :])
    if token == "rust:native":
        return _probe_rust_native()
    if token.startswith("rust:cross:"):
        body = token[len("rust:cross:") :]
        target, _, pin = body.partition("@")
        return _probe_rust_cross(target, pin or None)
    if token == "cmake":
        return _probe_cmake()
    if token == "meson":
        return _probe_meson()
    return ToolchainCheck(
        name=token, ok=True,
        detail=f"no probe registered for {token!r} — assuming OK",
        fix_cmd=None, auto_remediable=False,
    )


def run_preflight(required: frozenset[str]) -> ToolchainPreflightReport:
    """Probe every required toolchain. Sequential — probes are sub-second."""
    checks = tuple(_probe_one(tok) for tok in sorted(required))
    return ToolchainPreflightReport(checks=checks)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_preflight(report: ToolchainPreflightReport) -> str:
    """Render a human-readable pre-flight table.

    Output style mirrors :func:`sysforge.primitives.llvm_state.render_preflight`
    so the two preflight blocks line up visually in update output.
    """
    if not report.checks:
        return ""

    header = f"  [{_TAG}]" + " " * max(1, 17 - len(_TAG) - 2)
    lines: list[str] = []
    lines.append(
        f"{header}Toolchain pre-flight ({len(report.checks)} check"
        f"{'s' if len(report.checks) != 1 else ''})"
    )

    for c in report.checks:
        status = "ok " if c.ok else "FAIL"
        lines.append(f"    {status}  {c.name:<40}  {c.detail}")

    failed = report.failed
    if failed:
        lines.append("")
        lines.append(f"{header}fix:")
        for c in failed:
            if c.fix_cmd:
                tag = " (auto)" if c.auto_remediable else ""
                lines.append(f"    {c.name}: {c.fix_cmd}{tag}")
            else:
                lines.append(f"    {c.name}: (no clean automatic fix — investigate manually)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-remediation
# ---------------------------------------------------------------------------

def _run_fix(fix_cmd: str) -> tuple[int, str]:
    """Execute a fix command via shell. Returns ``(exit, combined_output)``.

    Shell is used so the same string we *print to the user as the suggested
    fix* is what we run — pasting from the preflight output and running it
    here must be byte-identical.
    """
    r = subprocess.run(
        fix_cmd, shell=True, capture_output=True, text=True,
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def auto_remediate(
    report: ToolchainPreflightReport,
    *,
    non_interactive: bool = False,
) -> ToolchainPreflightReport:
    """For each ``auto_remediable`` failure, prompt + run the fix, then re-probe.

    Returns a fresh report reflecting the post-remediation state. Failures
    that aren't auto-remediable, or that the user declines, stay in the
    returned report.
    """
    if not report.failed:
        return report

    remediated_tokens: set[str] = set()

    for c in report.failed:
        if not c.auto_remediable or not c.fix_cmd:
            continue

        if non_interactive or not is_interactive():
            _log.info(
                f"non-interactive: skipping auto-remediation for {c.name}; "
                f"suggested fix: {c.fix_cmd}"
            )
            continue

        answer = prompt_choice(
            f"Run `{c.fix_cmd}` to fix {c.name}? [Y/n] ",
            choices=["y", "n"], default="y", eof_default="n",
            tag=_TAG,
        )
        if answer != "y":
            continue

        _log.info(f"Running: {c.fix_cmd}")
        rc, out = _run_fix(c.fix_cmd)
        if rc != 0:
            _log.warn(f"{c.fix_cmd!r} failed (exit {rc}):\n{out}")
            continue
        remediated_tokens.add(c.name)

    if not remediated_tokens:
        return report

    # Re-probe everything that was remediated.
    fresh_required = frozenset(c.name for c in report.checks if c.name in remediated_tokens)
    fresh = {c.name: c for c in run_preflight(fresh_required).checks}
    new_checks = tuple(
        fresh.get(c.name, c) for c in report.checks
    )
    return ToolchainPreflightReport(checks=new_checks)
