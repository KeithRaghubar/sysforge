# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
payload_layout.py — post-build lint for config placed where its consumer never
looks (3.2.0-F15).

``cosmic-greeter-git`` once installed its PAM file with ``install -t
"$pkgdir/etc/pam.d/cosmic-greeter/"``: the payload carried a *directory*
``/etc/pam.d/cosmic-greeter/`` where PAM resolves a service to the flat file
``/etc/pam.d/<service>``. PAM fell through to ``pam.d/other``, no
``XDG_RUNTIME_DIR`` was created, and the compositor panicked on every greeter
restart. Nothing in the build, ``pacman -Qkk`` (which checks the payload against
its own manifest, not the consumer's lookup rules) or the install noticed.

The rule is deliberately narrow: it fires only for directories whose spec says
the consumer reads flat files and does not recurse (:data:`NON_RECURSIVE_DIRS`,
cited by standards row 28), which keeps it free of false positives on the many
config trees that legitimately nest. Non-fatal by contract — a layout the table
does not model is a false positive, and a build is expensive.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from sysforge import log
from sysforge.primitives import run

_log = log.get_logger("ABI")

#: Archive-relative directory → the spec saying its consumer reads only flat
#: files there. Standards row 28 is this table's citation.
NON_RECURSIVE_DIRS: dict[str, str] = {
    "etc/pam.d": "pam.d(5)",
    "etc/sudoers.d": "sudoers(5)",
    "usr/lib/sysusers.d": "sysusers.d(5)",
    "usr/lib/tmpfiles.d": "tmpfiles.d(5)",
    "etc/ld.so.conf.d": "ld.so.conf(5)",
}


# This run's findings, carried to the end-of-run summary (the ``ui()`` home for
# check results) — the mid-run ``warn()`` is ``-v`` only, and a finding hidden
# at default verbosity is the outage it exists to prevent. Same shape as
# ``cache_probe``'s session: ``build_core`` resets it per run.
_SESSION: list[str] = []


def reset_session() -> None:
    _SESSION.clear()


def session_findings() -> list[str]:
    return list(_SESSION)


@dataclass(frozen=True)
class LayoutFinding:
    member: str
    directory: str
    spec: str

    @property
    def message(self) -> str:
        return (
            f"{self.member} is nested inside /{self.directory}, whose consumer "
            f"reads only flat files there ({self.spec}) — it will never be read"
        )


def nested_members(names) -> list[LayoutFinding]:
    """Members sitting deeper than a non-recursive consumer looks. Pure.

    Directory entries themselves are skipped: the file inside is the thing the
    consumer misses, and naming it is what points at the fix.
    """
    findings: list[LayoutFinding] = []
    for raw in names:
        name = raw.strip().lstrip("./")
        if not name or name.endswith("/"):
            continue
        for directory, spec in NON_RECURSIVE_DIRS.items():
            prefix = directory + "/"
            if name.startswith(prefix) and "/" in name[len(prefix):]:
                findings.append(LayoutFinding(name, directory, spec))
    return findings


def check_package_layout(pkg_path: Path) -> list[LayoutFinding]:
    """One archive walk (``bsdtar -t``, names only) — the listing the ABI check
    already uses, with a different predicate. Raises on a failed listing so the
    caller's single non-fatal handler reports it."""
    result = run.probe(["bsdtar", "-t", "-f", str(pkg_path)], stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"bsdtar list failed for {Path(pkg_path).name}: "
                           f"{result.stderr.strip()}")
    return nested_members(result.stdout.splitlines())


def report_payload_layout(built_pkgs) -> None:
    """Warn on each misplaced member of this build's packages. Never raises —
    the build already succeeded, so the lint must not turn it red."""
    try:
        for pkg in built_pkgs:
            for finding in check_package_layout(pkg):
                line = f"{Path(pkg).name}: {finding.message}"
                _log.warn(line)
                _SESSION.append(line)
    except Exception as exc:  # noqa: BLE001 — advisory lint
        _log.warn(f"payload layout check failed: {exc}")
