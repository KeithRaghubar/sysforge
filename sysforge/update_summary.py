# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
update_summary.py — the ``sysforge update`` Phase 4 summary display.

Renders the per-package version-check results (``_UpdateResult``) into the
totals header and per-package lines. Pure presentation: it only formats and
prints — no logging, no state, no external dependencies beyond the result type.
``update.py`` re-imports ``_print_summary`` for the orchestrator and the test
surface.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from sysforge.primitives import render
from sysforge.update_result import _UpdateResult

# (tag, count_label, line_template) per action. line_template is formatted
# with the _UpdateResult fields plus a trailing {star} ("" or " *"). Order
# here is the order each action appears in summary header + per-package
# section.
_ACTION_FORMATS: dict[str, tuple[str, str, str]] = {
    "NEEDS_REBUILD":     ("NEEDS_REBUILD",     "need rebuild",
                          "{pkgbase}: {installed_ver} → {pkgbuild_ver}{star}"),
    "NEEDS_PACMAN_UPGRADE": ("NEEDS_PACMAN", "need pacman upgrade",
                          "{pkgbase}: {installed_ver} → {pkgbuild_ver} (pacman -Syu){star}"),
    "UP_TO_DATE":        ("UP_TO_DATE",        "up to date",
                          "{pkgbase}: {pkgbuild_ver}{star}"),
    "DEVEL":             ("DEVEL",             "devel",
                          "{pkgbase}: skipped (use --devel to rebuild){star}"),
    "DEVEL_EVAL_FAILED": ("DEVEL_EVAL_FAILED", "devel-eval-failed",
                          "{pkgbase}: pkgver() resolution failed (skipped){star}"),
    "DOWNGRADE":         ("DOWNGRADE",         "downgrade",
                          "{pkgbase}: installed {installed_ver} > pkgbuild "
                          "{pkgbuild_ver} (skipped){star}"),
    "PULL_FAILED":       ("PULL_FAILED",       "pull failed",
                          "{pkgbase}: git pull failed (skipped){star}"),
    "RATE_LIMITED":      ("RATE_LIMITED",      "rate-limited",
                          "{pkgbase}: AUR rate-limited (skipped, retry later){star}"),
    "PURGE_REFUSED":     ("PURGE_REFUSED",     "purge refused",
                          "{pkgbase}: --cleansrc refused (local work present, skipped){star}"),
    "FROZEN":            ("FROZEN",            "source freeze",
                          "{pkgbase}: source freeze denied fetch (use --thaw){star}"),
    "SKIPPED_NO_CHECKUPDATES": ("NO_CHECKUPDATES", "skipped (no checkupdates)",
                          "{pkgbase}: checkupdates unavailable, install pacman-contrib{star}"),
}

# Actions that are always printed per-package regardless of verbosity.
# Everything else only appears under -v / verbose mode.
_ALWAYS_VERBOSE_ACTIONS = frozenset({
    "NEEDS_REBUILD", "NEEDS_PACMAN_UPGRADE", "DOWNGRADE", "FROZEN",
})


def _print_summary(results: list[_UpdateResult], args) -> None:
    if not results:
        print("[SYSFORGE] No packages to check.")
        return

    verbose = bool(getattr(args, "verbose", 0))

    # Totals header
    counts: dict[str, int] = {}
    no_record_count = 0
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
        if not r.has_build_record:
            no_record_count += 1

    parts = [f"{len(results)} packages"]
    for action, (_tag, label, _tmpl) in _ACTION_FORMATS.items():
        n = counts.get(action, 0)
        if n:
            parts.append(f"{n} {label}")
    if no_record_count:
        parts.append(f"{no_record_count} no build record")

    print(f"\n  Checking {', '.join(parts)}")
    print()

    for r in results:
        if not verbose and r.action not in _ALWAYS_VERBOSE_ACTIONS:
            continue
        fmt = _ACTION_FORMATS.get(r.action)
        if fmt is None:
            continue
        tag, _label, tmpl = fmt
        star = " *" if not r.has_build_record else ""
        line = tmpl.format(
            pkgbase=r.pkgbase,
            installed_ver=r.installed_ver,
            pkgbuild_ver=r.pkgbuild_ver,
            star=star,
        )
        print(f"{render.tag_header(tag)}{line}")

    if not verbose and any(r.action not in _ALWAYS_VERBOSE_ACTIONS for r in results):
        print("  (run with -v to list each skipped/up-to-date package)")
    if no_record_count:
        print("\n  * = no build record")
    print()


# Owner-stage → advisory verb hint. kernel → "run kernel", toolchain →
# "run toolchain". Falls back to "run <stage>" for any future stage.
def _stage_verb(owner_stage: str) -> str:
    return f"run {owner_stage}"


def _arrow() -> str:
    """Back-compat alias for :func:`sysforge.primitives.render.arrow` (2.6.1-F9).

    The glyph gate now lives in the shared renderer alongside the version-pair
    and gutter helpers; this name is kept because it is part of this module's
    established test surface.
    """
    return render.arrow()


@dataclass
class ResultSummary:
    built_pkgs: list[str] = field(default_factory=list)
    failed_pkgs: list[str] = field(default_factory=list)
    pacman_upgrade_pkgs: list[str] = field(default_factory=list)
    installed_deps: list[str] = field(default_factory=list)
    pgo_skipped_pkgs: list[str] = field(default_factory=list)
    cleansrc_failures: list[str] = field(default_factory=list)
    install_only: bool = False
    pacman_upgrade_failed: bool = False
    # 3.0.0-F4: the trailing `pacman -Syu` ran as a requested system upgrade
    # rather than off a classified pacman-class package list, so there are no
    # per-package lines to render — only the fact that the transaction ran.
    system_upgrade_ran: bool = False
    skipped: int = 0
    # pkgbase -> (installed_ver, pkgbuild_ver)
    versions: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    # (pkgbase, installed_ver, upstream_ver, owner_stage)
    stage_owned_updates: list[tuple[str, str | None, str | None, str]] = field(
        default_factory=list
    )


def _fmt_pkg(summary: ResultSummary, pkgbase: str) -> str:
    """`pkgbase: old → new` when a version pair is known, else bare name."""
    pair = summary.versions.get(pkgbase)
    if pair is None:
        return pkgbase
    installed_ver, pkgbuild_ver = pair
    if installed_ver is None or pkgbuild_ver is None:
        return pkgbase
    # equal_marker=False: a built package reports what it was rebuilt to, so an
    # unchanged version still reads as a transition rather than "(=)".
    return f"{pkgbase}: {render.version_pair(installed_ver, pkgbuild_ver, equal_marker=False)}"


def _print_result_summary(
    summary: ResultSummary,
    *,
    emit: "Callable[[str], None]" = print,
) -> None:
    """Render the end-of-run summary line-by-line through ``emit``.

    ``emit`` defaults to :func:`print` (stdout only), keeping the renderer pure
    and its tests stdout-based. ``update.py`` passes ``log.ui`` so the summary
    is mirrored into the unified log the same way the old inline block was.
    """
    built_label = "installed" if summary.install_only else "built"
    header = (
        f"\n[SYSFORGE] Update complete: "
        f"{len(summary.built_pkgs)} {built_label}, "
        f"{len(summary.failed_pkgs)} failed, {summary.skipped} skipped"
        + (f", {len(summary.pgo_skipped_pkgs)} pgo-skipped"
           if summary.pgo_skipped_pkgs else "")
        + (f", {len(summary.pacman_upgrade_pkgs)} pacman-upgraded"
           if summary.pacman_upgrade_pkgs
           else (", system upgraded" if summary.system_upgrade_ran else ""))
        + (" (pacman -Syu FAILED)" if summary.pacman_upgrade_failed else "")
        + "."
    )
    emit(header)

    def _section(label: str, lines: list[str]) -> None:
        if not lines:
            return
        emit(f"  {label}")
        for line in lines:
            emit(f"    {line}")

    if summary.built_pkgs:
        label = "Installed:" if summary.install_only else "Built:"
        _section(label, [_fmt_pkg(summary, pb) for pb in summary.built_pkgs])

    if summary.installed_deps:
        _section("Dependencies:", list(summary.installed_deps))

    if summary.pacman_upgrade_pkgs:
        label = ("Pacman-Syu (transaction FAILED):"
                 if summary.pacman_upgrade_failed else "Pacman-Syu:")
        _section(label, [_fmt_pkg(summary, pb) for pb in summary.pacman_upgrade_pkgs])
    elif summary.system_upgrade_ran:
        # 3.0.0-F4: flag/config-triggered `pacman -Syu` — pacman resolved the
        # transaction, so the only fact this renderer owns is that it ran.
        label = ("Pacman-Syu (transaction FAILED):"
                 if summary.pacman_upgrade_failed else "Pacman-Syu:")
        _section(label, ["system upgrade (pacman resolved the transaction)"])

    if summary.failed_pkgs:
        _section("Failed:", [_fmt_pkg(summary, pb) for pb in summary.failed_pkgs])

    if summary.cleansrc_failures:
        emit(
            f"  --cleansrc refused {len(summary.cleansrc_failures)} package(s) "
            "with local work; commit/push or resolve manually before retrying."
        )

    if summary.pgo_skipped_pkgs:
        _section(
            "PGO-skipped:",
            [f"{' '.join(summary.pgo_skipped_pkgs)} "
             "(run 'sysforge run toolchain' to rebuild profdata)"],
        )

    if summary.stage_owned_updates:
        emit("  Stage-owned updates available:")
        for pkgbase, installed_ver, upstream_ver, owner_stage in summary.stage_owned_updates:
            if installed_ver and upstream_ver:
                ver = render.version_pair(
                    installed_ver, upstream_ver, equal_marker=False,
                )
            else:
                ver = upstream_ver or ""
            emit(f"    {pkgbase}   {ver}   {_stage_verb(owner_stage)}")
    emit("")
