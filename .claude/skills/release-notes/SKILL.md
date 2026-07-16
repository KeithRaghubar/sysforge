---
name: release-notes
description: Reconcile and lint the running release-notes accumulator (docs/release-notes/unreleased.md) before a release. Entries are authored per-task as items ship; tools/release.sh renames unreleased.md to vX.Y.Z.md at release and hard-fails if it has no authored sections, so run this before `make release-{major,minor,patch}`. Use whenever a release is about to be cut or the user asks for release notes.
---

# release-notes

**Reconciles and lints** the running accumulator
(`docs/release-notes/unreleased.md`) that `tools/release.sh` requires. Notes are
authored **incrementally** — each landing commit that completes a ROADMAP item
appends its entry to the accumulator in the same commit — so this skill no longer
reconstructs the whole file at release time. It curates what's already there.

At release, Phase 1 of `tools/release.sh` renames `unreleased.md` → `v$NEW.md`,
stamps the `# ` title with the version + ISO date, and reseeds a fresh
accumulator. The renamed file is committed in the `release: vX.Y.Z` commit and fed
to `gh release create --notes-file` in Phase 4. **This skill does not rename,
date-stamp, or commit** — the release script owns those steps.

## What to do when invoked

1. **Determine the target version.** Read the current version from
   `pyproject.toml` and compute the bump. **Suggest the bump kind from the
   accumulator's Keep a Changelog sections** (SemVer signals), then confirm with
   the user before proceeding:

   - any `**Breaking:**`-prefixed entry, or a `## Removed` section → **major**
   - a `## Added` section (and no breaking/removed) → **minor**
   - only `## Fixed` / `## Security` / `## Deprecated` → **patch**

   State the recommendation and the entries that drove it (e.g. "Recommend
   **minor**: two `Added` items, no breaking changes"). If the user already named
   a bump kind, honour it but flag any mismatch (e.g. they said "patch" but the
   notes contain a `Removed` section).

2. **Read the accumulator** (`docs/release-notes/unreleased.md`) — this holds the
   per-task entries appended as items shipped this cycle. Cross-check for gaps
   against the history if entries look missing (an item may have landed without its
   note):

   ```bash
   git describe --tags --abbrev=0          # last tag
   git log --oneline <last-tag>..HEAD
   ```

3. **Read the framing.** `docs/design/17-release-plan.md` describes how the release
   is positioned (flagship features, breaking changes, the v-series narrative). The
   notes must agree with it; prefer its wording for feature names.

4. **Reconcile + lint the accumulator in place**, following the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
   category vocabulary (the accumulator itself is the working model):

   - Keep the `# ` title line as-is — the release script rewrites it to
     `# sysforge vX.Y.Z — YYYY-MM-DD` at Phase 1. Don't hand-stamp the version/date.
   - **Drop the leading `<!-- … -->` instructional comment** and ensure a single
     curated one-line summary sits directly under the title (framing from
     `docs/design/17-release-plan.md`). The comment is inert boilerplate from the
     reseed; it must not ship in the published notes.
   - Body grouped under these `##` section headings only, in this order, omitting
     any that don't apply: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`,
     `Security`. (This vocabulary is enforced by `make check-standards`, which now
     lints `unreleased.md` too.)
   - Flag breaking changes inline with a **Breaking:** prefix under `Changed` or
     `Removed`, including the migration path.

   Reconcile, don't transcribe: merge duplicate/overlapping bullets, group related
   entries, fix mis-filed sections, add any entry a landing commit forgot, drop
   pure-noise lines, and never reference competing projects by name. Retain the
   inline roadmap IDs (e.g. `(1.2.0-F35)`).

5. **Do not rename, date-stamp, or commit.** Leave the curated content in
   `unreleased.md`; `tools/release.sh` Phase 1 renames + stamps + reseeds + commits.
   Confirm to the user the accumulator is clean and the release command can now run.
