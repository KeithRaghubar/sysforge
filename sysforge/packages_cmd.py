# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
packages_cmd.py — packages.toml lifecycle management

Implements the `sysforge packages` subcommand namespace. packages.toml stores
build-rule overrides applied to the live install set at steady-state (and
serves as the install list during pipeline bootstrap; see DESIGN.md
§Package Manifest).

    list       — show stored overrides
    add        — add or update an override entry
    add-group  — write a curated desktop-environment package group
    remove     — remove an override entry

`build_state.toml` inspection lives under the separate `sysforge state`
namespace.

Public API:
    cmd_packages_list(args)
    cmd_packages_add(args)
    cmd_packages_add_group(args)
    cmd_packages_remove(args)
"""
import re
import sys
import tomllib
from pathlib import Path

from sysforge import log
_log = log.get_logger("PACKAGES")
from sysforge.primitives.config import (
    load_config,
    PKG_KEY_BUILD_FROM_SOURCE,
    _LEGACY_PKG_KEY_BUILD_FROM_SOURCE,
)
from sysforge.primitives.paths import resolve_packages_path


# Behavior-changing override fields. `source` is metadata (it pins routing
# but doesn't change build behavior), so it doesn't count toward the
# "at least one override" rule for `add` validation or auto-prune.
OVERRIDE_FIELDS = (PKG_KEY_BUILD_FROM_SOURCE, "cache", "reason")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_packages_file(args_packages: str | None) -> Path:
    """Resolve packages.toml path from arg, config, or default."""
    if args_packages:
        return Path(args_packages)
    config = load_config() or {}
    return resolve_packages_path(config)


def _load_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


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

    Deliberately legacy-agnostic. ``_rewrite_packages_toml`` runs the legacy-key
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
# Every mutation goes through `_rewrite_packages_toml` so we (a) preserve
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


def _rewrite_packages_toml(path: Path, *, append: str = "", drop_name: str | None = None) -> None:
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
# packages list
# ---------------------------------------------------------------------------

def cmd_packages_list(args):
    path = _resolve_packages_file(getattr(args, "packages", None))
    if not path.exists():
        _log.fatal(f"No packages.toml at {path}")

    data = _load_toml(path)
    entries = data.get("package", [])
    groups = data.get("group", {}) or {}
    if not entries and not groups:
        print(f"No packages defined in {path}")
        return

    if getattr(args, "orphans", False):
        from sysforge.primitives.pacman import get_all_installed_packages
        installed = set(get_all_installed_packages().keys())
        entries = [e for e in entries if e.get("name") not in installed]
        if not entries:
            print(f"No orphan entries in {path} — all override targets are installed.")
            return

    if entries:
        max_name = max(len(e.get("name", "")) for e in entries)
        max_src = max(len(e.get("source", "")) for e in entries)

        header = f"  {'NAME':<{max_name}}  {'SOURCE':<{max_src}}  OVERRIDES"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for e in entries:
            name = e.get("name", "")
            source = e.get("source", "")
            flags = []
            if e.get(PKG_KEY_BUILD_FROM_SOURCE):
                flags.append(PKG_KEY_BUILD_FROM_SOURCE)
            if e.get("cache") is False:
                flags.append("cache=false")
            if e.get("reason"):
                flags.append(f"reason={e['reason']!r}")
            flag_str = ", ".join(flags)
            print(f"  {name:<{max_name}}  {source:<{max_src}}  {flag_str}")

    # [group.*] tables expand at load time (config.expand_package_groups);
    # list them as written so the file view stays faithful. Skipped under
    # --orphans, which is an explicit-entry cleanup tool.
    if groups and not getattr(args, "orphans", False):
        for gname, g in sorted(groups.items()):
            if not isinstance(g, dict):
                continue
            members = g.get("packages", [])
            defaults = ", ".join(
                f"{k}={v!r}" for k, v in sorted(g.items()) if k != "packages"
            )
            print(f"\n  [group.{gname}]  {len(members)} package(s)"
                  + (f"  defaults: {defaults}" if defaults else ""))
            for m in members:
                print(f"    {m}")


# ---------------------------------------------------------------------------
# packages add
# ---------------------------------------------------------------------------

def cmd_packages_add(args):
    """Add or update an override entry.

    Requires at least one behavior-changing override flag
    (`--enable-build-from-source`, `--no-cache`, `--reason`). `--source` is
    optional metadata and does not satisfy validation on its own.
    """
    pkg = args.pkg
    has_build_from_source = bool(getattr(args, PKG_KEY_BUILD_FROM_SOURCE, False))
    has_no_cache = bool(getattr(args, "no_cache", False))
    reason = getattr(args, "reason", None)
    source = getattr(args, "source", None)

    if not (has_build_from_source or has_no_cache or reason):
        print(
            f"[SYSFORGE] {pkg}: at least one behavior-changing override is required "
            f"(--enable-build-from-source, --no-cache, or --reason). "
            f"--source alone is metadata.",
            file=sys.stderr,
        )
        sys.exit(1)

    path = _resolve_packages_file(getattr(args, "packages", None))

    new_entry: dict = {"name": pkg}
    if source is not None:
        new_entry["source"] = source
    if has_build_from_source:
        new_entry[PKG_KEY_BUILD_FROM_SOURCE] = True
    if has_no_cache:
        new_entry["cache"] = False
    if reason:
        new_entry["reason"] = reason

    # If an entry for this name already exists, replace it: drop first, append second.
    drop_name = None
    if path.exists():
        data = _load_toml(path)
        if any(e.get("name") == pkg for e in data.get("package", [])):
            drop_name = pkg

    block_text = "\n" + entry_toml_block(new_entry) + "\n"

    # Drop the old entry first (if any), then append. We do two writes to
    # keep the line-level helper simple — it accepts at most one drop_name
    # plus an append, but auto-prune runs on every call anyway.
    if drop_name is not None:
        _rewrite_packages_toml(path, drop_name=drop_name)
    if not path.exists():
        # Fresh file: write the standard header so the file is self-documenting.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# packages.toml — managed by sysforge packages\n"
            "\n[build]\n"
            'pkgbuild_src_dir = "~/src"\n'
        )
    _rewrite_packages_toml(path, append=block_text)

    overrides_str = ", ".join(
        f"{k}={v!r}" for k, v in new_entry.items() if k != "name"
    )
    print(f"{'Updated' if drop_name else 'Added'} {pkg}: {overrides_str}")


# ---------------------------------------------------------------------------
# packages add-group
# ---------------------------------------------------------------------------

def cmd_packages_add_group(args):
    """Write a curated desktop-environment package group into packages.toml.

    Delegates the catalog + ``[group.*]`` writing to
    :mod:`sysforge.primitives.pkg_catalog` (the same code path the configure
    stage and reconfigure step use) so all three surfaces stay in lockstep.
    """
    from sysforge.primitives.pkg_catalog import write_desktop_group

    de = args.desktop
    path = _resolve_packages_file(getattr(args, "packages", None))
    write_desktop_group(path, de)
    print(f"Wrote [group.{de}] to {path}. Install with: sysforge run packages")


# ---------------------------------------------------------------------------
# packages remove
# ---------------------------------------------------------------------------

def cmd_packages_remove(args):
    pkg = args.pkg
    path = _resolve_packages_file(getattr(args, "packages", None))

    if not path.exists():
        _log.fatal(f"packages.toml not found: {path}")

    data = _load_toml(path)
    if not any(e.get("name") == pkg for e in data.get("package", [])):
        _log.fatal(f"{pkg} not found in {path}")

    _rewrite_packages_toml(path, drop_name=pkg)
    print(f"Removed {pkg} from {path}")


# ---------------------------------------------------------------------------
# Verb wrappers
# ---------------------------------------------------------------------------

from sysforge.verbs import ExecResult, PreCheckResult, Verb  # noqa: E402


class PackagesListVerb(Verb):
    """Read-only: show packages.toml override entries."""

    name = "packages-list"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        cmd_packages_list(args)
        return ExecResult()


class PackagesAddVerb(Verb):
    """Add or update an override entry in packages.toml.

    No sentinel: packages.toml is a config file, not the live install
    set. An interrupted write reverts to the previous file via the
    atomic write-then-rename in :func:`_rewrite_packages_toml`.
    """

    name = "packages-add"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        has_build_from_source = bool(getattr(args, PKG_KEY_BUILD_FROM_SOURCE, False))
        has_no_cache = bool(getattr(args, "no_cache", False))
        reason = getattr(args, "reason", None)
        if not (has_build_from_source or has_no_cache or reason):
            return PreCheckResult(
                blocker=(
                    f"{args.pkg}: at least one behavior-changing override is required "
                    "(--enable-build-from-source, --no-cache, or --reason). "
                    "--source alone is metadata."
                ),
                exit_code=1,
            )
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        cmd_packages_add(args)
        return ExecResult()


class PackagesAddGroupVerb(Verb):
    """Write a curated desktop-environment package group into packages.toml.

    No sentinel: like the other packages subcommands, this only edits the
    config file — the packages stage installs the group later.
    """

    name = "packages-add-group"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        cmd_packages_add_group(args)
        return ExecResult()


class PackagesRemoveVerb(Verb):
    """Remove an override entry from packages.toml."""

    name = "packages-remove"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        cmd_packages_remove(args)
        return ExecResult()
