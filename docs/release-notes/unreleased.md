# sysforge (unreleased)

<!--
Running accumulator for the next release. Every landing commit that COMPLETES a
ROADMAP item appends its entry here (in the same commit that drops the item from
ROADMAP.md), under the matching Keep a Changelog section — one of Added, Changed,
Deprecated, Removed, Fixed, Security, in that order. Reference the roadmap ID
inline, e.g. (1.2.0-F35). Flag breaking changes with a **Breaking:** prefix and
the migration path. At release time tools/release.sh (Phase 1) renames this file
to vX.Y.Z.md, stamps the `# ` title with the version and date, and reseeds a fresh
accumulator. Run the release-notes skill first to reconcile/lint the entries and
finalize the one-line summary below (drop this comment). Keep a Changelog:
https://keepachangelog.com/en/1.1.0/
-->

## Added

- `sysforge doctor --cache` — a read-only compile-cache readiness axis reporting
  whether ccache/sccache are installed, their cache dir is writable, and a non-zero
  size cap is set, *before* a build relies on them. Absence is informational (not a
  failure); a misconfigured tool warns with remediation. Runs in the default/`--all`
  sweep. Distinct from build verbs' `--cache-report`, which measures per-package hit
  rates after a build. (2.2.0-F1)
- `[build] mem_limit` — an optional per-build memory ceiling (e.g. `"24G"`) so a
  runaway build (OOM-prone link, sanitizer, LTO) can't take down the workstation.
  Delivered by a dual mechanism: an `RLIMIT_AS` clamp on the makepkg child off the
  `cpu_quota` path, or a tighter cgroup `MemoryMax` on the `systemd-run --scope` path
  when `cpu_quota` is also set, arbitrated so the two never double-apply. Unset = no
  change. (2.2.0-F4)

## Fixed

- Pager corruption on the `update --interactive` PKGBUILD-review path
  (`less -X` alt-screen suppression leaking through the interactive
  pager-suppression gap and mangling the following build subprocess's output).
  The shared paging seam (`primitives/pager.py`) now strips the alt-screen-hostile
  `-X`/`--no-init` flags from an inherited `$PAGER` `less` argv and sanitizes the
  `$LESS` environment variable for the pager subprocess, so the fix holds for every
  `maybe_pager` caller regardless of the caller's mode. (2.3.0-B1)
