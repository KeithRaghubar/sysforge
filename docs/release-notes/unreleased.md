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

## Changed

- **`4.0.0-DEV1` — releases go through a release PR instead of a push to `main`.** The `main` ruleset requires a pull request, so the push the release script asked for was rejected. `tools/release.sh` now cuts each release on a `release/vX.Y.Z` branch and asks for `git push -u origin release/vX.Y.Z && git push origin vX.Y.Z`. Resuming requires that branch, and the script refuses to start if it already exists. The release checklist's last stage opens the release PR and merges it with a merge commit, never a squash: a squash rewrites the signed `release:` commit, leaving the `vX.Y.Z` tag outside `main`'s history, so `git describe` in the `-git` package keeps reporting the previous version.

---

- **`4.0.0-DEV2` — the release script publishes long release notes and resumes after a failed publish.** GitHub rejects a release body over 125,000 characters, and the 4.0.0 notes are longer, so `gh release create` failed. A notes file over the limit is now published as its summary plus a link to the full file at the tag. That failure also showed the resume was not idempotent: Phase 3 had already rewritten `PKGBUILD`'s `sha256sums`, and `make release-resume` then refused the dirty tree. A resume now accepts a `PKGBUILD` whose only change is that array, and still refuses any other edit.
