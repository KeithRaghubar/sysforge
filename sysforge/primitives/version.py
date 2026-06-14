# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
version.py — Arch package version comparison

Public API:
    vercmp(ver_a, ver_b) -> int      Compare two Arch version strings (-1, 0, 1)
    format_version(globals_) -> str  Assemble epoch:pkgver-pkgrel from PKGBUILD globals
"""
import subprocess

from sysforge import log
_log = log.get_logger("VERSION")


def vercmp(ver_a: str, ver_b: str) -> int:
    """
    Compare two Arch version strings using the system vercmp binary.

    Epoch must be included in the string if non-zero: "1:2.3.0-1" format.
    Returns -1 (a < b), 0 (equal), or 1 (a > b).
    Raises RuntimeError if vercmp is not found or returns unexpected output.
    """
    _log.info(f"vercmp {ver_a!r} {ver_b!r}")
    try:
        result = subprocess.run(
            ["vercmp", ver_a, ver_b],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError("vercmp not found on PATH — is pacman installed?")

    stdout = result.stdout.strip()
    try:
        n = int(stdout)
    except ValueError:
        raise RuntimeError(f"vercmp returned unexpected output: {stdout!r}")

    if n < 0:
        return -1
    if n > 0:
        return 1
    return 0


def format_version(globals_: dict) -> str:
    """
    Assemble a version string from parsed PKGBUILD globals.

    Format: [epoch:]pkgver-pkgrel
    Epoch prefix is omitted when epoch is "0" or absent.
    """
    pkgver = globals_.get("pkgver", "")
    pkgrel = globals_.get("pkgrel", "1")
    epoch = globals_.get("epoch", "0")

    base = f"{pkgver}-{pkgrel}"
    if epoch and epoch != "0":
        return f"{epoch}:{base}"
    return base
