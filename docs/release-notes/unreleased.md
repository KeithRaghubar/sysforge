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

### Fixed

- Toolchain stage no longer refuses on a spurious "dirty (no upstream tracking
  branch)" LLVM blocker for release-tag checkouts. A `source=repo` LLVM tree
  pinned to a tag sits on a detached HEAD (no `@{u}`) whose commit is still
  reachable from `origin` — upstream's own history, not local work. The
  pre-flight report now applies the same `head_reachable_from_remote` test
  `purge_src` already uses, so such clean trees pass and no longer wedge the
  stage where no `--cleansrc-force` could ever clear the blocker. (2.1.0-B3)
