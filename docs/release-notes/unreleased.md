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

- Pager corruption on the `update --interactive` PKGBUILD-review path
  (`less -X` alt-screen suppression leaking through the interactive
  pager-suppression gap and mangling the following build subprocess's output).
  The shared paging seam (`primitives/pager.py`) now strips the alt-screen-hostile
  `-X`/`--no-init` flags from an inherited `$PAGER` `less` argv and sanitizes the
  `$LESS` environment variable for the pager subprocess, so the fix holds for every
  `maybe_pager` caller regardless of the caller's mode. (2.3.0-B1)
