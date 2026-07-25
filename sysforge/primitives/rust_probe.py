# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
rust_probe.py — Rust-toolchain provenance diagnostics (doctor ``rust`` axis).

Read-only, advisory report of which Rust toolchain a ``cargo``/``rustc`` build
will actually use. Three concerns are invisible otherwise:

  - the *effective* toolchain: distro ``rust`` package vs a ``rustup``-managed
    channel (and which channel);
  - a non-``stable`` global default (nightly/beta/pinned) silently affecting
    every Rust build — the live case on this workstation;
  - a tree-level ``rust-toolchain.toml`` pin that overrides the default and can
    make rustup *download* a toolchain mid-build (see rust_probe pin findings,
    Task 2).

Emits only INFO/WARN — never ERROR. Mutates nothing; never rewrites a pin.
Returns :class:`diagnostics.Finding` (category ``"rust"``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

from sysforge.primitives import config
from sysforge.primitives import diagnostics as diag

_CATEGORY = "rust"


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except (FileNotFoundError, OSError):
        return None


def _which(tool: str) -> str | None:
    return shutil.which(tool)


def _owner_pkg(path: str) -> str | None:
    """`pacman -Qo <path>` → owning pkgname, or None if unowned/unavailable.

    Output form: ``/usr/bin/cargo is owned by rustup 1.27.1-1``.
    """
    proc = _run(["pacman", "-Qo", path])
    if proc is None or proc.returncode != 0:
        return None
    parts = proc.stdout.split(" is owned by ")
    if len(parts) != 2:
        return None
    return parts[1].split()[0] or None


def _is_rustup_layout(path: str) -> bool:
    """True iff *path* lives inside a rustup-managed tree.

    Covers the upstream shell installer, which is not pacman-managed: its
    ``cargo`` shadows ``/usr/bin/cargo`` on PATH but has no owning package, so
    ``_owner_pkg`` returns None. Honours ``RUSTUP_HOME``/``CARGO_HOME`` before
    falling back to the default ``~/.cargo`` root.
    """
    candidates = []
    for env in ("RUSTUP_HOME", "CARGO_HOME"):
        val = os.environ.get(env)
        if val:
            candidates.append(Path(val))
    candidates.append(Path.home() / ".cargo")
    resolved = Path(path)
    for root in candidates:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _channel_of(active: str) -> str:
    """Channel token from a ``rustup show active-toolchain`` line.

    ``stable-x86_64-unknown-linux-gnu (default)`` → ``stable``;
    ``1.75.0-x86_64-...`` → ``1.75.0``.
    """
    first = active.strip().split()[0] if active.strip() else ""
    for ch in ("stable", "nightly", "beta"):
        if first.startswith(ch):
            return ch
    # numeric pin like 1.75.0-<triple>: strip the target triple.
    return first.split("-")[0] if first else ""


def _rustup_active() -> str | None:
    """`rustup show active-toolchain` first line, or None on failure."""
    proc = _run(["rustup", "show", "active-toolchain"])
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.splitlines()[0].strip()


def _rust_pkg_version() -> str:
    proc = _run(["pacman", "-Q", "rust"])
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return "rust (version unknown)"
    return proc.stdout.strip()


def collect_active_findings() -> list[diag.Finding]:
    """Cases 1, 2, and no-toolchain. See module docstring."""
    cargo = _which("cargo") or _which("rustc")
    if cargo is None:
        return [diag.Finding(
            category=_CATEGORY, severity=diag.SEV_INFO, check_id="rust-none",
            message="no Rust toolchain detected (no cargo/rustc on PATH)")]

    owner = _owner_pkg(cargo)
    # An unowned cargo inside a rustup tree is the upstream shell installer —
    # rustup all the same, just not pacman-managed. Without this the probe would
    # fall through and mislabel it as the distro `rust` package (2.5.1-B10).
    user_local = owner is None and _is_rustup_layout(cargo)
    if owner == "rustup" or user_local:
        provenance = "rustup (user-local)" if user_local else "rustup"
        active = _rustup_active()
        if active is None:
            return [diag.Finding(
                category=_CATEGORY, severity=diag.SEV_WARN,
                check_id="rust-active",
                message="rustup owns cargo but `rustup show active-toolchain` "
                        "failed; effective toolchain unknown")]
        channel = _channel_of(active)
        findings = [diag.Finding(
            category=_CATEGORY, severity=diag.SEV_INFO, check_id="rust-active",
            message=f"effective Rust toolchain: {provenance} `{active}`")]
        if channel != "stable":
            findings.append(diag.Finding(
                category=_CATEGORY, severity=diag.SEV_WARN,
                check_id="rust-nightly-default",
                message=f"active rustup toolchain is non-stable "
                        f"(`{channel}`); every Rust build uses it",
                remediation="run `rustup default stable` to restore the "
                            "stable default, or set a per-project override"))
        return findings

    # Distro rust package (or any non-rustup owner).
    return [diag.Finding(
        category=_CATEGORY, severity=diag.SEV_INFO, check_id="rust-active",
        message=f"effective Rust toolchain: distro package "
                f"`{_rust_pkg_version()}`")]


def _pin_for_dir(src_dir: Path) -> tuple[str, str] | None:
    """`(channel, raw)` from ``rust-toolchain.toml`` in src_dir, else None.

    Raises ValueError on a present-but-unreadable file so the caller can emit
    a WARN naming it (rather than silently skipping a real pin).
    """
    pin = src_dir / "rust-toolchain.toml"
    if not pin.is_file():
        return None
    try:
        data = tomllib.loads(pin.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ValueError(str(exc)) from exc
    channel = (data.get("toolchain") or {}).get("channel")
    if not channel:
        return None
    return channel, channel


def _toolchain_installed(channel: str) -> bool:
    proc = _run(["rustup", "toolchain", "list"])
    if proc is None or proc.returncode != 0:
        return False
    return any(line.split()[0].startswith(channel)
               for line in proc.stdout.splitlines() if line.strip())


def collect_pin_findings(config_dict, packages) -> list[diag.Finding]:
    findings: list[diag.Finding] = []
    for pkg in packages or []:
        try:
            pkgbuild = config.find_pkgbuild(pkg, config_dict)
        except Exception:  # noqa: S112
            continue  # unresolvable target — nothing to say about its pin
        src_dir = Path(pkgbuild).parent
        try:
            pin = _pin_for_dir(src_dir)
        except ValueError as exc:
            findings.append(diag.Finding(
                category=_CATEGORY, severity=diag.SEV_WARN,
                check_id="rust-pin-unreadable",
                message=f"{pkg}: unreadable rust-toolchain.toml "
                        f"({src_dir}/rust-toolchain.toml): {exc}"))
            continue
        if pin is None:
            continue
        channel, _raw = pin
        if _toolchain_installed(channel):
            findings.append(diag.Finding(
                category=_CATEGORY, severity=diag.SEV_INFO, check_id="rust-pin",
                message=f"{pkg}: pins Rust `{channel}` via rust-toolchain.toml "
                        f"(installed)"))
        else:
            findings.append(diag.Finding(
                category=_CATEGORY, severity=diag.SEV_WARN,
                check_id="rust-pin-missing",
                message=f"{pkg}: pins Rust `{channel}` via rust-toolchain.toml "
                        f"— not installed; rustup will fetch it mid-build",
                remediation=f"rustup toolchain install {channel}"))
    return findings


def collect_rust_findings(config_dict, packages=None) -> list[diag.Finding]:
    """Public axis producer: effective-toolchain findings plus, for any named
    package targets, rust-toolchain.toml pin findings."""
    return collect_active_findings() + collect_pin_findings(config_dict, packages)
