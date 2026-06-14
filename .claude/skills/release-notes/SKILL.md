---
name: release-notes
description: Draft docs/release-notes/vX.Y.Z.md for the next release. tools/release.sh hard-fails in preflight if the notes file for the target version is missing, so run this before `make release-{major,minor,patch}`. Use whenever a release is about to be cut or the user asks for release notes.
---

# release-notes

Generates the curated release-notes file that `tools/release.sh` requires
(`docs/release-notes/v$NEW.md`). The file is committed by Phase 1 of the release
script and fed to `gh release create --notes-file` in Phase 4.

## What to do when invoked

1. **Determine the target version.** Read the current version from
   `pyproject.toml` and compute the bump. If the user hasn't said which bump kind
   (major/minor/patch) they intend, ask.

2. **Collect the changes:**

   ```bash
   git describe --tags --abbrev=0          # last tag
   git log --oneline <last-tag>..HEAD
   ```

3. **Read the framing.** `docs/design/17-release-plan.md` describes how the release
   is positioned (flagship features, breaking changes, the v-series narrative). The
   notes must agree with it; prefer its wording for feature names.

4. **Write `docs/release-notes/v<NEW>.md`** following the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
   category vocabulary (see `docs/release-notes/v2.0.0.md` as the model):

   - `# sysforge vX.Y.Z` heading + one-line summary (add a ` — YYYY-MM-DD` date
     suffix at release time, ISO 8601).
   - Body grouped under these `##` section headings only, in this order, omitting
     any that don't apply: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`,
     `Security`. (This vocabulary is enforced by `make check-standards`.)
   - Flag breaking changes inline with a **Breaking:** prefix under `Changed` or
     `Removed`, including the migration path.

   Curate, don't transcribe: group related commits into one bullet, drop pure-noise
   commits, and never reference competing projects by name.

5. **Do not commit.** Phase 1 of `tools/release.sh` stages and commits the file as
   part of the `release: vX.Y.Z` commit. Just confirm to the user the file exists
   and the release command can now run.
