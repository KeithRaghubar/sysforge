# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
os_release.py — the single home for distro identity (``os-release(5)``).

SysForge deliberately has no distro-conditional behaviour: it assumes
``pacman``/``makepkg`` and nothing narrower, which is why it ports to an
Arch derivative unchanged. What it lacked was anywhere to *report* the running
distro, so a user on a derivative had no way to see what sysforge thinks it is
running on, and a portability regression had no observable surface.

This module is that surface, and the only permitted one. Identity must never be
inferred from ``pacman.conf`` section names, ``/etc/arch-release``, the
hostname, or a mirror URL — those are the assumptions that break derivatives.

Parsing follows ``os-release(5)``:

  - ``/etc/os-release`` first, then ``/usr/lib/os-release`` (the spec makes the
    former a symlink to the latter, but only the latter is guaranteed present).
  - Lines are shell-compatible ``KEY=value`` assignments; ``#`` comments and
    blank lines are ignored. Values may be single- or double-quoted.
  - ``ID`` defaults to ``linux`` when absent, per the spec.
  - ``ID_LIKE`` is a space-separated, most-closely-related-first list of parent
    distro IDs. A derivative of Arch carries ``ID_LIKE=arch``.

Public API:
    DistroIdentity
    read_os_release(paths=None) -> dict[str, str]
    identify(paths=None) -> DistroIdentity
    collect_distro_findings(*, explicit=False, paths=None) -> list[Finding]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sysforge.primitives import diagnostics as diag

# Spec-ordered lookup: /etc wins because it is the local-administrator copy.
DEFAULT_PATHS: tuple[Path, ...] = (
    Path("/etc/os-release"),
    Path("/usr/lib/os-release"),
)

# The base sysforge targets. Anything else Arch-derived is expected to work but
# is not validated per-release; anything not Arch-derived is out of scope.
PRIMARY_ID = "arch"

# os-release(5): "If not set, a default of ID=linux may be used."
_DEFAULT_ID = "linux"


@dataclass(frozen=True)
class DistroIdentity:
    """The subset of ``os-release(5)`` fields sysforge reports on.

    ``source`` is the file the values came from, or ``None`` when no
    ``os-release`` was readable — the one case where identity is unknown rather
    than merely unfamiliar.
    """
    id: str = _DEFAULT_ID
    id_like: tuple[str, ...] = ()
    name: str = ""
    pretty_name: str = ""
    build_id: str = ""
    source: Path | None = None
    fields: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def known(self) -> bool:
        """Whether an ``os-release`` file was actually read."""
        return self.source is not None

    @property
    def is_primary(self) -> bool:
        """Arch itself — the fully validated base."""
        return self.id == PRIMARY_ID

    @property
    def is_arch_derived(self) -> bool:
        """Arch, or a distro declaring Arch as a parent via ``ID_LIKE``.

        Derivatives are in scope: sysforge's packaging invariants forbid the
        repo-name and toolchain-default assumptions that would break them.
        """
        return self.is_primary or PRIMARY_ID in self.id_like

    @property
    def label(self) -> str:
        """Human-readable identity for a report line."""
        pretty = self.pretty_name or self.name
        base = f"{pretty} (ID={self.id}" if pretty else f"ID={self.id}"
        if self.id_like:
            base += f", ID_LIKE={' '.join(self.id_like)}"
        return base + ")"


def _unquote(value: str) -> str:
    """Strip one layer of matching shell quotes.

    Deliberately not a full shell unescape: os-release values are constrained
    to printable ASCII with only ``\\"``/``\\$``/``\\`` escaping permitted, and
    every field sysforge reads is a plain token or a display string.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def read_os_release(paths: tuple[Path, ...] | None = None) -> dict[str, str]:
    """Parse the first readable ``os-release`` into a key → value mapping.

    Returns an empty dict when none is readable — callers distinguish "no
    os-release" from "unfamiliar distro" by the emptiness, which is what
    :attr:`DistroIdentity.known` exposes.
    """
    for path in paths or DEFAULT_PATHS:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            key = key.strip()
            if key:
                out[key] = _unquote(raw)
        # A file that exists but parses to nothing is treated as unreadable, so
        # a truncated os-release doesn't masquerade as ID=linux.
        if out:
            out["_source"] = str(path)
            return out
    return {}


def identify(paths: tuple[Path, ...] | None = None) -> DistroIdentity:
    """Read and structure the running distro's identity."""
    fields = read_os_release(paths)
    if not fields:
        return DistroIdentity()
    source = fields.pop("_source", "")
    return DistroIdentity(
        id=(fields.get("ID") or _DEFAULT_ID).strip().lower(),
        id_like=tuple(fields.get("ID_LIKE", "").lower().split()),
        name=fields.get("NAME", ""),
        pretty_name=fields.get("PRETTY_NAME", ""),
        build_id=fields.get("BUILD_ID", ""),
        source=Path(source) if source else None,
        fields=fields,
    )


def collect_distro_findings(
    *,
    explicit: bool = False,
    paths: tuple[Path, ...] | None = None,
) -> list[diag.Finding]:
    """Report the running distro and its support tier (doctor ``distro`` axis).

    Quiet on the primary base unless ``explicit`` — a plain Arch host learns
    nothing from being told it is Arch, so the axis renders its ``clean_msg``
    instead. ``explicit`` (the user passed ``--distro``) always emits the
    identity line, which is the whole point of asking.

    Severity ladder: an Arch derivative is INFO (in scope, packaging invariants
    hold, VM-tier checks unvalidated); a non-derived distro and an unreadable
    ``os-release`` are both WARN, never errors — sysforge may well still work,
    and doctor's exit code should not turn on a support tier.
    """
    ident = identify(paths)

    if not ident.known:
        return [diag.Finding(
            "distro", diag.SEV_WARN, "distro_unknown",
            "no readable os-release: cannot identify the running distribution",
            remediation="expected /etc/os-release or /usr/lib/os-release "
                        "(os-release(5)); sysforge assumes an Arch-derived host")]

    if not ident.is_arch_derived:
        return [diag.Finding(
            "distro", diag.SEV_WARN, "distro_unsupported",
            f"{ident.label} does not declare Arch as a base",
            remediation="sysforge assumes pacman/makepkg and an Arch-derived "
                        "package set; behaviour here is untested")]

    if not ident.is_primary:
        return [diag.Finding(
            "distro", diag.SEV_INFO, "distro_derivative",
            f"{ident.label} — Arch-derived",
            remediation="packaging, dependency-resolution and makepkg.conf "
                        "invariants are validated here; bootstrap, kernel "
                        "staging and graphics/DKMS checks are Arch-only")]

    if explicit:
        return [diag.Finding(
            "distro", diag.SEV_INFO, "distro_primary",
            f"{ident.label} — primary supported base")]

    return []
