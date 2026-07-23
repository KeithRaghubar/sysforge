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

## Changed

- Toolchain LLVM source pre-flight now annotates split-off members (e.g.
  `llvm-libs`, built from the `llvm` PKGBUILD) as `(split of llvm)` instead of
  showing empty `origin=missing`/`sync=missing` source columns for a tree they
  don't own. The pre-flight guards source trees (pkgbases), so a split binary's
  per-member row was pure noise. (2.5.1-B2)

## Fixed

- Toolchain Gate-3 no longer false-rolls-back on a pkgrel-only LLVM suite skew:
  an independent `llvm` rebuild (e.g. `-2`) beside `clang`/`lld`/… at `-1` is a
  legitimate state, not an interrupted install. `detect_suite_skew` now enforces
  pkgver lockstep across the suite plus full `pkgver-pkgrel` lockstep only within
  the shared-pkgbase pair `llvm`/`llvm-libs`. (2.5.1-B1)
- Pass-4 toolchain build-reuse cache no longer misses on a post-install sanity
  re-run. The input fingerprint folded in the *installed* versions of the LLVM
  suite (`llvm`/`llvm-libs`/…), but Pass 4 links the *staged* libLLVM
  (`--nodeps`), so installing the just-built suite moved those versions and
  invalidated every otherwise-identical package. Build-set members are now
  excluded from `makedep_versions` (the staged libLLVM is still pinned via the
  Merkle-chained `staged_dep_fps`); external build-deps still invalidate.
  Fingerprint schema bumped to v3. (2.5.1-B2)
- Kernel-stage install no longer sweeps in unrelated `linux-*` packages. When
  the build-time manifest was absent, install scoping fell back to pkgname
  matching, whose filename parser anchored on a bare `<pkgname>-` prefix — so a
  kernel with pkgname `linux` matched `linux-custom`, `linux-sysforge`,
  `linux-steam-integration`, etc., and a lenient `rsplit` let the bogus tail
  parse as a valid version. The parser now requires an exact
  `[epoch:]pkgver-pkgrel-arch` tail (three hyphen-delimited fields, per
  `PKGBUILD(5)`'s no-hyphens-in-pkgver rule), rejecting foreign prefix matches.
  (2.5.1-B3)
