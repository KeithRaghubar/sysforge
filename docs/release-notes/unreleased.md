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

### Changed

- Bootstrap disk + base install + system identity now run through archinstall
  (generated from `bootstrap.toml` via its headless JSON config + `--silent`),
  replacing the hand-rolled partition/base-install/identity stages. The
  `configure` stage now applies only sysforge-specific tuning. (`2.0.1-F3`)

### Fixed

- Build-failure recovery compiler swaps now persist for single-package
  PKGBUILDs. After a recovery-menu compiler swap on a package without an
  explicit `pkgbase=` line (the common non-split case), the resulting override
  was silently dropped: `_run_build` keyed the persist call on the raw,
  absent `pkgbase` global, so it no-op'd on the empty-key guard and
  `[package_compiler_overrides]` in `profiles.toml` stayed empty — leaving the
  next `update` to re-trigger the same failure. The persist now uses the same
  pkgbase-or-pkgname key the recovery menu labels with and `resolve_profile`
  reads the override back with, so a known-good swap self-heals subsequent
  builds. (2.1.0-B2)

- Toolchain stage no longer refuses on a spurious "dirty (no upstream tracking
  branch)" LLVM blocker for release-tag checkouts. A `source=repo` LLVM tree
  pinned to a tag sits on a detached HEAD (no `@{u}`) whose commit is still
  reachable from `origin` — upstream's own history, not local work. The
  pre-flight report now applies the same `head_reachable_from_remote` test
  `purge_src` already uses, so such clean trees pass and no longer wedge the
  stage where no `--cleansrc-force` could ever clear the blocker. (2.1.0-B3)
