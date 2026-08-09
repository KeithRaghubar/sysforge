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

## Added

- **`3.0.0-F2` — Frozen sources: a hard gate on AUR/VCS downloads.** New
  `[security] freeze_sources` config key and global `--frozen` / `--no-frozen` /
  `--thaw PKG` flags refuse all new source ingress at five seams: AUR clones,
  `pkgctl repo clone` checkouts, source-sync fetches, and both VCS pkgver seams. Existing checkouts still build. A refused
  package is a per-package blocker (`STATUS_FROZEN`) — the run continues and
  exits non-zero with the blocked set named. Ships off; lifts are run-scoped
  only. makepkg's own `source=()` downloads cannot be mediated and are warned
  about instead.

## Changed

- **`2.5.1-F4` — Abandoned roadmap entries moved to their own file.** `ROADMAP.md` now
  carries forward-looking work only; the `## Abandoned / decided against` section — the
  record of what was considered and rejected, with each entry's rationale and reopen
  condition — lives in `docs/ROADMAP-ABANDONED.md`. The two files remain **one ID
  namespace**: an abandoned ID is never reissued, so `make next-id` reads both alongside
  `docs/release-notes/`, and `make check-standards` still rejects an ID listed as both
  Planned and Abandoned — a cross-file check now rather than a state machine over one
  file's headings. Two new guards keep the split honest: the `roadmap_ids` group fails if
  an `## Abandoned` heading reappears in `ROADMAP.md`, and the extractor keeps honouring
  that heading anyway, so a misfiled section is reported without its IDs ever falling back
  into the allocation pool.

---

- **`3.0.0-STD1` — Shipped configs distinguish prose comments from activatable settings.**
  Every file in `etc/sysforge/` now follows one convention, declared in its own header:
  explanatory prose is `# text` (hash plus a space), while a setting you can turn on is
  `#key = value` flush against the hash, so activating it is a one-character edit. Section
  headers are no longer commented out — only their keys are — which removes the three-line
  dance of uncommenting a header, a blank line, and then the setting; `[build]`, `[desktop]`,
  `[makepkg]`, `[kconfig]`, `[packages]`, `[llvm]` and `[failure_handling]` all ship live and
  empty, which every reader treats as identical to absent. The exceptions are blocks that
  declare a named entity rather than a settings table (`[[package]]`, `[group.*]`, `[[rules]]`):
  those stay fully commented, since a header without its keys is an incomplete entry.
  Each file also gained an `END OF HEADER` banner and a pointer near the top citing its line
  number, so it is obvious where documentation stops and configuration starts — the
  `config_comments` group in `tools/check_shipped.py` verifies the banner exists exactly once
  and that the cited line still matches, so the number cannot go stale. `packages.toml`'s field
  reference also had its `enable_build_from_source` row realigned to the column the other rows use.

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
