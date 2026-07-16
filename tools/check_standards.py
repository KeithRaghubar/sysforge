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
                `##` category headings.
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
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
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
    """Next free ID for a '<version>-<TYPE>' prefix, across all three sources."""
    version, sep, typ = prefix.partition("-")
    if not sep or not re.fullmatch(r"\d+\.\d+\.\d+", version) or not typ:
        raise ValueError(
            f"--next-id expects <version>-<TYPE>, e.g. 2.2.0-F; got {prefix!r}"
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
# Driver
# ===========================================================================

GROUPS = {
    "paths":       check_paths,
    "spdx":        check_spdx,
    "changelog":   check_changelog,
    "encoding":    check_encoding,
    "claude_md":   check_claude_md,
    "roadmap_ids": check_roadmap_ids,
    "run_seam":    check_run_seam,
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
    p.add_argument("--next-id", metavar="VERSION-TYPE",
                   help="print the next free roadmap ID for a prefix "
                        "(e.g. 2.2.0-F) and exit")
    args = p.parse_args(argv)

    if args.next_id:
        try:
            print(next_id(args.repo.resolve(), args.next_id))
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
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
