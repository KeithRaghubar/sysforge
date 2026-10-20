# SysForge Roadmap

Planned features and changes. This is the single tracked home for
forward-looking work; **`DESIGN.md` describes only implemented design** and
never carries roadmap IDs.

**Purposely-excluded and abandoned ideas live in
[`docs/ROADMAP-ABANDONED.md`](docs/ROADMAP-ABANDONED.md)**, together with their
rationale and reopen conditions. That file is history rather than backlog, but
its IDs remain part of this file's ID namespace — see `## ID scheme`.

**Shipped work is not recorded here** — it lives in `docs/release-notes/` and git
history (the commit that lands an item is its record).

## ID scheme

IDs are `<version>-<TYPE><n>`, e.g. `1.2.0-F1` (feature), `1.2.0-B1` (bug),
`1.2.0-Q1` (open question), `1.2.0-STD1` (standards), `1.2.0-DOC1` (user-facing
documentation), `1.2.0-DEV1` (contributor-only work). The version prefix is the
current `pyproject.toml` version. The per-type counter **resets to 1 only on a
major or minor version bump** (`X.Y.Z` → `(X+1).0.0` / `X.(Y+1).0`), never on a
patch bump — i.e. the counter is scoped to the minor-release cycle and stays
monotonic across patch releases within it. The version prefix keeps IDs globally
unique and records the cycle an item originated in. An item still open at release
time keeps its existing ID
(it records the cycle the item originated in, not its target). IDs appear only here,
in `docs/ROADMAP-ABANDONED.md`, and in release notes.

**Those three files are one ID namespace.** An abandoned ID stays spent — it is
never reissued — so `make next-id` reads all three, and `make check-standards`
rejects an ID listed both here and in `docs/ROADMAP-ABANDONED.md`.

**Never hand-pick an ID — derive it.** The open items above keep their origin-cycle
prefixes, so eyeballing a neighbour gives the wrong cycle (and the wrong counter)
right after a release. Run `make next-id TYPE=F` (or `B`/`DEV`/`DOC`/`Q`/`STD`) — it reads the
current `pyproject.toml` version, scopes to that cycle's counter, and prints the next
free ID (e.g. `2.4.0-F1`). `make check-standards` also flags collisions and
active-cycle sequence gaps.

Within each subsection, entries are kept in **ascending ID order** (by type
counter, then version) — sort on every add so the list stays scannable. Every
entry opens `- **`<ID>` — <title sentence>.**` and is separated from its
neighbour by a `---` rule, so a single item is readable on its own rather than
running into the next one. `docs/release-notes/` entries use the same two rules.

**`DOC` covers user-facing documentation only** — guides under `docs/guides/`, concept
pages under `docs/concepts/`, `README.md`, the man page — so doc work stays visible in release notes without
being forced into `F` or `B`. A `DOC` entry always lands under `## Changed`: the
section drives the derived SemVer bump, and documentation changes no API (a
`DOC` under `## Added` would demand a minor release). `make check-standards`
enforces this. Internal docs (`docs/design/`, `CLAUDE.md`, this file) get no ID,
and wrong `--help`/man output is a `B`, since it is CLI behaviour.

**`DEV` covers contributor-only work** — a fix or feature that touches only
`tests/`, `tools/`, the `Makefile`, `.claude/` or dev-only docs (`tools/vm/README.md`
and the like). Anything under `sysforge/` or a shipped file stays `B`/`F` even when a
dev problem prompted it, and changing a *rule* (a standards row, a process
convention) stays `STD`. A `DEV` item files its release-note entry under
`## Changed` only, like `DOC`: it changes no API, and the section drives the derived
SemVer bump, so it may only ever imply a patch (`3.4.0-STD1`; until then `DEV` filed
no entry, and those items' squash-commit subjects remain their only record, which
`make next-id` and the gap check still read). `make check-standards` rejects a
`DEV`-filed entry under any other section.

**Open questions (`Q`) must be resolved before any implementation.** A `Q`
entry is undecided by definition; investigation/spikes to inform the decision
are fine, but before writing production code the question must first be either
**promoted** to a proper `F`/`B`/`STD`/`DOC`/`DEV` entry (which then follows the normal
landing flow) or moved to `docs/ROADMAP-ABANDONED.md` with a rationale. Never
implement straight off a `Q`.

## Priority, effort & bump tags

Every **Planned** entry ends with a machine-readable tag of the form
`*Priority: <level> · Effort: <size> · Bump: <kind>* — <rationale>`:

- **Priority** ∈ `low` / `med` / `high` — impact *within this backlog*. The backlog
  is polish by construction (anything correctness-bearing is implemented, not parked),
  so judge relatively: `low` = cosmetic/churn/latent; `med` = observability, or a gap
  that is *currently active* on a real system; `high` = user-visible friction now.
- **Effort** ∈ `small` / `medium` / `large` — implementation cost, independent of
  priority. On a uniformly-low-impact backlog this is often the deciding axis
  (cheapest-to-land first). Effort is stable; it does not decay the way a per-host
  "is this live right now" flag would, which is why urgency folds into *priority*
  rather than a separate tag.
- **Bump** ∈ `patch` / `minor` / `major` — the SemVer impact the entry will carry
  when it lands (standards row 3). `major` means it changes or removes an
  existing contract and can therefore only ship in an `X.0.0` release; `minor`
  is purely additive; `patch` is a fix or an internal change with no surface
  effect. This is **planning-time** advice: an item is removed from ROADMAP by
  the commit that lands it, so by release time the authoritative record is the
  release-notes accumulator, from which `make next-bump` derives the required
  bump.

`docs/ROADMAP-ABANDONED.md` entries carry no tag — the tag is planning advice,
and an abandoned item has no landing to plan. The summary table below is **generated** from these
tags — run `make roadmap-table` after any add/remove/retag; `make check-roadmap-table`
(wired into `pre-release`) fails if the committed table has drifted.

The committed table is always in triage order (priority, then effort, then ID).
To read the backlog along another axis without touching the file, use `make
roadmap-view SORT=<triage|id|item|priority|effort|bump>` (add `REVERSE=1` to
flip it) — e.g. `SORT=effort` for cheapest-to-land first. That view is
read-only by construction, so it can never be committed in place of the
canonical ordering.

---

## Planned

<!-- BEGIN roadmap-table (generated by tools/gen_roadmap_table.py — do not edit by hand; run `make roadmap-table`) -->
| ID | Item | Priority | Effort | Bump |
|----|------|----------|--------|------|
| `3.5.0-B108` | a split AUR package whose pkgbase is not a package name never gets its RPC version | med | small | patch |
| `3.5.0-B109` | lib32-* packages are built in a sandbox without the multilib toolchain | med | small | patch |
| `3.5.0-B111` | the shipped optimized profile disables LTO under the default gcc toolchain | med | small | patch |
| `3.5.0-B113` | a compiler swap kept from the build-failure menu is lost on the default layout | med | small | patch |
| `3.5.0-DEV8` | make make test the complete suite, so pre-release matches CI | med | small | patch |
| `3.5.0-B54` | the PGO staging dirs in /var/tmp are used without an ownership check | med | medium | patch |
| `3.5.0-B59` | kernel Gate 2 audits the wrong config when a stale package is reused | med | medium | patch |
| `3.5.0-B64` | some sysforge installs are not recorded as self-installs | med | medium | patch |
| `3.5.0-DEV1` | test the branches mutation testing showed are unchecked | med | medium | patch |
| `3.5.0-B110` | [safety] llvm_preflight = false has no effect | low | small | patch |
| `3.5.0-B112` | the shipped configs carry stale and dead guidance | low | small | patch |
| `3.5.0-DEV5` | name the failing gate on the CI run page | low | small | patch |
| `3.5.0-DEV6` | report test failures in full in CI | low | small | patch |
| `3.5.0-DEV7` | print the container's environment at the top of the CI log | low | small | patch |
| `3.5.0-DEV9` | correct the Makefile's stale comments and small gaps | low | small | patch |
| `3.5.0-B81` | state files share a fixed temp name and skip fsync | low | medium | patch |
| `3.5.0-DEV2` | raise test coverage where this review's bugs clustered | low | medium | patch |
<!-- END roadmap-table -->

### Features

### Bugs

- **`3.5.0-B54` — the PGO staging dirs in `/var/tmp` are used without an ownership check.**
  `/var/tmp/sysforge-llvm-stage{1,2,3}` are fixed names in a world-writable directory, created
  with `mkdir(exist_ok=True)` and then used as `PATH`, `LD_LIBRARY_PATH`, `CMAKE_PREFIX_PATH`
  and CC (`profdata.stage_env`, `staged_cc`), with no check of owner, mode or symlink. Another
  local user can create one (for example between the purge and the extraction an hour later) and
  have their files run during the build; pre-creating one only blocks the run. Create the dirs
  0700 and verify owner and mode before use, or move them under a user-owned path from
  `primitives/paths.py`. Found in the 2026-10-18 whole-repo review; confirmed in the code.
  Tests: a pre-existing staging dir owned by another user, or a symlink, is refused.
  *Priority: med · Effort: medium · Bump: patch* — another local user can run code in the build.

---

- **`3.5.0-B59` — kernel Gate 2 audits the wrong config when a stale package is reused.** When
  makepkg exits 13 and `resolve_reused_config` finds no config for the artifact,
  `kernel/gates.gate2_audit` falls back to the newest `.config` in the build tree (which may
  belong to another build) or, with none, only warns and continues. A package Gate 2 rejected
  once and left in PKGDEST can then be reused and installed after the build tree is wiped. The
  boot-entry version and the Gate 3 DKMS check take the release the same way. Refuse when
  nothing ties a config to the artifact, or read the config from the package. Found in the
  2026-10-18 whole-repo review; from a reviewer's reading of the code, not yet reproduced, so
  confirm before fixing. Tests: a reused artifact with no matching config is refused (unless
  `--skip-boot-audit`).
  *Priority: med · Effort: medium · Bump: patch* — can install a kernel config Gate 2 rejected.

---

- **`3.5.0-B64` — some sysforge installs are not recorded as self-installs.**
  `record_self_install` is called only from `pacman.batch_install_pkgs`, not from the
  sudo-timeout recovery install in `makepkg_invoke`, `install_built_packages` or `configure`'s
  `pacman -U`. A package installed through those paths looks external to the next `update`,
  which demotes it to a pacman entry if it has a repo counterpart. The window is also reset only
  by `update`, so `build foo`, then `pacman -S foo`, then `update` keeps it as self-installed
  and rebuilds over the switch; `revert` and `uninstall` reconcile without update's sync-repo
  filter. Record every `-U`, and clear the window where the consumer runs. Found in the
  2026-10-18 whole-repo review; from a reviewer's reading of the code, not yet reproduced, so
  confirm before fixing. Tests: each install path records a self-install; a later external
  install is still demoted.
  *Priority: med · Effort: medium · Bump: patch* — packages can be demoted or rebuilt against the user's choice.

---

- **`3.5.0-B81` — state files share a fixed temp name and skip fsync.** `build_state`,
  `pipeline_state`, `source_meta`, `stage_sentinel`, `opt_out`, `artifacts`,
  `build_fingerprint`, `kernel_fdo` and `kbuild_map` write `<file>.tmp` then rename, with no
  fsync and a fixed temp name. Two concurrent writers can publish each other's partial file or
  fail the second rename, and a power loss after the rename can leave an empty file; an empty
  `build_state.toml` then repopulates every package as `pacman`, losing all provenance. Use
  `mkstemp` in the destination directory plus fsync, the `bench_state` and `stage_timings`
  shape, for all of them. Found in the 2026-10-18 whole-repo review; from a reviewer's reading
  of the code, not yet reproduced, so confirm before fixing. Tests: each writer uses a unique
  temp name and fsyncs (patched seam).
  *Priority: low · Effort: medium · Bump: patch* — rare, but the loss is total when it hits.

---

- **`3.5.0-B108` — a split AUR package whose pkgbase is not a package name never gets its RPC
  version.** The source-sync scheduler queries the AUR RPC `info` endpoint with pkgbases
  (`source_sync._ensure_rpc`, primed by `update_sync`) and keys the results by `Name`. `info`
  matches package names only, so a split package whose pkgbase is none of its pkgnames
  (`lib32-libdbusmenu` builds `lib32-libdbusmenu-glib`/`-gtk3`) gets no result. Its PKGBUILD's
  `pkgver=${_pkgver%~*}` cannot be evaluated statically, the `rpc_version` fallback in
  `update_version` finds nothing, and `update` skips the package every run ("unresolved shell
  expansion and no cached RPC version"); the RPC short-circuit also never fires for such a
  package, so it is fetched every run. Query the member pkgnames as well and key results by the
  RPC's `PackageBase`. Seen in the 2026-10-19 `update --devel` run log; confirmed against the live
  RPC (the pkgbase returns 0 results, `lib32-libdbusmenu-glib` returns `PackageBase:
  lib32-libdbusmenu`). Tests: a split pkgbase that is not a pkgname resolves its RPC version
  through a member; the short-circuit fires for it.
  *Priority: med · Effort: small · Bump: patch* — split AUR packages silently stop updating.

---

- **`3.5.0-B109` — `lib32-*` packages are built in a sandbox without the multilib toolchain.**
  Arch builds its multilib PKGBUILDs in a chroot carrying the `multilib-devel` group
  (`lib32-gcc-libs`, `lib32-glibc`, …), so those PKGBUILDs do not declare them. sysforge's build
  sandbox installs only the PKGBUILD's own dependencies into a plain `base-devel` chroot
  (`build_sandbox.py` has no `lib32` handling), so a 32-bit link finds only the 64-bit
  `libgcc_s.so.1` and fails (`ld.lld: error: …/libgcc_s.so.1 is incompatible with elf32-i386`).
  Every `lib32-*` source build that links against `libgcc_s` fails this way in the sandbox.
  Install `multilib-devel` into the container for `lib32-*` targets (as Arch's `multilib-build`
  does), and refuse with a clear message when `[multilib]` is not enabled in the chroot. Seen in
  the 2026-10-19 `update --devel` run log (`lib32-libvdpau`); confirmed in the chroot (no
  `lib32-gcc-libs`). Tests: a `lib32-*` target's container deps include `multilib-devel`; a
  non-`lib32` target's do not.
  *Priority: med · Effort: small · Bump: patch* — every sandboxed lib32 build can fail.

---

- **`3.5.0-B110` — `[safety] llvm_preflight = false` has no effect.** `sysforge.toml` documents
  `llvm_preflight` as the switch that suppresses the informational LLVM pre-flight table for
  `fetch`, `update` and `build`, and ships it as `true`, but no code reads it: `fetch.cmd_fetch`,
  `build_cmd` and `update._phase_llvm_preflight` check only `--no-llvm-preflight`. The design text
  (§primitives-layer, the pre-flight wiring) also claims the key works. Resolve it through
  `config.resolve_flag_default(args, "no_llvm_preflight", safety_cfg, …)` (inverted) at all three
  sites, or remove the key; drop the comment's stale `converge` verb either way. Found in the
  2026-10-19 shipped-config audit; confirmed in the code. Tests: `llvm_preflight = false`
  suppresses the table on each verb; the CLI flag still wins.
  *Priority: low · Effort: small · Bump: patch* — a documented setting silently does nothing.

---

- **`3.5.0-B111` — the shipped `optimized` profile disables LTO under the default gcc
  toolchain.** `[profiles.optimized]` hardcodes `-fuse-ld=lld` in `LDFLAGS` while the shipped
  `[defaults] toolchain` is `"gcc"`. gcc plus lld trips `emit_makepkg_conf`'s guard ("GCC LTO
  bitcode is incompatible with lld"), which clears `LTOFLAGS` and flips `lto` to `!lto`, so every
  package on the out-of-the-box `optimized` profile builds without LTO. The `llvm` toolchain
  bundle (`profile._expand_toolchain`) already adds `-fuse-ld=lld` when no linker is declared, so
  the hardcoded token gains nothing under llvm. Drop it (keep `--icf=all`, which the linker guard
  strips when lld is not in use) in `etc/sysforge/profiles.toml` and the fixture. Found in the
  2026-10-19 shipped-config audit; the run log showed the guard firing on every gcc package.
  Tests: the shipped `optimized` profile under `toolchain = "gcc"` keeps LTO; under `"llvm"` it
  still links with lld.
  *Priority: med · Effort: small · Bump: patch* — the default optimized profile silently loses LTO.

---

- **`3.5.0-B112` — the shipped configs carry stale and dead guidance.** From the 2026-10-19
  shipped-config audit, all confirmed in the code:
  (1) `[profiles.batch]` and `[profiles.kernel]` `makepkg_flags` list `--syncdeps`/`--install`
  (and `batch` `--rmdeps`), and the `profiles.toml` header lists them as common choices, though
  every build strips `--syncdeps`/`--install` since `3.4.0-F1` (`BATCH_STRIP_FLAGS`/`SYNC_FLAGS`);
  (2) `[profiles.kernel]` and its commented `[[rules]]` example are no longer needed: the kernel
  stage stamps `owner_stage = "kernel"`, which turns on the kernel patchers, and never selects the
  profile;
  (3) `sysforge.toml` names a `converge` verb that no longer exists, and `sysforge.toml` and
  `bootstrap.toml` say `sysforge reconfigure` for what is now `sysforge run reconfigure`;
  (4) the header's example `CPPFLAGS = "-D_FORTIFY_SOURCE=2"` contradicts Arch's current
  `-Wp,-D_FORTIFY_SOURCE=3` in `CFLAGS` (copying it defines the macro twice);
  (5) the header calls `[defaults] profile` "(required)", but the resolver falls back to `bare`,
  and it points at a hardcoded "line 220" that drifts with every edit.
  Fix the text, drop the dead flags and decide whether `[profiles.kernel]` stays as an explicit
  example or goes; keep the fixtures in parity. Tests: `make check-shipped`; a test that no
  shipped profile's `makepkg_flags` contains a flag in `BATCH_STRIP_FLAGS`.
  *Priority: low · Effort: small · Bump: patch* — misleading guidance in the files users edit.

---

- **`3.5.0-B113` — a compiler swap kept from the build-failure menu is lost on the default
  layout.** `profile_writer.write_package_compiler_override` writes `profiles.toml.tmp` next to
  the file and renames it over, by hand, instead of through `atomic_write.replace_file`, the one
  home for replacing a config (`sysforge/CLAUDE.md`). On the default layout the target
  (`config._active_profiles_path`) is root's `/etc/sysforge/profiles.toml`, so creating the temp
  file fails and the `OSError` becomes a warning shown only at `-v`: the menu's swap is never
  saved and the next build fails the same way. A symlinked `profiles.toml` is replaced by a
  regular file, and the file's mode and owner are not kept. Route the write through
  `atomic_write.replace_file`, which escalates through the privilege seam, as `3.5.0-B47` did
  for `packages.toml`. Found while implementing `3.5.0-F12`. Tests: a root-owned target is
  written via the privilege seam; a symlink is kept and its target replaced; mode is preserved.
  *Priority: med · Effort: small · Bump: patch* — the recovery menu's "keep this swap" silently does nothing.

### Developer tooling

- **`3.5.0-DEV1` — test the branches mutation testing showed are unchecked.** Mutating
  `abi_check`, `stage_sentinel`, `flag_drift`, `version` and `build_estimate` left 36 mutants
  alive whose tests still passed. In `abi_check` (19): the 32/64-bit class matching
  (`_elf_class`, `_ldconfig_tag_class`), the bsdtar failure path and the owner filter that
  decides which findings count as source-built. In `stage_sentinel` (6): the recovery prompt's
  non-TTY and state-dir-mismatch paths. In `flag_drift` (5): the extracted-profile branch and
  the empty `stored_flags` path. In `version` (5) and `build_estimate` (1): rpmvercmp separator
  and seed edge cases. `opt_out` and `stage_timings` killed every mutant. Add tests that kill
  each survivor. Found in the 2026-10-18 whole-repo review; confirmed in the code. Tests:
  rerunning the same mutations kills every one.
  *Priority: med · Effort: medium · Bump: patch* — the ABI and sentinel gates are the ones a regression would hurt most.

---

- **`3.5.0-DEV2` — raise test coverage where this review's bugs clustered.** Branch coverage is
  89.1% overall, and the least-covered modules are the ones this review found bugs in:
  `snapshot.py` 53%, `packages_cmd.py` 61%, `pipeline/stages/reconfigure.py` 64%,
  `verbs/artifact.py` 70%, `dep_analysis.py` 71%, `pipeline/stages/packages.py` 72%,
  `configure.py` 75%, `makepkg_pgo.py` 77%, `toolchain/pkgbuilds.py` 79% and
  `makepkg_wrapper.py` 81%. Cover their failure and resume paths first, then raise the coverage
  ratchet. Found in the 2026-10-18 whole-repo review; confirmed in the code. Tests: each listed
  module's coverage rises and the ratchet floor is re-stamped.
  *Priority: low · Effort: medium · Bump: patch* — test debt.

---

- **`3.5.0-DEV5` — name the failing gate on the CI run page.** `tools/ci.sh` runs every gate
  in one `make -k` inside the one "Lint, suite and gates" step, so a red run shows only that the
  step failed and the gate has to be found in roughly ten minutes of log. Wrap each gate in
  `::group::<gate>`/`::endgroup::` and append a pass/fail table to `$GITHUB_STEP_SUMMARY`,
  keeping the `-k` behaviour (every gate runs, the job still fails) and leaving `make ci-local`
  output unchanged outside Actions. Found while debugging the 2026-10-19 red run.
  Tests: `make ci-local` still exits non-zero on a failing gate; `shellcheck` passes.
  *Priority: low · Effort: small · Bump: patch* — the run page says which gate broke.

---

- **`3.5.0-DEV6` — report test failures in full in CI.** The `coverage` target, which CI runs
  as the suite, passes `-q` with `addopts` cleared, so the short summary truncates each failure
  to one line and there is no machine-readable result. Add `-rfE --tb=short` and a
  `--junitxml` report, and annotate each failure with its file and line in Actions (a small
  reporter over the JUnit file, or a pytest plugin added the way `pytest-cov` is). Found while
  debugging the 2026-10-19 red run. Tests: a deliberately failing test shows its full assertion
  in the summary and an annotation in a CI run.
  *Priority: low · Effort: small · Bump: patch* — failures readable without the raw log.

---

- **`3.5.0-DEV7` — print the container's environment at the top of the CI log.** A test that
  depends on the host (`3.5.0-DEV3`) fails only where the environment differs, and the log does
  not say how it differs. Have `tools/ci.sh` print, before the gates, the package count from
  `pacman -Q`, whether the LLVM suite and `clang` are installed, and the Python, pytest and
  `uv` versions, so a CI-only failure becomes a comparison with the local machine. Found while
  debugging the 2026-10-19 red run. Tests: `make ci-local` output starts with the block;
  `shellcheck` passes.
  *Priority: low · Effort: small · Bump: patch* — a CI-only failure is a diff, not a guess.

---

- **`3.5.0-DEV8` — make `make test` the complete suite, so `pre-release` matches CI.** `test`
  runs the system `pytest`, so `tests/test_sync_config.py` (which `importorskip`s tomlkit) runs
  only where `python-tomlkit` happens to be installed; the Makefile's own comment calls
  `coverage` "the complete run". CI runs `coverage`, but `make pre-release`, the gate before a
  release, runs `test`, so its completeness depends on the machine. Run `test`/`test-x` through
  `uv run --no-sync --with tomlkit pytest`, and drop the comments that explain the gap. Then
  define the gate list once: it is written twice, as `pre-release`'s prerequisites and as the
  `make -k …` line in `tools/ci.sh`, which is how `test` and `coverage` drifted apart. A `GATES`
  variable that `pre-release` depends on and `ci.sh` runs (or `ci.sh` running
  `make -k pre-release`) keeps them equal by construction, and makes true the
  `docs/RELEASE-CHECKLIST.md` line saying `ci-local` runs "the same gate list as `pre-release`".
  `make ci-local` stays: it answers a different question (the same gates, away from this
  workstation's packages, shell environment, live config and `/boot`), which is what caught
  `3.5.0-DEV3` and the second round of `3.5.0-DEV4`. Found in the 2026-10-19 Makefile audit.
  Tests: on a machine without `python-tomlkit`, `make test` runs `tests/test_sync_config.py`
  instead of skipping it; a test that `tools/ci.sh` and `pre-release` resolve to the same gates.
  *Priority: med · Effort: small · Bump: patch* — the release gate runs fewer tests than CI on some machines.

---

- **`3.5.0-DEV9` — correct the Makefile's stale comments and small gaps.** From the 2026-10-19
  audit: the `check-shipped` comment says "seven check groups" (`GROUPS` in
  `tools/check_shipped.py` has 11); the `next-id` comment lists `F/B/Q/STD` (it also takes
  `DEV` and `DOC`); the `help` "Variables:" footer omits `TESTS=`, `NAME=`/`GUI=`, `DISTRO=`,
  `VM_USER=` and the preflight `RUN_*` skips; `vm-ssh-root` duplicates `vm-ssh`, whose
  `VM_USER` already defaults to root; `dev` does not depend on `venv`, so it fails on a fresh
  checkout; `clean` leaves `man/sysforge.1.scd` and `.ruff_cache/`; and `typecheck` covers only
  `sysforge/` while `lint-py` covers `sysforge/ tests/ tools/` (decide whether that is intended
  and say so). Tests: `tests/test_makefile_help.py` still passes; `make dev` works on a fresh
  checkout.
  *Priority: low · Effort: small · Bump: patch* — the Makefile's comments are its documentation.

### Open questions
