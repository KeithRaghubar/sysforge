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

- **`3.0.0-B2` — zsh completion listing split names from descriptions on over-wide rows.** Option
  descriptions in `completions/_sysforge` could be long enough that the rendered
  `<match>  -- <description>` row exceeded the terminal width; past that point zsh abandons the
  inline two-column table and emits every description as its own list entry, so a listing such as
  `sysforge doctor system -` showed each axis with its description detached and offset. The usable
  budget is `COLUMNS - (longest option name in the block) - 4`, which is per-`_arguments`/`_describe`
  call — one over-wide row degrades the layout for the whole block, and adding a long flag name to a
  verb shrinks the budget for every description in it. All 80 over-budget descriptions across 14
  blocks are now shortened to fit an 80-column terminal, the tightest being `update` (48 characters,
  bounded by `--rebuild-on-toolchain-drift`). The bash completion is unaffected — it emits bare
  `compgen -W` word lists with no descriptions. A new `completion_widths` group in
  `tools/check_shipped.py` computes the per-call budget and fails on any description that exceeds
  it, so a long flag name or a wordy description can no longer reintroduce the fault unnoticed.
