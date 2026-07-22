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

- `sysforge --help` now groups the top-level `COMMAND` list into usage tiers —
  **Everyday** (`build`, `update`, `fetch`, `search`), **Inspect** (`doctor`,
  `resolve`, `env`, `log`, `state`, `artifact`), and **Maintain** (`setup`,
  `config`, `packages`, `run`, `revert-to-stock`, `uninstall`) — instead of one
  flat, registration-ordered block, so routine drivers are easy to tell apart
  from ad-hoc introspection. Presentation only, no behavioural change; the
  man-page COMMANDS section and both shell completions follow the same order.
  (2.5.0-F1)
