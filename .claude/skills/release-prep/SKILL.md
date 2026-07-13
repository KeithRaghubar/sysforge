---
name: release-prep
description: Pre-flight checks before `make release-{major,minor,patch}`. Verifies branch/working-tree state, version-marker consistency across pyproject.toml/PKGBUILD/PKGBUILD-git/README.md/DESIGN.md, lint passes, completions/_sysforge has every CLI verb, and chroot infrastructure is ready. Use whenever you are about to cut a release.
disable-model-invocation: true
---

# release-prep

Run **before** `make release-major`, `make release-minor`, or `make release-patch`. Catches the drift cases that cause `tools/release.sh` to fail half-way through (e.g. PKGBUILD pkgver doesn't match pyproject.toml version, version markers out of sync, missing chroot, completions out of date with CLI surface).

## What to do when invoked

1. **Run the pre-flight script:**

   ```bash
   bash .claude/skills/release-prep/scripts/preflight.sh
   ```

2. **Report the script's output verbatim** to the user — its `[PASS]` / `[WARN]` / `[FAIL]` lines and final summary tell the whole story. Do not paraphrase or summarise the script output.

3. **Interpret the result:**
   - **Exit 0 with no `[WARN]`s** — release is green. Tell the user they can run `make release-{major,minor,patch}`.
   - **Exit 0 with `[WARN]`s** — release is *probably* OK. List each warning in plain language and ask the user whether to proceed. Common warnings (chroot missing, man page stale, completions missing a verb) are non-blocking but should usually be fixed first.
   - **Exit non-zero (any `[FAIL]`)** — release **must not** proceed until every fail is resolved. List each failure with the suggested fix from the table below.

## Common failures and fixes

| Failure | Fix |
|---|---|
| `not on main branch` | `git checkout main` |
| `working tree not clean` | Commit or stash; release.sh refuses dirty trees |
| `PKGBUILD pkgver mismatch` | Edit PKGBUILD `pkgver=` to match `pyproject.toml` `version =` (release.sh's sed assumes they already match) |
| `PKGBUILD-git pkgver mismatch` | Edit PKGBUILD-git's leading `X.Y.Z` portion of `pkgver=` to match pyproject.toml; preserve the `.r0.g…` suffix |
| `README.md version marker mismatch` | Edit the `<!--version-->vX.Y.Z<!--/version-->` token to match pyproject.toml |
| `DESIGN.md version marker mismatch` | Same — there are 3 markers in DESIGN.md; sync all three |
| `ruff check failed` | Run `make lint` and fix the reported issues, or `ruff check --fix sysforge/` for autofixable ones |
| `completions missing CLI verb '<verb>'` | Add `<verb>) _sysforge_<verb> ;;` to the case in `completions/_sysforge` and define `_sysforge_<verb>()` |

## Prerequisite: release notes

`tools/release.sh` preflight hard-fails if `docs/release-notes/v<NEW>.md` is missing
for the target version. If it doesn't exist yet, run the `/release-notes` skill to
draft it before invoking the release command.

## Coverage ratchet (opt-in)

The preflight skips the coverage ratchet by default — the instrumented suite is
slow. To include it, run with `RUN_COVERAGE=1`:

```bash
RUN_COVERAGE=1 bash .claude/skills/release-prep/scripts/preflight.sh
```

It runs `make coverage` and compares the TOTAL against the floor in
`tests/COVERAGE_BASELINE.md`. A drop below the floor is a `[WARN]` (advisory,
never a `[FAIL]`). If the drop is intended — or coverage improved — re-stamp the
floor before releasing and commit it:

```bash
make coverage-ratchet-update TESTS=$(make test 2>/dev/null | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+')
```

## What this skill does NOT check

- **Tests pass** — too slow to run automatically (1547 tests). Tell the user: "Run `make test` separately before releasing if you haven't already."
- **The bumped-to version isn't already tagged** — release.sh checks this itself, and it depends on the bump kind which this skill does not take as an argument.
- **uv.lock is stale** — release.sh regenerates it during Phase 1. We do verify uv.lock exists.
- **AUR push** — out of scope; release.sh prints push instructions in Phase 4.
