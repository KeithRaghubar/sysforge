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

- **`3.4.0-F8` — version changes are coloured by how big the jump is.** Every `old → new` row looked the same whether it was an epoch change, a major release or a rebuild, so in a long `update --devel` summary the few rows worth a second look were lost among dozens of `r1234 → r1240` VCS ticks. On a colour terminal each pair is now tinted by the first part of the version that changed: red for an epoch change or a downgrade, yellow for a major version, cyan for a minor one, and dim for a pkgrel-only rebuild or a VCS tick (only the trailing `rN` / commit hash moved). Patch-level changes, and versions whose shape can't be read reliably (such as a `YYYYMMDD` date), stay uncoloured. This applies to the `update` summary (built packages, the system upgrade, and stage-owned updates), the change summary after each pipeline stage, the toolchain stage's list of version changes before it builds, and the LLVM and toolchain pre-flights. With colour off (`--color=never`, `NO_COLOR`, or output piped elsewhere) the output is unchanged, and colour never reaches the log files.

---

- **`3.4.0-F9` — `bench toolchain` shows what it is doing while it runs.** At default verbosity the verb printed nothing between launch and its report: the download of the stock `clang` and `llvm-libs` (about 105 MB when they aren't cached), the extraction, and every timed compile round (about 40 seconds before any download) looked like a hang. The progress line now names each step: `fetching stock clang <version>`, then the same for `llvm-libs`, `unpacking stock clang`, the warm-up, and `round 3/5` for each timed round. The report replaces it when the bench finishes, and so does the error message if a step fails. Piped or non-interactive output shows the same steps as `[PROGRESS]` lines. The `-vv` narration is unchanged, and so is the output with `--quiet`.

## Changed

- **`3.4.0-STD2` — the standards table no longer has two rows numbered 28.** The standards table in `docs/design/21-standards.md` numbers every row in one sequence shared by its two subsections, and code, tests and release notes cite rows by that number. Row 28 was used twice: by the `pam.d(5)` / `sysusers.d(5)` non-recursive directories row, and later by "Subprocess goes through `primitives/run`". The subprocess row took the number second, so it is now row 31 and notes its former number. The `pam.d(5)` row keeps 28, so the one published citation of row 28 (the v3.4.0 note for `3.2.0-F15`) still points at the right row. Every citation in code, tests and design docs now points at the right row. A new `make check-standards` group, `standards_rows`, rejects a repeated row number and checks that the table's "next row is N" counter is one past the highest row, since a stale counter is how the number got reused.

## Fixed

- **`3.4.0-B15` — `--no-update`'s help no longer says it skips `git pull --rebase`.** sysforge stopped running `git pull --rebase` when source sync moved to its scheduler, which fetches and fast-forwards each checkout. The help for `--no-update` still described the old command on all six verbs that take it (`build`, `fetch`, and `run pipeline`, `toolchain`, `packages` and `kernel`), and on the `run` verbs it said "before each build", which is wrong for the kernel stage's single build. Each now says it skips the source sync and builds what is checked out, and `build` and `fetch` note that `--cleansrc` still syncs. The man page and zsh completions match. The flag itself is unchanged.

---

- **`3.4.0-B16` — stage numbers in the README, `--help`, shipped configs and messages now match the pipeline.** The pipeline runs seven stages: install, hardware, configure, reconfigure, toolchain, packages and kernel. It used to have eight, with partitioning and the base install as separate stages, and the old numbers outlived the merge. `kernel.toml` called the kernel stage 8 and `toolchain.toml` called the toolchain stage 6. `bootstrap.toml` placed system identity, the mirrorlist, the desktop and the makepkg defaults in stage 4. The README said the post-reboot run covers stages 5–8, `run pipeline --help` (and its man page entry and zsh completion) said "stages 1–8", and the live-ISO stop said "Bootstrap complete (stages 1–4 done)". All now use the registry's numbers: kernel is 7, toolchain is 5, system identity is applied by archinstall in stage 1 and the other `bootstrap.toml` sections in stage 3, the post-reboot run is stages 4–7, and the ISO stop reports stages 1–3. A new test keeps every one of these, and each stage module's own header, in step with the stage list.

---

- **`3.5.0-B1` — colour no longer leaks into the run logs as raw escape codes.** Output sysforge prints as the answer to a command is also copied into the run log (`sysforge-<verb>.log`) and the per-package log, and that copy kept any colour codes. On a colour terminal, the end of a `build` run (the `Build complete` line and its `Built:`, `Not installed:`, `Failed:` and `Skipped:` rows) was written into the log as text like `^[[32mBuilt:^[[0m`, which reads as noise in a pager or editor and breaks a plain `grep` for the label. The log copy is now stripped of colour; the terminal keeps it.

---

- **`3.5.0-B2` — `update` no longer says it pulls with git.** sysforge's source sync fetches each checkout and fast-forwards it; it has not run `git pull` since sync moved to its scheduler. A package whose sync failed still showed in the `update` summary as `[PULL_FAILED] <pkg>: git pull failed (skipped)` and was counted as `pull failed`; it now reads `[SYNC_FAILED] <pkg>: source sync failed (skipped)` and counts as `sync failed`. The `update --offline` help and its zsh completion said it skips "git pulls" and now say it skips the source sync. When a build fails because related PKGBUILDs are at different versions, the hint told you to run `git pull --rebase` in each package directory, which bypasses sysforge's sync; it now points to `sysforge fetch <pkg>...`. The man page matches. Nothing changes in what any command does.
