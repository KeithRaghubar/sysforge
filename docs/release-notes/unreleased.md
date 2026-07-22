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

## Fixed

- Toolchain Gate-3 no longer false-rolls-back on a pkgrel-only LLVM suite skew:
  an independent `llvm` rebuild (e.g. `-2`) beside `clang`/`lld`/… at `-1` is a
  legitimate state, not an interrupted install. `detect_suite_skew` now enforces
  pkgver lockstep across the suite plus full `pkgver-pkgrel` lockstep only within
  the shared-pkgbase pair `llvm`/`llvm-libs`. (2.5.1-B1)
