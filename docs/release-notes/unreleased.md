# sysforge (unreleased)

<!--
Running accumulator for the next release. Every landing commit that COMPLETES a
ROADMAP item appends its entry here (in the same commit that drops the item from
ROADMAP.md), under the matching Keep a Changelog section — one of Added, Changed,
Deprecated, Removed, Fixed, Security, in that order. An entry leads with its
roadmap ID, in the same shape ROADMAP.md entries use:

    - **`1.2.0-F35` — <title sentence>.** <body>

    ---

    - **`1.2.0-F36` — …

Entries are separated by a `---` rule and kept in ascending ID order within each
section. Flag breaking changes with a **Breaking:** prefix opening the body, plus
the migration path. At release time tools/release.sh (Phase 1) renames this file
to vX.Y.Z.md, stamps the `# ` title with the version and date, and reseeds a fresh
accumulator. Run the release-notes skill first to reconcile/lint the entries and
finalize the one-line summary below (drop this comment). Keep a Changelog:
https://keepachangelog.com/en/1.1.0/
-->

## Fixed

- **`3.1.0-B1` — Record the version the build actually produced, not the oldest artifact in PKGDEST.** Build-state recording reverse-engineers `pkgver`/`pkgrel`/`epoch` from built `.pkg.tar.*` filenames, but took the *first* match a directory glob yielded. When `PKGDEST` is a shared, long-lived package archive holding every historical build (the common `/home/packages` layout), that first match is an arbitrary — in practice the oldest — version, so `build_state.toml` recorded a years-stale `pkgver` as "last built". The next run's `vercmp` then saw the on-disk PKGBUILD as newer, reported "upstream PKGBUILD has moved", and rebuilt the package again, indefinitely. Artifact selection now goes through `select_built_version()`, which considers only filenames parsing as the target pkgname and picks the most recently modified — the one the build that just finished produced. A local state file showed 69 affected packages.

---

- **`3.1.0-B2` — Stop advertising a drift-rebuild flag in the same run that already acted on it.** The toolchain- and flag-drift advisories printed "Pass `--rebuild-on-toolchain-drift`/`--rebuild-on-flag-drift` to rebuild" unconditionally whenever drift was detected, including when that rebuild was already enabled via the flag, the per-axis `[update]` key, or the `rebuild_on_drift` umbrella. A run would emit the "pass this flag" hint immediately above "promoted 26 UP_TO_DATE package(s) to NEEDS_REBUILD", making legitimate same-version rebuilds read as spurious reinstalls. The drift axes are now resolved before the advisories, which report `rebuilding (--rebuild-on-flag-drift)` when the rebuild is already in effect and keep the opt-in hint only when it is not.

---

- **`3.1.0-B3` — Stop pinning repo checkouts to a superseded release when the local sync DB is stale.** `SourceSyncScheduler._pin_repo_checkout` resolved the release tag to check out via `pacman.get_pacman_sync_version`, which reads `/var/lib/pacman/sync/*.db` through pyalpm or `pacman -Si` — neither touches the network. On a rolling repo that means the "available" version is whatever the last `pacman -Sy` left on disk, so a sync DB even a few hours old pins an already-superseded release; the kernel stage reported 7.1.8 as the newest available while the repos carried 7.1.9. The pin now resolves through a new `pacman.get_repo_candidate_version`, which cross-checks the sync DB against `checkupdates_map()` — `checkupdates` refreshes a *side copy* of the sync DBs, so it needs no sudo and cannot leave a partial-upgrade state — and takes whichever version is newer by `vercmp`. It is a strict enrichment: an absent `checkupdates` entry, an unavailable tool, or an older value all fall back to the previous sync-DB answer, and the probe is memoized for the process so one run covers every pinned package. Applies to all `source = "repo"` pins, not just the kernel.
