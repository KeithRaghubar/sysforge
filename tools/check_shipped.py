#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
tools/check_shipped.py - pre-release validator for shipped artifacts.

Catches drift between the runtime code and what the PKGBUILD installs:
shipped TOML schema, PKGBUILD install graph, hook -> helper parity,
completions <-> CLI parity, and version-marker lockstep.

Run before cutting a release. tools/release.sh invokes it from preflight.

Usage:
    python tools/check_shipped.py [--warn] [--check=GROUP] [--list] [--repo=PATH]

Default groups (each runs in order, all surface to a single summary):

    configs          etc/sysforge/*.toml schema audit + tests/data parity +
                     real-loader smoke (load_config, load_sysforge_toml).
    config_comments  etc/sysforge/*.toml comment prose: no comment may name a
                     config file or a schema section that does not exist, and a
                     key whose validator accepts multiple surface forms must
                     have its comment show every form (_GRAMMAR_DOCS).
    pkgbuild         backup=() matches install lines; install sources exist;
                     sha256sums is not a placeholder.
    pkgbuild_parity  PKGBUILD vs PKGBUILD-git: depends/makedepends/optdepends/
                     backup arrays must be byte-identical.
    hooks            pacman hook Exec arg is a subcommand
                     tools/pacman-hook-helper.sh documents.
    completions      every verb and long-flag in the argparse parser appears
                     in both completions/_sysforge and completions/sysforge.bash;
                     no stale verb entries in the zsh case statement.
    versions         pyproject.toml == PKGBUILD pkgver == PKGBUILD-git leading
                     pkgver == README.md / DESIGN.md <!--version--> markers.
    manpage          `make man` regen output (tools/gen_options.py splice +
                     scdoc render) matches the committed man/sysforge.1.
                     Compared through _normalize_roff, which neutralises
                     scdoc-version artifacts (header date, generator
                     banner, hyphen escaping) so the guard tracks content,
                     not the local renderer build. Skipped with a warning
                     if scdoc is not installed.

Drift detection cases (verify these still fire after editing this script):
    - Add `frobnicate = true` to etc/sysforge/sysforge.toml.
    - Rename a shipped etc/sysforge/*.toml without updating comments that cite it.
    - Document a `[section]` in a shipped config that is not in _KNOWN_SECTIONS.
    - Teach a _coerce_* function a new accepted form without updating the comment.
    - Remove an entry from PKGBUILD backup=() that still has an install line.
    - Rename a subcommand in tools/pacman-hook-helper.sh case statement.
    - Delete a verb function from completions/_sysforge.
    - Bump pyproject.toml version without touching PKGBUILD.
    - Add a new argument to sysforge/cli.py without running `make man`.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


@dataclass
class Finding:
    group: str
    severity: str  # "error" or "warn"
    location: str
    message: str


# ===========================================================================
# Group: configs
# ===========================================================================

# Top-level sections accepted in each shipped TOML. Adding a section here is
# the deliberate signal that the schema changed; the schema and the file are
# always edited together.
_KNOWN_SECTIONS: dict[str, set[str]] = {
    "sysforge.toml":  {"ui", "log", "git", "aur", "build", "failure_handling",
                       "safety", "mesa", "pgo", "update", "doctor", "artifacts",
                       "security"},
    "profiles.toml":  {"paths", "defaults", "profiles", "rules",
                       "append_conflict_groups", "consumes_inference",
                       "package_compiler_overrides"},
    "packages.toml":  {"build", "package", "group"},
    "kernel.toml":    {"kconfig"},
    "toolchain.toml": {"packages", "llvm", "bolt"},
    "bootstrap.toml": {"partition", "system", "mirror", "desktop", "makepkg"},
}

# Top-level scalar keys (in addition to sections).
_KNOWN_TOP_KEYS: dict[str, set[str]] = {
    "sysforge.toml":  set(),
    "profiles.toml":  {"extends_system"},
    "packages.toml":  set(),
    # Kept in lockstep with what KernelStage actually reads via
    # kernel_cfg.get(...) — enforced by tests/test_check_shipped.py's
    # test_kernel_allowlist_matches_stage_reads. The kernel stage does NO
    # PGO, so the toolchain's pgo/skip_build/pgo_staging/pgo_store keys are
    # deliberately absent here.
    "kernel.toml":    {"enabled", "compiler", "pkgname", "upstream_pkgname", "srcdir",
                       "bootloader", "interactive", "pkgbuild_src_dir",
                       "source", "base_config", "require_fallback_kernel",
                       "boot_audit", "min_boot_free_mb",
                       "capture_lsmod_snapshot", "device_kconfig",
                       "kconfig_merge", "build_headers", "build_docs",
                       "kconfig_targets", "keep_hotplug_drivers"},
    "toolchain.toml": {"enabled", "compiler", "pgo", "skip_build",
                       "drift_detect",
                       "pgo_staging", "pgo_staging1", "pgo_staging3",
                       "pgo_store", "min_build_free_gb", "require_multilib",
                       "rebuild_soname_consumers", "reuse_unchanged"},
    "bootstrap.toml": {"target"},
}


def check_configs(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    shipped_dir = repo / "etc/sysforge"
    live_dir = repo / "tests/data/etc/sysforge"

    if not shipped_dir.is_dir():
        return [Finding("configs", "error", str(shipped_dir.relative_to(repo)),
                        "etc/sysforge/ missing")]

    shipped_files = {p.name for p in shipped_dir.glob("*.toml")}
    live_files = {p.name for p in live_dir.glob("*.toml")} if live_dir.is_dir() else set()
    # bootstrap.toml ships as /usr/share/sysforge/bootstrap.toml.example
    # (per-host); it has no live tests/data counterpart by design.
    expected_live = shipped_files - {"bootstrap.toml"}
    for name in sorted(expected_live - live_files):
        findings.append(Finding(
            "configs", "error", f"tests/data/etc/sysforge/{name}",
            f"shipped {name} has no counterpart in tests/data/etc/sysforge/",
        ))

    for path in sorted(shipped_dir.glob("*.toml")):
        findings.extend(_audit_shipped_toml(path, repo))

    findings.extend(_check_fixture_lockstep(shipped_dir, live_dir))
    findings.extend(_strict_load_smoke(repo))
    return findings


# Flat-schema configs whose fixture must mirror shipped's documented key
# inventory exactly (see _check_fixture_lockstep). Rich-body configs
# (packages.toml, profiles.toml) are deliberately excluded.
_LOCKSTEP_FILES = {"kernel.toml", "toolchain.toml", "sysforge.toml"}


# Matches an active or commented `key = ...` assignment at line start. The `=`
# immediately after the identifier is what keeps prose comments from matching;
# only one leading `#` is stripped, so doubly-commented continuation lines
# (`#      # default ...`) are ignored.
_DOC_KEY_RE = re.compile(r"^\s*#?\s*([a-z_][a-z0-9_]*)\s*=")
# Matches an active or commented `[section]` / `[[array]]` header.
_DOC_SECTION_RE = re.compile(r"^\s*#?\s*\[\[?([a-z_][a-z0-9_.]*)\]\]?\s*(#.*)?$")


def _documented_keys(path: Path) -> set[str]:
    """Key inventory of a config file: every key name that appears as an
    active assignment *or* a commented `# key = ...` example, plus every
    section name (active or commented). Section-body keys are lumped in with
    top-level keys; that is fine because the inventory is only ever *diffed*
    against a sibling file parsed the same way, so any over-collection is
    symmetric and cancels out. The diff catches a key documented in one file
    but missing from the other.
    """
    keys: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        msec = _DOC_SECTION_RE.match(raw)
        if msec:
            keys.add(msec.group(1).split(".")[0])
            continue
        mkey = _DOC_KEY_RE.match(raw)
        if mkey:
            keys.add(mkey.group(1))
    return keys


def _check_fixture_lockstep(shipped_dir: Path, live_dir: Path) -> list[Finding]:
    """Fixture <-> shipped key-inventory lockstep for the flat-schema configs.

    The tracked test fixture (tests/data/etc/sysforge/) is the only thing that
    keeps the shipped defaults honest now that the personal live config is a
    fully decoupled, untracked dir. Values may differ (the fixture carries test
    baselines), but the *set* of documented keys must not drift in either
    direction.

    Scoped to the flat stage/global configs, where the documented key inventory
    *is* the schema. packages.toml / profiles.toml are excluded: they are
    array-of-tables / rich-body configs whose fixtures legitimately carry
    test-specific bodies (real [[package]] entries, test profiles/rules) that
    diverge from shipped's minimal examples; their schema is already guarded by
    _audit_shipped_toml. bootstrap.toml has no fixture by design.
    """
    findings: list[Finding] = []
    for shipped in sorted(shipped_dir.glob("*.toml")):
        if shipped.name not in _LOCKSTEP_FILES:
            continue
        fixture = live_dir / shipped.name
        if not fixture.exists():
            continue  # absence already reported above
        rel = f"tests/data/etc/sysforge/{shipped.name}"
        shipped_keys = _documented_keys(shipped)
        fixture_keys = _documented_keys(fixture)
        for missing in sorted(shipped_keys - fixture_keys):
            findings.append(Finding(
                "configs", "error", rel,
                f"key {missing!r} documented in shipped {shipped.name} "
                f"but absent from the fixture"))
        for extra in sorted(fixture_keys - shipped_keys):
            findings.append(Finding(
                "configs", "error", rel,
                f"key {extra!r} in fixture {shipped.name} "
                f"but not documented in shipped"))
    return findings


def _audit_shipped_toml(path: Path, repo: Path) -> list[Finding]:
    name = path.name
    rel = str(path.relative_to(repo))
    if name not in _KNOWN_SECTIONS:
        return [Finding("configs", "warn", rel,
                        f"no schema allowlist for {name} - extend _KNOWN_SECTIONS")]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        return [Finding("configs", "error", rel, f"TOML parse error: {e}")]

    findings: list[Finding] = []
    known_sections = _KNOWN_SECTIONS[name]
    known_top = _KNOWN_TOP_KEYS[name]
    for key, val in data.items():
        if isinstance(val, dict):
            if key not in known_sections:
                findings.append(Finding("configs", "error", rel,
                                        f"unknown top-level section [{key}]"))
        elif isinstance(val, list):
            if key not in known_sections:
                findings.append(Finding("configs", "error", rel,
                                        f"unknown array section [[{key}]]"))
        else:
            if key not in known_top:
                findings.append(Finding("configs", "error", rel,
                                        f"unknown top-level key {key!r}"))
    return findings


# ---------------------------------------------------------------------------
# Group: config_comments
# ---------------------------------------------------------------------------
#
# The shipped configs are documentation as much as defaults - the comment bodies
# are what a user reads to learn a knob. `configs` validates the file's
# *structure*; nothing validated the prose, so a renamed file or a dropped
# section could stay documented indefinitely. Two confirmed instances motivated
# this (a `flag_profiles.toml` renamed out from under 5 references, and a
# `[cache]` section documented in packages.toml that never existed); both were
# fixed by hand, so this ships green and earns its keep as a regression guard.

# A *.toml filename mentioned anywhere in a comment.
_COMMENT_TOML_RE = re.compile(r"\b([a-z_][a-z0-9_]*\.toml)\b")

# `[section]` mentioned in a comment. Deliberately >=2 chars: real schema
# sections are words ("build", "paths"); a single-letter bracket like
# "[c] swap" in a recovery-menu comment is a UI hotkey, not a TOML section.
_COMMENT_SECTION_RE = re.compile(r"\[\[?([a-z_][a-z0-9_.]{1,})\]\]?")

# A comment describing an *external* file's section ("the [multilib] repo in
# /etc/pacman.conf") is not documenting our schema at all - it just happens to
# use the same `[name]` shorthand. Scoped to the bracket's own *clause* (see
# _CLAUSE_BREAK_RE below), not the whole paragraph: a paragraph routinely
# mentions an external file in one clause and a real schema section in the
# next ("merged over /etc/makepkg.conf, the [cache] block tunes downloads" -
# [cache] is ours and must still be checked), so block-wide suppression would
# hide exactly the drift this group exists to catch.
_EXTERNAL_FILE_RE = re.compile(r"/etc/\S+|\.conf\b")

# *.toml names that are real but never shipped under etc/sysforge/ - runtime
# state generated by sysforge itself, legitimately mentioned in comments that
# explain where a value comes from or is cached. Also clause-scoped, for the
# same reason as _EXTERNAL_FILE_RE above.
_NON_SHIPPED_TOML_ALLOWLIST = {
    "hardware_profile.toml",  # written by the hardware-detection stage
    "build_state.toml",       # sysforge's own build-state cache
}
# Matches the bare stems above *and* the English phrase "hardware stage" (the
# stage that writes hardware_profile.toml) - a comment naming the stage rather
# than the file it produces is the same "not our schema" signal.
_NON_SHIPPED_STEM_RE = re.compile(
    r"\b(?:hardware_profile|build_state|hardware stage)\b")

# Tight, punctuation-only-gap patterns that bind a *specific* `[section]`
# mention to a *specific* owning file - "sysforge.toml's [build] block",
# "[build] in sysforge.toml", "profiles.toml. ([paths] ...)" (a cross-line
# parenthetical immediately after the file is named). Deliberately not a
# "some shipped file was named anywhere nearby" union: an unrelated filename
# mentioned in the same paragraph or clause (e.g. "see sysforge.toml for X;
# packages.toml has no [mesa] section") must NOT lend its allowlist to a
# section it has no syntactic connection to - only a bracket with nothing but
# whitespace/punctuation between it and the file name is "qualified"; every
# other bracket resolves against the containing file's own allowlist (or is
# clause-hatched per the constants above).
_OWNER_BEFORE_RE = re.compile(
    r"\b([a-z_][a-z0-9_]*\.toml)(?:'s)?[.\s]{0,3}\(?\[\[?([a-z_][a-z0-9_.]{1,})\]\]?")
_STEM_ADJACENT_RE = re.compile(
    r"\b(?:hardware_profile|build_state)\b[.\s]{0,3}\(?\[\[?([a-z_][a-z0-9_.]{1,})\]\]?")

# "[section] ... in profiles.toml" / "[section] ... (profiles.toml)": the file
# follows the bracket rather than preceding it. Unlike _OWNER_BEFORE_RE this
# tolerates a few filler words ("[paths] pkgbuild_src_dir in\nprofiles.toml"),
# but is still bounded to the text *after* the specific bracket and *within
# its own clause* (never searched backwards, never crossing a clause break) -
# an owner marker earlier in the clause ("set in profiles.toml. ([paths]...)
# or [build] ...") must not retroactively claim a later, unrelated bracket.
_OWNER_MARKER_RE = re.compile(
    r"(?:\(|\bin\b)\s*(?:[a-z][a-z0-9_-]*\s+){0,4}([a-z_][a-z0-9_]*\.toml)\b")

# Comma / semicolon / spaced-dash: the boundaries that scope every hatch/
# marker above to "this clause", not "this paragraph".
_CLAUSE_BREAK_RE = re.compile(r"[,;]|\s[-–—]{1,2}\s")


def _clause_bounds(text: str, pos: int) -> tuple[int, int]:
    """Start/end offsets of the comma/semicolon/dash-delimited clause of
    `text` containing offset `pos`."""
    start, end = 0, len(text)
    for m in _CLAUSE_BREAK_RE.finditer(text):
        if m.start() < pos:
            start = m.end()
        elif end == len(text):
            end = m.start()
    return start, end


def _comment_blocks(text: str) -> list[list[tuple[int, str]]]:
    """Group contiguous full-line `#` comments into paragraph blocks.

    A cross-reference ("set in profiles.toml" / "([paths] ...)") routinely
    splits the filename and the `[section]` mention across adjacent comment
    lines rather than repeating the filename on every line, so resolution
    needs a window around the line a bracket lands on, not just that line
    alone. Each returned block is [(lineno, comment-body-without-#), ...].

    A trailing comment on an active assignment is excluded because the
    assignment itself is already schema-checked, and a value like
    `pattern = "x # [y]"` would otherwise read as a section mention.
    """
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("#"):
            current.append((i, stripped.lstrip("#").strip()))
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


# Keys whose validator accepts more than one surface form, mapped to substrings
# the shipped comment must contain so every accepted form is visible to a reader.
#
# This is the guard against the failure class where a feature *widens an existing
# key's grammar* rather than adding a key: the schema allowlists never move, so
# every structural check stays green while the comment silently under-documents.
# 2.1.0-F6 is the worked example - it taught _coerce_cpu_quota a fractional form
# that neither sysforge.toml nor profiles.toml mentioned.
#
# MAINTENANCE: when a _coerce_* function in primitives/build_throttle.py gains or
# drops an accepted form, update this table in the same commit. The table is a
# hand-maintained assertion, not an inference - no static signal reliably
# distinguishes an accepted-form branch from any other conditional.
_GRAMMAR_DOCS: dict[tuple[str, str], list[str]] = {
    # _coerce_cpu_quota: absolute "N%" and a decimal fraction of os.cpu_count().
    ("sysforge.toml", "cpu_quota"): ["%", "fraction"],
    ("profiles.toml", "cpu_quota"): ["%", "fraction"],
    # _coerce_mem_limit: bare byte count or binary suffix - both tokens
    # required so the check cannot pass on the suffix example alone (every
    # shipped example is `mem_limit = "24G"`, which always contains "G").
    ("sysforge.toml", "mem_limit"): ["byte count", "G"],
    ("profiles.toml", "mem_limit"): ["byte count", "G"],
    # _coerce_ionice: the two accepted classes.
    ("sysforge.toml", "ionice"): ["idle", "best-effort"],
    ("profiles.toml", "ionice"): ["idle", "best-effort"],
    # _coerce_nice: the accepted range.
    ("sysforge.toml", "nice"): ["0", "19"],
    ("profiles.toml", "nice"): ["0", "19"],
}


def _key_comment_block(text: str, key: str) -> str:
    """The comment prose documenting `key`: the contiguous run of comment lines
    immediately above the line that assigns it, key-scoped so it stops at the
    previous key's own anchor line rather than at that key's paragraph start.

    Relies on the shipped-config comment convention (design §Config Layer,
    "Shipped-config comment style"): documentation leads its key, never trails
    it. Walk up from the key's line until either a non-comment line or a
    comment line that is itself a `_DOC_KEY_RE` anchor for a *different* key -
    the shipped configs separate key paragraphs with bare `#` spacer lines, not
    blank lines, so a plain "non-comment line" boundary would run straight
    through the spacer into the previous key's entire paragraph. Stopping at
    the previous anchor keeps the block scoped to this key alone, so one key's
    required token can no longer be satisfied by prose that actually documents
    a different key.

    The key's own line is included (but not counted as "a different key" when
    encountered as the starting point), so a commented-out example
    (`# cpu_quota = "600%"`) counts as documenting its own form, and a trailing
    hint on that same line (`nice = 19  # scheduling niceness 0..19`) is part
    of its block too.

    The anchor is _DOC_KEY_RE, the same regex the fixture-lockstep check uses,
    so "what documents a key" has one definition in this file.

    Limitation: returns the block for the FIRST line matching `key`, so a key
    documented twice in one file (e.g. a global example and a later
    per-profile example) only has its first occurrence checked. No shipped
    config does this today.
    """
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        m = _DOC_KEY_RE.match(raw)
        if not m or m.group(1) != key:
            continue
        start = i
        while start > 0:
            prev_raw = lines[start - 1]
            prev = prev_raw.strip()
            if not prev.startswith("#"):
                break
            pm = _DOC_KEY_RE.match(prev_raw)
            if pm and pm.group(1) != key:
                break
            start -= 1
        return "\n".join(ln.strip().lstrip("#") for ln in lines[start:i + 1])
    return ""


def check_config_comments(repo: Path) -> list[Finding]:
    """Assert no shipped comment names a config file or section that does not exist."""
    cfg_dir = repo / "etc" / "sysforge"
    if not cfg_dir.is_dir():
        return [Finding("config_comments", "error", "etc/sysforge",
                        "shipped config dir missing")]

    shipped = {p.name for p in cfg_dir.glob("*.toml")}
    findings: list[Finding] = []

    for path in sorted(cfg_dir.glob("*.toml")):
        rel = str(path.relative_to(repo))
        text = path.read_text(encoding="utf-8")
        own_sections = _KNOWN_SECTIONS.get(path.name, set())

        for block in _comment_blocks(text):
            for lineno, body in block:
                for fname in _COMMENT_TOML_RE.findall(body):
                    if fname not in shipped and fname not in _NON_SHIPPED_TOML_ALLOWLIST:
                        findings.append(Finding(
                            "config_comments", "error", f"{rel}:{lineno}",
                            f"comment references {fname!r}, which is not a shipped "
                            f"config (shipped: {', '.join(sorted(shipped))})"))

            for idx, (lineno, body) in enumerate(block):
                sections = _COMMENT_SECTION_RE.findall(body)
                if not sections:
                    continue

                # A 3-line window (previous/current/next) around this line,
                # just wide enough to resolve a cross-line parenthetical
                # qualifier ("profiles.toml.\n([paths] ...)") - not the whole
                # paragraph.
                prev_body = block[idx - 1][1] if idx > 0 else ""
                next_body = block[idx + 1][1] if idx + 1 < len(block) else ""
                window = f"{prev_body} {body} {next_body}"
                body_offset = len(prev_body) + 1

                # Tight owner qualification: a specific bracket bound to a
                # specific file/stem by pre-bracket adjacency, not paragraph
                # co-occurrence ("sysforge.toml's [build]", "hardware_profile
                # [kconfig]").
                qualified: dict[str, str] = {}
                for m in _OWNER_BEFORE_RE.finditer(window):
                    qualified[m.group(2)] = m.group(1)
                for m in _STEM_ADJACENT_RE.finditer(window):
                    qualified.setdefault(m.group(1), "")  # non-shipped stem: untracked

                for section in sections:
                    owner = qualified.get(section)
                    if owner is not None:
                        if owner == "" or owner in _NON_SHIPPED_TOML_ALLOWLIST:
                            continue  # bound to a file/stem we track no schema for
                        if owner not in shipped:
                            continue  # already reported by the filename check above
                        allowed = _KNOWN_SECTIONS.get(owner, set())
                        label = owner
                    else:
                        bm = re.search(r"\[\[?" + re.escape(section) + r"\]\]?", body)
                        bracket_start = body_offset + (bm.start() if bm else 0)
                        bracket_end = body_offset + (bm.end() if bm else len(body))
                        cstart, cend = _clause_bounds(window, bracket_start)
                        clause = window[cstart:cend]

                        # Post-bracket owner marker, searched forward only and
                        # never past the clause boundary: "[section] ... in
                        # file.toml" / "[section] ... (file.toml)".
                        after = window[bracket_end:cend]
                        am = _OWNER_MARKER_RE.search(after)
                        if am and am.group(1) in shipped:
                            allowed = _KNOWN_SECTIONS.get(am.group(1), set())
                            label = am.group(1)
                        else:
                            # Fully unqualified: same-clause hatches only - a
                            # filename mentioned elsewhere in the clause is
                            # NOT evidence of ownership (that's the tight
                            # regexes' and the marker's job above), only these
                            # two explicit "not our schema" signals are.
                            if _EXTERNAL_FILE_RE.search(clause):
                                continue
                            if _NON_SHIPPED_STEM_RE.search(clause):
                                continue
                            allowed = own_sections
                            label = path.name
                    if not allowed:
                        continue  # no allowlist for this file - configs group warns
                    root = section.split(".")[0]
                    if root not in allowed:
                        findings.append(Finding(
                            "config_comments", "error", f"{rel}:{lineno}",
                            f"comment documents section [{section}] not found in "
                            f"{label}'s schema allowlist: {', '.join(sorted(allowed))}"))

    for (fname, key), required in sorted(_GRAMMAR_DOCS.items()):
        path = cfg_dir / fname
        if not path.exists():
            # Not shipped in *this* config dir. A synthetic fixture that only
            # exercises a subset of files is not drift; a real shipped file
            # named in _GRAMMAR_DOCS but absent from the repo is caught by
            # test_grammar_docs_table_keys_exist_in_shipped_configs.
            continue
        block = _key_comment_block(path.read_text(encoding="utf-8"), key)
        if not block:
            # The key itself doesn't appear as an assignment (active or
            # commented-out example) anywhere in this file - nothing to
            # under-document. A synthetic fixture testing an unrelated key is
            # not drift; a real shipped config missing the key entirely is
            # caught by test_grammar_docs_table_keys_exist_in_shipped_configs.
            continue
        missing = [tok for tok in required if tok not in block]
        if missing:
            findings.append(Finding(
                "config_comments", "error", f"etc/sysforge/{fname}",
                f"comment for {key!r} does not show every form its validator "
                f"accepts - missing {', '.join(repr(m) for m in missing)}; "
                f"see _coerce_{key} in primitives/build_throttle.py"))

    return findings


def _strict_load_smoke(repo: Path) -> list[Finding]:
    """Run the runtime config loaders against the shipped files.

    Drops cached sysforge modules and re-imports with SYSFORGE_CONFIG_DIR
    pointing at the shipped config dir (`repo/etc/sysforge`) so CONFIG_DIR in
    paths.py resolves to the shipped files (the env var is the config dir
    itself, not an FHS root prefix). Safe to call from a subprocess; risky
    in-process if other code holds onto sysforge module references.
    """
    findings: list[Finding] = []
    saved_env = os.environ.get("SYSFORGE_CONFIG_DIR")
    saved_modules = {k: v for k, v in sys.modules.items() if k.startswith("sysforge")}

    os.environ["SYSFORGE_CONFIG_DIR"] = str(repo / "etc/sysforge")
    for mod in list(sys.modules):
        if mod.startswith("sysforge"):
            del sys.modules[mod]
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    try:
        from sysforge.primitives.config import (
            load_config,
            load_conflict_groups,
            load_consumes_inference,
            load_sysforge_toml,
        )
        try:
            load_config()
            load_conflict_groups()
            load_consumes_inference()
        except Exception as e:
            findings.append(Finding("configs", "error",
                                    "etc/sysforge/profiles.toml",
                                    f"load_config raised: {e!r}"))
        try:
            load_sysforge_toml()
        except Exception as e:
            findings.append(Finding("configs", "error",
                                    "etc/sysforge/sysforge.toml",
                                    f"load_sysforge_toml raised: {e!r}"))
    finally:
        if saved_env is None:
            os.environ.pop("SYSFORGE_CONFIG_DIR", None)
        else:
            os.environ["SYSFORGE_CONFIG_DIR"] = saved_env
        for mod in list(sys.modules):
            if mod.startswith("sysforge"):
                del sys.modules[mod]
        sys.modules.update(saved_modules)

    return findings


# ===========================================================================
# Group: pkgbuild
# ===========================================================================

_INSTALL_RE = re.compile(
    r'install\s+-Dm[0-9]+\s+(\S+)\s+("[^"]+"|\'[^\']+\'|\S+)'
)
_BACKUP_RE = re.compile(r"^backup=\((.*?)\)", re.DOTALL | re.MULTILINE)
_LOCAL_VAR_RE = re.compile(r'local\s+(\w+)\s*=\s*"([^"]+)"')

# Line-continuation join: several `install -Dm...` calls split src/dst across a
# `\`-continued line for readability. Join before regex matching so group(2)
# lands on the real destination token, not the literal backslash.
_CONT_RE = re.compile(r"\\\s*\n\s*")

# Packaged paths the dev-install symlink mapping intentionally does NOT mirror:
#   - sysusers.d/tmpfiles.d: generated stubs that provision the sysforge group +
#     state dirs, not real shipped files.
#   - bootstrap.toml.example: a per-host template (device/hostname/passwords)
#     shipped as an example under /usr/share, not a functional config sysforge
#     reads at runtime — same rationale as the provisioning stubs. Matched by
#     exact path (not a /usr/share/sysforge/ prefix) so a future real file there
#     is not silently over-excluded.
_EXCLUDED_SYSTEM_PATH_MARKERS = (
    "/usr/lib/sysusers.d/",
    "/usr/lib/tmpfiles.d/",
    "/usr/share/sysforge/bootstrap.toml.example",
)


def _parse_pkgbuild_install_targets(text: str) -> set[str]:
    """Every absolute system path `install -Dm...` writes under $pkgdir.

    Parses the full install-target set from the PKGBUILD `package()` body; used
    by check_dev_install_parity to verify dev_install.sh's MAPPING covers the
    packaged set. (check_pkgbuild does its own inline `_INSTALL_RE` scan.)
    """
    joined = _CONT_RE.sub(" ", text)
    local_vars = {f"${name}": val for name, val in _LOCAL_VAR_RE.findall(joined)}
    targets: set[str] = set()
    for m in _INSTALL_RE.finditer(joined):
        dst = m.group(2).strip("'\"")
        for var, val in local_vars.items():
            dst = dst.replace(var, val)
        if dst.startswith("$pkgdir/"):
            targets.add("/" + dst[len("$pkgdir/"):])
    return targets

# Placeholder fingerprint shipped in PKGBUILD until the maintainer fills in their
# real key. tools/release.sh refuses to publish while this is present; check_shipped
# tolerates it so dev gates pass before a signing key exists.
_VALIDPGPKEYS_SENTINEL = "REPLACE_WITH_MAINTAINER_KEY_FINGERPRINT"


def check_pkgbuild(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    pkgbuild = repo / "PKGBUILD"
    if not pkgbuild.exists():
        return [Finding("pkgbuild", "error", "PKGBUILD", "missing")]
    text = pkgbuild.read_text()

    local_vars = {f"${name}": val for name, val in _LOCAL_VAR_RE.findall(text)}

    bm = _BACKUP_RE.search(text)
    backup: set[str] = set()
    if bm:
        for q1, q2 in re.findall(r"'([^']+)'|\"([^\"]+)\"", bm.group(1)):
            backup.add(q1 or q2)

    install_etc_paths: set[str] = set()
    for m in _INSTALL_RE.finditer(text):
        src = m.group(1)
        dst = m.group(2).strip("'\"")
        for var, val in local_vars.items():
            dst = dst.replace(var, val)

        # Source paths in this PKGBUILD are repo-relative (no $srcdir prefix
        # in front of the `install` arg - the package() cd's into $srcdir
        # first). Skip /dev/null and similar.
        if src.startswith("/"):
            if src != "/dev/null" and not Path(src).exists():
                findings.append(Finding("pkgbuild", "error", "PKGBUILD",
                                        f"install source not found: {src}"))
        else:
            if not (repo / src).exists():
                findings.append(Finding("pkgbuild", "error", "PKGBUILD",
                                        f"install source not found: {src}"))

        if "$pkgdir/etc/" in dst:
            etc_rel = dst.split("$pkgdir/", 1)[1]
            install_etc_paths.add(etc_rel)

    for p in sorted(install_etc_paths - backup):
        findings.append(Finding("pkgbuild", "error", "PKGBUILD",
                                f"installed to {p} but not declared in backup=()"))
    for p in sorted(backup - install_etc_paths):
        findings.append(Finding("pkgbuild", "error", "PKGBUILD",
                                f"backup=() lists {p} but no install line writes it"))

    # sha256sums / source / validpgpkeys: parse the arrays so a SKIP can be
    # paired with its source. A detached signature source (*.asc / *.sig) is
    # GPG-verified against validpgpkeys, so SKIP is *correct* for it — but SKIP
    # on a hashable source (or an all-zero/DRYRUN value) is still a placeholder.
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    g = parse_pkgbuild(str(pkgbuild))["globals"]
    sources = g.get("source") or []
    sums = g.get("sha256sums") or []
    for i, s in enumerate(sums):
        src = sources[i] if i < len(sources) else ""
        is_sig = src.endswith(".asc") or src.endswith(".sig")
        if s == "SKIP":
            if not is_sig:
                findings.append(Finding("pkgbuild", "error", "PKGBUILD",
                    f"sha256sums=('SKIP') for non-signature source {src!r} "
                    "(SKIP is only valid for a detached .asc/.sig source)"))
        elif re.fullmatch(r"0+", s) or s.startswith("DRYRUN"):
            findings.append(Finding("pkgbuild", "error", "PKGBUILD",
                                    f"sha256sums contains placeholder: {s}"))

    # Release-signing trust anchor: the stable PKGBUILD must declare a maintainer
    # key so makepkg verifies the .asc. Accept the dev sentinel (release.sh blocks
    # publishing while it's present) or a real 40-hex fingerprint.
    # install= scriptlet (F1 first-install notice): when declared, the file
    # must ship in the repo root alongside the PKGBUILD.
    inst = g.get("install")
    if inst and not (repo / inst).exists():
        findings.append(Finding("pkgbuild", "error", "PKGBUILD",
                                f"install scriptlet not found: {inst}"))
    # The install= scriptlet is read by makepkg from the build startdir, not the
    # source tarball — so it must be copied into the AUR repos at publish time.
    # tools/release.sh's Phase-4 instructions are the sole publish path; if the
    # scriptlet isn't referenced there, every AUR-clone bootstrap aborts with
    # "install file does not exist" (1.2.0-B2). Guard the rename/forget regression.
    if inst:
        release_sh = repo / "tools" / "release.sh"
        if release_sh.exists() and inst not in release_sh.read_text():
            findings.append(Finding("pkgbuild", "error", "PKGBUILD",
                                    f"install scriptlet {inst} not copied to the "
                                    f"AUR repos by tools/release.sh publish steps"))

    keys = g.get("validpgpkeys") or []
    if not keys:
        findings.append(Finding("pkgbuild", "error", "PKGBUILD",
            "validpgpkeys=() not declared — required to verify the release signature"))
    for k in keys:
        if k != _VALIDPGPKEYS_SENTINEL and not re.fullmatch(r"[0-9A-Fa-f]{40}", k):
            findings.append(Finding("pkgbuild", "error", "PKGBUILD",
                f"validpgpkeys entry {k!r} is not a 40-hex fingerprint "
                f"(or the {_VALIDPGPKEYS_SENTINEL} sentinel)"))
    return findings


# ===========================================================================
# Group: pkgbuild_parity
# ===========================================================================

# Keys allowed to differ between PKGBUILD (stable) and PKGBUILD-git (VCS).
# Everything else must be byte-identical after parsing.
_ALLOWED_PKGBUILD_DIVERGENCE = {
    "pkgname", "pkgver", "pkgrel", "pkgdesc",
    "source", "sha256sums", "conflicts", "provides",
    # Stable verifies a maintainer-signed release tarball; the VCS package
    # tracks a git clone (no release .asc), so validpgpkeys is stable-only.
    "validpgpkeys",
}


def check_pkgbuild_parity(repo: Path) -> list[Finding]:
    stable = repo / "PKGBUILD"
    git = repo / "PKGBUILD-git"
    if not (stable.exists() and git.exists()):
        return [Finding("pkgbuild_parity", "error", "PKGBUILD/PKGBUILD-git",
                        "one or both files missing")]

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    stable_g = parse_pkgbuild(str(stable))["globals"]
    git_g = parse_pkgbuild(str(git))["globals"]

    findings: list[Finding] = []
    for key in sorted(set(stable_g) | set(git_g)):
        if key in _ALLOWED_PKGBUILD_DIVERGENCE:
            continue
        if stable_g.get(key) != git_g.get(key):
            findings.append(Finding("pkgbuild_parity", "error",
                                    "PKGBUILD vs PKGBUILD-git",
                                    f"{key} differs: PKGBUILD={stable_g.get(key)!r} "
                                    f"PKGBUILD-git={git_g.get(key)!r}"))
    return findings


# ===========================================================================
# Group: hooks
# ===========================================================================

# Match a documented kind line in the helper:
#   #   kernel       - kernel package changed
# Em-dash (U+2014) is the actual separator used in the script.
_HELPER_KIND_RE = re.compile(r"^#\s*(\w+)\s+(?:[-—])", re.MULTILINE)
_EXEC_RE = re.compile(r"^Exec\s*=\s*(\S+)\s*(.*)$", re.MULTILINE)


def check_hooks(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    hooks_dir = repo / "etc/pacman.d/hooks"
    helper = repo / "tools/pacman-hook-helper.sh"
    if not helper.exists():
        return [Finding("hooks", "error", "tools/pacman-hook-helper.sh",
                        "helper script missing")]
    if not hooks_dir.is_dir():
        return [Finding("hooks", "error", "etc/pacman.d/hooks",
                        "hooks directory missing")]

    doc_kinds = set(_HELPER_KIND_RE.findall(helper.read_text()))

    for hook in sorted(hooks_dir.glob("*.hook")):
        rel = f"etc/pacman.d/hooks/{hook.name}"
        text = hook.read_text()
        m = _EXEC_RE.search(text)
        if not m:
            findings.append(Finding("hooks", "error", rel,
                                    "no [Action] Exec= line found"))
            continue
        exec_path, exec_args = m.group(1), m.group(2).strip()
        if not exec_path.endswith("/pacman-hook-helper.sh"):
            findings.append(Finding("hooks", "error", rel,
                                    f"Exec path is not pacman-hook-helper.sh: {exec_path}"))
        arg = exec_args.split()[0] if exec_args else ""
        if arg and doc_kinds and arg not in doc_kinds:
            findings.append(Finding("hooks", "error", rel,
                                    f"Exec arg {arg!r} not documented in helper "
                                    f"(known: {sorted(doc_kinds)})"))

    # The runtime provisioner (primitives/pacman_hooks.py) and the wheel
    # force-include must both cover every shipped hook, or `sysforge setup` /
    # `doctor --pacman` would silently ignore a newly-added hook on installed
    # systems.
    shipped = {h.name for h in hooks_dir.glob("*.hook")}
    findings += _check_hook_runtime_parity(repo, shipped)
    return findings


def _check_hook_runtime_parity(repo: Path, shipped: set[str]) -> list[Finding]:
    findings: list[Finding] = []

    # 1. pacman_hooks.HOOK_NAMES must match the shipped .hook files exactly.
    #    (Guarded: minimal test repos copy only shipped files, not the source
    #    tree; skip the parity assertions when those files are absent.)
    src_file = repo / "sysforge/primitives/pacman_hooks.py"
    if src_file.is_file():
        listed = set(re.findall(r'"(sysforge-[\w-]+\.hook)"', src_file.read_text()))
        if listed != shipped:
            findings.append(Finding(
                "hooks", "error", "sysforge/primitives/pacman_hooks.py",
                f"HOOK_NAMES {sorted(listed)} != shipped hooks {sorted(shipped)}"))

    # 2. The wheel force-include must ship the hooks dir + helper.
    pyproject_file = repo / "pyproject.toml"
    if pyproject_file.is_file():
        pyproject = pyproject_file.read_text()
        for needed in ("etc/pacman.d/hooks", "tools/pacman-hook-helper.sh"):
            if f'"{needed}"' not in pyproject:
                findings.append(Finding(
                    "hooks", "error", "pyproject.toml",
                    f"force-include missing {needed!r} (hooks won't ship in the wheel)"))
    return findings


# ===========================================================================
# Group: completions
# ===========================================================================


def check_completions(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    zsh = repo / "completions/_sysforge"
    bash = repo / "completions/sysforge.bash"
    if not (zsh.exists() and bash.exists()):
        return [Finding("completions", "error", "completions/",
                        "completion file(s) missing")]

    parser = _import_build_parser(repo)
    sub_action = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )
    if sub_action is None:
        return [Finding("completions", "error", "sysforge/cli.py",
                        "no subparsers found in _build_parser")]

    verb_flags: dict[str, set[str]] = {}
    for verb, subparser in sub_action.choices.items():
        flags: set[str] = set()
        for action in subparser._actions:
            for s in action.option_strings:
                if s.startswith("--") and s != "--help":
                    flags.add(s)
        verb_flags[verb] = flags

    # `completions` is an internal command used by the shell scripts
    # themselves; it intentionally has no user-facing completion entry.
    user_verbs = {v for v in verb_flags if v != "completions"}

    zsh_text = zsh.read_text()
    bash_text = bash.read_text()

    for verb in sorted(user_verbs):
        if not re.search(rf"\b{re.escape(verb)}\b", zsh_text):
            findings.append(Finding("completions", "error",
                                    "completions/_sysforge",
                                    f"verb {verb!r} missing"))
        if not re.search(rf"\b{re.escape(verb)}\b", bash_text):
            findings.append(Finding("completions", "error",
                                    "completions/sysforge.bash",
                                    f"verb {verb!r} missing"))

    for verb in sorted(user_verbs):
        for flag in sorted(verb_flags[verb]):
            if flag not in zsh_text:
                findings.append(Finding("completions", "error",
                                        "completions/_sysforge",
                                        f"verb {verb!r} flag {flag!r} missing"))
            if flag not in bash_text:
                findings.append(Finding("completions", "error",
                                        "completions/sysforge.bash",
                                        f"verb {verb!r} flag {flag!r} missing"))

    # Stale verbs: only flag entries in the top-level dispatch, where the
    # case arm dispatches to `_sysforge_<verb>` (function suffix equals the
    # case word). Subverb dispatch uses `_sysforge_<parent>_<subverb>`,
    # which never matches \1 and is correctly ignored.
    for m in re.finditer(r"^\s*(\w+)\)\s+_sysforge_(\w+)\s*;;",
                         zsh_text, re.MULTILINE):
        verb, fn_suffix = m.group(1), m.group(2)
        if verb != fn_suffix:
            continue
        if verb not in verb_flags and verb != "completions":
            findings.append(Finding("completions", "error",
                                    "completions/_sysforge",
                                    f"stale verb entry {verb!r} (not in parser)"))

    return findings


# ===========================================================================
# Group: completion_widths
# ===========================================================================

# A zsh completion listing renders one row per match as
#
#     <match><pad>  -- <description>
#
# where the match column is padded to the longest match in that generator
# call. When a row exceeds the terminal width, zsh abandons the inline
# two-column table and emits every description as its own list entry, so the
# whole block renders with names and descriptions detached (3.0.0-B2). The
# budget is therefore per-call, and one over-wide row degrades every row
# beside it.
COMPLETION_TARGET_COLUMNS = 80

# Width of the "  -- " gutter compdescribe inserts between the padded match
# column and the description.
_DESC_GUTTER = 4

# An `_arguments` option spec: an optional (exclusion list), an optional bare
# option name, then the bracketed description. The brace form
# `'(-m --makepkg)'{-m,--makepkg}'[desc]'` carries its names in the braces
# instead, picked up separately below.
_ARG_SPEC_RE = re.compile(r"'(?:\([^)]*\))?(-[^'\[]*)?\[([^]]*)\]")
_BRACE_NAMES_RE = re.compile(r"\{([^}]*)\}'\[")

# A `_describe` array entry: 'name:description' alone on its line. Lines
# holding an `_arguments` spec are excluded by the absence of a bracket.
_DESCRIBE_ENTRY_RE = re.compile(r"^\s*'([^':]+):([^']+)'\s*$")

_FUNC_RE = re.compile(r"^(\w+)\s*\(\)\s*\{")


def _completion_blocks(text: str) -> list[tuple[str, str, list[tuple[int, str, str]]]]:
    """Group completion entries by the generator call that emits them.

    Returns (function, kind, [(lineno, match, description)]). Each
    `_arguments` spec list and each `_describe` array is a separate compadd
    call and so is budgeted independently.
    """
    # Keyed by (function, kind) — one entry per generator call. A function
    # holds at most one `_arguments` spec list and one `_describe` array, so
    # the key is enough to keep the two apart without tracking run boundaries.
    grouped: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
    func = "<toplevel>"

    for lineno, line in enumerate(text.splitlines(), 1):
        m = _FUNC_RE.match(line)
        if m:
            func = m.group(1)
            continue

        for spec in _ARG_SPEC_RE.finditer(line):
            brace = _BRACE_NAMES_RE.search(line)
            raw = brace.group(1).split(",") if brace else [spec.group(1) or ""]
            names = [n.strip() for n in raw if n.strip().startswith("-")]
            for name in names:
                grouped.setdefault((func, "_arguments"), []).append(
                    (lineno, name, spec.group(2)))

        if "[" not in line:
            d = _DESCRIBE_ENTRY_RE.match(line)
            if d:
                grouped.setdefault((func, "_describe"), []).append(
                    (lineno, d.group(1), d.group(2)))

    return [(fn, kind, entries) for (fn, kind), entries in grouped.items()]


def check_completion_widths(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    zsh = repo / "completions/_sysforge"
    if not zsh.exists():
        return [Finding("completion_widths", "error", "completions/_sysforge",
                        "completion file missing")]

    for func, kind, entries in _completion_blocks(zsh.read_text()):
        pad = max(len(name) for _, name, _ in entries)
        budget = COMPLETION_TARGET_COLUMNS - pad - _DESC_GUTTER
        for lineno, name, desc in entries:
            if len(desc) <= budget:
                continue
            findings.append(Finding(
                "completion_widths", "error",
                f"completions/_sysforge:{lineno}",
                f"{func} ({kind}) {name}: description is {len(desc)} chars, "
                f"budget {budget} "
                f"({COMPLETION_TARGET_COLUMNS} cols - {pad} pad - {_DESC_GUTTER}); "
                f"shorten it, or the whole block loses its inline layout",
            ))

    return findings


def _import_build_parser(repo: Path):
    """Import sysforge.cli._build_parser with CONFIG_DIR set to the repo fixtures.

    Drops cached sysforge modules so paths.py picks up the right CONFIG_DIR
    on re-import. SYSFORGE_CONFIG_DIR is the config dir *itself* (the dir that
    directly holds the TOML files), so it points at `repo/etc/sysforge`, not
    `repo`. Restores the caller's SYSFORGE_CONFIG_DIR before returning so
    downstream checks (notably manpage, which shells out to gen_options.py and
    inherits the env) see the user's original setting.
    """
    saved_env = os.environ.get("SYSFORGE_CONFIG_DIR")
    saved_modules = {k: v for k, v in sys.modules.items() if k.startswith("sysforge")}

    os.environ["SYSFORGE_CONFIG_DIR"] = str(repo / "etc/sysforge")
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    for mod in list(sys.modules):
        if mod.startswith("sysforge"):
            del sys.modules[mod]
    try:
        from sysforge.cli import _build_parser
        return _build_parser()
    finally:
        if saved_env is None:
            os.environ.pop("SYSFORGE_CONFIG_DIR", None)
        else:
            os.environ["SYSFORGE_CONFIG_DIR"] = saved_env
        for mod in list(sys.modules):
            if mod.startswith("sysforge"):
                del sys.modules[mod]
        sys.modules.update(saved_modules)


# ===========================================================================
# Group: versions
# ===========================================================================


def check_versions(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    pyp = repo / "pyproject.toml"
    if not pyp.exists():
        return [Finding("versions", "error", "pyproject.toml", "missing")]
    target = tomllib.loads(pyp.read_text(encoding="utf-8"))["project"]["version"]

    # SemVer 2.0.0: the released project version must be strict X.Y.Z — numeric
    # major.minor.patch with no pre-release or build-metadata suffix.
    if not re.fullmatch(r"\d+\.\d+\.\d+", target):
        findings.append(Finding("versions", "error", "pyproject.toml",
                                f"version {target!r} is not strict SemVer X.Y.Z"))

    pkgbuild = repo / "PKGBUILD"
    if pkgbuild.exists():
        m = re.search(r"^pkgver=(\S+)", pkgbuild.read_text(), re.MULTILINE)
        if not m:
            findings.append(Finding("versions", "error", "PKGBUILD",
                                    "no pkgver= line"))
        elif m.group(1) != target:
            findings.append(Finding("versions", "error", "PKGBUILD",
                                    f"pkgver={m.group(1)} != pyproject {target}"))

    pkgbuild_git = repo / "PKGBUILD-git"
    if pkgbuild_git.exists():
        gm = re.search(r"^pkgver=(\S+)", pkgbuild_git.read_text(encoding="utf-8"), re.MULTILINE)
        if gm:
            leading = gm.group(1).split(".r", 1)[0]
            if leading != target:
                findings.append(Finding("versions", "error", "PKGBUILD-git",
                                        f"pkgver leading {leading!r} != pyproject {target}"))
            # VCS suffix grammar: X.Y.Z.rN.gHASH (the committed template form).
            if not re.fullmatch(r"\d+\.\d+\.\d+\.r\d+\.g[0-9a-f]+", gm.group(1)):
                findings.append(Finding("versions", "error", "PKGBUILD-git",
                                        f"pkgver {gm.group(1)!r} not in X.Y.Z.rN.gHASH form"))

    # Markers carry a literal `vX.Y.Z` semver. Documentation may also embed
    # `<!--version-->vX.Y.Z<!--/version-->` verbatim as a syntax example;
    # only flag numeric semver matches so the prose form is ignored.
    marker_re = re.compile(r"<!--version-->v(\d+\.\d+\.\d+)<!--/version-->")
    for doc_name in ("README.md", "DESIGN.md"):
        doc = repo / doc_name
        if not doc.exists():
            continue
        versions = marker_re.findall(doc.read_text())
        if not versions:
            findings.append(Finding("versions", "error", doc_name,
                                    "no <!--version-->vX.Y.Z<!--/version--> marker"))
        else:
            for v in versions:
                if v != target:
                    findings.append(Finding("versions", "error", doc_name,
                                            f"marker v{v} != pyproject {target}"))
    return findings


# ===========================================================================
# Group: manpage
# ===========================================================================

_TH_DATE_RE = re.compile(r'^(\.TH\s+\S+\s+\S+)\s+"[^"]+"', re.MULTILINE)
_SCDOC_GEN_RE = re.compile(r'^(\.\\" Generated by scdoc)\s+\S+\s*$', re.MULTILINE)


def _normalize_roff(text: str) -> str:
    """Strip scdoc-version-specific noise so the diff compares *content*.

    The committed man page is a rendered artifact, so a byte-compare silently
    pins it to whichever scdoc version last ran `make man`. Two renderer
    details vary between versions while being functionally inert in roff:

      * the ``.\\" Generated by scdoc <version>`` banner;
      * hyphen escaping — 1.11.5 emits ``\\-`` where 1.11.4 emitted ``-``
        (roff renders both identically).

    Normalising them keeps the guard answering the question it exists for
    ("does the page match the argparse tree?") instead of "is your scdoc the
    same build as mine?". The ``.TH`` date is normalised for the same reason.
    """
    text = _SCDOC_GEN_RE.sub(r'\1 VERSION', text, count=1)
    text = _TH_DATE_RE.sub(r'\1 "DATE"', text, count=1)
    return text.replace("\\-", "-")


def check_manpage(repo: Path) -> list[Finding]:
    """Regenerate the scdoc-hybrid man page into temp files and diff.

    Mirrors `make man`: tools/gen_options.py splices the argparse-derived
    COMMANDS sections into man/sysforge.1.scd.in, then scdoc renders the
    roff page. The committed man/sysforge.1 must match (date header
    normalised).
    """
    committed = repo / "man/sysforge.1"
    if not committed.exists():
        return [Finding("manpage", "error", "man/sysforge.1", "missing")]
    template = repo / "man/sysforge.1.scd.in"
    if not template.exists():
        return [Finding("manpage", "error", "man/sysforge.1.scd.in", "missing")]
    if not _which("scdoc"):
        return [Finding("manpage", "warn", "man/sysforge.1",
                        "scdoc not installed - skipping regen-diff")]

    with tempfile.NamedTemporaryFile(
        encoding="utf-8", mode="w", suffix=".scd", delete=False
    ) as tmp:
        scd_path = Path(tmp.name)
    try:
        env = {**os.environ}
        env["PYTHONPATH"] = str(repo) + ":" + env.get("PYTHONPATH", "")
        # Pin COLUMNS so any argparse-derived wrapping is deterministic
        # regardless of the caller's terminal width — `make man` pins the
        # same value, so the committed file is reproducible.
        env["COLUMNS"] = "80"
        # Intentionally inherit SYSFORGE_CONFIG_DIR from the caller's shell
        # rather than forcing it to `repo`: _build_parser() embeds path
        # defaults into the help text, and `make man` uses whatever env the
        # user has set. Overriding here would produce a spurious diff against
        # the committed man page (regenerated by `make man` in the same shell).
        res = subprocess.run(
            [sys.executable, "tools/gen_options.py",
             "--template", str(template),
             "--out", str(scd_path)],
            cwd=repo, env=env, capture_output=True, text=True,
        )
        if res.returncode != 0:
            return [Finding("manpage", "error", "man/sysforge.1",
                            f"gen_options.py failed: {res.stderr.strip()[:200]}")]
        scd_res = subprocess.run(
            ["scdoc"], input=scd_path.read_text(encoding="utf-8"),
            capture_output=True, text=True,
        )
        if scd_res.returncode != 0:
            return [Finding("manpage", "error", "man/sysforge.1",
                            f"scdoc failed: {scd_res.stderr.strip()[:200]}")]
        regen = _normalize_roff(scd_res.stdout)
        current = _normalize_roff(committed.read_text())
        if regen == current:
            return []
        diff = list(difflib.unified_diff(
            current.splitlines(), regen.splitlines(),
            lineterm="", fromfile="man/sysforge.1 (committed)",
            tofile="man/sysforge.1 (regenerated)", n=2,
        ))
        return [Finding("manpage", "error", "man/sysforge.1",
                        "stale - run `make man` and commit. First diff lines:\n  "
                        + "\n  ".join(diff[:20]))]
    finally:
        scd_path.unlink(missing_ok=True)


def _which(name: str) -> bool:
    return any(p and (Path(p) / name).exists() for p in os.environ.get("PATH", "").split(":"))


# ===========================================================================
# Group: provisioning
# ===========================================================================
#
# Both PKGBUILDs generate usr/lib/{sysusers,tmpfiles}.d/sysforge.conf inline.
# The runtime provisioning primitive (primitives/fs_provision.py) owns the same
# dirs root:sysforge with setgid mode 2775; install-time tmpfiles must agree so
# a package-installed system never needs the runtime sudo fallback. This check
# pins that contract: the sysforge group is declared, every sysforge runtime dir
# is tmpfiles-provisioned root:sysforge 2775, and both PKGBUILDs match.

# The runtime dirs that must be group-owned + setgid by tmpfiles, mirroring the
# FHS-rooted paths fs_provision provisions. /etc/sysforge is deliberately absent
# — config stays root-owned (read-only to sysforge).
_PROVISIONED_DIRS = {
    "/var/lib/sysforge",
    "/var/lib/sysforge/sentinels",
    "/var/cache/sysforge",
    "/var/cache/sysforge/llvm-pgo",
}
_EXPECTED_DIR_MODE = "2775"
_EXPECTED_DIR_OWNER = ("root", "sysforge")

# tmpfiles `d <path> <mode> <user> <group> <age>` line emitted via printf '...'.
_TMPFILES_LINE_RE = re.compile(
    r"printf\s+'d\s+(?P<path>\S+)\s+(?P<mode>\S+)\s+(?P<user>\S+)\s+(?P<group>\S+)\s+"
)
_SYSUSERS_GROUP_RE = re.compile(r"printf\s+'g\s+(?P<group>\S+)\s")


def _parse_tmpfiles_dirs(text: str) -> dict[str, tuple[str, str, str]]:
    """Map tmpfiles dir path -> (mode, user, group) from inline printf lines."""
    return {
        m.group("path"): (m.group("mode"), m.group("user"), m.group("group"))
        for m in _TMPFILES_LINE_RE.finditer(text)
    }


def _check_one_provisioning(group: str, label: str, text: str) -> list[Finding]:
    findings: list[Finding] = []

    groups = {m.group("group") for m in _SYSUSERS_GROUP_RE.finditer(text)}
    if _EXPECTED_DIR_OWNER[1] not in groups:
        findings.append(Finding(group, "error", label,
            f"sysusers.d does not declare group '{_EXPECTED_DIR_OWNER[1]}' "
            "(needed before tmpfiles assigns it)"))

    dirs = _parse_tmpfiles_dirs(text)
    for path in sorted(_PROVISIONED_DIRS):
        if path not in dirs:
            findings.append(Finding(group, "error", label,
                f"tmpfiles.d does not provision {path}"))
            continue
        mode, user, grp = dirs[path]
        if (user, grp) != _EXPECTED_DIR_OWNER:
            findings.append(Finding(group, "error", label,
                f"{path} owned {user}:{grp}, expected "
                f"{_EXPECTED_DIR_OWNER[0]}:{_EXPECTED_DIR_OWNER[1]}"))
        if mode != _EXPECTED_DIR_MODE:
            findings.append(Finding(group, "error", label,
                f"{path} mode {mode}, expected setgid {_EXPECTED_DIR_MODE}"))
    return findings


def check_provisioning(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    pkgbuild = repo / "PKGBUILD"
    git = repo / "PKGBUILD-git"
    if not pkgbuild.exists() or not git.exists():
        return [Finding("provisioning", "error", "PKGBUILD",
                        "PKGBUILD or PKGBUILD-git missing")]
    stable_text = pkgbuild.read_text()
    git_text = git.read_text()
    findings += _check_one_provisioning("provisioning", "PKGBUILD", stable_text)
    findings += _check_one_provisioning("provisioning", "PKGBUILD-git", git_text)

    # Parity: the generated tmpfiles must be identical between the two.
    if _parse_tmpfiles_dirs(stable_text) != _parse_tmpfiles_dirs(git_text):
        findings.append(Finding("provisioning", "error", "PKGBUILD vs PKGBUILD-git",
            "tmpfiles.d dir provisioning differs between PKGBUILD and PKGBUILD-git"))
    return findings


# ===========================================================================
# Group: dev_install
# ===========================================================================

def check_dev_install_parity(repo: Path) -> list[Finding]:
    """tools/dev_install.sh's MAPPING must mirror exactly the system paths
    the PKGBUILD package() installs, minus the generated sysusers.d/tmpfiles.d
    stubs (provisioning only, never symlinked - see dev_install.sh comment).
    """
    findings: list[Finding] = []
    pkgbuild = repo / "PKGBUILD"
    dev_install = repo / "tools" / "dev_install.sh"
    if not pkgbuild.exists():
        return [Finding("dev_install", "error", "PKGBUILD", "missing")]
    if not dev_install.exists():
        return [Finding("dev_install", "error", "tools/dev_install.sh", "missing")]

    packaged = _parse_pkgbuild_install_targets(pkgbuild.read_text())
    packaged = {
        p for p in packaged
        if not any(marker in p for marker in _EXCLUDED_SYSTEM_PATH_MARKERS)
    }

    result = subprocess.run(
        ["bash", str(dev_install), "print-targets"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return [Finding("dev_install", "error", "tools/dev_install.sh",
                        f"print-targets failed: {result.stderr.strip()}")]
    mapped = set(result.stdout.split())

    missing = sorted(packaged - mapped)
    extra = sorted(mapped - packaged)
    if missing:
        findings.append(Finding("dev_install", "error", "tools/dev_install.sh",
            f"missing from dev_install.sh: {missing}"))
    if extra:
        findings.append(Finding("dev_install", "error", "tools/dev_install.sh",
            f"extra in dev_install.sh: {extra}"))
    return findings


# ===========================================================================
# Driver
# ===========================================================================

GROUPS = {
    "configs":         check_configs,
    "config_comments": check_config_comments,
    "pkgbuild":        check_pkgbuild,
    "pkgbuild_parity": check_pkgbuild_parity,
    "provisioning":    check_provisioning,
    "hooks":           check_hooks,
    "completions":     check_completions,
    "completion_widths": check_completion_widths,
    "versions":        check_versions,
    "manpage":         check_manpage,
    "dev_install":     check_dev_install_parity,
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Pre-release validator for shipped sysforge artifacts.",
    )
    p.add_argument("--warn", action="store_true",
                   help="downgrade errors to warnings (exit 0 on findings)")
    p.add_argument("--check", action="append", default=[],
                   help="run only this group (repeatable)")
    p.add_argument("--list", action="store_true",
                   help="list groups and exit")
    p.add_argument("--repo", type=Path, default=REPO,
                   help="repo root to validate (default: this script's repo)")
    args = p.parse_args(argv)

    if args.list:
        for name in GROUPS:
            print(name)
        return 0

    selected = args.check or list(GROUPS.keys())
    unknown = [g for g in selected if g not in GROUPS]
    if unknown:
        print(f"unknown group(s): {unknown}", file=sys.stderr)
        return 2

    repo = args.repo.resolve()
    all_findings: list[Finding] = []
    for name in selected:
        try:
            all_findings.extend(GROUPS[name](repo))
        except Exception as e:
            all_findings.append(Finding(name, "error", "checker",
                                        f"check group crashed: {e!r}"))

    by_group: dict[str, list[Finding]] = {}
    for f in all_findings:
        by_group.setdefault(f.group, []).append(f)

    for group_name in selected:
        group_findings = by_group.get(group_name, [])
        if not group_findings:
            print(f"[OK]   {group_name}")
            continue
        worst = "FAIL" if any(f.severity == "error" for f in group_findings) else "WARN"
        print(f"[{worst}] {group_name}")
        for f in group_findings:
            print(f"  {f.severity.upper():5} {f.location}: {f.message}")

    error_count = sum(1 for f in all_findings if f.severity == "error")
    warn_count = sum(1 for f in all_findings if f.severity == "warn")
    print()
    print(f"summary: {error_count} error(s), {warn_count} warning(s)")
    if error_count and not args.warn:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
