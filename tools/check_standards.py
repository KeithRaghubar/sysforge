#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
tools/check_standards.py - pre-release validator for standards compliance.

Guards the mechanically-checkable subset of the committed standards documented
in docs/design/21-standards.md. The behavioural subset (NO_COLOR, --version,
stdout/stderr discipline, RFC 3339 timestamps, reproducibility) is covered by
tests/test_standards_compliance.py instead.

Run before cutting a release. tools/release.sh invokes it from preflight, and
`make check-standards` / `make pre-release` run it locally.

Usage:
    python tools/check_standards.py [--warn] [--check=GROUP] [--list] [--repo=PATH]

Groups:

    paths       XDG/FHS discipline: no source module other than paths.py may
                *construct* a sysforge user dir (Path.home()/expanduser/Path
                with a "...sysforge..." home-relative literal). Docstrings and
                log messages that merely mention a path are not flagged.
    spdx        Every first-party .py under sysforge/ and tools/ carries an
                SPDX-License-Identifier header. If the `reuse` CLI is on PATH,
                the full-tree `reuse lint` is run instead.
    changelog   Every docs/release-notes/v*.md — and the running accumulator
                unreleased.md — has a `#` title and uses only Keep a Changelog
                `##` category headings. In the accumulator only, entries within
                each section must also cite a roadmap ID and ascend by it
                (released notes are immutable history, so they are exempt).
    encoding    UTF-8 discipline: ruff's PLW1514 (preview) reports no text-mode
                open/read_text/write_text without an explicit encoding. Skipped
                with a warning if ruff is not available.
    claude_md   Citation freshness for every CLAUDE.md in the tree: backticked
                path citations must exist and `module.symbol` / `file.py::sym`
                citations must grep-resolve in the cited module. Fail-safe:
                tokens that cannot be mapped to a repo file (class attributes,
                CLI flags, globs, prose) are skipped, never flagged.
    roadmap_ids Cross-checks ROADMAP.md (open items) against docs/release-notes/
                (shipped items): flags an open ID reusing a shipped number, an ID
                listed both Planned and Abandoned, a shipped Q-typed ID, and
                (warn) sequence gaps in the active pyproject version's prefix.
    run_seam    External-command execution discipline: subprocess calls use
                argv-list form (never a string command), and shell=True carries
                a justified `# noqa: S602` from the single-site allowlist.
    privilege_seam  Escalation discipline: root-escalating argv routes through
                    primitives/privilege.py; raw ["sudo", ...] outside it is an
                    error (probe/drop-priv forms allowlisted).
    deprecations    Deprecation registry discipline (STD row 24): every record in
                    primitives/deprecations.py has a presence proof (a
                    warn_used call site for compat, a resolvable anchor for a
                    shim) and every warn_used literal resolves to a record; a
                    compat surface's removed_in is a major; and nothing declared
                    removed at or before the release target is still present
                    (--target-version). A registry that parses to zero records
                    is an error.
    semver_bump     Declared bump selection (STD row 3): a surface declared
                    removed in the release target must be named by a `## Removed`
                    entry in docs/release-notes/unreleased.md. The bump
                    comparison itself is --require-bump / --derive-bump, used by
                    tools/release.sh preflight, not a group.

Drift detection cases (verify these still fire after editing this script):
    - Add `Path.home() / ".cache/sysforge"` to a module other than paths.py.
    - Drop the SPDX header from a sysforge/*.py file.
    - Add a `## Notes` heading to a docs/release-notes/*.md file.
    - Add `open(p, "w")` without encoding= to a sysforge/*.py file.
    - Cite a nonexistent `tools/foo.sh` path in a CLAUDE.md file.
    - Rename a function cited as `module.symbol` in sysforge/CLAUDE.md.
    - Add an ID to ROADMAP Planned that already appears in a shipped v*.md.
    - List the same ID in both the Planned and Abandoned ROADMAP sections.
    - Reference a Q-typed ID in a shipped release-notes file.
    - Add `subprocess.run("echo hi")` (string form) to a sysforge/*.py file.
    - Add `shell=True` without `# noqa: S602` to a sysforge/*.py file.
    - Add `subprocess.run(["sudo", ...])` (raw escalation) outside privilege.py.
    - Delete a warn_used("…") call site but leave its registry record.
    - Add warn_used("not-registered") to a sysforge/*.py file.
    - Change a compat record's removed_in to a minor (e.g. 3.1.0).
    - Rename the symbol a shim record's anchor cites.
    - Replace the _REGISTRY literal with a comprehension (parse finds 0 records).
    - Run with --target-version=3.0.0 while a 3.0.0 removal is still present.
    - Break deprecations.py syntactically and run --check=semver_bump alone
      (a registry parse failure must error there too, not silently pass).
"""

from __future__ import annotations

import argparse
import ast
import inspect
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from _semver_vocab import BUMP_ORDER

REPO = Path(__file__).resolve().parent.parent


@dataclass
class Finding:
    group: str
    severity: str  # "error" or "warn"
    location: str
    message: str


# ===========================================================================
# Group: paths  (XDG / FHS discipline)
# ===========================================================================

# Path *construction* idioms that build a sysforge user dir. These must route
# through the paths.py constants (USER_CONFIG_DIR / USER_CACHE_DIR /
# USER_STATE_DIR). Matching requires the literal "sysforge" so that legitimate
# non-sysforge home paths (e.g. makepkg.conf under ~/.config, Steam config) and
# plain docstring/log mentions of a path are not flagged.
_PATH_CONSTRUCT_RES = (
    re.compile(r"Path\.home\(\)\s*/[^#\n]*sysforge"),
    re.compile(r"""expanduser\(\s*['"]~[^'"]*sysforge"""),
    re.compile(r"""\bPath\(\s*['"]~[^'"]*sysforge"""),
)

# paths.py is the one home allowed to construct these dirs.
_PATHS_EXEMPT = {"sysforge/primitives/paths.py"}


def check_paths(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    for py in sorted((repo / "sysforge").rglob("*.py")):
        rel = py.relative_to(repo).as_posix()
        if rel in _PATHS_EXEMPT:
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if any(rx.search(line) for rx in _PATH_CONSTRUCT_RES):
                findings.append(Finding(
                    "paths", "error", f"{rel}:{lineno}",
                    "constructs a sysforge user dir directly — import "
                    "USER_CONFIG_DIR/USER_CACHE_DIR/USER_STATE_DIR from paths.py",
                ))
    return findings


# ===========================================================================
# Group: spdx  (REUSE / SPDX licensing)
# ===========================================================================

# The header tag we grep for in the fallback path. Isolated here behind
# REUSE-Ignore guards so `reuse` does not mistake this literal for a license
# declaration *on this file* (the file's real header is at the top).
# REUSE-IgnoreStart
_SPDX_TAG = "SPDX-License-Identifier:"
# REUSE-IgnoreEnd


def check_spdx(repo: Path) -> list[Finding]:
    # Authoritative path: full-tree REUSE compliance when the CLI is available.
    if shutil.which("reuse"):
        proc = subprocess.run(
            ["reuse", "--root", str(repo), "lint"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return [Finding("spdx", "error", "reuse lint",
                            "tree is not REUSE-compliant (run `reuse lint` for detail)")]
        return []

    # Fallback: require an SPDX license header in every first-party .py source.
    findings: list[Finding] = []
    for base in ("sysforge", "tools"):
        for py in sorted((repo / base).rglob("*.py")):
            rel = py.relative_to(repo).as_posix()
            head = py.read_text(encoding="utf-8")[:512]
            if _SPDX_TAG not in head:
                findings.append(Finding("spdx", "error", rel,
                                        f"missing {_SPDX_TAG} header"))
    return findings


# ===========================================================================
# Group: changelog  (Keep a Changelog)
# ===========================================================================

_KAC_HEADINGS = {"Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"}


def check_changelog(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    notes_dir = repo / "docs" / "release-notes"
    if not notes_dir.is_dir():
        return findings
    # Lint published notes (v*.md) plus the running accumulator (unreleased.md),
    # since per-task entries are authored in the accumulator all cycle and must
    # obey the same Keep a Changelog vocabulary before they are renamed.
    notes_files = sorted(notes_dir.glob("v*.md"))
    unreleased = notes_dir / "unreleased.md"
    if unreleased.is_file():
        notes_files.append(unreleased)
    for md in notes_files:
        rel = md.relative_to(repo).as_posix()
        text = md.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not any(ln.startswith("# ") for ln in lines):
            findings.append(Finding("changelog", "error", rel,
                                    "missing a `# ` title heading"))
        for lineno, ln in enumerate(lines, 1):
            m = re.match(r"(#+)\s+(.*)", ln)
            if not m:
                continue
            level, heading = len(m.group(1)), m.group(2).strip()
            if level == 1:
                continue  # the `# ` title, validated above
            if heading in _KAC_HEADINGS:
                # Category headings must sit at `## ` exactly. A mis-leveled
                # `### Changed` would otherwise be invisible to this lint and
                # drift in commit-by-commit, defeating incremental authoring.
                if level != 2:
                    findings.append(Finding(
                        "changelog", "error", f"{rel}:{lineno}",
                        f"`{'#' * level} {heading}` must be a `## ` "
                        f"Keep a Changelog section heading",
                    ))
            elif level == 2:
                findings.append(Finding(
                    "changelog", "error", f"{rel}:{lineno}",
                    f"`## {heading}` is not a Keep a Changelog category "
                    f"({', '.join(sorted(_KAC_HEADINGS))})",
                ))
    if unreleased.is_file():
        findings.extend(_check_accumulator_id_order(repo, unreleased))
    return findings


def _entry_sort_key(block: str) -> tuple[int, int, int, str, int, int] | None:
    """Ascending-order key for one accumulator entry: its first roadmap ID.

    The *first* ID is the entry's filing ID — later ones are cross-references
    (a related item, the item a fix regressed). `promoted from <ID>` names the
    old pre-promotion ID and is stripped for the same reason
    :func:`shipped_ids_from_text` strips it.

    The trailing element is the `(n/N)` facet number of a repeated ID (0 when
    absent). Two entries filed under the same ID otherwise compare *equal*,
    which would leave their relative order the one spot in the accumulator no
    lint constrains; the facet makes `(1/2)` sort before `(2/2)`.
    """
    body = _HTML_COMMENT_RE.sub("", block)
    body = re.sub(r"promoted from\s+`?" + _ID_RE.pattern, "", body)
    m = _ID_RE.search(body)
    if not m:
        return None
    ver, typ, num = _parse_id(m.group(0))  # type: ignore[misc]
    major, minor, patch = (int(p) for p in ver.split("."))
    lead = _RN_ENTRY_RE.match(block)
    facet = int(lead.group(2)) if lead and lead.group(2) else 0
    return (major, minor, patch, typ, num, facet)


# An accumulator entry's lead: `- **`<ID>` — <title>.** <body>`, the same shape
# ROADMAP.md entries use (gen_roadmap_table._ENTRY_RE), so an item reads
# identically on the backlog and in the notes. Leading with the ID also makes
# `_entry_sort_key`'s "first ID is the filing ID" structural rather than a
# convention about where in the prose the ID happened to be dropped.
#
# One item routinely ships several distinct user-visible changes, so its ID
# repeats across entries. A repeat within one section carries a `(n/N)` facet
# suffix — `- **`<ID>` (n/N) — <title>.**` — so the entry states up front that
# it is one facet of a larger item rather than reading as a duplicate filing.
# Cross-section repeats carry none: one Keep a Changelog entry cannot span
# `Changed` and `Removed`, and a `(2/3)` under a heading the reader arrives at
# with no memory of `(1/3)` explains nothing.
_RN_ENTRY_RE = re.compile(
    r"^- \*\*`(\d+\.\d+\.\d+-[A-Z]+\d+)`(?:\s+\((\d+)/(\d+)\))?\s*—\s")


def _check_accumulator_id_order(repo: Path, unreleased: Path) -> list[Finding]:
    """Entries within each `## ` section must lead with their ID and ascend by it.

    Scoped to the accumulator only, on the same reasoning as the `roadmap_ids`
    Q-promotion check: released `v*.md` files are immutable history and are
    grandfathered. The accumulator is the only file still being appended to,
    and it is the one the ordering rule exists for — CLAUDE.md requires a
    re-sort on every add/remove precisely so concurrent entries land
    deterministically instead of accreting in landing order.

    Ordering is (version, type, number), so within a cycle `B` precedes `F`
    precedes `Q` precedes `STD`. An entry with no ID is reported rather than
    silently sorted last: every entry is required to cite its roadmap ID.
    """
    findings: list[Finding] = []
    rel = unreleased.relative_to(repo).as_posix()
    heading: str | None = None
    blocks: list[tuple[int, list[str]]] = []

    def flush() -> None:
        if heading is None:
            return
        # Pass 1: parse. The facet suffix is validated against how often its ID
        # occurs in *this* section, so nothing can be checked until every entry
        # under the heading has been seen.
        parsed: list[tuple[int, str, tuple[int, int, int, str, int, int],
                           re.Match[str] | None]] = []
        for lineno, block in blocks:
            text = "".join(block)
            key = _entry_sort_key(text)
            if key is None:
                findings.append(Finding(
                    "changelog", "error", f"{rel}:{lineno}",
                    f"entry under `## {heading}` cites no roadmap ID "
                    f"(every entry records the item it shipped)",
                ))
                continue
            lead = _RN_ENTRY_RE.match(block[0])
            if lead is None:
                findings.append(Finding(
                    "changelog", "error", f"{rel}:{lineno}",
                    f"entry under `## {heading}` must lead with its roadmap ID: "
                    "``- **`<ID>` — <title>.** <body>``, the same shape "
                    "ROADMAP.md entries use",
                ))
            cur_id = f"{key[0]}.{key[1]}.{key[2]}-{key[3]}{key[4]}"
            parsed.append((lineno, cur_id, key, lead))

        counts = Counter(cur_id for _, cur_id, _, _ in parsed)
        seen: dict[str, set[int]] = {}
        prev_key = None
        prev_label = None
        for lineno, cur_id, key, lead in parsed:
            total = counts[cur_id]
            facet = key[5]
            if total == 1 and facet:
                findings.append(Finding(
                    "changelog", "error", f"{rel}:{lineno}",
                    f"`{cur_id}` appears once under `## {heading}` — drop the "
                    f"`(n/N)` suffix (a lone entry is not one facet of several)",
                ))
            elif total > 1 and not facet:
                findings.append(Finding(
                    "changelog", "error", f"{rel}:{lineno}",
                    f"`{cur_id}` repeats {total} times under `## {heading}` — "
                    f"suffix each entry's ID `(n/{total})` so it reads as one "
                    f"facet, not a duplicate filing",
                ))
            elif total > 1 and lead is not None and int(lead.group(3)) != total:
                findings.append(Finding(
                    "changelog", "error", f"{rel}:{lineno}",
                    f"`{cur_id}` denominator is `/{lead.group(3)}` but it has "
                    f"{total} entries under `## {heading}` — restate as "
                    f"`(n/{total})`",
                ))
            if total > 1 and facet:
                bucket = seen.setdefault(cur_id, set())
                if facet in bucket or not 1 <= facet <= total:
                    findings.append(Finding(
                        "changelog", "error", f"{rel}:{lineno}",
                        f"`{cur_id}` facets must number 1..{total} once each "
                        f"under `## {heading}` — `({facet}/…)` is duplicated "
                        f"or out of range",
                    ))
                bucket.add(facet)
            label = f"{cur_id} ({facet}/{total})" if facet else cur_id
            if prev_key is not None and key < prev_key:
                findings.append(Finding(
                    "changelog", "error", f"{rel}:{lineno}",
                    f"`## {heading}` entries must ascend by roadmap ID: "
                    f"{label} follows {prev_label} — re-sort the section "
                    f"(CLAUDE.md: re-sort on every add/remove)",
                ))
            prev_key, prev_label = key, label

    for lineno, ln in enumerate(unreleased.read_text(encoding="utf-8").splitlines(),
                                1):
        if ln.startswith("## "):
            flush()
            heading, blocks = ln[3:].strip(), []
        elif heading is None:
            continue
        elif ln.startswith("- "):
            blocks.append((lineno, [ln]))
        elif blocks:
            blocks[-1][1].append(ln)
    flush()
    return findings


# ===========================================================================
# Group: encoding  (UTF-8 discipline via ruff PLW1514)
# ===========================================================================

def check_encoding(repo: Path) -> list[Finding]:
    if not shutil.which("ruff"):
        return [Finding("encoding", "warn", "ruff",
                        "ruff not found — skipping PLW1514 encoding check")]
    proc = subprocess.run(
        ["ruff", "check", "sysforge", "tools",
         "--select", "PLW1514", "--preview", "--output-format", "concise"],
        cwd=repo, capture_output=True, text=True,
    )
    findings: list[Finding] = []
    for line in proc.stdout.splitlines():
        if "PLW1514" in line:
            loc = line.split(":", 1)[0] if ":" in line else "?"
            findings.append(Finding("encoding", "error", loc,
                                    "text-mode I/O without explicit encoding= (UTF-8)"))
    return findings


# ===========================================================================
# Group: claude_md  (CLAUDE.md citation freshness)
# ===========================================================================

# Backticked tokens in CLAUDE.md files. A token is only *flagged* when it maps
# unambiguously to a repo file that is missing, or to a module that no longer
# contains the cited symbol. Anything ambiguous is skipped — the lint must
# never force prose rewrites to appease a false positive.
_BACKTICK_RE = re.compile(r"`([^`]+)`")

# Directory trees to ignore when discovering CLAUDE.md files.
_CLAUDE_MD_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}

# Bare-token first segments that are stdlib/external, never sysforge modules.
_EXTERNAL_MODULES = {"os", "sys", "re", "json", "pathlib", "subprocess", "shutil"}

# Characters that mark a token as prose/pattern rather than a checkable
# citation (globs, env reads, placeholders, shell fragments, ellipses).
_UNCHECKABLE_RE = re.compile(r"[\s*<>\[\]{}$~@=|,'\"§…]")

_PATHLIKE_RE = re.compile(r"^[\w./-]+$")
_DOTTED_RE = re.compile(r"^[A-Za-z_][\w.]*$")


def _resolve_module_file(repo: Path, name: str) -> Path | None:
    """Locate `<name>.py` in the tree; None if absent or ambiguous."""
    hits = [p for p in repo.rglob(f"{name}.py")
            if not _CLAUDE_MD_SKIP_DIRS & set(p.parts)]
    return hits[0] if len(hits) == 1 else None


def _check_citation(repo: Path, token: str) -> str | None:
    """Return an error message for a stale citation, or None (ok/skipped)."""
    # `file.py::symbol` — resolve the file, then grep for the symbol.
    if "::" in token:
        file_part, _, sym = token.partition("::")
        mod = _resolve_module_file(repo, file_part.removesuffix(".py")) \
            if file_part.endswith(".py") else None
        if mod is None or not sym.isidentifier():
            return None
        if sym not in mod.read_text(encoding="utf-8"):
            return f"cites `{token}` but {mod.relative_to(repo)} has no `{sym}`"
        return None

    # Call syntax: keep only the callee (`pacman.get_pkgdest()` etc.).
    token = token.split("(", 1)[0]
    if not token or _UNCHECKABLE_RE.search(token) or token.startswith(("/", "-", ".")):
        return None

    # Explicit relative path (contains `/`): must exist, repo- or sysforge-
    # relative (the fragment cites `primitives/paths.py` style paths).
    if "/" in token:
        if not _PATHLIKE_RE.match(token):
            return None
        rel = token.rstrip("/")
        if (repo / rel).exists() or (repo / "sysforge" / rel).exists():
            return None
        return f"cites path `{token}` which does not exist"

    # Bare script/module filename: resolvable anywhere in the tree.
    if token.endswith((".py", ".sh")):
        if (repo / token).exists() or _resolve_module_file(repo, token.rsplit(".", 1)[0]):
            return None
        return f"cites `{token}` — no such file in the tree"

    # Dotted `module[.module...].symbol` seam citation.
    if "." in token and _DOTTED_RE.match(token) and not token.endswith("."):
        segs = token.split(".")
        first = segs[0]
        # Skip stdlib, CamelCase class attrs, and 1-char tails (`build_core.X`).
        if first in _EXTERNAL_MODULES or not first.islower() or len(segs[-1]) < 2:
            return None
        # Literal file in the repo root (`sysforge.install`, `foo.toml`)?
        if (repo / token).exists():
            return None
        # Walk leading segments as packages: `verbs.runner.run_verb` →
        # sysforge/verbs/runner.py :: run_verb. Longest module prefix wins.
        mod: Path | None = None
        sym_segs: list[str] = []
        for i in range(len(segs) - 1, 0, -1):
            candidate = "/".join(segs[:i]) + ".py"
            for base in (repo, repo / "sysforge", repo / "sysforge" / "primitives"):
                if (base / candidate).is_file():
                    mod, sym_segs = base / candidate, segs[i:]
                    break
            if mod:
                break
        if mod is None:
            mod = _resolve_module_file(repo, first)
            sym_segs = segs[1:]
        if mod is None:
            return None  # can't map — skip, fail-safe
        if sym_segs and sym_segs[0] not in mod.read_text(encoding="utf-8"):
            return (f"cites `{token}` but {mod.relative_to(repo)} "
                    f"has no `{sym_segs[0]}`")
    return None


def check_claude_md(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    for md in sorted(repo.rglob("CLAUDE.md")):
        if _CLAUDE_MD_SKIP_DIRS & set(md.parts):
            continue
        rel = md.relative_to(repo).as_posix()
        seen: set[str] = set()
        for token in _BACKTICK_RE.findall(md.read_text(encoding="utf-8")):
            token = token.strip()
            if token in seen:
                continue
            seen.add(token)
            msg = _check_citation(repo, token)
            if msg:
                findings.append(Finding("claude_md", "error", rel, msg))
    return findings


# ===========================================================================
# Group: roadmap_ids  (ROADMAP.md <-> release-notes ID collision detection)
# ===========================================================================

# ID grammar: <version>-<TYPE><n>, e.g. 2.1.0-F1 -> ("2.1.0", "F", 1).
_ID_RE = re.compile(r"(\d+\.\d+\.\d+)-([A-Z]+)(\d+)")
# A ROADMAP entry: a bullet whose first token is a bold code-span holding an ID.
# Anchoring here skips the `## ID scheme` section's inline prose examples.
_ROADMAP_ENTRY_RE = re.compile(r"^- \*\*`(\d+\.\d+\.\d+-[A-Z]+\d+)`")
# HTML comment blocks carry instructional prose (e.g. the accumulator header's
# "e.g. (1.2.0-F35)") whose IDs are examples, not entries — strip before scan.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _parse_id(s: str) -> tuple[str, str, int] | None:
    m = _ID_RE.fullmatch(s)
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def roadmap_ids_from_text(text: str) -> dict[str, str]:
    """Map each bold-bullet ROADMAP entry ID to 'planned' or 'abandoned'."""
    state = "planned"
    out: dict[str, str] = {}
    for line in text.splitlines():
        if re.match(r"^##\s+Abandoned", line):
            state = "abandoned"
            continue
        if re.match(r"^##\s+", line):
            state = "planned"
            continue
        m = _ROADMAP_ENTRY_RE.match(line)
        if m:
            out[m.group(1)] = state
    return out


def shipped_ids_from_text(text: str) -> set[str]:
    """Every ID token in release-notes text, after HTML comments are stripped.

    A `promoted from <ID>` citation records historical lineage — the ID it names
    is the *old* (unshipped) roadmap ID, not a shipped one — so it is stripped
    before token extraction.
    """
    stripped = _HTML_COMMENT_RE.sub("", text)
    stripped = re.sub(r"promoted from\s+`?" + _ID_RE.pattern, "", stripped)
    return {m.group(0) for m in _ID_RE.finditer(stripped)}


def _project_version(repo: Path) -> str:
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return ""
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        m = re.match(r'\s*version\s*=\s*"(\d+\.\d+\.\d+)"', line)
        if m:
            return m.group(1)
    return ""


def _gather_ids(repo: Path) -> tuple[dict[str, str], set[str]]:
    roadmap: dict[str, str] = {}
    roadmap_md = repo / "ROADMAP.md"
    if roadmap_md.is_file():
        roadmap = roadmap_ids_from_text(roadmap_md.read_text(encoding="utf-8"))
    shipped: set[str] = set()
    notes_dir = repo / "docs" / "release-notes"
    if notes_dir.is_dir():
        files = sorted(notes_dir.glob("v*.md"))
        unreleased = notes_dir / "unreleased.md"
        if unreleased.is_file():
            files.append(unreleased)
        for md in files:
            shipped |= shipped_ids_from_text(md.read_text(encoding="utf-8"))
    return roadmap, shipped


def check_roadmap_ids(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    roadmap, shipped = _gather_ids(repo)
    planned = {i for i, s in roadmap.items() if s == "planned"}
    abandoned = {i for i, s in roadmap.items() if s == "abandoned"}

    # Check 1: an open (Planned) ID that reuses a shipped number.
    for i in sorted(planned & shipped):
        findings.append(Finding(
            "roadmap_ids", "error", "ROADMAP.md",
            f"{i} is open in ROADMAP but already shipped in release-notes "
            f"(collision — allocate a fresh ID via --next-id)",
        ))
    # Check 2: an ID that is both Planned and Abandoned. roadmap_ids_from_text
    # keeps a single final state per ID (last section wins), so an ID bulleted
    # under both headers would otherwise vanish from one side of this
    # intersection — re-scan the raw text tracking both memberships directly.
    roadmap_md = repo / "ROADMAP.md"
    if roadmap_md.is_file():
        state = "planned"
        seen_planned: set[str] = set()
        seen_abandoned: set[str] = set()
        for line in roadmap_md.read_text(encoding="utf-8").splitlines():
            if re.match(r"^##\s+Abandoned", line):
                state = "abandoned"
                continue
            m = _ROADMAP_ENTRY_RE.match(line)
            if m:
                (seen_abandoned if state == "abandoned" else seen_planned).add(m.group(1))
        for i in sorted(seen_planned & seen_abandoned):
            findings.append(Finding(
                "roadmap_ids", "error", "ROADMAP.md",
                f"{i} is listed in both Planned and Abandoned sections",
            ))
    # Check 4: a Q-typed ID that shipped without promotion to F/B/STD. Only the
    # mutable accumulator (unreleased.md) is checked — released v*.md files are
    # immutable history and are grandfathered against past-process Q misses.
    unreleased = repo / "docs" / "release-notes" / "unreleased.md"
    unreleased_ids: set[str] = set()
    if unreleased.is_file():
        unreleased_ids = shipped_ids_from_text(unreleased.read_text(encoding="utf-8"))
    for i in sorted(unreleased_ids):
        parsed = _parse_id(i)
        if parsed and parsed[1] == "Q":
            findings.append(Finding(
                "roadmap_ids", "error", "docs/release-notes/",
                f"{i} is a Q-typed (open-question) ID that appears shipped — "
                f"a Q must be promoted to F/B/STD before implementation",
            ))
    # Check 5: sequence gaps, active version prefix only (warn).
    active = _project_version(repo)
    if active:
        by_type: dict[str, set[int]] = {}
        for i in planned | abandoned | shipped:
            p = _parse_id(i)
            if p and p[0] == active:
                by_type.setdefault(p[1], set()).add(p[2])
        for typ, nums in sorted(by_type.items()):
            for k in range(1, max(nums) + 1):
                if k not in nums:
                    findings.append(Finding(
                        "roadmap_ids", "warn", "ROADMAP.md",
                        f"{active}-{typ}{k} is missing from the {active}-{typ} "
                        f"sequence (gap in the active cycle)",
                    ))
    return findings


def next_id(repo: Path, prefix: str) -> str:
    """Next free ID for a roadmap prefix, across all three sources.

    Accepts either a bare ``<TYPE>`` (e.g. ``F``) — the version is derived from
    ``pyproject.toml`` so the caller can't misattribute the cycle, which is the
    whole point of the tool — or a full ``<version>-<TYPE>`` (e.g. ``2.2.0-F``)
    to target a cycle other than the current one.
    """
    if "-" in prefix:
        version, _, typ = prefix.partition("-")
    else:
        version, typ = _project_version(repo), prefix
        if not version:
            raise ValueError(
                "--next-id: could not read the current version from "
                "pyproject.toml; pass an explicit <version>-<TYPE> instead"
            )
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"[A-Z]+", typ):
        raise ValueError(
            f"--next-id expects <TYPE> (e.g. F) or <version>-<TYPE> "
            f"(e.g. 2.2.0-F); got {prefix!r}"
        )
    roadmap, shipped = _gather_ids(repo)
    nums = [
        p[2] for i in (set(roadmap) | shipped)
        if (p := _parse_id(i)) and p[0] == version and p[1] == typ
    ]
    return f"{version}-{typ}{(max(nums) + 1) if nums else 1}"


# ===========================================================================
# Group: run_seam  (subprocess-seam discipline — STD row 17)
# ===========================================================================

_SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output"}
# Allowlist of paths permitted a justified shell=True (each entry also needs
# an inline "noqa: S602" marker at the call site). Empty: no sysforge/ site
# currently uses shell=True — this standard exists to keep it that way. A
# future justified shell=True would add its path here in the same commit.
_RUN_SEAM_SHELL_ALLOWLIST: frozenset[str] = frozenset()


def check_run_seam(repo: Path) -> list[Finding]:
    """External commands: argv-list form only; shell=True needs a justified noqa."""
    findings: list[Finding] = []
    for py in sorted((repo / "sysforge").rglob("*.py")):
        rel = py.relative_to(repo).as_posix()
        src = py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=rel)
        except SyntaxError:
            continue  # fail-safe: unparseable files are skipped, never flagged
        lines = src.splitlines()
        # Resolve how `subprocess` is imported in THIS file so aliased module
        # imports (`import subprocess as _sp`) and direct function imports
        # (`from subprocess import run`) are covered too — not just the literal
        # `subprocess.run(...)` spelling. Row 17 claims *all* subprocess sites.
        module_aliases: set[str] = set()
        direct_funcs: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "subprocess":
                        module_aliases.add(alias.asname or "subprocess")
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                for alias in node.names:
                    if alias.name in _SUBPROCESS_CALLS:
                        direct_funcs.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # module-qualified: subprocess.run(...) / _sp.run(...)
            is_seam_call = (
                isinstance(func, ast.Attribute)
                and func.attr in _SUBPROCESS_CALLS
                and isinstance(func.value, ast.Name)
                and func.value.id in module_aliases
            )
            # bare name from `from subprocess import run`
            if not is_seam_call:
                is_seam_call = isinstance(func, ast.Name) and func.id in direct_funcs
            if not is_seam_call:
                continue
            lineno = node.lineno
            # (a) first positional arg must not be a string literal
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                findings.append(Finding(
                    "run_seam", "error", f"{rel}:{lineno}",
                    "subprocess call with a string command — use argv-list form "
                    "(STD row 17)",
                ))
            # (b) shell=True must carry a "noqa: S602" marker and sit in the allowlist
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) \
                        and kw.value.value is True:
                    # scan the call's source lines for the noqa marker
                    end = getattr(node, "end_lineno", lineno)
                    span = "\n".join(lines[lineno - 1:end])
                    justified = "noqa: S602" in span
                    if not (justified and rel in _RUN_SEAM_SHELL_ALLOWLIST):
                        findings.append(Finding(
                            "run_seam", "error", f"{rel}:{lineno}",
                            "shell=True requires a justified `# noqa: S602` and "
                            "allowlist entry (STD row 17)",
                        ))
    return findings


# ===========================================================================
# Group: privilege_seam  (escalation-seam discipline — STD row 18)
# ===========================================================================

# Structural allowlist of NON-escalation sudo forms (matched on the argv tail
# after the "sudo" head). Anything else must route through privilege.py.
def _is_allowlisted_sudo(elts: list) -> bool:
    """elts: the ast list elements AFTER the leading "sudo" constant."""
    def const(e):
        return e.value if isinstance(e, ast.Constant) else object()
    if not elts:
        return False
    head = const(elts[0])
    if head == "-v":                       # sudo -v  (cred refresh)
        return True
    if head == "-n" and len(elts) >= 2 and const(elts[1]) == "true":
        return True                        # sudo -n true  (probe)
    if head == "-u":                       # sudo -u <any> ...  (drop-privilege)
        return True
    return False


def _privilege_seam_findings_for_tree(tree: ast.AST, rel: str) -> list[Finding]:
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        first = node.elts[0]
        if not (isinstance(first, ast.Constant) and first.value == "sudo"):
            continue
        if _is_allowlisted_sudo(node.elts[1:]):
            continue
        findings.append(Finding(
            "privilege_seam", "error", f"{rel}:{node.lineno}",
            "raw sudo escalation — route through primitives/privilege.py "
            "(privileged_argv/run_privileged) (STD row 18)",
        ))
    return findings


def check_privilege_seam(repo: Path) -> list[Finding]:
    """Root-escalating argv must go through primitives/privilege.py."""
    findings: list[Finding] = []
    for py in sorted((repo / "sysforge").rglob("*.py")):
        rel = py.relative_to(repo).as_posix()
        if rel == "sysforge/primitives/privilege.py":
            continue  # the sanctioned home
        src = py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=rel)
        except SyntaxError:
            continue  # fail-safe: unparseable files skipped, never flagged
        findings.extend(_privilege_seam_findings_for_tree(tree, rel))
    return findings


# ===========================================================================
# Group: distro_portability  (Arch-derivative portability — STD row 23)
# ===========================================================================

# The one module permitted to read os-release, and the identity sources that are
# forbidden outright. Distro identity read anywhere else — or inferred from
# pacman.conf, /etc/arch-release or a hostname — is what breaks on an Arch
# derivative, silently and only on someone else's machine.
_OS_RELEASE_HOME = "sysforge/primitives/os_release.py"
_OS_RELEASE_PATHS = ("/etc/os-release", "/usr/lib/os-release")
# Legacy/heuristic identity markers. /etc/arch-release is the classic one: it is
# present on derivatives too, so it identifies nothing.
_FORBIDDEN_IDENTITY_MARKERS = ("/etc/arch-release", "/etc/lsb-release")


def _check_identity_home(repo: Path) -> list[Finding]:
    """Sub-invariant (c): distro identity comes from os-release(5), one primitive."""
    findings: list[Finding] = []
    for py in sorted((repo / "sysforge").rglob("*.py")):
        rel = py.relative_to(repo).as_posix()
        src = py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=rel)
        except SyntaxError:
            continue  # fail-safe: unparseable files skipped, never flagged
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            val = node.value
            if val in _OS_RELEASE_PATHS and rel != _OS_RELEASE_HOME:
                findings.append(Finding(
                    "distro_portability", "error", f"{rel}:{node.lineno}",
                    f"os-release read outside {_OS_RELEASE_HOME} — distro "
                    "identity has one home (STD row 23)",
                ))
            elif val in _FORBIDDEN_IDENTITY_MARKERS:
                findings.append(Finding(
                    "distro_portability", "error", f"{rel}:{node.lineno}",
                    f"{val} is not a distro identity (an Arch derivative ships "
                    "it too) — use primitives/os_release.py (STD row 23)",
                ))
    if not (repo / _OS_RELEASE_HOME).is_file():
        findings.append(Finding(
            "distro_portability", "error", _OS_RELEASE_HOME,
            "the os-release home is missing (STD row 23)",
        ))
    return findings


# --- sub-invariant (a): no hardcoded sync-repo names ------------------------
#
# A derivative carries its own sync DBs, often ahead of core/extra, and drops or
# renames others. Any module that decides "is this a repo package?" by comparing
# against a repo-name literal is wrong there — ask pacman instead
# (`aur.repo_packages` runs `pacman -Si`, which is why the repo-vs-AUR makedep
# split in `build_core.prepare_deps` already ports; that split is the failure
# class behind the exit-8 regression documented at build_core.py:268).
#
# The sole allowlisted home is pacman.py's I/O fallback, used only when
# /etc/pacman.conf cannot be read at all.
_REPO_NAMES_HOME = "sysforge/primitives/pacman.py"
_REPO_NAME_LITERALS = frozenset({
    "core", "extra", "multilib", "community", "testing",
    "core-testing", "extra-testing", "multilib-testing",
})


def _check_repo_name_literals(repo: Path) -> list[Finding]:
    """Sub-invariant (a): sync-repo names are read, never hardcoded."""
    findings: list[Finding] = []
    for py in sorted((repo / "sysforge").rglob("*.py")):
        rel = py.relative_to(repo).as_posix()
        if rel == _REPO_NAMES_HOME:
            continue  # the sanctioned fallback
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError:
            continue  # fail-safe: unparseable files skipped, never flagged
        for node in ast.walk(tree):
            # Only *collection* members and comparison operands: a bare string
            # elsewhere (a kwarg name, a log word) is not a repo-name decision.
            if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                operands = node.elts
            elif isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
            else:
                continue
            for e in operands:
                if isinstance(e, ast.Constant) and e.value in _REPO_NAME_LITERALS:
                    findings.append(Finding(
                        "distro_portability", "error", f"{rel}:{e.lineno}",
                        f"hardcoded sync-repo name {e.value!r} — resolve repos from "
                        "/etc/pacman.conf (pacman._read_sync_repo_names) or ask "
                        "pacman (aur.repo_packages) (STD row 23)",
                    ))
    return findings


# --- sub-invariant (b): the system makepkg.conf is the merge baseline -------
#
# A derivative ships its own -march/LTO defaults in /etc/makepkg.conf. They must
# arrive as the *baseline* that profile keys override, never be replaced by a
# value sysforge invented. Two structural facts keep that true: the path is read
# in exactly one place, and the emitter both loads the system assignments and
# writes them out.
_MAKEPKG_CONF_PATH_HOME = "sysforge/primitives/config.py"
_MAKEPKG_CONF_PATH = "/etc/makepkg.conf"
_MAKEPKG_EMITTER = "sysforge/primitives/makepkg_conf.py"
_MAKEPKG_BASELINE_CALL = "parse_system_makepkg_conf"
_MAKEPKG_BASELINE_VAR = "system_assignments"


def _check_makepkg_baseline(repo: Path) -> list[Finding]:
    """Sub-invariant (b): the system makepkg.conf is read once and merged, not
    replaced."""
    findings: list[Finding] = []

    for py in sorted((repo / "sysforge").rglob("*.py")):
        rel = py.relative_to(repo).as_posix()
        if rel == _MAKEPKG_CONF_PATH_HOME:
            continue  # the sanctioned home (config.SYSTEM_MAKEPKG_CONF)
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and node.value == _MAKEPKG_CONF_PATH):
                findings.append(Finding(
                    "distro_portability", "error", f"{rel}:{node.lineno}",
                    f"{_MAKEPKG_CONF_PATH} read outside "
                    f"{_MAKEPKG_CONF_PATH_HOME} — use "
                    "config.SYSTEM_MAKEPKG_CONF / parse_system_makepkg_conf "
                    "(STD row 23)",
                ))

    emitter = repo / _MAKEPKG_EMITTER
    if not emitter.is_file():
        findings.append(Finding(
            "distro_portability", "error", _MAKEPKG_EMITTER,
            "the makepkg.conf emitter is missing (STD row 23)",
        ))
        return findings
    src = emitter.read_text(encoding="utf-8")
    if _MAKEPKG_BASELINE_CALL not in src:
        findings.append(Finding(
            "distro_portability", "error", _MAKEPKG_EMITTER,
            f"emitter no longer calls {_MAKEPKG_BASELINE_CALL}() — the system "
            "conf must be the merge baseline (STD row 23)",
        ))
    # The baseline must be *emitted*, not merely read: the conf is assembled by
    # iterating the system assignments and substituting overrides, so a lost
    # iteration would silently drop every key the derivative set.
    if f"for key, raw_val in {_MAKEPKG_BASELINE_VAR}.items()" not in src:
        findings.append(Finding(
            "distro_portability", "error", _MAKEPKG_EMITTER,
            f"emitter no longer iterates {_MAKEPKG_BASELINE_VAR} when building "
            "conf lines — system conf keys would be dropped from the merged "
            "conf (STD row 23)",
        ))
    return findings


def check_distro_portability(repo: Path) -> list[Finding]:
    """STD row 23 — repo, toolchain-default, and identity assumptions that would
    break an Arch derivative. Three sub-invariants, one group."""
    return (_check_repo_name_literals(repo)
            + _check_makepkg_baseline(repo)
            + _check_identity_home(repo))


# ===========================================================================
# Group: deprecations  (STD row 24 — the deprecation registry)
# ===========================================================================

_DEPRECATIONS_SRC = "sysforge/primitives/deprecations.py"


def _pyproject_version(repo: Path) -> str | None:
    """The current project version, or None if unreadable."""
    pyp = repo / "pyproject.toml"
    if not pyp.exists():
        return None
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyp.read_text(encoding="utf-8"),
                  re.MULTILINE)
    return m.group(1) if m else None


def _ver(s: str) -> tuple[int, int, int]:
    """SemVer X.Y.Z -> comparable tuple. Malformed sorts lowest."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", (s or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else (0, 0, 0)


_REGISTRY_EMPTY_MSG = (
    "registry parse found no records — the module exists but no "
    "Deprecation(...) literals were readable (built dynamically, "
    "_REGISTRY renamed, or the file has a syntax error?)"
)


def _parse_registry(repo: Path) -> list[dict]:
    """Statically read the Deprecation(...) records from deprecations.py.

    AST rather than import: this tool honours --repo=PATH, and importing would
    validate the *installed* package while claiming to validate that tree.
    Module-level `NAME = "literal"` assignments are resolved so `kind=CONFIG_KEY`
    yields "config_key".
    """
    src = repo / _DEPRECATIONS_SRC
    if not src.exists():
        return []
    try:
        tree = ast.parse(src.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    consts: dict[str, str] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            consts[node.targets[0].id] = node.value.value
    records: list[dict] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Deprecation"):
            continue
        rec: dict = {}
        for kw in node.keywords:
            if isinstance(kw.value, ast.Constant):
                rec[kw.arg] = kw.value.value
            elif isinstance(kw.value, ast.Name):
                rec[kw.arg] = consts.get(kw.value.id)
        records.append(rec)
    return records


def _warn_used_surfaces(repo: Path) -> dict[str, list[str]]:
    """Map surface -> call-site paths for every `warn_used("…")` in sysforge/."""
    out: dict[str, list[str]] = {}
    for py in sorted((repo / "sysforge").rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = py.relative_to(repo).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else fn.id if isinstance(fn, ast.Name) else None)
            if name != "warn_used" or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.setdefault(arg.value, []).append(rel)
    return out


def _anchor_error(repo: Path, anchor: str) -> str | None:
    """Presence probe for a SHIM record's anchor: `<repo-relative>.py::<symbol>`.

    Deliberately NOT _check_citation: that helper fail-safes to None whenever
    _resolve_module_file cannot uniquely resolve a name, which for a presence
    probe inverts the result — an unresolvable anchor would silently pass. Here
    an unresolvable anchor is an error.
    """
    file_part, sep, sym = anchor.partition("::")
    if not sep or not sym.isidentifier() or not file_part.endswith(".py"):
        return f"anchor {anchor!r} is not `<repo-relative path>.py::<symbol>`"
    p = repo / file_part
    if not p.exists():
        return f"anchor {anchor!r}: {file_part} does not exist"
    if not re.search(rf"\b{re.escape(sym)}\b", p.read_text(encoding="utf-8")):
        return f"anchor {anchor!r}: {file_part} has no `{sym}`"
    return None


def check_deprecations(repo: Path,
                       target_version: str | None = None) -> list[Finding]:
    """STD row 24 — registry integrity and declared-removal enforcement.

    R1  bijection: every record has >=1 presence proof, and every warn_used
        literal resolves to a record.
    R2  a COMPAT record's removed_in must be a major (X.0.0).
    R3  target_version >= removed_in while the surface is still present.

    target_version defaults to the current pyproject version, which catches
    "we already shipped past a declared removal".
    """
    findings: list[Finding] = []
    src_rel = _DEPRECATIONS_SRC

    if not (repo / _DEPRECATIONS_SRC).exists():
        return [Finding("deprecations", "error", src_rel, "registry module missing")]

    records = _parse_registry(repo)
    # A check that cannot fail is worse than no check. Don't early-return here:
    # an empty registry alongside a live warn_used() call site is still an R1a
    # bijection failure and must be reported too.
    if not records:
        findings.append(Finding("deprecations", "error", src_rel,
                        _REGISTRY_EMPTY_MSG))

    call_sites = _warn_used_surfaces(repo)
    registered = {r.get("surface") for r in records}

    # R1a: no unregistered warn_used literal.
    for surface, sites in sorted(call_sites.items()):
        if surface not in registered:
            findings.append(Finding(
                "deprecations", "error", sites[0],
                f"warn_used({surface!r}) is not in the registry "
                f"({src_rel}) — add a record or fix the literal"))

    for rec in records:
        surface = rec.get("surface") or "<unnamed>"
        function = rec.get("function")
        removed_in = rec.get("removed_in") or ""

        # R1b: every record needs a presence proof.
        if function == "shim":
            anchor = rec.get("anchor")
            if not anchor:
                findings.append(Finding("deprecations", "error", src_rel,
                                        f"{surface}: shim record has no anchor"))
            else:
                msg = _anchor_error(repo, anchor)
                if msg:
                    findings.append(Finding("deprecations", "error", src_rel,
                                            f"{surface}: {msg}"))
        else:
            if rec.get("anchor"):
                findings.append(Finding(
                    "deprecations", "error", src_rel,
                    f"{surface}: compat record carries an anchor; presence is "
                    f"proven by its warn_used call sites"))
            if surface not in call_sites:
                findings.append(Finding(
                    "deprecations", "error", src_rel,
                    f"{surface}: no warn_used({surface!r}) call site — the "
                    f"record is vestigial; delete it, or add the call at the "
                    f"read path"))

        # R2: compat removals ride a major.
        if function == "compat" and not re.fullmatch(r"\d+\.0\.0", removed_in):
            findings.append(Finding(
                "deprecations", "error", src_rel,
                f"{surface}: compat removed_in={removed_in!r} is not a major "
                f"(X.0.0) — removing a working read path is breaking"))
        if function == "shim" and not re.fullmatch(r"\d+\.\d+\.0", removed_in):
            findings.append(Finding(
                "deprecations", "error", src_rel,
                f"{surface}: shim removed_in={removed_in!r} is not X.Y.0"))

        # R3: overdue removal.
        target = target_version or _pyproject_version(repo)
        if target and _ver(target) >= _ver(removed_in) > (0, 0, 0):
            still_present = (surface in call_sites) or bool(rec.get("anchor"))
            if still_present:
                findings.append(Finding(
                    "deprecations", "error", src_rel,
                    f"{surface}: declared removed_in={removed_in} and the "
                    f"release target is {target} — the surface is still "
                    f"present. Delete it (and this record) before releasing."))
    return findings


# ===========================================================================
# Group: semver_bump  (STD row 3 — declared bump selection)
# ===========================================================================

# Keep a Changelog section -> the bump it implies (row 13 supplies the
# vocabulary; SemVer §§6-8 supply the mapping).
_SECTION_BUMP = {
    "Added": "minor",
    "Changed": "patch",      # a breaking Changed must say **Breaking:**
    "Deprecated": "minor",
    "Removed": "major",
    "Fixed": "patch",
    "Security": "patch",
}

_ACCUMULATOR = "docs/release-notes/unreleased.md"


def derive_bump(text: str) -> tuple[str | None, str]:
    """Required bump for an accumulator body, plus the evidence for it.

    Returns (bump, evidence); bump is None when the accumulator has no authored
    entries (tools/release.sh already hard-fails that case, so this does not
    duplicate the error). The strongest signal present wins. A `**Breaking:**`
    marker forces major regardless of the section it sits in — which is the
    documented residual risk of deriving from sections: a breaking `Changed`
    entry that omits the marker reads as patch. release.sh prints this evidence
    so the inference is auditable.

    The marker is searched for across the entry's whole block, not just its
    bullet line: an entry opens with a bold `**`<ID>` — title.**` lead, so
    `**Breaking:**` starts the body and lands on a continuation line whenever
    the title fills the first one.
    """
    body = _HTML_COMMENT_RE.sub("", text)
    best: str | None = None
    evidence = "no authored entries"
    section: str | None = None
    blocks: list[tuple[int, str | None, list[str]]] = []
    for lineno, line in enumerate(body.splitlines(), start=1):
        m = re.match(r"^##\s+(\w+)\s*$", line)
        if m:
            section = m.group(1)
            continue
        if line.lstrip().startswith("- "):
            blocks.append((lineno, section, [line]))
        elif blocks:
            blocks[-1][2].append(line)
    for lineno, sect, block in blocks:
        if "**Breaking:**" in "\n".join(block):
            candidate, why = "major", f"**Breaking:** marker, line {lineno}"
        elif sect is not None and sect in _SECTION_BUMP:
            candidate = _SECTION_BUMP[sect]
            why = f"## {sect}, line {lineno}"
        else:
            continue
        if best is None or BUMP_ORDER.index(candidate) > BUMP_ORDER.index(best):
            best, evidence = candidate, why
    return best, evidence


def check_semver_bump(repo: Path,
                      target_version: str | None = None) -> list[Finding]:
    """STD row 3 — declared bump selection.

    R4  a registry record whose removed_in equals the release target must be
        named by a `## Removed` entry in the accumulator, so a removal cannot
        ship undocumented (ties row 3 to row 13).

    The bump comparison itself is a release-time concern and lives behind
    --require-bump / --derive-bump, not in this group: `make pre-release` runs
    before the bump is chosen.
    """
    findings: list[Finding] = []
    acc = repo / _ACCUMULATOR
    if not acc.exists():
        return [Finding("semver_bump", "error", _ACCUMULATOR, "missing")]
    text = acc.read_text(encoding="utf-8")

    target = target_version or _pyproject_version(repo)
    if not target:
        return findings

    body = _HTML_COMMENT_RE.sub("", text)
    # The `## Removed` section body, for surface-name matching.
    removed_block = ""
    section = None
    for line in body.splitlines():
        m = re.match(r"^##\s+(\w+)\s*$", line)
        if m:
            section = m.group(1)
            continue
        if section == "Removed":
            removed_block += line + "\n"

    records = _parse_registry(repo)
    # Same distinction as check_deprecations: "registry unreadable" (error) is
    # not the same as "registry read, nothing due" (empty findings is a clean
    # pass). Don't let an unreadable registry fall through as the latter.
    if not records:
        findings.append(Finding("semver_bump", "error", _DEPRECATIONS_SRC,
                        _REGISTRY_EMPTY_MSG))
        return findings

    for rec in records:
        surface = rec.get("surface") or ""
        removed_in = rec.get("removed_in") or ""
        if not surface or _ver(removed_in) != _ver(target):
            continue
        if surface not in removed_block:
            findings.append(Finding(
                "semver_bump", "error", _ACCUMULATOR,
                f"{surface} is declared removed_in={removed_in} (the release "
                f"target) but no `## Removed` entry names it — a removal must "
                f"not ship undocumented"))
    return findings


# ===========================================================================
# Driver
# ===========================================================================

GROUPS = {
    "paths":          check_paths,
    "spdx":           check_spdx,
    "changelog":      check_changelog,
    "encoding":       check_encoding,
    "claude_md":      check_claude_md,
    "roadmap_ids":    check_roadmap_ids,
    "run_seam":       check_run_seam,
    "privilege_seam": check_privilege_seam,
    "distro_portability": check_distro_portability,
    "deprecations":   check_deprecations,
    "semver_bump":    check_semver_bump,
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Pre-release validator for sysforge standards compliance.",
    )
    p.add_argument("--warn", action="store_true",
                   help="downgrade errors to warnings (exit 0 on findings)")
    p.add_argument("--check", action="append", default=[],
                   help="run only this group (repeatable)")
    p.add_argument("--list", action="store_true", help="list groups and exit")
    p.add_argument("--repo", type=Path, default=REPO,
                   help="repo root to validate (default: this script's repo)")
    p.add_argument("--target-version", metavar="X.Y.Z",
                   help="version this release will ship as; version-relative "
                        "rules (overdue removals, release-note removal parity) "
                        "evaluate against it instead of the current "
                        "pyproject.toml version")
    p.add_argument("--next-id", metavar="TYPE",
                   help="print the next free roadmap ID and exit; a bare TYPE "
                        "(e.g. F) derives the current cycle from pyproject.toml, "
                        "or pass VERSION-TYPE (e.g. 2.2.0-F) for another cycle")
    p.add_argument("--derive-bump", action="store_true",
                   help="print the bump the accumulated release notes require "
                        "(major|minor|patch) and exit")
    p.add_argument("--require-bump", metavar="KIND",
                   help="exit non-zero if KIND (major|minor|patch) is weaker "
                        "than the accumulated release notes require; always "
                        "prints the derived value and its evidence")
    args = p.parse_args(argv)

    if args.target_version is not None and not re.fullmatch(r"\d+\.\d+\.\d+",
                                                args.target_version):
        print(f"ERROR: --target-version={args.target_version!r} is not strict "
              f"SemVer X.Y.Z", file=sys.stderr)
        return 2

    if args.next_id:
        try:
            print(next_id(args.repo.resolve(), args.next_id))
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0

    if args.derive_bump or args.require_bump is not None:
        if args.require_bump is not None and args.require_bump not in BUMP_ORDER:
            print(f"ERROR: --require-bump={args.require_bump!r} is not one of "
                  f"{BUMP_ORDER}", file=sys.stderr)
            return 2
        repo = args.repo.resolve()
        acc = repo / _ACCUMULATOR
        if not acc.exists():
            print(f"ERROR: {_ACCUMULATOR} is missing", file=sys.stderr)
            return 2
        bump, evidence = derive_bump(acc.read_text(encoding="utf-8"))
        if bump is None:
            print(f"ERROR: {_ACCUMULATOR} has no authored entries",
                  file=sys.stderr)
            return 1
        if args.derive_bump:
            print(bump)
            return 0
        print(f"required = {bump} (from: {evidence})")
        if BUMP_ORDER.index(args.require_bump) < BUMP_ORDER.index(bump):
            print(f"ERROR: --bump={args.require_bump} is weaker than the "
                  f"accumulated release notes require ({bump})", file=sys.stderr)
            return 1
        return 0

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
            fn = GROUPS[name]
            # Version-relative groups opt in by accepting target_version.
            if "target_version" in inspect.signature(fn).parameters:
                all_findings.extend(fn(repo, target_version=args.target_version))
            else:
                all_findings.extend(fn(repo))
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
