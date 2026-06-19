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
                     scdoc render) matches the committed man/sysforge.1
                     (header date normalised). Skipped with a warning if
                     scdoc is not installed.

Drift detection cases (verify these still fire after editing this script):
    - Add `frobnicate = true` to etc/sysforge/sysforge.toml.
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
    "sysforge.toml":  {"ui", "git", "build", "failure_handling", "safety"},
    "profiles.toml":  {"paths", "defaults", "profiles", "rules",
                       "append_conflict_groups", "consumes_inference"},
    "packages.toml":  {"build", "package", "group"},
    "kernel.toml":    {"kconfig"},
    "toolchain.toml": {"packages", "llvm"},
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
    "kernel.toml":    {"enabled", "compiler", "pkgname", "srcdir",
                       "bootloader", "interactive", "pkgbuild_src_dir",
                       "source", "base_config", "require_fallback_kernel",
                       "boot_audit", "min_boot_free_mb",
                       "capture_lsmod_snapshot", "device_kconfig",
                       "kconfig_merge"},
    "toolchain.toml": {"enabled", "compiler", "pgo", "skip_build",
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
_SHA256_RE = re.compile(r"^sha256sums=\((.*?)\)", re.DOTALL | re.MULTILINE)


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

    sm = _SHA256_RE.search(text)
    if sm:
        for s in re.findall(r"'([^']+)'", sm.group(1)):
            if s == "SKIP":
                findings.append(Finding("pkgbuild", "error", "PKGBUILD",
                    "sha256sums=('SKIP') in stable PKGBUILD (only valid for PKGBUILD-git)"))
            elif re.fullmatch(r"0+", s) or s.startswith("DRYRUN"):
                findings.append(Finding("pkgbuild", "error", "PKGBUILD",
                                        f"sha256sums contains placeholder: {s}"))
    return findings


# ===========================================================================
# Group: pkgbuild_parity
# ===========================================================================

# Keys allowed to differ between PKGBUILD (stable) and PKGBUILD-git (VCS).
# Everything else must be byte-identical after parsing.
_ALLOWED_PKGBUILD_DIVERGENCE = {
    "pkgname", "pkgver", "pkgrel", "pkgdesc",
    "source", "sha256sums", "conflicts", "provides",
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

    with tempfile.NamedTemporaryFile(encoding="utf-8", mode="w", suffix=".scd", delete=False) as tmp:
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
        regen = _TH_DATE_RE.sub(r'\1 "DATE"', scd_res.stdout, count=1)
        current = _TH_DATE_RE.sub(r'\1 "DATE"', committed.read_text(), count=1)
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
    for p in os.environ.get("PATH", "").split(":"):
        if p and (Path(p) / name).exists():
            return True
    return False


# ===========================================================================
# Driver
# ===========================================================================

GROUPS = {
    "configs":         check_configs,
    "pkgbuild":        check_pkgbuild,
    "pkgbuild_parity": check_pkgbuild_parity,
    "hooks":           check_hooks,
    "completions":     check_completions,
    "versions":        check_versions,
    "manpage":         check_manpage,
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
