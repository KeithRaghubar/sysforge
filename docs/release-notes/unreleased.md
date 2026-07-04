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

- `update`: the end-of-run summary now reports build dependencies installed as a
  prerequisite (via `prepare_deps`) as their own `Dependencies:` category,
  separate from built/failed/pacman (1.2.0-F38).
- `update`: when a stage-owned (kernel/toolchain) package has a newer upstream
  version available, the summary advises running the owning pipeline stage
  (e.g. `run kernel`) instead of silently skipping it. Detection only — never a
  rebuild from `update` — and it respects `--offline` like every other package
  (1.2.0-F32).
- Configurable default verbosity: a new `sysforge.toml [log] verbosity` key (0–3)
  sets the baseline stderr level when no flag is passed, and a global `--quiet`
  flag forces silence (verbosity 0) for a run. CLI flags always win over the
  config; the shipped default stays 0 (errors + primary output). Paired with a
  re-levelling of the day-to-day `build`/`update` path so per-package progress
  narration ("Loaded…", "Building…", "Installing from repo…", "done") lands at
  `-vv` (info) rather than the always-printed path (1.2.0-F36).
- `update` honors `[update] rebuild_on_drift` (+ per-axis) config defaults; CLI flags still win. (1.2.0-F30)
- `build` honors `[build] abi_check` / `cache_report` / `persist_log` config defaults; CLI flags still win. (2.0.1-F1)

## Changed

- `update`: the end-of-run summary is consolidated into a single renderer with
  grouped, aligned sections, and built/failed packages now show their
  `old → new` versions instead of bare names (2.0.1-F2, 1.2.0-F33).

## Fixed

- Release script: the `.SRCINFO-git` generation tmpdir was missing `sysforge.install`,
  so `makepkg --printsrcinfo` aborted after the v2.0.1 chroot build passed.
- Release script: the Phase-1 release commit omitted `docs/design/00-header.md`
  from `git add`, leaving the stamped version marker uncommitted and blocking
  `--resume` on a dirty tree.
- `update`: the version check for a coexist-renamed AUR package (installed as
  `<pkg>-sysforge`) whose `PKGBUILD` uses bash `pkgver` expansion now falls back
  to the RPC version cached under its stock upstream base via `origin_pkgbase`,
  instead of missing the rescue and skipping the package (2.0.1-Q2).
