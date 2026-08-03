# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Post-build change summary for pipeline stages (2.6.1-F24).

Diffs two snapshots of the pacman local DB to state what a stage actually
changed. Snapshot diffing is authoritative where stage-side bookkeeping is
not: it captures the target package, its split members, and any dependency
pulled in during the build, with no per-stage instrumentation.

Layering: this is a leaf primitive. It may import ``render`` and ``pacman``
and never reaches up into ``sysforge.pipeline``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sysforge.primitives import pacman
from sysforge.primitives.render import arrow, em_dash, fmt_bytes, version_pair


class SnapshotError(RuntimeError):
    """A local-DB snapshot could not be taken. Yields the UNKNOWN outcome."""


@dataclass(frozen=True)
class PkgFacts:
    """What one installed package contributes to the summary."""

    version: str
    isize: int | None = None


@dataclass(frozen=True)
class ChangeRow:
    """One package's transition. ``old`` None = added; ``new`` None = removed."""

    name: str
    old: PkgFacts | None
    new: PkgFacts | None


@dataclass(frozen=True)
class ExtraBlock:
    """A stage-supplied block rendered below the version rows.

    Lines are pre-formatted by the stage; the renderer only indents them, so
    the runner never needs to know what (say) a kconfig symbol is.
    """

    label: str
    lines: list[str] = field(default_factory=list)


class ChangeOutcome(StrEnum):
    """What the summary can honestly claim about a stage's effect.

    Derived from two independent facts — did the stage raise, and did the diff
    show changes. Reporting-only: this never influences exit codes or the
    runner's success determination. ``stage.run()`` raising remains the sole
    authority, so a misclassification can mislead but can never break a build.
    """

    COMPLETE = "complete"  # ran ok, applied changes
    NO_CHANGES = "no-changes"  # ran ok, genuine no-op
    PARTIAL = "partial"  # raised after applying some changes
    NONE_APPLIED = "none-applied"  # raised before applying anything
    UNKNOWN = "unknown"  # snapshot or diff failed; nothing claimable


def classify(
    rows: list[ChangeRow],
    *,
    stage_failed: bool,
    unavailable: str | None = None,
) -> ChangeOutcome:
    """Classify a stage's effect. ``unavailable`` wins over everything else."""
    if unavailable is not None:
        return ChangeOutcome.UNKNOWN
    if stage_failed:
        return ChangeOutcome.PARTIAL if rows else ChangeOutcome.NONE_APPLIED
    return ChangeOutcome.COMPLETE if rows else ChangeOutcome.NO_CHANGES


def snapshot(root: Path | None = None) -> dict[str, PkgFacts]:
    """Return ``{pkgname: PkgFacts}`` for the given root (None = live root).

    Raises SnapshotError if the local DB cannot be read, so the caller can
    classify the run as UNKNOWN rather than mistaking it for "no changes".
    """
    try:
        raw = (
            pacman.get_installed_facts(root=root)
            if root is not None
            else pacman.get_installed_facts()
        )
    except Exception as e:  # noqa: BLE001 — any read failure is a snapshot failure
        raise SnapshotError(str(e)) from e
    return {name: PkgFacts(version=ver, isize=isize) for name, (ver, isize) in raw.items()}


def diff(
    before: dict[str, PkgFacts],
    after: dict[str, PkgFacts],
) -> list[ChangeRow]:
    """Diff two snapshots into changed, then added, then removed rows.

    A package whose version is unchanged but whose installed size moved is
    still a change — a rebuild at the same pkgrel that shrinks the package is
    exactly the kind of thing this surface exists to show.
    """
    changed: list[ChangeRow] = []
    added: list[ChangeRow] = []
    removed: list[ChangeRow] = []

    for name in sorted(after):
        new = after[name]
        old = before.get(name)
        if old is None:
            added.append(ChangeRow(name=name, old=None, new=new))
        elif old != new:
            changed.append(ChangeRow(name=name, old=old, new=new))

    for name in sorted(before):
        if name not in after:
            removed.append(ChangeRow(name=name, old=before[name], new=None))

    return changed + added + removed


def _fmt_row(row: ChangeRow, *, name_width: int, show_size: bool) -> str:
    """One package line: padded name, version pair, optional size pair + delta."""
    old_ver = row.old.version if row.old else None
    new_ver = row.new.version if row.new else None
    # equal_marker=False: a stage reports what it applied, so an unchanged
    # version still reads as a transition (matching update_summary._fmt_pkg).
    line = f"{row.name:<{name_width}}  {version_pair(old_ver, new_ver, equal_marker=False)}"

    if not show_size:
        return line

    old_size = row.old.isize if row.old else None
    new_size = row.new.isize if row.new else None
    if old_size is None and new_size is None:
        return line

    if old_size is not None and new_size is not None:
        sizes = f"{fmt_bytes(old_size)} {arrow()} {fmt_bytes(new_size)}"
        delta = new_size - old_size
    else:
        known = new_size if new_size is not None else old_size
        assert known is not None  # noqa: S101 — guarded by the both-None return above
        sizes = fmt_bytes(known)
        delta = known if new_size is not None else -known

    sign = "+" if delta >= 0 else "-"
    return f"{line}   {sizes}  ({sign}{fmt_bytes(abs(delta))})"


def _header(stage: str, rows: list[ChangeRow], outcome: ChangeOutcome, reason: str | None) -> str:
    """The one-line ``[SYSFORGE] <Stage> stage ...`` summary, keyed by outcome."""
    label = stage.capitalize()
    if outcome is ChangeOutcome.UNKNOWN:
        why = reason or "reason unrecorded"
        return f"\n[SYSFORGE] {label} stage: change summary unavailable ({why})."
    if outcome is ChangeOutcome.NO_CHANGES:
        return f"\n[SYSFORGE] {label} stage: no package changes (nothing to apply)."
    if outcome is ChangeOutcome.NONE_APPLIED:
        return (
            f"\n[SYSFORGE] {label} stage FAILED before applying changes "
            f"{em_dash()} system unchanged."
        )

    updated = sum(1 for r in rows if r.old and r.new)
    added = sum(1 for r in rows if r.old is None)
    removed = sum(1 for r in rows if r.new is None)
    counts = ", ".join(
        part for part, n in (
            (f"{updated} updated", updated),
            (f"{added} added", added),
            (f"{removed} removed", removed),
        ) if n
    )
    if outcome is ChangeOutcome.PARTIAL:
        return f"\n[SYSFORGE] {label} stage FAILED after applying changes: {counts}."
    return f"\n[SYSFORGE] {label} stage changes: {counts}."


def render(
    rows: list[ChangeRow],
    *,
    stage: str,
    outcome: ChangeOutcome,
    extras: Sequence[ExtraBlock] = (),
    reason: str | None = None,
    emit: Callable[[str], None] = print,
) -> None:
    """Render the change summary line-by-line through ``emit``.

    ``emit`` defaults to :func:`print` so tests capture stdout, mirroring
    ``update_summary._print_result_summary``; the runner passes a ``log.ui``
    binding so the summary lands in the unified log too. Indentation matches
    that renderer's two-space label / four-space body grammar.
    """
    emit(_header(stage, rows, outcome, reason))

    def _section(label: str, lines: list[str]) -> None:
        if not lines:
            return
        emit(f"  {label}")
        for line in lines:
            emit(f"    {line}")

    if rows:
        name_width = max(len(r.name) for r in rows)
        # The size column is dropped entirely rather than printing a column of
        # dashes when no row carries size data (e.g. the -Qi fallback failed).
        show_size = any(
            (r.old and r.old.isize is not None) or (r.new and r.new.isize is not None)
            for r in rows
        )
        groups = (
            ("Updated:", [r for r in rows if r.old and r.new]),
            ("Added:", [r for r in rows if r.old is None]),
            ("Removed:", [r for r in rows if r.new is None]),
        )
        for label, group in groups:
            _section(label, [
                _fmt_row(r, name_width=name_width, show_size=show_size) for r in group
            ])

    for block in extras:
        _section(block.label, list(block.lines))
