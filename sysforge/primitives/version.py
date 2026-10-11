# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
version.py — Arch package version comparison

Public API:
    vercmp(ver_a, ver_b) -> int      Compare two Arch version strings (-1, 0, 1)
    vercmp_local(ver_a, ver_b) -> int  The same comparison in pure Python (no subprocess)
    format_version(globals_) -> str  Assemble epoch:pkgver-pkgrel from PKGBUILD globals
"""

from sysforge import log
from sysforge.primitives import run
_log = log.get_logger("VERSION")


def vercmp(ver_a: str, ver_b: str) -> int:
    """
    Compare two Arch version strings using the system vercmp binary.

    Epoch must be included in the string if non-zero: "1:2.3.0-1" format.
    Returns -1 (a < b), 0 (equal), or 1 (a > b).
    Raises RuntimeError if vercmp is not found or returns unexpected output.
    """
    result = run.capture(["vercmp", ver_a, ver_b])
    if result is None:
        raise RuntimeError("vercmp not found on PATH: is pacman installed?")

    stdout = result.stdout.strip()
    try:
        n = int(stdout)
    except ValueError:
        raise RuntimeError(f"vercmp returned unexpected output: {stdout!r}") from None

    verdict = -1 if n < 0 else (1 if n > 0 else 0)
    # Log the operands *with* the result. Logging them before the comparison
    # (as this did) leaves a log line that states a question and never answers
    # it — useless when reading sysforge-update.log after the fact.
    _log.info(f"vercmp {ver_a!r} {ver_b!r} -> {verdict}")
    return verdict


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


# ---------------------------------------------------------------------------
# vercmp_local: a pure-Python port of libalpm's alpm_pkg_vercmp (3.4.0-F8)
#
# For presentation: render.classify_version_change runs it once per summary
# row, where a vercmp(8) subprocess (plus its info() line) per row would be
# the cost of a colour. Decisions that change what gets built keep calling
# vercmp() above. tests/test_version.py cross-checks this port against the
# binary over a corpus of awkward pairs, so the two cannot drift silently.
# ---------------------------------------------------------------------------

def _is_digit(c: str) -> bool:
    return "0" <= c <= "9"


def _is_alpha(c: str) -> bool:
    return "a" <= c <= "z" or "A" <= c <= "Z"


def _rpmvercmp(a: str, b: str) -> int:
    """libalpm's rpmvercmp: segment-wise compare of one version component."""
    if a == b:
        return 0
    i = j = 0
    n, m = len(a), len(b)
    while i < n and j < m:
        si, sj = i, j
        while i < n and not (_is_digit(a[i]) or _is_alpha(a[i])):
            i += 1
        while j < m and not (_is_digit(b[j]) or _is_alpha(b[j])):
            j += 1
        if i >= n or j >= m:
            break
        if (i - si) != (j - sj):
            return -1 if (i - si) < (j - sj) else 1
        isnum = _is_digit(a[i])
        same_kind = _is_digit if isnum else _is_alpha
        ei, ej = i, j
        while ei < n and same_kind(a[ei]):
            ei += 1
        while ej < m and same_kind(b[ej]):
            ej += 1
        if ej == j:
            # Segments of different kinds: a number beats letters.
            return 1 if isnum else -1
        seg_a, seg_b = a[i:ei], b[j:ej]
        if isnum:
            seg_a, seg_b = seg_a.lstrip("0"), seg_b.lstrip("0")
            if len(seg_a) != len(seg_b):
                return 1 if len(seg_a) > len(seg_b) else -1
        if seg_a != seg_b:
            return 1 if seg_a > seg_b else -1
        i, j = ei, ej
    if i >= n and j >= m:
        return 0
    # A remaining alpha segment never beats an empty one.
    rest_a = a[i] if i < n else ""
    rest_b = b[j] if j < m else ""
    if (not rest_a and not _is_alpha(rest_b)) or _is_alpha(rest_a):
        return -1
    return 1


def _parse_evr(ver: str) -> tuple[str, str, str | None]:
    """Split ``[epoch:]pkgver[-pkgrel]`` the way libalpm's parseEVR does."""
    epoch = "0"
    head, sep, rest = ver.partition(":")
    if sep and head.isdigit() and head.isascii():
        epoch, ver = head, rest
    elif sep and not head:
        ver = rest
    pkgver, dash, pkgrel = ver.rpartition("-")
    if not dash:
        return epoch, ver, None
    return epoch, pkgver, pkgrel


def vercmp_local(ver_a: str, ver_b: str) -> int:
    """Compare two Arch version strings without a subprocess: -1, 0 or 1.

    Same answer as :func:`vercmp` (libalpm's ``alpm_pkg_vercmp``): epochs
    first, then pkgver, then pkgrel only when both sides carry one.
    """
    if ver_a == ver_b:
        return 0
    ea, va, ra = _parse_evr(ver_a)
    eb, vb, rb = _parse_evr(ver_b)
    ret = _rpmvercmp(ea, eb)
    if ret == 0:
        ret = _rpmvercmp(va, vb)
        if ret == 0 and ra is not None and rb is not None:
            ret = _rpmvercmp(ra, rb)
    return ret
