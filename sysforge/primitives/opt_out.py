# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
opt_out.py — the durable "stop maintaining this" decision (3.4.0-F4).

``revert`` and ``state forget`` delete a package's ``build_state`` record, but
``update`` rebuilds its target set every run from independent adoption sources
(every foreign package, a non-inert ``packages.toml`` override,
``repo_mode = "build_from_source"``), so a reverted or forgotten package was
quietly re-adopted by the next run. This file records the decision itself.

Stored as ``<state_dir>/opted-out.toml``, a sibling of
``artifacts-ignored.toml`` and for the same reason: ``build_state.toml`` is
rewritten from the install set every run, and the user's "no" must survive
that. One record per pkgbase, carrying every member pkgname the decision
covered (renamed and stock spellings), so a split set opts out together and a
candidate is matched by name with no ``%BASE%`` lookup.

Written only through ``verbs.shared.forget_packages(opt_out=…)``; subtracted in
``update_assemble._assemble_package_set``; cleared only by an explicit
adoption (``build <pkg>``, ``update <pkg>``, ``packages add <pkg>``).
"""
from __future__ import annotations

import tomllib
from datetime import datetime, timezone
from pathlib import Path

FILE_NAME = "opted-out.toml"

# The verb that recorded the decision.
VIA_REVERT = "revert"
VIA_FORGET = "forget"


class OptOutError(Exception):
    """The opt-out file exists but could not be read or parsed.

    Raised rather than read as empty: ``save()`` writes the complete set, so
    folding corruption into "empty" would let the next save erase every
    recorded decision, and ``update`` would re-adopt all of them silently.
    """


def _esc(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


class OptOutList:
    """Persistent pkgbase -> ``{"via", "at", "pkgnames"}`` record."""

    def __init__(self, state_dir):
        self._state_dir = Path(state_dir)

    @property
    def path(self) -> Path:
        return self._state_dir / FILE_NAME

    def load(self) -> dict[str, dict]:
        """Parse the file. Missing → empty; corrupt → :class:`OptOutError`."""
        if not self.path.exists():
            return {}
        try:
            raw = tomllib.loads(self.path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise OptOutError(
                f"opt-out record at {self.path} is corrupt and could not be "
                "parsed as TOML; repair it by hand or remove it (which lets "
                "`update` re-adopt every reverted or forgotten package)"
            ) from exc
        except OSError as exc:
            raise OptOutError(
                f"opt-out record at {self.path} exists but could not be read "
                f"({exc}); check permissions"
            ) from exc
        out: dict[str, dict] = {}
        for pkgbase, row in (raw.get("opted_out") or {}).items():
            if not isinstance(row, dict):
                continue
            names = row.get("pkgnames") or []
            out[pkgbase] = {
                "via": str(row.get("via", "")),
                "at": str(row.get("at", "")),
                "pkgnames": sorted({str(n) for n in names if n} | {pkgbase}),
            }
        return out

    def save(self, entries: dict[str, dict]) -> None:
        """Write the complete set atomically (temp file + replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# sysforge opt-out record: packages `update` must not re-adopt.",
            "# Written by `revert` / `state forget`; cleared by `build <pkg>`,",
            "# `update <pkg>` or `packages add <pkg>`. Hand-edits are overwritten.",
            "",
        ]
        for pkgbase in sorted(entries):
            e = entries[pkgbase]
            names = ", ".join(f'"{_esc(n)}"' for n in e.get("pkgnames", []))
            lines.append(f'[opted_out."{_esc(pkgbase)}"]')
            lines.append(f'via = "{_esc(e.get("via", ""))}"')
            lines.append(f'at = "{_esc(e.get("at", ""))}"')
            lines.append(f"pkgnames = [{names}]")
            lines.append("")
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text("\n".join(lines))
        tmp.replace(self.path)

    def add(self, pkgbase: str, pkgnames, *, via: str) -> None:
        """Record (or refresh) the decision for ``pkgbase`` and save."""
        entries = self.load()
        prior = set(entries.get(pkgbase, {}).get("pkgnames", []))
        entries[pkgbase] = {
            "via": via,
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pkgnames": sorted(prior | {n for n in pkgnames if n} | {pkgbase}),
        }
        self.save(entries)

    def clear(self, names) -> list[str]:
        """Drop every record covering any of ``names``; return the pkgbases.

        A name matches a record by its pkgbase or any member pkgname, so
        ``build vulkan-radeon`` clears the whole ``mesa`` split set. Saves only
        when something was cleared, so a read of a missing file stays a no-op.
        """
        entries = self.load()
        wanted = set(names or ())
        cleared = sorted(pb for pb, e in entries.items()
                         if wanted & set(e.get("pkgnames", [])))
        if cleared:
            for pb in cleared:
                del entries[pb]
            self.save(entries)
        return cleared


def covering(entries: dict[str, dict], name: str) -> str | None:
    """The pkgbase whose record covers ``name``, else None."""
    for pkgbase, e in entries.items():
        if name in e.get("pkgnames", ()):
            return pkgbase
    return None
