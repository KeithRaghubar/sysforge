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

- **`3.0.0-F5` — Itemize the flag-triggered `pacman -Syu` in the update summary.** Phase 6.5 has two routes into the same transaction, and only the classified one could report what it did: a `--sysupgrade` / `[build] system_upgrade` run hands the whole transaction to pacman, so the summary knew only that it had happened and printed `system upgrade (pacman resolved the transaction)`. That was honest but strictly less useful than a hand-run `-Syu`, and it was the common case — the managed set rarely holds `build_mode = "pacman"` entries, so the classified list is usually empty precisely when the flag route fires. The block now itemizes `pkg: old → new` for the requested-upgrade route too, from a reporting-only capture that snapshots the local package DB either side of the transaction and diffs the pair (`pacman.diff_installed`). Reading the DB *back* is the point: it reports what pacman actually did rather than what a second resolver predicted, and it needs no `checkupdates` probe — which is what decoupling the flag route removed in the first place. Fresh installs render `(new) <ver>` and removals `<ver> (removed)`. Output is scaled by verbosity, because a stock `-Syu` can be hundreds of packages unlike the bounded managed list: the default view caps the block and closes with `... and N more (-v for the full list)`, `-v` and above print it in full. The capture is on by default, applies only to the requested-upgrade route, and is turned off with `--no-sysupgrade-report`; it is best-effort throughout, so a failed snapshot drops the report and never the run, falling back to the existing single line.
