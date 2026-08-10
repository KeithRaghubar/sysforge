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

- **`2.6.1-F19` — The editor persistence prompt warns when `sysforge.toml [ui] editor`
  will shadow what it is about to write.** `[ui] editor` is rung 2 of the editor
  resolution chain and `$EDITOR` is rung 3, so persisting to `/etc/environment` or
  `~/.zshenv` *without* also selecting target 1 hands the user a system-wide `EDITOR`
  that sysforge itself ignores. It is reachable in two runs — pick target 1 first, then a
  different editor and target 2 — and was previously silent. The prompt now names both
  values and says which one will actually launch before writing, rather than leaving the
  chain display to contradict the write that just happened.

---

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

- **`2.6.1-F20` — The editor resolution chain display is complete.** Three coupled gaps
  closed. Each source in a rung's sub-listing now shows the value it contributes, so
  `~/.zshenv` setting `vim` and `/etc/environment` setting `nano` are told apart instead of
  appearing as two paths with no indication which produced the `$EDITOR` above them. The
  sub-listing now hangs off `$VISUAL` as well as `$EDITOR`, since the persistence step
  writes both. And a single environment snapshot is collected once per render and threaded
  into every lookup — `sources_defining()` collects its own when passed none, which reads
  ~14 init files and spawns a `systemctl` probe, so adding the second sub-listing would
  otherwise have doubled that cost. Renderer coverage was extended to the two boundaries
  the original change left untested: no rung resolving at all, and a single-element source
  list (the `from`/`also` prefix boundary).

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

- **`2.6.1-B11` — `format_assignment` could emit env-file syntax `env_chain` cannot read
  back.** Values failing the safe-characters pattern fell back to `shlex.quote`, which
  renders an embedded single quote as `'a'"'"'b'` — a form neither sysforge's own reader
  nor pam_env parses — and passed a newline through literally, which in `/etc/environment`
  becomes a second, bogus assignment line. This was unreachable only because the sole
  caller pre-filters every value through `shutil.which`; that is safety by caller
  discipline, in a primitive whose write target PAM parses at login. The primitive now
  rejects at plan time — before any file is touched — every value with no encoding all
  three readers agree on: quotes, newlines, carriage returns, NULs, surrounding whitespace
  and the empty string. Values needing only ordinary quoting, such as `code -w`, are
  unaffected, and a round-trip matrix now pins that anything the writer accepts survives
  being read back on both targets.

---

- **`2.6.1-B12` — `plan_write` missed the `KEY=value; export KEY` assignment form.** The
  per-syntax patterns matched the bare and `export` forms but not the split form, which
  sysforge's reader *does* parse — and parses ahead of its bare-file fallback, so on both
  `~/.zshenv` and `/etc/environment`. A file written that way made the persistence prompt
  report `EDITOR currently unset → nvim` directly beneath a chain display showing the real
  old value, and appended a new line rather than replacing the old assignment. The planner
  now recognises all three forms in the reader's own precedence order, so its `current`
  reading and the reader's agree by construction.

---

- **`2.6.1-B13` — `describe_editor_chain` hardcoded the detected rung's index.** The
  last-resort rung was built with `index=5` and claimed the winner as `4`, both literals
  matching the current length of the rung list above them. Adding or removing a rung would
  have mis-numbered the whole display and pointed the winner at the wrong row — and since
  `resolve_editor()` is a reader over this function, a wrong winner index is a wrong
  editor, not merely a wrong label. Both are now derived from the list.

---

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
