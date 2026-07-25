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

- `--cache-report` now routes its summary through the sysforge logger instead of
  raw `print()`, so the report is captured by the unified run-log (`sysforge log`
  no longer omits it) and its divider honours the Unicode gate — `NO_UNICODE` /
  `--ascii` runs no longer emit raw box-drawing glyphs (2.3.0-B2).
- `doctor --rust` no longer reports a shell-installed (non-pacman) rustup as the
  distro `rust` package. An unowned `cargo`/`rustc` inside a rustup tree
  (`RUSTUP_HOME`, `CARGO_HOME`, or `~/.cargo`) is now reported as a user-local
  rustup install with its active toolchain, and the non-stable-channel warning
  applies to it as well (2.5.1-B10).
