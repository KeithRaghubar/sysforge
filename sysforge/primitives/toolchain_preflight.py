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


def _probe_one(token: str) -> ToolchainCheck:
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
