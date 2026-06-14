---
name: pkgbuild-spec-check
description: Cross-check PKGBUILD parsing/detection/patching changes against PKGBUILD(5). Use whenever editing sysforge/primitives/pkgbuild_meta.py, pkgbuild_patcher.py, vcs_pkgver.py, source_meta.py, aur.py, or any other code that reads/writes PKGBUILD fields. Catches the easy-to-miss spec details (array vs string fields, escape rules, split-package pkgbase semantics, pkgver()/_pkgver interaction, source/sha256sums alignment).
user-invocable: false
---

# pkgbuild-spec-check

CLAUDE.md mandates: **PKGBUILD parsing/detection/patching changes are cross-checked against `PKGBUILD(5)` before merging.** This skill is the checklist.

Trigger this whenever a change touches:

- `sysforge/primitives/pkgbuild_meta.py`
- `sysforge/primitives/pkgbuild_patcher.py`
- `sysforge/primitives/vcs_pkgver.py`
- `sysforge/primitives/source_meta.py`
- `sysforge/primitives/aur.py`
- Anywhere else that reads or rewrites PKGBUILD fields.

## What to do

1. **Look up the spec for every field touched.** The authoritative reference is local:

   ```bash
   man 5 PKGBUILD
   ```

   Use `man 5 PKGBUILD | sed -n '/^   FIELDNAME/,/^$/p'` to jump to a specific field. Don't rely on memory — `pkgver`/`_pkgver`, `pkgbase`/`pkgname`, `source`/`sha256sums` alignment, and the array-vs-string distinction all have edge cases.

2. **Walk the checklist below.** For every item that's relevant to the diff, verify the implementation matches the spec.

## Checklist

### Field shape

- [ ] **Array vs string.** `pkgname` is a string for single packages but an array for split packages. `depends`, `makedepends`, `optdepends`, `source`, `sha256sums`, `noextract`, `arch`, `license`, `groups`, `provides`, `conflicts`, `replaces`, `backup`, `validpgpkeys` are arrays. `pkgver`, `pkgrel`, `epoch`, `pkgbase`, `pkgdesc`, `url`, `install`, `changelog` are strings. Treat shape mismatches as parser bugs.
- [ ] **Architecture-specific overrides.** `depends_x86_64`, `source_x86_64`, etc. — make sure parser handles these as separate keys, not as part of the base array.
- [ ] **Quoted vs unquoted.** Bash arrays may use single quotes, double quotes, or no quotes. Escape sequences (`\$`, `\"`, `\\`) must round-trip through the patcher.

### Split packages

- [ ] **`pkgbase` semantics.** For split packages, `pkgbase` is the build identity; `pkgname` is the array of subpackages produced. Match-rule logic (CLAUDE.md gotcha #2) must check `pkgbase` **and** every entry in `pkgname` — don't regress this.
- [ ] **Per-subpackage `package_*()` functions.** A split PKGBUILD has `package_foo()` / `package_bar()` instead of (or in addition to) `package()`. If the patcher inserts code into `package()`, decide whether per-subpackage variants need the same treatment.

### VCS / `pkgver()` interaction

- [ ] **`pkgver()` overrides the static `pkgver`.** For `-git` / `-svn` / `-hg` packages, the dynamic `pkgver()` runs at build time and rewrites `pkgver`. Don't trust the static field for VCS sources.
- [ ] **`_pkgver` is convention, not spec.** Many AUR PKGBUILDs use a `_pkgver` shell variable as a single source of truth. Rewriting `pkgver=` without updating `_pkgver` (or vice versa) is a common drift bug.
- [ ] **`--devel` short-circuit.** The ls-remote shortcut (per `project_future_devel_lsremote.md` memory) skips the full clone when upstream commit hash is unchanged — make sure parser changes don't break the commit-hash path stored in `build_state.toml`.

### Sources, integrity, and signing

- [ ] **`source` ↔ `sha256sums` (or `sha512sums` / `b2sums` / `md5sums`) alignment.** Indices must line up element-for-element. `'SKIP'` is allowed for VCS sources. Detect mismatched lengths as a hard error.
- [ ] **Renamed sources.** `source=('myname::https://...')` — the part before `::` is the destination filename. Affects how the source list maps to downloaded files.
- [ ] **Local sources.** A bare filename in `source=()` refers to a file alongside the PKGBUILD. Patchers that add/remove sources must keep these intact.
- [ ] **`validpgpkeys` and signature files.** `.sig` / `.asc` entries imply PGP verification; check `validpgpkeys` is preserved.

### Build environment

- [ ] **`options=()` array.** `!strip`, `!debug`, `!lto`, `ccache`, `staticlibs`, `zipman`, etc. — patchers that toggle options must use the `!` prefix correctly (it disables a default).
- [ ] **`prepare()`, `build()`, `check()`, `package()` ordering.** Don't reorder; `prepare()` runs once before any per-subpackage `package_*()`.

### Patcher hygiene

- [ ] **Idempotency.** Running the patcher twice on the same PKGBUILD must produce the same result. No duplicate inserts, no double-quoted strings.
- [ ] **Comment preservation.** Comments above fields are often load-bearing (maintainer notes, AUR rules). Don't strip them.
- [ ] **Trailing newline.** Bash sources require a trailing newline; verify the patcher always emits one.

### Sysforge-specific gotchas

- [ ] **Source sync goes through the scheduler** (CLAUDE.md gotcha #3). Any new code path that needs a fresh PKGBUILD calls `sysforge.primitives.source_sync.get_scheduler().request(SyncRequest(...))` — never `git pull` directly.
- [ ] **`build_state.toml` invariants.** If you touch what's parsed from PKGBUILD, check whether the cached `build_state.toml` schema needs to evolve too.
- [ ] **Tests.** `tests/test_parser.py`, `tests/test_patcher.py`, `tests/test_pkgbuild_meta.py` (if present), and `tests/test_aur.py` are the regression nets — add or update cases that exercise the spec detail you just changed.

## Reporting

When the user finishes a change covered by this skill, summarize:

1. Which fields/sections of the diff were spec-relevant.
2. Which checklist items were verified (and how — `man 5 PKGBUILD` quote, test added, etc.).
3. Any remaining drift the user should resolve before merging.
