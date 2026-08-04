# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Phase 3 of ``update``: per-pkgbase version check.

``_check_one_pkgbase`` decides, for a single pkgbase, whether an upgrade/
rebuild is pending and what summary action to record. It runs once per
pkgbase inside the update orchestrator's ``ThreadPoolExecutor``, so it must
stay free of shared mutable state — every input arrives as an argument and
the only output is an ``_UpdateResult`` (or ``None`` to skip).

The module imports downward only (primitives + the update leaves
``update_result`` / ``update_common``); ``update.py`` re-exports
``_check_one_pkgbase`` so existing ``from sysforge.update import
_check_one_pkgbase`` call sites and tests are unchanged.
"""

import re
from pathlib import Path

from sysforge import log
from sysforge.update_result import _UpdateResult
from sysforge.update_common import _SYNC_STATUS_TO_ACTION, _is_vcs
from sysforge.primitives.version import format_version, vercmp
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.vcs_pkgver import evaluate_vcs_pkgver, peek_upstream_commit

_log = log.get_logger("UPDATE")

_UNRESOLVED_EXPANSION = re.compile(r"[${}]")


def _oldest_installed_ver(
    pkgnames: list[str], all_installed: dict[str, str],
) -> str | None:
    """Lowest installed version across ``pkgnames``, or None if none installed.

    Split-package members are normally lock-stepped, but they can drift (a
    subpackage toggled off keeps its last-built version installed forever).
    Taking the oldest is both deterministic and conservative: a rebuild is
    what brings the laggard forward, so the laggard is what decides. A
    ``vercmp`` failure degrades to sorted-first rather than to hash order.
    """
    present = sorted(pn for pn in pkgnames if pn in all_installed)
    if not present:
        return None
    oldest = all_installed[present[0]]
    for pn in present[1:]:
        ver = all_installed[pn]
        try:
            if vercmp(ver, oldest) < 0:
                oldest = ver
        except RuntimeError:
            continue
    return oldest


def _produced_pkgnames(pkgbuild_path: Path, pkgnames: list[str]) -> list[str]:
    """Restrict ``pkgnames`` to members the next build will actually produce.

    ``PKGBUILD.sysforge`` is the patched copy the last build ran from, and it
    persists in the pkgbuild_dir — so its ``pkgname=()`` array is an exact,
    stage-agnostic record of which subpackages are being built (the kernel
    stage drops ``-headers``/``-docs`` there via
    ``pkgbuild_patcher.patch_kernel_subpackages``). A member missing from it is
    an orphan left installed by an older build with a different toggle: no
    rebuild will ever refresh it, so letting it drive the verdict would pin a
    permanent phantom "update available".

    Falls back to the full list whenever the patched copy is absent,
    unparseable, or names no member of this pkgbase — a filter that returns
    nothing is a filter that misread the PKGBUILD, not proof of zero members.
    """
    patched = pkgbuild_path.parent / "PKGBUILD.sysforge"
    if not patched.exists():
        return pkgnames
    try:
        patched_meta = parse_pkgbuild(patched)
    except Exception as e:  # noqa: BLE001 — advisory filter; never fail the check
        _log.debug(f"failed to parse {patched}: {e} — counting all members")
        return pkgnames
    declared = patched_meta.get("globals", {}).get("pkgname")
    if isinstance(declared, str):
        declared = [declared]
    if not declared:
        return pkgnames
    produced = [pn for pn in pkgnames if pn in set(declared)]
    return produced or pkgnames


def _check_one_pkgbase(
    pkgbase: str,
    pkgnames: list[str],
    entry: dict,
    sync_failures: dict[str, tuple[str, str]],
    all_installed: dict[str, str],
    unrecorded_names: set[str],
    skip_sync_check: bool,
    rpc_version_by_base: dict[str, str],
    force_devel: bool = False,
    built_upstream_commit: str | None = None,
    pacman_updates_map: dict[str, str] | None = None,
) -> _UpdateResult | None:
    """Check a single pkgbase and return an _UpdateResult, or None on skip.

    Pacman-class repo packages (``entry["repo_class"] == "pacman"``) take a
    fast path: no PKGBUILD parse, no clone — installed-vs-checkupdates
    vercmp only. The slow PKGBUILD-parse path below runs for AUR/git and
    override-tagged repo packages.
    """
    has_record = not any(pn in unrecorded_names for pn in pkgnames)
    source = entry.get("source")

    # Pacman fast-path: checkupdates already told us if a repo upgrade is
    # pending. The pkgbuild_dir doesn't need to exist (we never source-build
    # this class), so this branch precedes the directory existence check.
    if entry.get("repo_class") == "pacman":
        # No source build here, so there is no patched PKGBUILD to consult —
        # just take the oldest member so the verdict can't ride hash order.
        installed_ver: str | None = _oldest_installed_ver(pkgnames, all_installed)
        if installed_ver is None:
            return None
        if pacman_updates_map is None:
            # checkupdates unavailable; can't decide. Surface once, skip
            # action so the package shows up in the summary as deferred.
            return _UpdateResult(
                pkgbase=pkgbase, pkgnames=pkgnames,
                action="SKIPPED_NO_CHECKUPDATES",
                installed_ver=installed_ver, pkgbuild_ver=None,
                pkgbuild_path=None, has_build_record=has_record, source=source,
            )
        # checkupdates lists each pkgname (not pkgbase) that needs upgrade.
        # Pick the newest mapped version across this pkgbase's pkgnames.
        new_ver: str | None = None
        for pn in pkgnames:
            mapped = pacman_updates_map.get(pn)
            if mapped is None:
                continue
            if new_ver is None or vercmp(mapped, new_ver) > 0:
                new_ver = mapped
        if new_ver is None:
            return _UpdateResult(
                pkgbase=pkgbase, pkgnames=pkgnames, action="UP_TO_DATE",
                installed_ver=installed_ver, pkgbuild_ver=installed_ver,
                pkgbuild_path=None, has_build_record=has_record, source=source,
            )
        return _UpdateResult(
            pkgbase=pkgbase, pkgnames=pkgnames, action="NEEDS_PACMAN_UPGRADE",
            installed_ver=installed_ver, pkgbuild_ver=new_ver,
            pkgbuild_path=None, has_build_record=has_record, source=source,
        )

    # VCS fast-path: without ``--devel`` we never resolve or rebuild VCS
    # packages, so the PKGBUILD parse, the pkgbuild_dir existence probe, and
    # the sync-failure log are all wasted work. Return DEVEL straight from
    # the installed version. Mirrors the source-sync filter in
    # ``_sync_sources`` — both edges of the build pipeline ignore VCS dirs
    # entirely when ``--devel`` is absent.
    if _is_vcs(pkgbase) and not force_devel:
        devel_installed_ver = _oldest_installed_ver(pkgnames, all_installed)
        if devel_installed_ver is None:
            return None
        return _UpdateResult(
            pkgbase=pkgbase, pkgnames=pkgnames, action="DEVEL",
            installed_ver=devel_installed_ver, pkgbuild_ver=None,
            pkgbuild_path=None, has_build_record=has_record, source=source,
        )

    pkgbuild_dir = Path(entry["pkgbuild_dir"])

    if not pkgbuild_dir.is_dir():
        _log.warn(f"{pkgbase}: pkgbuild_dir {pkgbuild_dir} not found — skipping")
        return None

    pkgbuild_path = pkgbuild_dir / "PKGBUILD"
    if not pkgbuild_path.exists():
        _log.warn(f"{pkgbase}: PKGBUILD not found at {pkgbuild_path} — skipping")
        return None

    if not skip_sync_check and pkgbase in sync_failures:
        status, msg = sync_failures[pkgbase]
        _log.error(msg)
        action = _SYNC_STATUS_TO_ACTION.get(status, "PULL_FAILED")
        return _UpdateResult(
            pkgbase=pkgbase, pkgnames=pkgnames, action=action,
            installed_ver=None, pkgbuild_ver=None, pkgbuild_path=pkgbuild_path,
            has_build_record=has_record, source=source,
        )

    try:
        pkgmeta = parse_pkgbuild(pkgbuild_path)
    except Exception as e:
        _log.warn(f"{pkgbase}: failed to parse PKGBUILD: {e} — skipping")
        return None

    globals_ = pkgmeta.get("globals", {})
    pkgbuild_ver = format_version(globals_)

    # Static PKGBUILD parser can't evaluate bash parameter expansion
    # (${var//-/_}, ${var/[a-z]/.sfx}, etc.). When pkgver still contains
    # shell metacharacters, fall back to the AUR RPC version we already
    # cached in source_meta.toml — it's the authoritative released version
    # and is vercmp-ready (already includes pkgrel and any epoch prefix).
    if _UNRESOLVED_EXPANSION.search(pkgbuild_ver):
        # For a coexist-renamed package the entry's ``pkgbase`` carries the
        # renamed value (e.g. ``llvm-sysforge``), but the RPC cache is keyed by
        # the stock upstream base — so fall back to ``origin_pkgbase`` when the
        # renamed base misses. This is the sole read of origin_pkgbase in the
        # update path; the rest of the correlation flows through pkgbuild_dir.
        rpc_ver = rpc_version_by_base.get(pkgbase)
        if not rpc_ver:
            origin = entry.get("origin_pkgbase")
            if origin:
                rpc_ver = rpc_version_by_base.get(origin)
        if rpc_ver:
            pkgbuild_ver = rpc_ver
        else:
            _log.warn(
                f"{pkgbase}: pkgver '{pkgbuild_ver}' has unresolved shell "
                "expansion and no cached RPC version — skipping"
            )
            return None

    # Live-install-set iteration guarantees every pkgbase reaching here has
    # at least one installed sub-package; pick that version for vercmp.
    #
    # Members can disagree (a subpackage toggled off keeps its last-built
    # version installed forever), and pkgnames arrives in set-iteration order,
    # so "first installed member wins" made the verdict depend on the hash
    # seed. Compare against the oldest member the next build will actually
    # produce: deterministic, and it ignores orphans no rebuild can refresh.
    # The full-list parse is skipped whenever the members already agree.
    installed_vers = {all_installed[pn] for pn in pkgnames if pn in all_installed}
    if len(installed_vers) > 1:
        candidates = _produced_pkgnames(pkgbuild_path, pkgnames)
    else:
        candidates = pkgnames
    installed_ver = _oldest_installed_ver(candidates, all_installed)
    assert installed_ver is not None, f"{pkgbase}: no installed pkgname in {pkgnames}"  # noqa: S101 — internal invariant (live-install-set guarantees a hit), not input validation

    # VCS packages under --devel: static pkgver is just the seed; the real
    # version comes from running pkgver() against the fetched upstream
    # sources. The --devel-off case short-circuits at the top of this
    # function (no PKGBUILD parse, no source sync) — anything reaching this
    # branch is opt-in --devel work.
    if _is_vcs(pkgbase):
        # Cheap short-circuit: if the upstream HEAD still matches the SHA we
        # built last time (recorded in build_state.toml), skip the full
        # ``makepkg -od --nobuild`` resolve. peek_upstream_commit returns
        # None for multi-git-source / unparseable PKGBUILDs / network errors,
        # and we fall through to the canonical path in that case.
        if built_upstream_commit is not None:
            current_commit = peek_upstream_commit(pkgbuild_dir)
            if current_commit is not None and current_commit == built_upstream_commit:
                return _UpdateResult(
                    pkgbase=pkgbase, pkgnames=pkgnames, action="UP_TO_DATE",
                    installed_ver=installed_ver, pkgbuild_ver=installed_ver,
                    pkgbuild_path=pkgbuild_path, has_build_record=has_record,
                    source=source,
                )

        resolved = evaluate_vcs_pkgver(pkgbuild_dir)
        if resolved is None:
            _log.warn(
                f"{pkgbase}: pkgver() evaluation failed — skipping rebuild "
                "(re-run --devel after the upstream/network issue clears)"
            )
            return _UpdateResult(
                pkgbase=pkgbase, pkgnames=pkgnames, action="DEVEL_EVAL_FAILED",
                installed_ver=installed_ver, pkgbuild_ver=pkgbuild_ver,
                pkgbuild_path=pkgbuild_path, has_build_record=has_record,
                source=source,
            )

        try:
            cmp = vercmp(resolved, installed_ver)
        except RuntimeError as e:
            _log.warn(f"{pkgbase}: vercmp failed on resolved {resolved!r}: {e} — skipping")
            return _UpdateResult(
                pkgbase=pkgbase, pkgnames=pkgnames, action="DEVEL_EVAL_FAILED",
                installed_ver=installed_ver, pkgbuild_ver=resolved,
                pkgbuild_path=pkgbuild_path, has_build_record=has_record,
                source=source,
            )

        if cmp > 0:
            action = "NEEDS_REBUILD"
        elif cmp == 0:
            action = "UP_TO_DATE"
        else:
            action = "DOWNGRADE"
            _log.warn(f"{pkgbase}: resolved {resolved} is older than installed {installed_ver}")
        return _UpdateResult(
            pkgbase=pkgbase, pkgnames=pkgnames, action=action,
            installed_ver=installed_ver, pkgbuild_ver=resolved,
            pkgbuild_path=pkgbuild_path, has_build_record=has_record,
            source=source,
        )

    try:
        cmp = vercmp(pkgbuild_ver, installed_ver)
    except RuntimeError as e:
        _log.warn(f"{pkgbase}: version comparison failed: {e} — skipping")
        return None

    # Drift observability: when the installed version matches what sysforge
    # built last time but the on-disk PKGBUILD now describes a different
    # version, surface that upstream PKGBUILD has moved. The action is still
    # whatever vercmp says — this is purely informational (-v only).
    bs_pkgver = entry.get("pkgver")
    bs_pkgrel = entry.get("pkgrel")
    bs_epoch = entry.get("epoch", "0")
    if bs_pkgver is not None and bs_pkgrel is not None:
        bs_ver = f"{bs_epoch}:{bs_pkgver}-{bs_pkgrel}" if bs_epoch and bs_epoch != "0" \
            else f"{bs_pkgver}-{bs_pkgrel}"
        if installed_ver == bs_ver and pkgbuild_ver != bs_ver:
            _log.info(
                f"{pkgbase}: PKGBUILD on disk ({pkgbuild_ver}) differs from "
                f"last built ({bs_ver}) — upstream PKGBUILD has moved"
            )

    if cmp > 0:
        action = "NEEDS_REBUILD"
    elif cmp == 0:
        action = "UP_TO_DATE"
    else:
        action = "DOWNGRADE"
        _log.warn(f"{pkgbase}: PKGBUILD {pkgbuild_ver} is older than installed {installed_ver}")

    return _UpdateResult(
        pkgbase=pkgbase, pkgnames=pkgnames, action=action,
        installed_ver=installed_ver, pkgbuild_ver=pkgbuild_ver,
        pkgbuild_path=pkgbuild_path, has_build_record=has_record,
        source=source,
    )
