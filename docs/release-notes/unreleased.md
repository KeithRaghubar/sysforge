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

- `doctor`: new `storage` axis — build-dir free-space and `/etc/fstab` integrity
  checks (`--storage`); `shutil.disk_usage` now has a single home
  (`storage_probe.probe_free_space`), shared with the reconfigure disk step
  (1.2.0-F17, 1.2.0-F18).
- `doctor` `pacman` axis: package-cache size and mirrorlist-freshness warnings,
  thresholds via the new `[doctor]` config section (1.2.0-F15, 1.2.0-F16).
- `doctor` `state` axis: flags a live record-stage PGO build (bare `.profraw`,
  no merged `.profdata`) and points at `--pgo=use` or a repo rollback
  (1.2.0-F14).
- `doctor` `services` axis: broadened the current-boot journal scan beyond
  firmware to surface failed-start / core-dump / filesystem / OOM errors
  (1.2.0-F19).

## Fixed

- `doctor` progress indicator no longer sits on "starting…" for the whole run —
  it now advances per axis and per audited package (2.1.0-B11).
