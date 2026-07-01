# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
build_state.py — build history tracking

Manages /var/lib/sysforge/build_state.toml (or configured override).
Records per-package build metadata after each successful sysforge build.
Used by `sysforge update` to determine which packages need rebuilding.

build_state.toml is a superset of `pacman -Q`: every installed package has
an entry, regardless of whether sysforge built it. Entries are distinguished
by the `build_mode` field:

  - "source_built" — built by sysforge from source; carries pkgbuild_dir and
                  flags_string. (Legacy files use "profiled" for this value;
                  it is normalized to "source_built" on load — see __init__.)
  - "pacman"    — installed via pacman; pkgver/pkgrel/epoch parsed from
                  `pacman -Q` output; pkgbuild_dir and flags_string absent.
  - "pgo_llvm_toolchain" — LLVM toolchain packages with profdata reuse;
                  makepkg_wrapper injects -fprofile-use when compatible
                  clang.profdata is available, otherwise prompts.

`BuildState.sync_with_installed()` keeps the file in lockstep with pacman:
it adds pacman-mode entries for newly installed packages and prunes entries
for packages that are no longer installed.

Build *failures* live in a separate reserved top-level ``[failures]`` table
(keyed by pkgbase), written by `record_failure()` and surfaced by
`sysforge state failed`. It is kept out of the per-package install mirror so
`all_packages()` / `sync_with_installed()` stay a clean superset of
`pacman -Q`. A successful `record()` clears any prior failure for that
pkgbase, so the failed list self-heals on the next good build.

State dir resolution follows pipeline/state.py (highest priority first):
  1. Explicit Path passed at construction (from --state-dir CLI flag)
  2. SYSFORGE_STATE_DIR environment variable
  3. /var/lib/sysforge (default)

Public API:
    BuildState(state_dir)
    group_by_pkgbase(packages)
    parse_pacman_version(ver_str) -> (epoch, pkgver, pkgrel)
"""
import tomllib
from datetime import datetime, timezone
from pathlib import Path

# Reserved top-level key in build_state.toml for the failures namespace. Held
# apart from the per-package install records so it never leaks into
# all_packages()/sync_with_installed(). A package literally named "failures"
# would collide; none exists in practice.
_FAILURES_KEY = "failures"

# build_mode values. "source_built" replaced the legacy "profiled" token (which
# was confusingly overloaded with PGO and repo_mode "profiled"). Legacy files
# are normalized to the new value on load; readers compare against the new
# constant. The "!= pacman" predicate (update's rebuild scope) is unaffected.
BUILD_MODE_SOURCE = "source_built"
BUILD_MODE_PACMAN = "pacman"
_LEGACY_BUILD_MODE_SOURCE = "profiled"

# Bounds for the stored failure ``error`` blob — keep the tail (the real
# compiler/makepkg error is at the bottom), capped so build_state.toml stays
# human-readable.
_ERROR_MAX_LINES = 6
_ERROR_MAX_CHARS = 600


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _toml_escape(value) -> str:
    """Escape a value for a TOML basic (double-quoted) string."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _truncate_error(error) -> str:
    """Reduce an error/exception to a compact tail for storage."""
    s = str(error).strip()
    lines = s.splitlines()
    if len(lines) > _ERROR_MAX_LINES:
        s = "\n".join(lines[-_ERROR_MAX_LINES:])
    if len(s) > _ERROR_MAX_CHARS:
        s = s[-_ERROR_MAX_CHARS:]
    return s


def parse_pacman_version(ver_str: str) -> tuple[str, str, str]:
    """
    Parse a `pacman -Q` version string into (epoch, pkgver, pkgrel).

    Accepts ``[epoch:]pkgver-pkgrel``. Missing epoch defaults to "0".
    Missing pkgrel defaults to "1". An empty input yields ("0", "", "1").
    """
    if not ver_str:
        return ("0", "", "1")
    rest = ver_str
    epoch = "0"
    if ":" in rest:
        epoch, _, rest = rest.partition(":")
    if "-" in rest:
        pkgver, _, pkgrel = rest.rpartition("-")
    else:
        pkgver, pkgrel = rest, "1"
    return (epoch, pkgver, pkgrel)

def group_by_pkgbase(packages: dict) -> tuple[dict, dict]:
    """
    Group {pkgname: record} from BuildState.all_packages() by pkgbase.

    Returns:
        pkgbase_map   — {pkgbase: [pkgname, ...]}
        pkgbase_entry — {pkgbase: representative_record}  (first seen wins)
    """
    pkgbase_map: dict[str, list] = {}
    pkgbase_entry: dict[str, dict] = {}
    for pkgname, entry in packages.items():
        base = entry.get("pkgbase", pkgname)
        pkgbase_map.setdefault(base, []).append(pkgname)
        if base not in pkgbase_entry:
            pkgbase_entry[base] = entry
    return pkgbase_map, pkgbase_entry


class BuildState:
    """
    Read/write wrapper around build_state.toml.

    Records per-package build metadata (versions, pkgbuild directory) after
    each successful build. Used by `sysforge update` to determine what needs
    rebuilding.

    The state file is TOML for human readability and manual recovery.
    """

    def __init__(self, state_dir):
        self._dir = Path(state_dir)
        self.path = self._dir / "build_state.toml"
        raw = self._load()
        # Split the reserved failures namespace out of the install mirror.
        failures = raw.pop(_FAILURES_KEY, {})
        self._failures = failures if isinstance(failures, dict) else {}
        # Normalize the legacy "profiled" build_mode token to "source_built" at
        # this single load chokepoint, so every downstream comparison (and
        # read-only command) sees the new value regardless of file vintage. The
        # file self-migrates to the new token the next time save() runs.
        for entry in raw.values():
            if isinstance(entry, dict) and \
                    entry.get("build_mode") == _LEGACY_BUILD_MODE_SOURCE:
                entry["build_mode"] = BUILD_MODE_SOURCE
        self._data = raw

    def _load(self):
        if not self.path.exists():
            return {}
        with open(self.path, "rb") as f:
            return tomllib.load(f)

    def record(self, pkgname: str, pkgver: str, pkgrel: str,
               epoch: str, pkgbase: str, pkgbuild_dir: Path,
               build_mode: str | None = None,
               flags_string: str | None = None,
               built_at: str | None = None,
               built_upstream_commit: str | None = None,
               source: str | None = None,
               owner_stage: str | None = None,
               toolchain_variant: str | None = None,
               toolchain_fingerprint: str | None = None,
               reviewed_commit: str | None = None,
               origin_pkgbase: str | None = None) -> None:
        """Record build metadata for a single package name.

        ``built_at`` defaults to now; callers performing a repair pass may
        pass the original timestamp to preserve true build history.

        ``built_upstream_commit`` is the resolved upstream git SHA of a
        single-source VCS package at the time of build — read by
        ``sysforge update --devel`` to short-circuit ``pkgver()`` resolution
        via ``git ls-remote``. None for non-VCS or multi-git-source packages.

        ``source`` records where the PKGBUILD came from at build time
        ("aur" | "repo" | "git" | "local"). Read by ``sysforge update`` so the
        source classification is persisted across runs instead of being
        re-derived from live pacman + overrides every invocation. None for
        back-compat entries written before the field existed. ``"local"``
        means the PKGBUILD is hand-maintained with no upstream remote to
        sync from — source sync is skipped entirely.

        ``owner_stage`` names the pipeline stage that owns this package's
        lifecycle (e.g. ``"kernel"``). When set, ``sysforge update`` skips
        the package by default and tells the user to invoke the owning
        stage instead; ``--include-stage-owned`` overrides the skip. None
        for packages not claimed by any stage.

        ``toolchain_variant`` is the toolchain identity active at build
        time ("gcc" | "stock_llvm" | "pgo_llvm" | "system"). Read by
        ``sysforge update`` to surface toolchain drift — packages whose
        recorded variant differs from the now-active variant are flagged
        as candidates for rebuild. Sticky like ``source``/``owner_stage``:
        callers that don't know the variant (repair/backfill paths)
        preserve any prior value instead of erasing it.

        ``toolchain_fingerprint`` is an opaque identity string for the active
        toolchain at build time (see ``build_fingerprint.toolchain_fingerprint``)
        — path/size/mtime/version by default, or a libLLVM content hash under
        ``[toolchain] drift_detect = "content_hash"``. It lets ``sysforge
        update`` flag a *same-variant* toolchain rebuild (fresh codegen, same
        soname) that the ``toolchain_variant`` string alone would miss. Sticky
        like ``toolchain_variant``.

        ``reviewed_commit`` is the source clone's HEAD at the time of a
        successful build — the baseline for the PKGBUILD review gate
        (``primitives/pkgbuild_review.py``): a later build whose clone HEAD
        differs prompts for review of the intervening diff. None for local
        (non-git) PKGBUILDs. Sticky like the other provenance fields.

        ``origin_pkgbase`` is the *pre-rename* pkgbase for packages built with
        the ``-sysforge`` suffix (e.g. ``"llvm"`` for an installed
        ``llvm-sysforge``). The ``pkgbase`` field then holds the renamed value,
        so ``origin_pkgbase`` is what ``sysforge update`` uses to correlate the
        artifact back to its upstream identity (version checks, source sync —
        upstream ships ``llvm``, not ``llvm-sysforge``). None for un-renamed
        builds. Sticky like the other provenance fields.
        """
        entry = {
            "pkgver": pkgver,
            "pkgrel": pkgrel,
            "epoch": epoch,
            "pkgbase": pkgbase,
            "pkgbuild_dir": str(pkgbuild_dir),
            "built_at": built_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if build_mode is not None:
            entry["build_mode"] = build_mode
        if flags_string is not None:
            entry["flags_string"] = flags_string
        if built_upstream_commit is not None:
            entry["built_upstream_commit"] = built_upstream_commit
        # ``source``, ``owner_stage``, and ``toolchain_variant`` are sticky
        # provenance fields: once recorded by the stage that built the
        # package, they should not be erased by subsequent rebuilds that
        # don't know about them (e.g. an ``--include-stage-owned`` rebuild
        # that goes through ``sysforge update``'s call to makepkg_wrapper,
        # which inherits source from _UpdateResult but has no owner_stage
        # in scope). Preserve the prior value when the caller doesn't pass
        # one.
        prior = self._data.get(pkgname) or {}
        if source is not None:
            entry["source"] = source
        elif "source" in prior:
            entry["source"] = prior["source"]
        if owner_stage is not None:
            entry["owner_stage"] = owner_stage
        elif "owner_stage" in prior:
            entry["owner_stage"] = prior["owner_stage"]
        if toolchain_variant is not None:
            entry["toolchain_variant"] = toolchain_variant
        elif "toolchain_variant" in prior:
            entry["toolchain_variant"] = prior["toolchain_variant"]
        # ``toolchain_fingerprint`` (Q9): a finer-grained companion to
        # ``toolchain_variant`` that distinguishes a same-variant toolchain
        # rebuild (e.g. fresh PGO profdata, same soname). Sticky for the same
        # reason as the variant — a backfill rebuild that doesn't recompute it
        # must not erase it, or the entry would read as "never stamped" and be
        # exempted from the fingerprint drift rule.
        if toolchain_fingerprint is not None:
            entry["toolchain_fingerprint"] = toolchain_fingerprint
        elif "toolchain_fingerprint" in prior:
            entry["toolchain_fingerprint"] = prior["toolchain_fingerprint"]
        if reviewed_commit is not None:
            entry["reviewed_commit"] = reviewed_commit
        elif "reviewed_commit" in prior:
            entry["reviewed_commit"] = prior["reviewed_commit"]
        if origin_pkgbase is not None:
            entry["origin_pkgbase"] = origin_pkgbase
        elif "origin_pkgbase" in prior:
            entry["origin_pkgbase"] = prior["origin_pkgbase"]
        self._data[pkgname] = entry
        # A successful build clears any recorded failure for this pkgbase so
        # `sysforge state failed` self-heals on the next good build.
        self._failures.pop(pkgbase, None)

    def delete(self, pkgname: str) -> bool:
        """Remove an entry by pkgname.  Returns True if it existed."""
        return self._data.pop(pkgname, None) is not None

    def reconcile_external_installs(self, external_names) -> list[str]:
        """Demote source-built entries reinstalled externally via ``pacman -S``.

        ``external_names`` is the set of packages installed by something other
        than sysforge (computed by ``install_reconcile.external_install_targets``:
        buildstate-hook targets minus sysforge's own ``pacman -U`` targets).
        For each one currently recorded ``build_mode = "source_built"``, demote
        it to a plain ``pacman`` marker: keep the version identity
        (pkgver/pkgrel/epoch/pkgbase) but strip the source provenance
        (pkgbuild_dir, flags_string, source, built_upstream_commit,
        toolchain_variant, reviewed_commit, origin_pkgbase). This is what makes
        ``sysforge build mesa`` → ``pacman -S mesa`` stick: the next ``update``
        no longer rebuilds mesa from source.

        Demote (not delete) preserves the "build_state ⊇ ``pacman -Q``"
        invariant. **Stage-owned** entries (kernel/toolchain, carrying
        ``owner_stage``) are never auto-demoted — their lifecycle belongs to the
        owning stage. Returns the list of demoted pkgnames (caller saves).
        """
        demoted: list[str] = []
        for name in external_names:
            entry = self._data.get(name)
            if not entry:
                continue
            if entry.get("build_mode") != BUILD_MODE_SOURCE:
                continue
            if entry.get("owner_stage"):
                continue
            new_entry = {
                k: entry[k]
                for k in ("pkgver", "pkgrel", "epoch", "pkgbase")
                if k in entry
            }
            new_entry["build_mode"] = BUILD_MODE_PACMAN
            self._data[name] = new_entry
            demoted.append(name)
        return demoted

    def record_failure(self, pkgbase: str, *, error,
                       pkgver: str | None = None,
                       signature: str | None = None,
                       fix_cmd: str | None = None,
                       failed_at: str | None = None) -> None:
        """Record a build failure for ``pkgbase`` in the ``[failures]`` table.

        ``error`` is the failure message/exception (stored truncated to its
        tail). ``signature`` / ``fix_cmd`` come from postflight diagnosis
        (`build_diag`) when a known pattern matched. Overwrites any prior
        failure for the same pkgbase. ``failed_at`` defaults to now (ISO-8601).
        """
        entry = {
            "failed_at": failed_at or _now_iso(),
            "error": _truncate_error(error),
        }
        if pkgver:
            entry["pkgver"] = pkgver
        if signature:
            entry["signature"] = signature
        if fix_cmd:
            entry["fix_cmd"] = fix_cmd
        self._failures[pkgbase] = entry

    def clear_failure(self, pkgbase: str) -> bool:
        """Remove a recorded failure by pkgbase.  Returns True if it existed."""
        return self._failures.pop(pkgbase, None) is not None

    def all_failures(self) -> dict[str, dict]:
        """Return all recorded failures as {pkgbase: record}."""
        return dict(self._failures)

    def sync_with_installed(self, installed: dict[str, str]) -> tuple[int, int]:
        """
        Reconcile state with `pacman -Q` output so the file is a superset of
        installed packages.

        Adds a ``build_mode = "pacman"`` entry for every installed package
        that does not already have one, with pkgver/pkgrel/epoch parsed from
        the pacman version string. Prunes entries whose pkgname is no longer
        installed — this also removes zombies left by pre-superset parser
        runs (e.g. keys containing unexpanded ``$_pkgname``).

        Returns (added, removed).
        """
        installed_names = set(installed.keys())
        existing_names = set(self._data.keys())

        removed_names = existing_names - installed_names
        for name in removed_names:
            del self._data[name]

        added_names = installed_names - existing_names
        for name in added_names:
            epoch, pkgver, pkgrel = parse_pacman_version(installed[name])
            self._data[name] = {
                "pkgver": pkgver,
                "pkgrel": pkgrel,
                "epoch": epoch,
                "build_mode": "pacman",
            }

        return (len(added_names), len(removed_names))

    def get(self, pkgname: str) -> dict | None:
        """Return build record for pkgname, or None if not recorded."""
        return self._data.get(pkgname)

    def all_packages(self) -> dict[str, dict]:
        """Return all recorded packages as {pkgname: record}."""
        return dict(self._data)

    def save(self) -> None:
        """Write current state to disk atomically (write + rename)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text(self._serialize())
        tmp.rename(self.path)

    def _serialize(self) -> str:
        lines = [
            "# SysForge build state",
            "# Records per-package build metadata for use by sysforge update.",
            "",
        ]
        for pkgname, entry in sorted(self._data.items()):
            escaped = pkgname.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'["{escaped}"]')
            for key in ("pkgver", "pkgrel", "epoch", "pkgbase", "pkgbuild_dir", "build_mode", "flags_string", "built_at", "built_upstream_commit", "source", "owner_stage", "toolchain_variant", "toolchain_fingerprint", "reviewed_commit", "origin_pkgbase"):
                if key in entry:
                    val = _toml_escape(entry[key])
                    lines.append(f'{key} = "{val}"')
            lines.append("")
        if self._failures:
            lines.append("# Build failures (cleared on the next successful build).")
            lines.append("")
            for pkgbase, entry in sorted(self._failures.items()):
                escaped = pkgbase.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'[{_FAILURES_KEY}."{escaped}"]')
                for key in ("failed_at", "pkgver", "signature", "fix_cmd", "error"):
                    if key in entry:
                        val = _toml_escape(entry[key])
                        lines.append(f'{key} = "{val}"')
                lines.append("")
        return "\n".join(lines)
