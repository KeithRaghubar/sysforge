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
                          "{pkgbase}: installed {installed_ver} > pkgbuild {pkgbuild_ver} (skipped){star}"),
    "PULL_FAILED":       ("PULL_FAILED",       "pull failed",
                          "{pkgbase}: git pull failed (skipped){star}"),
    "RATE_LIMITED":      ("RATE_LIMITED",      "rate-limited",
                          "{pkgbase}: AUR rate-limited (skipped, retry later){star}"),
    "PURGE_REFUSED":     ("PURGE_REFUSED",     "purge refused",
                          "{pkgbase}: --cleansrc refused (local work present, skipped){star}"),
    "SKIPPED_NO_CHECKUPDATES": ("NO_CHECKUPDATES", "skipped (no checkupdates)",
                          "{pkgbase}: checkupdates unavailable, install pacman-contrib{star}"),
}

# Actions that are always printed per-package regardless of verbosity.
# Everything else only appears under -v / verbose mode.
_ALWAYS_VERBOSE_ACTIONS = frozenset({
    "NEEDS_REBUILD", "NEEDS_PACMAN_UPGRADE", "DOWNGRADE",
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
        print(f"  [{tag}]{' ' * max(1, 17 - len(tag) - 2)}{line}")

    if not verbose and any(r.action not in _ALWAYS_VERBOSE_ACTIONS for r in results):
        print("  (run with -v to list each skipped/up-to-date package)")
    if no_record_count:
        print("\n  * = no build record")
    print()
