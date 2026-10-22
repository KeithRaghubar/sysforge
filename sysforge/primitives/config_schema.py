# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
config_schema.py — what a live config file may contain, and the edits that bring
one back in line (3.5.0-F18).

A live config dir is never rewritten by an upgrade (pacman's backup=() or
`make sync-config`, which only adds), so a key sysforge stopped reading stays
there, silently ignored, and a misspelt one is never reported. This module is
the one home for:

* the schema: the top-level sections and keys each file accepts
  (``KNOWN_SECTIONS``/``KNOWN_TOP_KEYS``, also used by tools/check_shipped.py),
  the sections whose keys the user names (``OPEN_SECTIONS``: profiles, rules,
  packages, ...), and, for every other section, the keys the shipped default
  documents, active or as a commented example;
* what was retired: ``RETIRED_KEYS`` (renamed or dropped keys) and
  ``RETIRED_VALUES`` (values that are now refused or ignored);
* the shipped defaults themselves (``shipped_text``), read from the checkout or
  from the copy packaged in the wheel;
* ``inspect`` (what `config check` and `doctor system --config` report) and the
  edits `config clean` and `config reset` make.

Edits are line-based so comments and layout survive, and every edited file is
re-parsed with tomllib before it is written: an edit that would leave the file
unparsable is refused.

Public API:
    CONFIG_FILES, KNOWN_SECTIONS, KNOWN_TOP_KEYS, OPEN_SECTIONS
    RETIRED_KEYS, RETIRED_VALUES
    Issue
    shipped_text(name) -> str | None
    inspect(config_dir) -> list[Issue]
    clean_text(name, text, *, unknown) -> tuple[str, list[tuple[Issue, str]]]
    reset_keys_text(name, text, keys) -> tuple[str, list[str]]
    ConfigEditError
"""
from __future__ import annotations

import fnmatch
import re
import tomllib
from dataclasses import dataclass
from importlib import resources as importlib_resources
from pathlib import Path

# Files a live config dir holds. bootstrap.toml is a per-host install answer
# file shipped as an example, not a live config.
CONFIG_FILES: tuple[str, ...] = (
    "sysforge.toml", "profiles.toml", "packages.toml", "kernel.toml", "toolchain.toml",
)

# Top-level sections accepted in each file. Adding one here is the deliberate
# signal that the schema changed; the schema and the shipped file are edited
# together (tools/check_shipped.py `configs` checks the shipped side).
KNOWN_SECTIONS: dict[str, set[str]] = {
    "sysforge.toml":  {"ui", "log", "git", "aur", "build", "failure_handling",
                       "safety", "mesa", "pgo", "update", "doctor", "artifacts",
                       "security"},
    "profiles.toml":  {"paths", "defaults", "profiles", "rules",
                       "append_conflict_groups", "preserved_system_tokens",
                       "consumes_inference",
                       "package_compiler_overrides"},
    "packages.toml":  {"build", "package", "group"},
    "kernel.toml":    {"kconfig", "fdo"},
    "toolchain.toml": {"packages", "llvm", "bolt", "bench"},
    "bootstrap.toml": {"partition", "system", "mirror", "desktop", "makepkg"},
}

# Top-level scalar keys (in addition to sections).
KNOWN_TOP_KEYS: dict[str, set[str]] = {
    "sysforge.toml":  set(),
    "profiles.toml":  {"extends_system"},
    "packages.toml":  set(),
    # Kept in lockstep with what KernelStage actually reads via
    # kernel_cfg.get(...) — enforced by tests/test_check_shipped.py's
    # test_kernel_allowlist_matches_stage_reads. The kernel stage does NO
    # PGO, so the toolchain's pgo/skip_build/pgo_staging/pgo_store keys are
    # deliberately absent here.
    "kernel.toml":    {"enabled", "compiler", "pkgname", "upstream_pkgname",
                       "srcdir", "boot_entries", "boot_entries_prefix",
                       "bootloader", "interactive", "pkgbuild_src_dir",
                       "source", "base_config", "base_config_merge",
                       "require_fallback_kernel", "boot_audit", "min_boot_free_mb",
                       "capture_lsmod_snapshot", "device_kconfig",
                       "kconfig_merge", "build_headers", "build_docs",
                       "kconfig_targets", "keep_hotplug_drivers", "hardening"},
    "toolchain.toml": {"enabled", "compiler", "pgo", "skip_build",
                       "drift_detect",
                       "pgo_staging", "pgo_staging1", "pgo_staging3",
                       "pgo_store", "min_build_free_gb", "require_multilib",
                       "rebuild_soname_consumers", "reuse_unchanged",
                       # read by makepkg_pgo.resolve_profile_store_root; the
                       # list predates it (3.5.0-F18 found it missing)
                       "profile_store"},
    "bootstrap.toml": {"target"},
}

# Sections whose keys (or sub-table names) the user chooses: a profile's flag
# variables, a rule, a package entry, a group, a kconfig option. Nothing under
# them is reported as unknown.
OPEN_SECTIONS: dict[str, set[str]] = {
    "profiles.toml": {"profiles", "rules", "append_conflict_groups",
                      "preserved_system_tokens", "consumes_inference",
                      "package_compiler_overrides"},
    "packages.toml": {"package", "group"},
    "kernel.toml":   {"kconfig"},
}


@dataclass(frozen=True)
class RetiredKey:
    file: str
    section: str          # fnmatch pattern on the table name ("" = top level)
    key: str
    replacement: str | None
    retired_in: str


@dataclass(frozen=True)
class RetiredValue:
    file: str
    section: str
    key: str
    old: str
    new: str
    retired_in: str


RETIRED_KEYS: tuple[RetiredKey, ...] = (
    RetiredKey("sysforge.toml", "git", "pull_timeout", "fetch_timeout", "2.6.1-F5"),
    RetiredKey("packages.toml", "package", "pkgbuild_patch",
               "enable_build_from_source", "2.6.1-F5"),
)

RETIRED_VALUES: tuple[RetiredValue, ...] = (
    RetiredValue("packages.toml", "build", "repo_mode", "profiled",
                 "build_from_source", "2.6.1-F5"),
    RetiredValue("profiles.toml", "profiles.*", "build_mode", "patched_pkgbuild",
                 "source_built", "3.5.0-F16"),
)


@dataclass(frozen=True)
class Issue:
    file: str
    line: int             # 1-based
    # parse_error | retired_key | retired_value | unknown_key | unknown_section
    kind: str
    where: str            # "[git] pull_timeout", "[nosuch]", "extends_systm"
    message: str


class ConfigEditError(Exception):
    """An edit was refused (its result would not parse, or it cannot apply)."""


# ---------------------------------------------------------------------------
# Shipped defaults
# ---------------------------------------------------------------------------

def _repo_defaults_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "etc/sysforge"


def shipped_text(name: str) -> str | None:
    """The shipped default for ``name``: the checkout's etc/sysforge when
    running from one, else the copy packaged in the wheel."""
    repo = _repo_defaults_dir() / name
    if repo.is_file():
        return repo.read_text(encoding="utf-8")
    try:
        data = importlib_resources.files("sysforge").joinpath("_data", "config-defaults", name)
        return data.read_text(encoding="utf-8") if data.is_file() else None
    except (FileNotFoundError, ModuleNotFoundError):
        return None


# ---------------------------------------------------------------------------
# A line-level view of a TOML file
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(r"^\s*(\[\[?)\s*([^\[\]#]+?)\s*(\]\]?)\s*(#.*)?$")
_KEY_RE = re.compile(r"""^\s*((?:[A-Za-z0-9_-]+|"[^"]*"|'[^']*')"""
                     r"""(?:\s*\.\s*(?:[A-Za-z0-9_-]+|"[^"]*"|'[^']*'))*)\s*=""")


def _unquote_parts(dotted: str) -> list[str]:
    return [p.strip().strip("\"'") for p in re.findall(
        r"""(?:"[^"]*"|'[^']*'|[^.]+)""", dotted)]


@dataclass
class _Entry:
    start: int            # 0-based first line
    end: int              # 0-based last line, inclusive
    table: str            # dotted table name ("" = top level)
    table_id: int         # which occurrence of a header (array-of-tables entries differ)
    is_array_table: bool
    key: str              # first key part
    raw_value: str        # value text, comments included


@dataclass
class _Header:
    line: int
    table: str
    table_id: int
    is_array_table: bool


def _value_extent(lines: list[str], i: int, after_eq: str) -> int:
    """Last line of a value starting on line ``i`` (multi-line arrays, inline
    tables and triple-quoted strings span lines)."""
    text = after_eq.lstrip()
    for quote in ('"""', "'''"):
        if text.startswith(quote):
            if text.count(quote) >= 2:
                return i
            j = i + 1
            while j < len(lines) and quote not in lines[j]:
                j += 1
            return min(j, len(lines) - 1)
    depth, j, chunk = 0, i, after_eq
    while True:
        in_str = None
        for ch in chunk:
            if in_str:
                if ch == in_str:
                    in_str = None
            elif ch in "\"'":
                in_str = ch
            elif ch == "#":
                break
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
        if depth <= 0 or j + 1 >= len(lines):
            return j
        j += 1
        chunk = lines[j]


def _scan(lines: list[str]) -> tuple[list[_Header], list[_Entry]]:
    headers: list[_Header] = []
    entries: list[_Entry] = []
    table, table_id, is_aot = "", 0, False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        h = _HEADER_RE.match(line)
        if h and h.group(1) == ("[[" if h.group(3) == "]]" else "["):
            table = ".".join(_unquote_parts(h.group(2)))
            table_id += 1
            is_aot = h.group(1) == "[["
            headers.append(_Header(i, table, table_id, is_aot))
            i += 1
            continue
        k = _KEY_RE.match(line)
        if k:
            after = line[k.end():]
            end = _value_extent(lines, i, after)
            raw = "\n".join([after] + lines[i + 1:end + 1])
            entries.append(_Entry(i, end, table, table_id, is_aot,
                                  _unquote_parts(k.group(1))[0], raw))
            i = end + 1
            continue
        i += 1
    return headers, entries


def _documented(text: str) -> dict[str, set[str]]:
    """Table -> keys the shipped default documents, active or commented out."""
    out: dict[str, set[str]] = {"": set()}
    table = active = ""
    for line in text.splitlines():
        if not line.strip():
            # A commented example table covers its own comment block only: a
            # blank line returns to the last real header, so a top-level key
            # documented after the header's examples stays top-level.
            table = active
            continue
        commented = line.lstrip().startswith("#")
        body = re.sub(r"^\s*#\s?", "", line) if commented else line
        h = _HEADER_RE.match(body)
        if h and body.strip().startswith("["):
            table = ".".join(_unquote_parts(h.group(2)))
            if not commented:
                active = table
            out.setdefault(table, set())
            continue
        k = _KEY_RE.match(body)
        if k:
            out.setdefault(table, set()).add(_unquote_parts(k.group(1))[0])
    return out


def _value(raw: str):
    try:
        return tomllib.loads("v = " + raw.strip())["v"]
    except tomllib.TOMLDecodeError:
        return None


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

def _retired_key(name: str, table: str, key: str) -> RetiredKey | None:
    return next((r for r in RETIRED_KEYS if r.file == name and r.key == key
                 and fnmatch.fnmatchcase(table, r.section)), None)


def _retired_value(name: str, table: str, key: str, value) -> RetiredValue | None:
    return next((r for r in RETIRED_VALUES if r.file == name and r.key == key
                 and value == r.old and fnmatch.fnmatchcase(table, r.section)), None)


def _where(table: str, key: str | None = None) -> str:
    head = f"[{table}]" if table else ""
    return f"{head} {key}".strip() if key else head


def inspect_text(name: str, text: str) -> list[Issue]:
    """Issues in one config file's text (``name`` is its file name)."""
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        line = getattr(e, "lineno", 0) or 0
        return [Issue(name, line, "parse_error", "", f"does not parse: {e}")]
    lines = text.splitlines()
    headers, entries = _scan(lines)
    known_sections = KNOWN_SECTIONS.get(name, set())
    open_sections = OPEN_SECTIONS.get(name, set())
    shipped = shipped_text(name)
    documented = _documented(shipped) if shipped else {}
    issues: list[Issue] = []
    for h in headers:
        top = h.table.split(".")[0]
        if top not in known_sections:
            issues.append(Issue(name, h.line + 1, "unknown_section", _where(h.table),
                                f"{name} has no [{top}] section"))
    for e in entries:
        where = _where(e.table, e.key)
        top = e.table.split(".")[0] if e.table else ""
        rk = _retired_key(name, e.table, e.key)
        if rk:
            use = f"; use {rk.replacement}" if rk.replacement else ""
            issues.append(Issue(name, e.start + 1, "retired_key", where,
                                f"retired in {rk.retired_in}, no longer read{use}"))
            continue
        rv = _retired_value(name, e.table, e.key, _value(e.raw_value))
        if rv:
            issues.append(Issue(name, e.start + 1, "retired_value", where,
                                f"{rv.old!r} was retired in {rv.retired_in}; use {rv.new!r}"))
            continue
        if top and (top not in known_sections or top in open_sections):
            continue  # reported with its header, or the user names these keys
        if not top:
            if e.key not in KNOWN_TOP_KEYS.get(name, set()):
                issues.append(Issue(name, e.start + 1, "unknown_key", where,
                                    f"{name} has no top-level {e.key!r}"))
            continue
        if documented and e.key not in documented.get(e.table, set()):
            issues.append(Issue(name, e.start + 1, "unknown_key", where,
                                f"[{e.table}] has no {e.key!r} (misspelt, or no longer used)"))
    return sorted(issues, key=lambda i: i.line)


def inspect(config_dir: Path) -> list[Issue]:
    """Issues across every config file present in ``config_dir``."""
    issues: list[Issue] = []
    for name in CONFIG_FILES:
        path = Path(config_dir) / name
        if path.is_file():
            issues += inspect_text(name, path.read_text(encoding="utf-8"))
    return issues


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------

def _apply(lines: list[str], edits: list[tuple[int, int, list[str]]]) -> list[str]:
    """Replace each inclusive line range with new lines, last edit first."""
    out = list(lines)
    for start, end, new in sorted(edits, key=lambda e: e[0], reverse=True):
        out[start:end + 1] = new
    return out


def _checked(text: str) -> str:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigEditError(f"the edited file would not parse ({e}); nothing written") from e
    return text


def _rejoin(lines: list[str], original: str) -> str:
    return "\n".join(lines) + ("\n" if original.endswith("\n") else "")


def clean_text(name: str, text: str, *, unknown: bool) -> tuple[str, list[tuple[Issue, str]]]:
    """``text`` with retired keys renamed (or dropped when the replacement is
    already set) and retired values rewritten; with ``unknown``, unknown keys
    and sections removed too. Returns (new text, [(issue, what was done)])."""
    issues = inspect_text(name, text)
    if any(i.kind == "parse_error" for i in issues):
        raise ConfigEditError(f"{name} does not parse; fix it by hand first")
    lines = text.splitlines()
    headers, entries = _scan(lines)
    by_line = {e.start + 1: e for e in entries}
    present = {(e.table_id, e.key) for e in entries}
    edits: list[tuple[int, int, list[str]]] = []
    fixed: list[tuple[Issue, str]] = []
    for issue in issues:
        e = by_line.get(issue.line)
        if issue.kind == "retired_key" and e:
            rk = _retired_key(name, e.table, e.key)
            assert rk is not None  # noqa: S101 — inspect_text found it
            if rk.replacement and (e.table_id, rk.replacement) not in present:
                first = re.sub(rf"^(\s*){re.escape(e.key)}(\s*=)", rf"\g<1>{rk.replacement}\2",
                               lines[e.start], count=1)
                edits.append((e.start, e.start, [first]))
                fixed.append((issue, f"renamed to {rk.replacement}"))
            else:
                edits.append((e.start, e.end, []))
                fixed.append((issue, f"removed; {rk.replacement} is already set in this "
                                     "table and keeps its value" if rk.replacement
                              else "removed"))
        elif issue.kind == "retired_value" and e:
            rv = _retired_value(name, e.table, e.key, _value(e.raw_value))
            assert rv is not None  # noqa: S101
            new = re.sub(rf"(['\"]){re.escape(rv.old)}\1", rf"\g<1>{rv.new}\1",
                         lines[e.start], count=1)
            edits.append((e.start, e.start, [new]))
            fixed.append((issue, f"set to {rv.new!r}"))
        elif unknown and issue.kind == "unknown_key" and e:
            edits.append((e.start, e.end, []))
            fixed.append((issue, "removed"))
        elif unknown and issue.kind == "unknown_section":
            h = next(h for h in headers if h.line + 1 == issue.line)
            nxt = next((x.line for x in headers if x.line > h.line), len(lines))
            edits.append((h.line, nxt - 1, []))
            fixed.append((issue, "removed with everything in it"))
    if not edits:
        return text, []
    return _checked(_rejoin(_apply(lines, edits), text)), fixed


def _split_key(spec: str) -> tuple[str, str]:
    table, _, key = spec.rpartition(".")
    return table, key


def reset_keys_text(name: str, text: str, keys: list[str]) -> tuple[str, list[str]]:
    """``text`` with each ``section.key`` (or top-level ``key``) set back to the
    shipped default: the shipped line when the default sets it, else removed
    (the default only documents it). Returns (new text, what changed)."""
    shipped = shipped_text(name)
    if shipped is None:
        raise ConfigEditError(f"no shipped default for {name} is installed")
    s_lines = shipped.splitlines()
    _, s_entries = _scan(s_lines)
    lines = text.splitlines()
    headers, entries = _scan(lines)
    edits: list[tuple[int, int, list[str]]] = []
    appends: list[str] = []
    changes: list[str] = []
    for spec in keys:
        table, key = _split_key(spec)
        if any(h.table == table and h.is_array_table for h in headers) or any(
                e.table == table and e.is_array_table for e in s_entries):
            raise ConfigEditError(
                f"{spec}: [[{table}]] entries are yours to edit; reset the whole file instead")
        mine = [e for e in entries if e.table == table and e.key == key]
        theirs = next((e for e in s_entries if e.table == table and e.key == key), None)
        new_lines = s_lines[theirs.start:theirs.end + 1] if theirs else []
        if mine:
            current = lines[mine[0].start:mine[0].end + 1]
            if current == new_lines:
                changes.append(f"{spec}: already the shipped default")
                continue
            edits.append((mine[0].start, mine[0].end, new_lines))
            changes.append(f"{spec}: " + ("set to the shipped value" if theirs
                                          else "removed (the default leaves it unset)"))
        elif theirs:
            section_end = None
            for h in headers:
                if h.table == table:
                    nxt = next((x.line for x in headers if x.line > h.line), len(lines))
                    section_end = nxt - 1
            if not table:
                edits.append((-1, -1, new_lines))  # sentinel: before the first header
            elif section_end is not None:
                edits.append((section_end + 1, section_end, new_lines))
            else:
                appends += ["", f"[{table}]", *new_lines]
            changes.append(f"{spec}: added with the shipped value")
        else:
            changes.append(f"{spec}: unset here and in the shipped default; nothing to do")
    out = list(lines)
    for start, end, new in sorted(edits, key=lambda e: e[0], reverse=True):
        if start == -1:
            first = next((h.line for h in headers), len(out))
            out[first:first] = new
        else:
            out[start:end + 1] = new
    out += appends
    if out == lines:
        return text, changes
    return _checked(_rejoin(out, text)), changes
