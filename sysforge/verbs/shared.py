# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
verbs/shared.py — cross-verb operations with a single home.

Verb modules (``sysforge/*_cmd.py``) had started importing each other:
``revert_cmd`` and ``uninstall_cmd`` both pulled ``cmd_state_forget`` out of
``state_cmd``, and ``build_cmd`` reached into ``packages_cmd`` for the
``packages.toml`` writer. Each was defensible alone; together they were the
seed of a fourth informal layer with no guard, and the direction of those edges
was arbitrary — nothing makes ``state_cmd`` the natural owner of "stop
maintaining this package" just because ``sysforge state forget`` is the verb
that spells it.

So the *home* moves here rather than the operation being duplicated, preserving
the one-home invariants (§``sysforge/CLAUDE.md``: one ``packages.toml`` writer;
demotion reuses one forget path). Verb modules import from ``verbs/``,
``primitives/`` and ``pipeline/`` — never from a sibling ``*_cmd.py``, which
``tests/test_module_layering.py`` now enforces (3.2.0-F8).

This module is for operations *shared between verbs*. Single-verb logic stays in
its own ``*_cmd.py``; small generic utilities go in ``verbs/helpers.py``.

Two clusters live here:

* **packages.toml entry shape and writer** — ``OVERRIDE_FIELDS``,
  ``entry_toml_block``, ``entry_is_inert``, ``rewrite_packages_toml``. Every
  mutation goes through the writer so header comments and surrounding
  whitespace survive and inert entries are pruned on the same write.
* **build_state demotion** — ``forget_packages``, the "hand it back to pacman"
  operation behind ``state forget``, ``revert`` and ``uninstall``.
"""
import re
import tomllib
from pathlib import Path

from sysforge import log
from sysforge.primitives.config import (
    PKG_KEY_BUILD_FROM_SOURCE,
    _LEGACY_PKG_KEY_BUILD_FROM_SOURCE,
)

_log = log.get_logger("PACKAGES")


# ---------------------------------------------------------------------------
# packages.toml entry shape
# ---------------------------------------------------------------------------

# Behavior-changing override fields. `source` is metadata (it pins routing
# but doesn't change build behavior), so it doesn't count toward the
# "at least one override" rule for `add` validation or auto-prune.
OVERRIDE_FIELDS = (PKG_KEY_BUILD_FROM_SOURCE, "cache", "reason")


def entry_toml_block(entry: dict) -> str:
    """Serialise a package entry dict to a TOML [[package]] block string."""
    lines = ["[[package]]", f'name = "{entry["name"]}"']
    if "source" in entry:
        lines.append(f'source = "{entry["source"]}"')
    for key in OVERRIDE_FIELDS:
        if key not in entry:
            continue
        val = entry[key]
        if isinstance(val, bool):
            lines.append(f"{key} = {'true' if val else 'false'}")
        elif isinstance(val, str):
            lines.append(f'{key} = "{val}"')
        else:
            lines.append(f"{key} = {val!r}")
    return "\n".join(lines)


def entry_is_inert(entry: dict) -> bool:
    """An entry is inert if it has no behavior-changing override field set.

    Deliberately legacy-agnostic. ``rewrite_packages_toml`` runs the legacy-key
    migration over the raw lines *before* parsing entries and judging inertness,
    so on the write path a pre-rename ``pkgbuild_patch`` entry already carries
    ``enable_build_from_source`` by the time it reaches here. The other callers
    (``update.py``, ``update_assemble.py``) pass raw ``expand_package_groups``
    output, where a stale ``pkgbuild_patch`` is simply *not* an override — which
    is correct as of 3.0.0, since the read-side alias was removed (2.6.1-F5).
    Either way this predicate needs no legacy-key knowledge.
    """
    return not any(k in entry for k in OVERRIDE_FIELDS)


# ---------------------------------------------------------------------------
# Line-level packages.toml writers
#
# Every mutation goes through `rewrite_packages_toml` so we (a) preserve
# header comments and surrounding whitespace and (b) auto-prune inert
# entries on the same write.
# ---------------------------------------------------------------------------

def _split_blocks(lines: list[str]) -> tuple[list[str], list[tuple[int, int]]]:
    """Locate [[package]] blocks. Returns (lines, [(start, end), ...])."""
    block_starts = [i for i, line in enumerate(lines) if line.strip() == "[[package]]"]
    blocks: list[tuple[int, int]] = []
    for idx, start in enumerate(block_starts):
        end = block_starts[idx + 1] if idx + 1 < len(block_starts) else len(lines)
        blocks.append((start, end))
    return lines, blocks


def _block_entry(lines: list[str], start: int, end: int) -> dict:
    """Parse a single [[package]] block range into an entry dict."""
    snippet = "".join(lines[start:end])
    try:
        parsed = tomllib.loads(snippet)
    except tomllib.TOMLDecodeError:
        return {}
    pkgs = parsed.get("package", [])
    return pkgs[0] if pkgs else {}


def rewrite_packages_toml(path: Path, *, append: str = "", drop_name: str | None = None) -> None:
    """Apply changes to packages.toml at the line level, preserving comments.

    - `append`: text to append (typically a new [[package]] block, with
      its own leading newline).
    - `drop_name`: if set, remove the [[package]] block whose `name` matches.
    - Always: auto-prune any [[package]] block that contains no
      behavior-changing override field (see OVERRIDE_FIELDS).
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)

    if append:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(append if append.startswith("\n") else "\n" + append)
        if not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"

    # Migrate the legacy per-package key in place on every rewrite, *before*
    # block-parsing and the prune decision below — so a pre-rename entry is
    # already carrying the current key name when `entry_is_inert` judges it,
    # and is migrated rather than pruned as inert. Anchored at the start of
    # the line (after indentation) so it never touches the key name embedded
    # in a reason string or comment. Substitution never changes line count,
    # so it's safe to apply ahead of the index-based blank-line peeling below.
    _legacy_key_re = re.compile(
        rf"^(\s*){re.escape(_LEGACY_PKG_KEY_BUILD_FROM_SOURCE)}(\s*=)"
    )
    lines = [
        _legacy_key_re.sub(rf"\1{PKG_KEY_BUILD_FROM_SOURCE}\2", line)
        for line in lines
    ]

    keep_lines: list[str] = []
    drop_ranges: list[tuple[int, int]] = []

    _, blocks = _split_blocks(lines)
    for start, end in blocks:
        entry = _block_entry(lines, start, end)
        name = entry.get("name", "")
        if (drop_name is not None and name == drop_name) or entry_is_inert(entry):
            drop_ranges.append((start, end))

    # Build the kept-line list, also peeling a leading blank-line run before
    # each dropped block so we don't leave stacked blanks behind.
    cursor = 0
    drop_set: set[int] = set()
    for start, end in drop_ranges:
        peel_start = start
        while peel_start > cursor and lines[peel_start - 1].strip() == "":
            peel_start -= 1
        for i in range(peel_start, end):
            drop_set.add(i)
    keep_lines = [line for i, line in enumerate(lines) if i not in drop_set]

    # Drop trailing blank-line runs to avoid growth across rewrites.
    while keep_lines and keep_lines[-1].strip() == "":
        keep_lines.pop()
    if keep_lines:
        keep_lines.append("\n") if not keep_lines[-1].endswith("\n") else None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(keep_lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# build_state demotion
# ---------------------------------------------------------------------------

def forget_packages(state_dir, pkgnames: list[str]) -> tuple[list[str], list[str]]:
    """Stop maintaining the named package(s): delete their build_state records.

    build_state is the authority for what ``sysforge update`` rebuilds from
    source. Forgetting drops a package's record so update no longer tracks it —
    the "hand it back to pacman" escape hatch for the durable-by-default
    tracking model. The *installed* package is left in place; it still carries
    the ``sf-build`` pacman group, so ``pacman -Syu`` won't replace it. To fully
    revert to the stock repo binary, reinstall it explicitly with
    ``pacman -S <pkg>``. (The next update's ``sync_with_installed`` re-seeds a
    plain ``build_mode = "pacman"`` marker, which is inert.)

    A name matching a pkgbase forgets every split-package member sharing it.

    Returns ``(forgotten, missing)`` — the pkgnames actually deleted (split
    siblings expanded) and the requested names that had no record. Printing is
    the caller's business: ``state forget`` reports both lists, while ``revert``
    and ``uninstall`` fold demotion into their own output. Takes a state dir and
    a name list rather than an ``args`` namespace, because two of its three
    callers are not the ``forget`` verb and were synthesising a fake namespace
    to call it.
    """
    from sysforge.primitives.build_state import BuildState

    bs = BuildState(state_dir)
    all_pkgs = bs.all_packages()

    forgotten: list[str] = []
    missing: list[str] = []
    for name in list(pkgnames or []):
        # Delete the exact entry plus any split-package siblings whose pkgbase
        # equals the requested name (so `forget llvm` drops llvm-libs/polly too).
        targets = {name} if name in all_pkgs else set()
        targets |= {pn for pn, e in all_pkgs.items() if e.get("pkgbase") == name}
        if not targets:
            missing.append(name)
            continue
        for pn in sorted(targets):
            if bs.delete(pn):
                forgotten.append(pn)

    if forgotten:
        bs.save()
    return forgotten, missing
