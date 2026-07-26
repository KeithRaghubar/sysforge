---
name: release-prep
description: Pre-flight checks before `make release-{major,minor,patch}`. Verifies branch/working-tree state, version-marker consistency across pyproject.toml/PKGBUILD/PKGBUILD-git/README.md/DESIGN.md, lint passes, completions/_sysforge has every CLI verb, chroot infrastructure is ready, and the VM post-install packaging smoke has been run against a current build. Use whenever you are about to cut a release.
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
| `shellcheck failed` | Run `make lint-sh` and fix the reported issues; a genuine exception takes an inline `# shellcheck disable=SC…` **with a justifying comment** (see `tools/vm/build-pkg.sh`) |
| `completions missing CLI verb '<verb>'` | Add `<verb>) _sysforge_<verb> ;;` to the case in `completions/_sysforge` and define `_sysforge_<verb>()` |

## Prerequisite: release notes

`tools/release.sh` preflight hard-fails if `docs/release-notes/v<NEW>.md` is missing
for the target version. If it doesn't exist yet, run the `/release-notes` skill to
draft it before invoking the release command.

## Coverage ratchet (on by default)

The preflight runs the coverage ratchet by default. The instrumented suite is
slow, so to skip it on a quick check, set `RUN_COVERAGE=0`:

```bash
RUN_COVERAGE=0 bash .claude/skills/release-prep/scripts/preflight.sh
```

It runs `make coverage` and compares the TOTAL against the floor in
`tests/COVERAGE_BASELINE.md`. A drop below the floor is a `[WARN]` (advisory,
never a `[FAIL]`). If the drop is intended — or coverage improved — re-stamp the
floor before releasing and commit it:

```bash
make coverage-ratchet-update TESTS=$(make test 2>/dev/null | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+')
```

## VM packaging smoke (on by default)

The preflight ends with a `VM packaging smoke` section. This is the only check that
verifies the *installed* result rather than the repo sources — `check-shipped`
validates that `etc/sysforge`, hooks, completions, and the manpage are correct **in
the tree**, but nothing else confirms a real `pacman -U` lays them down where they
belong. That is what `tools/smoke.sh` asserts (3 hooks, both completions, the
tmpfiles.d sentinel dir, `pacman -Qi` registration).

Behaviour, and how to act on each outcome:

| Outcome | Meaning | What to tell the user |
|---|---|---|
| `[PASS] vm-smoke passed against an installed vX.Y.Z` | VM holds a current build; invariants hold | Nothing to do |
| `[WARN] VM not running` | Unverified — the VM is optional infra, so absence never blocks | Offer to boot it (`make vm-snapshot`) and run `make vm-test` |
| `[WARN] … not vX.Y.Z` | Smoke passed, but against an **older build** | Recommend `make vm-test` before trusting the result |
| `[FAIL] vm-smoke failed` | Real packaging break | Release must not proceed; the indented smoke output names the missing artifact |

Skip it with `RUN_VM_SMOKE=0` (mirrors `RUN_COVERAGE=0`):

```bash
RUN_VM_SMOKE=0 bash .claude/skills/release-prep/scripts/preflight.sh
```

Note `make vm-smoke` alone only *checks* an already-installed sysforge — it does not
build or install. `make vm-test` is the full round-trip (`vm-pkg-stable` →
`vm-install-stable` → `vm-smoke`) and is what refreshes a stale VM.

## Derivative portability (on by default)

The preflight's last section runs the container tier's derivative arm
(`tools/container/harness.sh smoke --distro=cachyos`) — the only check that
exercises **Standards row 23** against a real Arch derivative instead of
synthetic input: extra sync DBs ahead of `core`/`extra`, the derivative's own
`makepkg.conf` baseline, and bumped `pkgrel`s on core packages. It costs seconds,
not a VM boot.

| Outcome | Meaning | What to tell the user |
|---|---|---|
| `[PASS] derivative portability passed against an installed vX.Y.Z` | Row 23 holds on the derivative | Nothing to do |
| `[WARN] podman not installed` | Unverified — optional infra, so absence never blocks | Offer `make dev-deps-container`, then `make vm-pkg-stable && make container-smoke-cachyos` |
| `[WARN] container harness unavailable` | No network, or the base image could not be pulled | Retry when online; do not treat as a break |
| `[WARN] … not vX.Y.Z` | Passed, but against an **older build** | Recommend `make vm-pkg-stable` first |
| `[FAIL] derivative portability failed` | A row 23 invariant does not hold | Release must not proceed; the indented output names the failing check |

**Cadence — this is the part a script cannot enforce.** Because absence only
warns, the script alone cannot make "validated each minor" true. So:

> For a **minor or major** version bump, section 9 must be **green** — a
> `[WARN]` is not good enough. A yellow section 9 is acceptable for a **patch**
> release only.

Check the bump kind against that rule explicitly and say so in the report. If the
bump is minor/major and section 9 is yellow, the fix is to install podman / get
online and re-run — not to proceed. Keeping the version-sensitivity here rather
than in the script leaves `preflight.sh`'s policy uniform across all nine
sections.

Skip it with `RUN_DISTRO_SMOKE=0` (mirrors `RUN_VM_SMOKE=0`), which is only
appropriate for a patch release.

## What this skill does NOT check

- **Tests pass** — too slow to run automatically (1547 tests). Tell the user: "Run `make test` separately before releasing if you haven't already."
- **The bumped-to version isn't already tagged** — release.sh checks this itself, and it depends on the bump kind which this skill does not take as an argument.
- **uv.lock is stale** — release.sh regenerates it during Phase 1. We do verify uv.lock exists.
- **AUR push** — out of scope; release.sh prints push instructions in Phase 4.
