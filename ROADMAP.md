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

**`DOC` covers user-facing documentation only** — guides under `docs/guides/`,
`README.md`, the man page — so doc work stays visible in release notes without
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
| `3.5.0-B37` | the root pacman hook follows symlinks in the group-writable state dir | high | small | patch |
| `3.5.0-B38` | a staged PKGBUILD edit on a VCS package is treated as clean and wiped | high | small | patch |
| `3.5.0-B39` | run toolchain --install-only accepts an instrumented or half-finished build | high | small | patch |
| `3.5.0-B40` | a package interrupted mid-build is dropped on --resume | high | small | patch |
| `3.5.0-B41` | run pipeline --dry-run writes pipeline state | high | small | patch |
| `3.5.0-B42` | state repair drops each repaired entry's provenance | high | small | patch |
| `3.5.0-B45` | a recovered compiler override can corrupt profiles.toml | high | small | patch |
| `3.5.0-B46` | verbs other than build, update, run and setup clear a stale recovery sentinel | high | small | patch |
| `3.5.0-B43` | stale-sentinel recovery runs a command read from a group-writable file | high | medium | patch |
| `3.5.0-B44` | the first Ctrl-C inside a sentinel scope is swallowed and never acted on | high | medium | patch |
| `3.5.0-B47` | packages.toml, pacman.conf and repo-mode writes bypass atomic_write | med | small | patch |
| `3.5.0-B48` | a first build opt-in writes packages.toml without its header or [build] section | med | small | patch |
| `3.5.0-B49` | the single-pass toolchain build reuses a same-version package without checking it | med | small | patch |
| `3.5.0-B50` | kernel root detection misses the storage driver under LUKS, LVM or RAID | med | small | patch |
| `3.5.0-B51` | update exits 0 when its only package was refused a purge | med | small | patch |
| `3.5.0-B52` | a stale kernel config fragment survives when nothing is left to merge | med | small | patch |
| `3.5.0-B53` | dependency names from a PKGBUILD reach root pacman without -- | med | small | patch |
| `3.5.0-B55` | authenticate()'s result is ignored before the long kernel and PGO builds | med | small | patch |
| `3.5.0-B56` | a VCS package built but not installed is reported up to date by update --devel | med | small | patch |
| `3.5.0-B57` | forget_packages saves build_state before the opt-out record | med | small | patch |
| `3.5.0-B58` | Gate 3 checks consumers against the wrong libLLVM when a compat copy is installed | med | small | patch |
| `3.5.0-B60` | the AlreadyBuilt reuse path ignores -sysforge renamed artifacts | med | small | patch |
| `3.5.0-B61` | the build-succeeded-but-install-failed recovery can install stale packages | med | small | patch |
| `3.5.0-B62` | revert-to-stock on a split member reinstalls the pkgbase instead | med | small | patch |
| `3.5.0-B63` | update --include-stage-owned rebuilds never converge on flag drift | med | small | patch |
| `3.5.0-B65` | --cleansrc deletes the checkout before checking whether it can be re-cloned | med | small | patch |
| `3.5.0-B66` | [aur] enabled = false does not stop fetches for source = "git" checkouts | med | small | patch |
| `3.5.0-B67` | auto-repair downloads code without the source-freeze check | med | small | patch |
| `3.5.0-B68` | the cmake injection validator misses a statement ending in a comment or chained command | med | small | patch |
| `3.5.0-B69` | a DIVERGED auto-reset discards local commits written by someone else | med | small | patch |
| `3.5.0-B70` | a non-RuntimeError failure skips the pipeline's failure handling | med | small | patch |
| `3.5.0-B71` | every archinstall run enables root SSH login with a password | med | small | patch |
| `3.5.0-B72` | configure trusts username and target from bootstrap.toml | med | small | patch |
| `3.5.0-B73` | packages added to packages.toml after a pipeline's first run are skipped on resume | med | small | patch |
| `3.5.0-B74` | run toolchain --dry-run takes the stage lock and writes the sentinel | med | small | patch |
| `3.5.0-B75` | two toolchain repo installs arm the sentinel before checking sudo | med | small | patch |
| `3.5.0-B76` | Gate 3 rollback is refused when the snapshot names a package that was never installed | med | small | patch |
| `3.5.0-B77` | privileged directory provisioning follows symlinks in the shared state tree | med | small | patch |
| `3.5.0-B78` | a -sysforge renamed VCS package is not treated as VCS | med | small | patch |
| `3.5.0-B99` | the cache report shows 0/0 hits for sandboxed builds | med | small | patch |
| `3.5.0-B54` | the PGO staging dirs in /var/tmp are used without an ownership check | med | medium | patch |
| `3.5.0-B59` | kernel Gate 2 audits the wrong config when a stale package is reused | med | medium | patch |
| `3.5.0-B64` | some sysforge installs are not recorded as self-installs | med | medium | patch |
| `3.5.0-DEV1` | test the branches mutation testing showed are unchecked | med | medium | patch |
| `3.5.0-DOC3` | concept page: profile-guided and post-link optimization (pilot) | med | medium | patch |
| `3.5.0-DOC4` | concept page: shared libraries and ABI | med | medium | patch |
| `3.5.0-DOC5` | concept page: how an Arch package is built | med | medium | patch |
| `3.5.0-DOC6` | concept page: compiler optimization basics | med | medium | patch |
| `3.5.0-B79` | kernel FDO re-records keep the previous round's profile files | low | small | patch |
| `3.5.0-B80` | integer and hex [[kconfig]] values are written as quoted strings | low | small | patch |
| `3.5.0-B82` | sysforge fetch keys sync requests on the directory name | low | small | patch |
| `3.5.0-B83` | an AUR RPC 429 or 503 does not set the shared rate-limit penalty | low | small | patch |
| `3.5.0-B84` | read_built_upstream_commit assumes the srcdir is <pkgbuild_dir>/src | low | small | patch |
| `3.5.0-B85` | two members of one split AUR dependency are built twice | low | small | patch |
| `3.5.0-B86` | the PGP-key auto-repair can offer the wrong key | low | small | patch |
| `3.5.0-B87` | the run log can lose or misplace lines written from other threads | low | small | patch |
| `3.5.0-B88` | bootstrap.toml is readable in the target before it is restricted | low | small | patch |
| `3.5.0-B89` | two makepkg.conf scrubs miss values that come only from the system conf | low | small | patch |
| `3.5.0-B90` | the --nodeps probe can fail on a supersede conflict and skip the relaxation | low | small | patch |
| `3.5.0-B91` | the progress bar keeps ninja totals within a step that opens no tracker | low | small | patch |
| `3.5.0-B92` | an unmounted path drops its artifact-review decline permanently | low | small | patch |
| `3.5.0-B93` | a failed derename revert clears the sentinel with the package removed | low | small | patch |
| `3.5.0-B94` | dead helpers that only tests reach | low | small | patch |
| `3.5.0-B95` | strict pyright findings | low | small | patch |
| `3.5.0-B96` | the install stage does not refuse the disk the live system runs from | low | small | patch |
| `3.5.0-B97` | read-only update runs still save build state | low | small | patch |
| `3.5.0-B98` | Gate 3 promises a consumer rebuild that is not queued | low | small | patch |
| `3.5.0-DOC9` | concept page: measuring performance honestly | low | small | patch |
| `3.5.0-DOC10` | glossary | low | small | patch |
| `3.5.0-B81` | state files share a fixed temp name and skip fsync | low | medium | patch |
| `3.5.0-DEV2` | raise test coverage where this review's bugs clustered | low | medium | patch |
| `3.5.0-DOC7` | concept page: bootstrapping an optimized compiler | low | medium | patch |
| `3.5.0-DOC8` | concept page: kernel builds and boot basics | low | medium | patch |
<!-- END roadmap-table -->

### Features

### Bugs

- **`3.5.0-B37` — the root pacman hook follows symlinks in the group-writable state dir.**
  `tools/pacman-hook-helper.sh` runs as root in every pacman transaction and does `mkdir -p`,
  `chgrp sysforge`, `chmod 2775` and `>> "$dir/$kind"` on `/var/lib/sysforge/sentinels`. Its
  parent is `root:sysforge` 2775 with no sticky bit, so any `sysforge`-group member can rename
  `sentinels` and put a symlink to `/etc` (or any directory) in its place: the next transaction
  makes the target group-writable, which is root for that member. A symlinked `sentinels/<kind>`
  likewise turns the append into a root write to any file. Refuse when `$dir` or `$sentinel` is
  a symlink (`[ -L ]`), act only on a verified real directory, and let tmpfiles.d own the
  directory's mode instead of the hook. Found in the 2026-10-18 whole-repo review; confirmed in
  the code. Tests: a symlinked `sentinels` or `sentinels/<kind>` is left untouched and the hook
  still exits 0; a missing directory is still created.
  *Priority: high · Effort: small · Bump: patch* — a group member can gain root.

---

- **`3.5.0-B38` — a staged PKGBUILD edit on a VCS package is treated as clean and wiped.**
  `git_ops._diff_is_pkgver_only` runs `git diff -U0 -- PKGBUILD`, which compares the working
  tree to the index, so a staged change never appears in it. With a `git add`ed real edit and a
  working-tree `pkgver=` bump from makepkg, `git status` shows `MM PKGBUILD`, the filter sees
  only the pkgver line, and `_uncommitted_dirty_paths(is_vcs=True)` returns nothing. A diverged
  sync then runs `reset --hard FETCH_HEAD`, and `--cleansrc` deletes the tree. Compare against
  `HEAD` (`git diff HEAD`) rather than the index. Found in the 2026-10-18 whole-repo review;
  reproduced. Tests: a staged non-pkgver edit plus an unstaged pkgver bump is dirty; a
  staged-only pkgver bump is still clean.
  *Priority: high · Effort: small · Bump: patch* — silently destroys the user's own work.

---

- **`3.5.0-B39` — `run toolchain --install-only` accepts an instrumented or half-finished
  build.** Passes 1 to 3 build with `toolchain_variant="pgo_llvm"` (`pgo.py`, the instrument,
  bootstrap and train calls), so each records llvm in `build_state` with the same variant and
  version as the final Pass 4 build. `pkgbuilds.check_last_pgo_build` checks only those two
  fields, and `install_only` only that some package exists per PKGBUILD dir. A run that stops
  after Pass 1 therefore leaves an instrumented libLLVM and clang in PKGDEST that
  `--install-only`, the retry the stage itself suggests, installs as the optimized build. Leave
  the variant and `owner_stage` unset on passes 1 to 3 as `passes.build_pkg`'s docstring already
  says, or require a Pass-4 marker in the check. Found in the 2026-10-18 whole-repo review;
  confirmed in the code. Tests: a record left by Pass 1, 2 or 3 makes `--install-only` refuse;
  one left by Pass 4 passes.
  *Priority: high · Effort: small · Bump: patch* — can install an instrumented toolchain to /usr.

---

- **`3.5.0-B40` — a package interrupted mid-build is dropped on `--resume`.** `packages.py`
  calls `state.mark_package_building(name)`, which removes the package from `remaining` and
  saves, then builds inside `except RuntimeError` only. Ctrl-C, a kill, a power loss or any
  other exception leaves it in none of `remaining`, `built` or `failed`; on `--resume`,
  `init_package_list` is a no-op and the loop skips anything not in `remaining`, so the package
  is never built and the stage reports success. Treat a package in no bucket as pending on
  resume, or keep a `building` bucket and requeue it. Found in the 2026-10-18 whole-repo review;
  confirmed in the code. Tests: a package left mid-build by a non-`RuntimeError` exception is
  built on `--resume`.
  *Priority: high · Effort: small · Bump: patch* — a package silently goes missing from a resumed install.

---

- **`3.5.0-B41` — `run pipeline --dry-run` writes pipeline state.** `pipeline/runner.py` marks
  the stages before `--start-from` as `skipped_to`, calls `init_meta()` and saves, all before
  the dry-run check. After `run pipeline --dry-run --start-from packages`, a real `--resume`
  treats install, hardware and configure as done and never runs them, and a plain dry run leaves
  a state file that makes the next real run stop with "state file already exists". Guard every
  state change and save on `options.dry_run`. Found in the 2026-10-18 whole-repo review;
  confirmed in the code. Tests: a dry run, with and without `--start-from`, leaves no state file
  and no `skipped_to` mark.
  *Priority: high · Effort: small · Bump: patch* — a preview changes what the next real run does.

---

- **`3.5.0-B42` — `state repair` drops each repaired entry's provenance.**
  `state_cmd.cmd_state_repair` deletes every key of a broken group and then calls
  `BuildState.record`, which carries its sticky fields from the entry it just deleted. The new
  entries keep only `build_mode`, `flags_string` and `built_at`; `owner_stage`,
  `origin_pkgbase`, `source`, `toolchain_variant`, `toolchain_fingerprint`, `sandboxed`,
  `build_inputs`, `built_upstream_commit` and `build_seconds` are lost, and a renamed
  `-sysforge` member the PKGBUILD's `pkgname=()` does not list is not recreated at all. With
  `owner_stage` gone, `update` starts rebuilding stage-owned packages; without `origin_pkgbase`,
  `revert-to-stock` picks the wrong stock name. Carry the old fields into the new record. Found
  in the 2026-10-18 whole-repo review; confirmed in the code. Tests: a repaired entry keeps
  every sticky field of the entry it replaces; a renamed member survives.
  *Priority: high · Effort: small · Bump: patch* — silently undoes stage ownership and revert mapping.

---

- **`3.5.0-B43` — stale-sentinel recovery runs a command read from a group-writable file.**
  `stage_sentinel.check_and_recover_stale_sentinel` reads `recovery_cmd` from
  `<state_dir>/stage_in_progress.toml`, prints it and, on one `y`, runs
  `subprocess.run(recovery_cmd.split())`, which may be `sudo <anything>`. Any member of the
  `sysforge` group can write that file, so another user can plant a command the victim's next
  `update` or `build` offers to run. Store a recovery kind plus validated package names and
  rebuild the argv from fixed templates (names after `--`), and refuse a state file not owned by
  the current user or root. Found in the 2026-10-18 whole-repo review; confirmed in the code.
  Tests: a planted record with an arbitrary command is refused; each real recovery kind still
  rebuilds its command.
  *Priority: high · Effort: medium · Bump: patch* — lets one group member choose a command another runs, possibly with sudo.

---

- **`3.5.0-B44` — the first Ctrl-C inside a sentinel scope is swallowed and never acted on.**
  `sentinel_scope` wraps its body in `InterruptScope()`, whose handler swallows the first SIGINT
  and sets a flag, but nothing in `sysforge/` ever calls `check()` or reads `requested`, so the
  `except CleanExitRequested` branch cannot run. The advertised "stop at the next safe point"
  never happens: sysforge itself ignores the first Ctrl-C (children in the foreground group
  still get it), and a second Ctrl-C raises `KeyboardInterrupt` wherever the code is, possibly
  mid `pacman -U`. Call `check()` at the stages' step boundaries, or drop the scope and say what
  Ctrl-C does. Found in the 2026-10-18 whole-repo review; confirmed in the code. Tests: a first
  Ctrl-C inside a scope stops the run at the next boundary with the clean-exit message and the
  sentinel kept.
  *Priority: high · Effort: medium · Bump: patch* — the documented Ctrl-C behaviour does not exist.

---

- **`3.5.0-B45` — a recovered compiler override can corrupt `profiles.toml`.**
  `profile_writer._row` writes `{pkgbase} = {{ cc = "...", ... }}` with a bare key and unescaped
  values. `libstdc++` (or any name with `+`) makes `profiles.toml` invalid TOML, so every later
  config load fails; a name with `.` such as `python-ruamel.yaml` becomes a nested table that
  `overrides.get(pkgbase)` never matches; and a `"` typed into the recovery menu's manual entry
  breaks the file too. Quote the key and escape the values. Found in the 2026-10-18 whole-repo
  review; reproduced. Tests: names with `+`, `.` and `"` round-trip through `tomllib` to the
  same override.
  *Priority: high · Effort: small · Bump: patch* — one recovery-menu swap can break every later run.

---

- **`3.5.0-B46` — verbs other than build, update, run and setup clear a stale recovery
  sentinel.** `cli._gate_sentinel_check` checks for a stale sentinel only for `build`, `update`,
  `run` and `setup`, but `uninstall`, `revert-to-stock`, the `state` verbs and the artifact
  verbs also have `requires_sentinel=True`, and `sentinel_scope` replaces
  `stage_in_progress.toml` and clears it on success. After a toolchain install is interrupted,
  `sysforge uninstall foo` erases the only record that the live toolchain is half-installed, and
  the next `update` proceeds without the recovery prompt. Have `sentinel_scope` refuse, or run
  the recovery check, when a record exists. Found in the 2026-10-18 whole-repo review; from a
  reviewer's reading of the code, not yet reproduced, so confirm before fixing. Tests: a verb
  with `requires_sentinel=True` refuses (or offers recovery) over an existing stale record and
  leaves it in place.
  *Priority: high · Effort: small · Bump: patch* — loses the record of a half-installed toolchain.

---

- **`3.5.0-B47` — `packages.toml`, `pacman.conf` and repo-mode writes bypass `atomic_write`.**
  `verbs/shared.rewrite_packages_toml`, `pkg_catalog.write_desktop_group`,
  `setup_cmd._write_conf` (`/etc/pacman.conf`) and `reconfigure._set_repo_mode` replace an
  existing config with `path.write_text`, against the one-home rule for
  `atomic_write.replace_file`. A crash or a full disk mid-write truncates the file (an empty
  `pacman.conf` breaks pacman system-wide); on the default root-owned `/etc/sysforge` an
  unprivileged `packages add`, `remove` or `add-group` dies with a `PermissionError` traceback
  instead of escalating, and `reconfigure`'s build-mode step silently does nothing. `packages
  add` also rewrites the file twice (drop, then append), and its docstring claims an atomic
  rename. Route all four through `replace_file`, writing the final text once. Found in the
  2026-10-18 whole-repo review; confirmed in the code. Tests: each writer goes through
  `replace_file` (patched seam); a root-owned target escalates.
  *Priority: med · Effort: small · Bump: patch* — can truncate a config or crash on the default layout.

---

- **`3.5.0-B48` — a first `build` opt-in writes `packages.toml` without its header or `[build]`
  section.** `build_cmd` calls `rewrite_packages_toml(path, drop_name=name)` before its `if not
  path.exists()` check, and that call creates an empty file, so the branch that writes the
  header and the `[build] pkgbuild_src_dir` block never runs. On a machine without
  `packages.toml`, the first repo-package opt-in leaves a file with only the `[[package]]`
  block. Create the header first, or make the rewrite a no-op on a missing file. Found in the
  2026-10-18 whole-repo review; confirmed in the code. Tests: an opt-in with no `packages.toml`
  writes the header and `[build]` section.
  *Priority: med · Effort: small · Bump: patch* — a fresh install gets an incomplete manifest.

---

- **`3.5.0-B49` — the single-pass toolchain build reuses a same-version package without checking
  it.** `passes.build_pass` adds `--cleanbuild --force` only when `pgo_build` is set, so
  `pgo.build_llvm_single` runs makepkg without `-f`. A same-version package already in PKGDEST
  (from a PGO run, an instrumented pass, or a build with other `[llvm] targets`) makes makepkg
  exit 13, `build_pkg` treats it as reuse, and that package is installed and recorded as
  `stock_llvm`. This is reuse with no fingerprint, against the opt-in `--reuse-built` rule. Pass
  `--force` on this path too. Found in the 2026-10-18 whole-repo review; confirmed in the code.
  Tests: the single-pass build passes `--force`; a stale same-version artifact is rebuilt.
  *Priority: med · Effort: small · Bump: patch* — installs whatever same-version package is lying around.

---

- **`3.5.0-B50` — kernel root detection misses the storage driver under LUKS, LVM or RAID.**
  `kernel_safety.detect_root_topology` parses `lsblk -nso NAME,TYPE`, which prints parent rows
  with tree glyphs (`└─nvme0n1`, as on this machine). `_transport_from_name` tests
  `startswith("nvme")` on the glyph-prefixed name, so only an unstacked partition's own row is
  recognised. With root on LUKS or LVM over NVMe the first row is the `crypt` or `lvm` device
  and no transport is found, so a config without `CONFIG_BLK_DEV_NVME` passes Gate 2. Use `lsblk
  -l`/`--raw` (as `_partition_plan.py` does) and strip any glyphs. Found in the 2026-10-18
  whole-repo review; reproduced. Tests: a crypt-over-NVMe and an LVM-over-SATA stack (real
  `lsblk -s` output) each yield their transport.
  *Priority: med · Effort: small · Bump: patch* — Gate 2 can pass a kernel that cannot mount an encrypted root.

---

- **`3.5.0-B51` — `update` exits 0 when its only package was refused a purge.**
  `update._phase_build` returns 0 with "Nothing to rebuild." when `to_build` is empty, checking
  only `frozen_sync_pkgs`. A `--cleansrc` purge refusal never enters `to_build`, so a run whose
  only package was refused skips `_failure_exit_code` and the `cleansrc_failures` handling, and
  reports success to automation. `test_cleansrc_refusal_exits_nonzero` adds a second rebuilding
  package and so never reaches this exit. Fold the purge refusals into the early return. Found
  in the 2026-10-18 whole-repo review; confirmed in the code. Tests: a lone purge-refused
  package exits non-zero.
  *Priority: med · Effort: small · Bump: patch* — automation sees success for a skipped package.

---

- **`3.5.0-B52` — a stale kernel config fragment survives when nothing is left to merge.**
  `kernel/kconfig.py` returns early with no fragment when the merged set is empty, without
  removing the previous `sysforge.config`, while `prepare()` still merges any fragment it finds.
  A user who removes every `[[kconfig]]` entry, hardening and hardware profile keeps getting the
  old options, and because the fragment path is `None` the Rust preflight, DKMS enforcement gate
  and drift check are skipped, so a stale `CONFIG_MODULE_SIG_FORCE=y` goes through ungated.
  Unlink the old fragment on that path, as the `kconfig_merge = false` branch does. Found in the
  2026-10-18 whole-repo review; confirmed in the code. Tests: removing every source of entries
  deletes `sysforge.config`.
  *Priority: med · Effort: small · Bump: patch* — removed kernel options keep applying, ungated.

---

- **`3.5.0-B53` — dependency names from a PKGBUILD reach root pacman without `--`.**
  `pacman.batch_install_makedeps` runs `sudo pacman -S --needed --noconfirm <deps>`, and `pacman
  -T` and `aur.py`'s `pacman -Si` take names the same way, with no `--` and no name check; `-R`
  already uses `--`. A dependency string beginning with `-` in a malicious PKGBUILD is parsed as
  an option to a root pacman. Validate names against the pkgname grammar in `collect_builddeps`
  and put `--` before every target list. Found in the 2026-10-18 whole-repo review; confirmed in
  the code. Tests: a dependency beginning with `-` is refused before any pacman call; argv
  carries `--`.
  *Priority: med · Effort: small · Bump: patch* — hardening: an untrusted PKGBUILD can pass options to root pacman.

---

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

- **`3.5.0-B55` — `authenticate()`'s result is ignored before the long kernel and PGO builds.**
  `kernel/stage.py` and `toolchain/pgo.py` (two sites) call `sudo_session.authenticate()` and
  enter `keepalive()` whatever it returns, unlike `build_core` and `update`. After a mistyped or
  timed-out password the keepalive thread runs a bare `sudo -v` every 60 seconds with inherited
  stdio, prompting from a background thread under the progress bar, and its `join()` has no
  timeout, so the stage can hang at the end until that prompt is answered. Stop on `False`, and
  use `sudo -n -v` in the keepalive so an expired credential warns instead of prompting. Found
  in the 2026-10-18 whole-repo review; from a reviewer's reading of the code, not yet
  reproduced, so confirm before fixing. Tests: a failed first authenticate stops the stage
  before building; the keepalive never prompts.
  *Priority: med · Effort: small · Bump: patch* — breaks the authenticate-before-keepalive rule.

---

- **`3.5.0-B56` — a VCS package built but not installed is reported up to date by `update
  --devel`.** `build_state` records `built_upstream_commit` at build time, before the install,
  and `update_version` returns `UP_TO_DATE` when the remote head equals it, without comparing
  the installed version. When the install never happens (a sudo timeout, an abort, a crash after
  the record), the next `update --devel` reports the package current while the old version stays
  installed. The same short-circuit also ignores a `pkgrel` or `epoch` bump made without an
  upstream commit (a soname rebuild). Compare against the installed version, and include
  pkgrel/epoch in the short-circuit. Found in the 2026-10-18 whole-repo review; from a
  reviewer's reading of the code, not yet reproduced, so confirm before fixing. Tests: a
  recorded-but-uninstalled VCS build, and a pkgrel-only bump, are both rebuilt.
  *Priority: med · Effort: small · Bump: patch* — a pending update can be hidden indefinitely.

---

- **`3.5.0-B57` — `forget_packages` saves `build_state` before the opt-out record.**
  `verbs/shared.forget_packages` saves the forgotten `build_state` and only then records the
  opt-out. A corrupt `opted-out.toml`, an `OSError` or a Ctrl-C between the two leaves the
  package forgotten but not opted out, so after `revert-to-stock mesa` the next `update` can
  adopt mesa again and rebuild it, undoing the revert. Write the opt-out first; it is
  idempotent. Found in the 2026-10-18 whole-repo review; from a reviewer's reading of the code,
  not yet reproduced, so confirm before fixing. Tests: an opt-out write failure leaves
  `build_state` untouched.
  *Priority: med · Effort: small · Bump: patch* — a failed revert can be silently reversed by the next update.

---

- **`3.5.0-B58` — Gate 3 checks consumers against the wrong libLLVM when a compat copy is
  installed.** `toolchain_safety.check_installed_consumer_symbols` takes the lexically first
  `/usr/lib/libLLVM.so.*`; with a compat package such as `llvm21-libs` installed that is the old
  library, and consumers needing the new soname are filtered out, so the unhealable
  dropped-backend check that should trigger rollback passes. `verify._newest_so` already handles
  this ordering; reuse it or pick the shipped soname. Separately, Gate 3 checks clang and lld
  with `--version` only, which the repo's own comments say misses symbol breakage that a real
  compile (as in Gate 1) exposes. Found in the 2026-10-18 whole-repo review; from a reviewer's
  reading of the code, not yet reproduced, so confirm before fixing. Tests: with two libLLVM
  sonames installed, the check uses the new one; Gate 3 compiles a test file.
  *Priority: med · Effort: small · Bump: patch* — Gate 3 can pass a toolchain it should roll back.

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

- **`3.5.0-B60` — the AlreadyBuilt reuse path ignores `-sysforge` renamed artifacts.** On
  makepkg exit 13 `build_core` finds artifacts by upstream pkgname and static version, but a
  `--pgo=use` conflict rename or a kernel local rename produces `<name>-sysforge-*`. A leftover
  stock-named artifact of the same version is installed instead, with no `[reverted]` record, or
  the target fails with "no matching .pkg.tar" although the right files exist. `makepkg_wrapper`
  writes a `.sysforge-built.list` manifest for this case; use it here. Found in the 2026-10-18
  whole-repo review; from a reviewer's reading of the code, not yet reproduced, so confirm
  before fixing. Tests: a renamed already-built artifact is found and installed; a stock-named
  one is not.
  *Priority: med · Effort: small · Bump: patch* — can install the wrong artifact.

---

- **`3.5.0-B61` — the build-succeeded-but-install-failed recovery can install stale packages.**
  The recovery in `makepkg_invoke` globs `*.pkg.tar*` in the PKGBUILD directory only, and `[s]`
  runs `sudo pacman -U --noconfirm` on every match. Without PKGDEST, older versions left there
  are installed too (a downgrade, or unrelated split members); with PKGDEST (the usual setup)
  nothing matches and the recovery never offers itself. It is also gated on the pre-strip flag
  list. Use the same artifact lookup as the build, scoped to this build's version. Found in the
  2026-10-18 whole-repo review; from a reviewer's reading of the code, not yet reproduced, so
  confirm before fixing. Tests: only this build's artifacts are offered, from PKGDEST or the
  build dir.
  *Priority: med · Effort: small · Bump: patch* — can downgrade or install unrelated packages.

---

- **`3.5.0-B62` — `revert-to-stock` on a split member reinstalls the pkgbase instead.** For a
  plain `source_built` entry, `revert_cmd` installs `entry.get("pkgbase") or name`, so
  `revert-to-stock vulkan-radeon` runs `pacman -S mesa` and leaves the source-built
  `vulkan-radeon` installed, then forgets it and records an opt-out. For renamed split sets, an
  installed lone member is replaced with `origin_pkgbase` instead of itself, and a member's
  stock name does not resolve to its `-sysforge` package at all. Reinstall the member names, and
  reverse-map members as well as `origin_pkgbase`. Found in the 2026-10-18 whole-repo review;
  from a reviewer's reading of the code, not yet reproduced, so confirm before fixing. Tests:
  reverting a split member reinstalls that member; a member's stock name resolves.
  *Priority: med · Effort: small · Bump: patch* — revert can leave the source build in place, untracked.

---

- **`3.5.0-B63` — `update --include-stage-owned` rebuilds never converge on flag drift.**
  `build_core.build_and_install` passes no `owner_stage`, so a stage-owned package rebuilt
  through `update` is recorded as an ordinary build (sandboxed, with a PGO-profile input, with
  full CFLAGS) while drift replays it with the stored `owner_stage` (unsandboxed, no profile
  input, kernel flags). Every axis then reports drift forever, and `--rebuild-on-flag-drift`
  rebuilds it on every run. Thread the entry's `owner_stage` into the build options. Found in
  the 2026-10-18 whole-repo review; from a reviewer's reading of the code, not yet reproduced,
  so confirm before fixing. Tests: a stage-owned package rebuilt through update shows no drift
  on the next run.
  *Priority: med · Effort: small · Bump: patch* — repeated needless rebuilds on an opt-in path.

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

- **`3.5.0-B65` — `--cleansrc` deletes the checkout before checking whether it can be
  re-cloned.** `source_sync` runs `purge_src` before the offline check and before the clone's
  freeze check. `update --offline --cleansrc` deletes every clean tree and then reports
  `SKIPPED_OFFLINE` (non-blocking), and `--frozen --cleansrc` deletes the tree before the clone
  raises `NetworkFrozen`, leaving the package unbuildable. Check offline and freeze before
  purging, or refuse the combination in `pre_check`. Found in the 2026-10-18 whole-repo review;
  from a reviewer's reading of the code, not yet reproduced, so confirm before fixing. Tests:
  with `--offline` or a freeze, `--cleansrc` leaves the trees in place.
  *Priority: med · Effort: small · Bump: patch* — deletes source trees it cannot replace.

---

- **`3.5.0-B66` — `[aur] enabled = false` does not stop fetches for `source = "git"`
  checkouts.** The scheduler's AUR gate tests `source == "aur"`, but `"git"` means the same as
  `"aur"` elsewhere, so an existing `git` checkout with an AUR origin is fetched from
  aur.archlinux.org with the AUR disabled (`git_fetch_and_compare` checks only
  `KIND_SOURCE_FETCH`). The `NetPolicy` rule says not to decide this from a label; gate on
  `is_aur_checkout()` instead. A missing `git` checkout fails closed but is reported as
  `FROZEN`. Found in the 2026-10-18 whole-repo review; from a reviewer's reading of the code,
  not yet reproduced, so confirm before fixing. Tests: with the AUR disabled, a `git` checkout
  with an AUR origin is not fetched.
  *Priority: med · Effort: small · Bump: patch* — the AUR switch leaks.

---

- **`3.5.0-B67` — auto-repair downloads code without the source-freeze check.** `auto_repair`
  runs `meson subprojects download` and `git submodule update --init --recursive` on a build
  failure with no `net_policy` check, a code-ingress seam without its own `KIND_*`, so a frozen
  build can download subprojects unattended. It also assumes the srcdir is `<pkgbuild_dir>/src`,
  which is wrong when makepkg's `BUILDDIR` is set (as on this workstation), so the repair fails
  there anyway. Add a `KIND_*` for it and resolve the srcdir via `pacman.get_builddir()`. Found
  in the 2026-10-18 whole-repo review; from a reviewer's reading of the code, not yet
  reproduced, so confirm before fixing. Tests: a frozen build skips the download; the srcdir
  resolves under `BUILDDIR`.
  *Priority: med · Effort: small · Bump: patch* — breaks the freeze guarantee.

---

- **`3.5.0-B68` — the cmake injection validator misses a statement ending in a comment or
  chained command.** `pkgbuild_patcher._cmake_statement_end` and G2 of
  `validate_patched_pkgbuild` work on raw text: after `-DFOO=ON # comment` the injected
  continuation lands inside the comment, so the next `-D...` runs as its own command, and in
  `cmake .. && ninja` the injected flag goes to ninja. G2 only checks that the joined line
  starts with `cmake`, so both pass. Strip comments before anchoring and stop the statement at
  `&&`, `||` and `;`. Found in the 2026-10-18 whole-repo review; from a reviewer's reading of
  the code, not yet reproduced, so confirm before fixing. Tests: both shapes are either injected
  correctly or refused by G2.
  *Priority: med · Effort: small · Bump: patch* — a patched PKGBUILD can break or misplace flags unnoticed.

---

- **`3.5.0-B69` — a DIVERGED auto-reset discards local commits written by someone else.**
  `git_ops` classes local commits whose author email differs from `user.email` as upstream's,
  which counts as clean and allows `reset --hard`. A fix the user applied with `git am` or
  cherry-picked from an AUR comment keeps its original author, so it is reset away when upstream
  moves (recoverable only from the reflog). Compare the committer email (`%ce`) instead. Found
  in the 2026-10-18 whole-repo review; from a reviewer's reading of the code, not yet
  reproduced, so confirm before fixing. Tests: an applied commit with a foreign author but the
  user's committer email blocks the reset.
  *Priority: med · Effort: small · Bump: patch* — silently drops the user's applied patches.

---

- **`3.5.0-B70` — a non-`RuntimeError` failure skips the pipeline's failure handling.**
  `pipeline/runner.run_pipeline` catches only `NotImplementedError`, `BootstrapRebootRequired`
  and `RuntimeError`. `configure`'s `_chroot` uses `subprocess.run(check=True)`, and copies and
  removals raise `OSError`, so those end in a raw traceback with the stage left `running` and no
  `--resume` guidance printed. Wrap them as `RuntimeError` or catch `Exception` at the stage
  boundary. Found in the 2026-10-18 whole-repo review; from a reviewer's reading of the code,
  not yet reproduced, so confirm before fixing. Tests: a `CalledProcessError` in a stage marks
  it failed and prints the resume hint.
  *Priority: med · Effort: small · Bump: patch* — lost diagnostics and misleading state.

---

- **`3.5.0-B71` — every archinstall run enables root SSH login with a password.**
  `archinstall_config` always sets `PermitRootLogin yes` and `PasswordAuthentication yes` and
  enables `sshd`, and an omitted `!root-password` or `!password` falls back to `""`. These are
  VM-fixture defaults, but they apply to real hardware too, so a `bootstrap.toml` without
  passwords installs a machine reachable as root over SSH. Gate the SSH settings on a config key
  and refuse an empty root password while root login is enabled. Found in the 2026-10-18
  whole-repo review; from a reviewer's reading of the code, not yet reproduced, so confirm
  before fixing. Tests: a real-hardware config without the key gets no root SSH login; an empty
  password is refused.
  *Priority: med · Effort: small · Bump: patch* — an insecure default on installed machines.

---

- **`3.5.0-B72` — configure trusts `username` and `target` from `bootstrap.toml`.**
  `_bootstrap.py` validates other fields but not `username` or `target`. `configure` removes
  `<target>/home/<username>/sysforge-pkg`, passes the name to `chown -R` and into a `bash -lc
  "cd ... && ..."` run with `sudo -u`, so a username with `/`, `..` or shell characters points
  the removal elsewhere or injects into that shell, and an empty or relative `target` makes the
  removals relative to the working directory. Validate the username against
  `^[a-z_][a-z0-9_-]*$` and require an absolute, mounted `target`. Found in the 2026-10-18
  whole-repo review; from a reviewer's reading of the code, not yet reproduced, so confirm
  before fixing. Tests: invalid usernames and a relative or empty target are refused up front.
  *Priority: med · Effort: small · Bump: patch* — hardening for an operator-supplied file.

---

- **`3.5.0-B73` — packages added to `packages.toml` after a pipeline's first run are skipped on
  resume.** `PipelineState.init_package_list` seeds `remaining` only when both `remaining` and
  `built` are empty, so a manifest entry added after the first run is in the package list but
  never in `remaining`, and the packages stage skips it and reports success. Add names that are
  in no bucket to `remaining` on resume. Found in the 2026-10-18 whole-repo review; from a
  reviewer's reading of the code, not yet reproduced, so confirm before fixing. Tests: a package
  added between runs is built on `--resume`.
  *Priority: med · Effort: small · Bump: patch* — a new package is silently never installed.

---

- **`3.5.0-B74` — `run toolchain --dry-run` takes the stage lock and writes the sentinel.**
  `toolchain/stage.py` enters `sentinel_scope` unconditionally and guards only its body on
  `options.dry_run`. A dry run writes `stage_in_progress.toml`, refuses while a real run holds
  the lock, and an interrupted dry run leaves a stale sentinel for a mutation that never began.
  Use `nullcontext()` under `--dry-run`, as the PGO lock above it does. Found in the 2026-10-18
  whole-repo review; from a reviewer's reading of the code, not yet reproduced, so confirm
  before fixing. Tests: a dry run writes no sentinel and does not take the lock.
  *Priority: med · Effort: small · Bump: patch* — a preview can block the next real run.

---

- **`3.5.0-B75` — two toolchain repo installs arm the sentinel before checking sudo.** The
  repo-mode install in `toolchain/stage.py` and the Gate 1 install in `toolchain/gates.py` open
  `sentinel_scope` and call `pacman.install_repo_pkgs` without `sudo_session.authenticate()`
  first, unlike the kernel stage and the from-source install (`3.5.0-B30`). A timed-out or
  declined prompt leaves a sentinel whose recovery replaces the toolchain although nothing was
  installed. Authenticate before the scope. Found in the 2026-10-18 whole-repo review; from a
  reviewer's reading of the code, not yet reproduced, so confirm before fixing. Tests: an
  unauthorized sudo leaves no sentinel on either path.
  *Priority: med · Effort: small · Bump: patch* — a spurious recovery prompt on the next run.

---

- **`3.5.0-B76` — Gate 3 rollback is refused when the snapshot names a package that was never
  installed.** `gates.snapshot_suite` keys the snapshot by every suite and built name, and
  `cached_pkg_files_for` maps a name that is not installed to `None`. `rollback_to_snapshot`
  treats any `None` as a missing archive and refuses the whole restore, so a run that adds a new
  subpackage or lib32 member cannot roll back after a Gate 3 failure, although everything that
  was installed is cached. Skip names that were not installed before the run. Found in the
  2026-10-18 whole-repo review; from a reviewer's reading of the code, not yet reproduced, so
  confirm before fixing. Tests: a snapshot with a never-installed name still restores the
  installed ones.
  *Priority: med · Effort: small · Bump: patch* — a restorable system is left on the sentinel path.

---

- **`3.5.0-B77` — privileged directory provisioning follows symlinks in the shared state tree.**
  `fs_provision.ensure_writable_dir` falls back to `sudo install -d`, `sudo chgrp sysforge` and
  `sudo chmod 2775` on the path when it is not writable, and all three follow a symlink. A group
  member who replaces a provisioned directory under the shared state tree with a symlink gets
  those changes applied to the target by another user's sudo. `lstat` and refuse symlinks before
  escalating. Found in the 2026-10-18 whole-repo review; from a reviewer's reading of the code,
  not yet reproduced, so confirm before fixing. Tests: a symlinked path is refused before any
  sudo call.
  *Priority: med · Effort: small · Bump: patch* — a group member can redirect another user's sudo.

---

- **`3.5.0-B78` — a `-sysforge` renamed VCS package is not treated as VCS.**
  `update_common._is_vcs` tests the pkgbase suffix (`-git` and similar), so a renamed
  `foo-git-sysforge` is not VCS: `--devel` skips `evaluate_vcs_pkgver`, compares the static
  pkgver seed against the installed `r1234...`, reports a downgrade and never rebuilds. Strip
  the rename suffix (`install_reconcile`'s inverse) before the test. Found in the 2026-10-18
  whole-repo review; from a reviewer's reading of the code, not yet reproduced, so confirm
  before fixing. Tests: a renamed `-git` package is evaluated as VCS.
  *Priority: med · Effort: small · Bump: patch* — a renamed VCS package stops updating.

---

- **`3.5.0-B79` — kernel FDO re-records keep the previous round's profile files.**
  `fdo._plan_record` rewrites `round.toml` but leaves `propeller_cc_profile.txt`,
  `propeller_ld_profile.txt` and `kernel.afdo` from an earlier round, so a `use --propeller`
  without a fresh capture passes `kernel_fdo.require_profile` with a profile from another
  binary, and an old `.afdo` masks the AutoFDO version-drift warning. Remove the stale files
  when a record round starts. Found in the 2026-10-18 whole-repo review; from a reviewer's
  reading of the code, not yet reproduced, so confirm before fixing. Tests: after a re-record,
  `use` refuses until a new capture exists.
  *Priority: low · Effort: small · Bump: patch* — applies a profile that does not match the binary.

---

- **`3.5.0-B80` — integer and hex `[[kconfig]]` values are written as quoted strings.**
  `kernel/kconfig.format_kconfig_line` quotes every value other than `y`/`m`/`n`, so
  `CONFIG_NR_CPUS = "64"` becomes `CONFIG_NR_CPUS="64"`, which kconfig rejects for int and hex
  symbols, and the entry is dropped with only an advisory warning. Write numeric values
  unquoted. Found in the 2026-10-18 whole-repo review; from a reviewer's reading of the code,
  not yet reproduced, so confirm before fixing. Tests: decimal and `0x` values are written bare;
  strings stay quoted.
  *Priority: low · Effort: small · Bump: patch* — a manual kconfig entry is silently ignored.

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

- **`3.5.0-B82` — `sysforge fetch` keys sync requests on the directory name.** `fetch.py` builds
  `SyncRequest.pkgbase` from `pkgbuild_dir.name`, which the freeze, VCS check and dedup then
  use, where the rules require the authoritative pkgbase; it fails closed for a renamed
  directory. `fetch` also exits 0 on `FROZEN` or `PURGE_REFUSED`. Read the pkgbase from the
  PKGBUILD and fail the exit code on blockers. Found in the 2026-10-18 whole-repo review; from a
  reviewer's reading of the code, not yet reproduced, so confirm before fixing. Tests: a renamed
  checkout uses its pkgbase; a frozen result exits non-zero.
  *Priority: low · Effort: small · Bump: patch* — a rule violation that fails safe.

---

- **`3.5.0-B83` — an AUR RPC 429 or 503 does not set the shared rate-limit penalty.**
  `aur.aur_info` catches `HTTPError` as `URLError` and returns `{}`, and
  `rate_limit.http_get_with_rate_limit` is never used, so a throttled RPC is followed by one git
  fetch per package against the same host, contrary to the module's promise that a `Retry-After`
  on either path locks both. Route the RPC through the rate-limited helper. Found in the
  2026-10-18 whole-repo review; from a reviewer's reading of the code, not yet reproduced, so
  confirm before fixing. Tests: an RPC 429 with `Retry-After` defers the following fetches.
  *Priority: low · Effort: small · Bump: patch* — hammers the AUR after it asked for a pause.

---

- **`3.5.0-B84` — `read_built_upstream_commit` assumes the srcdir is `<pkgbuild_dir>/src`.**
  `vcs_pkgver.read_built_upstream_commit` looks under the PKGBUILD directory, but with
  `BUILDDIR` set the srcdir is `$BUILDDIR/<pkgbase>/src`, so it returns `None`, no
  `built_upstream_commit` is recorded, and the `--devel` ls-remote short-circuit never fires.
  Resolve it via `pacman.get_builddir()`. Found in the 2026-10-18 whole-repo review; from a
  reviewer's reading of the code, not yet reproduced, so confirm before fixing. Tests: with
  `BUILDDIR` set, the commit is read from the build root.
  *Priority: low · Effort: small · Bump: patch* — fails safe; costs every devel run a full sync.

---

- **`3.5.0-B85` — two members of one split AUR dependency are built twice.** `aur_resolve` keys
  `visited` by pkgname, so `foo-git` and `foo-libs-git` from one pkgbase become two dependencies
  with the same PKGBUILD, and `build/aur_deps.py` does not dedup by path; the second hits
  AlreadyBuilt and installs again. Dedup by PKGBUILD path. Found in the 2026-10-18 whole-repo
  review; from a reviewer's reading of the code, not yet reproduced, so confirm before fixing.
  Tests: two members of one pkgbase build once.
  *Priority: low · Effort: small · Bump: patch* — wasted build and a redundant install.

---

- **`3.5.0-B86` — the PGP-key auto-repair can offer the wrong key.** `auto_repair`'s regex takes
  the first 16 to 40 hex characters after "No public key", but gpg prints the key ID before that
  line, so a later commit or checksum hash is offered for import. Match the `using ... key <id>`
  line. Consent-gated, so the harm is a wrong prompt. Found in the 2026-10-18 whole-repo review;
  from a reviewer's reading of the code, not yet reproduced, so confirm before fixing. Tests:
  real gpg failure output yields the key ID it names.
  *Priority: low · Effort: small · Bump: patch* — a confusing prompt.

---

- **`3.5.0-B87` — the run log can lose or misplace lines written from other threads.**
  `log._write_to_files`, `write_pkg_log` and the close functions take no lock, so a line from a
  worker thread (the profraw merge daemon, the sudo keepalive) can hit a closed file and be
  dropped, land between `close_unified_log`'s truncate and its cleared marker, or race
  `_stamp`'s date-marker bookkeeping (`3.5.0-F5`). Guard the handles with one lock. Found in the
  2026-10-18 whole-repo review; from a reviewer's reading of the code, not yet reproduced, so
  confirm before fixing. Tests: concurrent writes during close leave either the line or a clean
  cleared file.
  *Priority: low · Effort: small · Bump: patch* — log fidelity only.

---

- **`3.5.0-B88` — `bootstrap.toml` is readable in the target before it is restricted.**
  `configure` copies the config tree with `copytree`, preserving the source's 0644, and chmods
  the file to 0600 afterwards, so its plaintext passwords are briefly readable, and the ISO-side
  original stays readable. Copy it into a 0700 directory with a restrictive umask. Found in the
  2026-10-18 whole-repo review; from a reviewer's reading of the code, not yet reproduced, so
  confirm before fixing. Tests: the copied file is never world-readable.
  *Priority: low · Effort: small · Bump: patch* — a brief local exposure of install passwords.

---

- **`3.5.0-B89` — two makepkg.conf scrubs miss values that come only from the system conf.** In
  `makepkg_conf.emit_makepkg_conf`, the full-LTO scrub strips only keys the profile already
  overrides, so `-flto` in a system-conf `CFLAGS` reaches a PGO pass whose profile sets none;
  and the lld-only strip is gated on the profile owning `LDFLAGS`, so a system `--icf=` reaches
  a non-lld linker. The other scrubs resolve the effective value first; do the same here. Found
  in the 2026-10-18 whole-repo review; from a reviewer's reading of the code, not yet
  reproduced, so confirm before fixing. Tests: system-conf `-flto` and `--icf=` are scrubbed
  when the profile sets no flags.
  *Priority: low · Effort: small · Bump: patch* — only bites on a hand-edited system makepkg.conf.

---

- **`3.5.0-B90` — the `--nodeps` probe can fail on a supersede conflict and skip the
  relaxation.** `pacman.deps_broken_by_install` probes with `pacman -U --print` without the
  `--ask=4` the real install adds for a superseding package, so a batch that is both a drop-in
  replacement and held by an exact sibling pin can fail the probe on the conflict, return
  nothing, and not add `--nodeps`, leaving the `3.1.0-B14` deadlock in place. Pass the same
  answers to the probe. Found in the 2026-10-18 whole-repo review; from a reviewer's reading of
  the code, not yet reproduced, so confirm before fixing. Tests: a superseding batch held by an
  exact pin gets `--nodeps`.
  *Priority: low · Effort: small · Bump: patch* — fails safe; the install just stops.

---

- **`3.5.0-B91` — the progress bar keeps ninja totals within a step that opens no tracker.**
  `ui/progress.heartbeat` forgets ninja totals on tick, tracker entry and exit, step and phase
  changes (`3.5.0-F6`), but builds that run inside one step with no tracker and no phase change
  (the `--bolt-only` repackage) are measured against the largest earlier total. Clear the totals
  when a build starts (`makepkg_invoke`'s first heartbeat). Found in the 2026-10-18 whole-repo
  review; from a reviewer's reading of the code, not yet reproduced, so confirm before fixing.
  Tests: a second build in the same step reads its own percentage.
  *Priority: low · Effort: small · Bump: patch* — a wrong percentage on the bar.

---

- **`3.5.0-B92` — an unmounted path drops its artifact-review decline permanently.**
  `artifacts.IgnoreList.load` prunes entries whose path does not exist, and the next save
  persists that, so a decline for a file on an unmounted filesystem is lost and the file is
  offered again once it returns. Keep entries whose filesystem is absent. Found in the
  2026-10-18 whole-repo review; from a reviewer's reading of the code, not yet reproduced, so
  confirm before fixing. Tests: a missing path's entry survives a load-and-save.
  *Priority: low · Effort: small · Bump: patch* — a repeated prompt.

---

- **`3.5.0-B93` — a failed derename revert clears the sentinel with the package removed.** In
  `derename` mode `revert_cmd` removes the renamed package and then installs the stock one; if
  that install fails it returns exit 1 without raising, so `sentinel_scope` clears the sentinel
  and the system is left without the package and no recovery prompt, with `build_state` and the
  opt-out not updated. Raise so the sentinel keeps the recovery. Found in the 2026-10-18
  whole-repo review; from a reviewer's reading of the code, not yet reproduced, so confirm
  before fixing. Tests: a failed stock install after the removal keeps the sentinel.
  *Priority: low · Effort: small · Bump: patch* — a half-done revert with no recovery prompt.

---

- **`3.5.0-B94` — dead helpers that only tests reach.** Static analysis found functions no
  production code calls: `config._rewrite_profiles_default_toolchain`,
  `pacman.collect_makedeps`, `pkgbuild_patcher.load_extracted_profile`,
  `profile.requires_llvm_toolchain` (the LLVM-only rule is enforced elsewhere),
  `rate_limit.http_get_with_rate_limit` (see the RPC entry),
  `toolchain_safety.check_link_resolution`, `diagnostics.from_toolchain_check` and
  `from_fix_suggestion`, `local_repo._split_pkgfile`, `pacman_hooks.needs_provision`,
  `deprecations.all_deprecations` and `update_summary._arrow` (whose docstring claims a test
  surface it no longer has), plus `aur_clone`'s unused `depth` parameter, which contradicts the
  full-history rule. Remove them or wire them up. Found in the 2026-10-18 whole-repo review;
  confirmed in the code. Tests: the suite still passes; `vulture --min-confidence 60` lists none
  of them.
  *Priority: low · Effort: small · Bump: patch* — cleanup.

---

- **`3.5.0-B95` — strict pyright findings.** Pyright in strict mode reports: `@contextmanager`
  functions annotated `-> Iterator[...]` (deprecated; use `Generator`) in `progress_hooks`,
  `stage_sentinel`, `sudo_session` and `ui/progress`; `failed_stage` in `makepkg_invoke`
  narrowed to `None` because it is set in a closure (annotate it); `verbs/shared.py` putting
  `None` into a `list[str]` before filtering it; and dead comparisons or `isinstance` checks in
  `reconfigure`, `toolchain/stage`, `build_fingerprint`, `dep_analysis`, `llvm_state`,
  `makepkg_env`, `rate_limit` and `update`. None changes behaviour. Found in the 2026-10-18
  whole-repo review; confirmed in the code. Tests: pyright strict (with the unknown-type rules
  off) reports none of these.
  *Priority: low · Effort: small · Bump: patch* — type hygiene.

---

- **`3.5.0-B96` — the install stage does not refuse the disk the live system runs from.**
  `install.py` and `_partition_plan.py` probe the target device's size and ask for confirmation,
  but do not refuse a `device` that backs the running ISO or `/`, holds a mounted partition, or
  is not a block device; only an `lsblk` failure stops it. A typo in `device` relies on the y/N
  prompt alone. Refuse those devices before the prompt. Found in the 2026-10-18 whole-repo
  review; from a reviewer's reading of the code, not yet reproduced, so confirm before fixing.
  Tests: a device backing `/`, a mounted one and a non-block path are each refused.
  *Priority: low · Effort: small · Bump: patch* — the prompt is the only guard against wiping the wrong disk.

---

- **`3.5.0-B97` — read-only `update` runs still save build state.** `--dry-run`,
  `--explain-drift` and `--versions` run `_reconcile_external_demotions` and
  `sync_with_installed(...).save()` in `_phase_init`, so a preview can demote packages and
  rewrite `build_state.toml`, against the `_read_only_run` contract. Skip the save (and the
  demotion) on read-only runs. Found in the 2026-10-18 whole-repo review; from a reviewer's
  reading of the code, not yet reproduced, so confirm before fixing. Tests: a dry run leaves
  `build_state.toml` byte-identical.
  *Priority: low · Effort: small · Bump: patch* — a preview mutates state.

---

- **`3.5.0-B98` — Gate 3 promises a consumer rebuild that is not queued.** `toolchain/stage.py`
  logs "std:: re-export drift (healable by the queued consumer rebuild)" also when
  `rebuild_soname_consumers = off`, where nothing is queued. Word it by mode, or point at the
  manual rebuild when it is off. Found in the 2026-10-18 whole-repo review; from a reviewer's
  reading of the code, not yet reproduced, so confirm before fixing. Tests: with the mode off,
  the message names the manual rebuild.
  *Priority: low · Effort: small · Bump: patch* — misleading output.

---

- **`3.5.0-B99` — the cache report shows `0/0 hits` for sandboxed builds.** A sandboxed build
  never reaches the host's compiler caches: `build_sandbox._HOST_ONLY_BUILDENV` forces
  `!ccache`/`!distcc` and `_ENV_EXPORT_DENY` drops `RUSTC_WRAPPER`/`CCACHE_DIR`/`SCCACHE_DIR`
  (`3.2.0-B2`). `makepkg_wrapper` still probes the host caches before and after each build
  (`probe_ccache`/`probe_sccache`), so the difference is always zero, and `--cache-report` prints
  `ccache 0/0 hits (n/a) cache=5.0 GiB` per package and in the totals. That reads as a cache that
  went unused, when it was never in the path, and `cache=` is the size of the host cache, not
  anything the build touched. Record a sandboxed build as not measured (no difference) and
  render it as `n/a (sandboxed, host cache not used)`. Leave it out of the ccache/sccache totals,
  and update the `--cache-report` help and the design text. Seen in the 2026-10-18 `update` run
  log (every package sandboxed, every line `0/0`); confirmed in the code. Tests: a sandboxed
  build records no difference and renders the sandboxed note; a host build still reports its
  difference; totals count host builds only.
  *Priority: med · Effort: small · Bump: patch* — misleading output on the default-on sandbox path.

### Documentation

The user docs explain tasks; these items add the general concepts behind them, so the docs
teach how the techniques work beyond sysforge. Shared shape for every page under
`docs/concepts/`: explain how it works and when it is (and isn't) worth doing, without
sysforge vocabulary; cite upstream sources (Arch wiki, LLVM, GCC, kernel docs); quote no
performance numbers measured on one machine (upstream figures, or how to measure); end with a
short "In sysforge" section linking the guide. Guides link the concept pages instead of
re-explaining them, and the README lists the section beside the guides.

- **`3.5.0-DOC3` — concept page: profile-guided and post-link optimization (pilot).** Start
  `docs/concepts/` with the topic people search for most. Instrumented PGO vs. sampled profiles
  (AutoFDO), Propeller, BOLT; what each changes (inlining and block layout vs. function and
  block order in the binary); why the training workload decides the result; profile staleness;
  when none of them pay off. Move pgo.md's "The idea in one minute" and the method-choice
  background here, leaving pgo.md the tasks. Sets the section's layout, the README link and the
  conventions above for the pages after it.
  *Priority: med · Effort: medium · Bump: patch*

- **`3.5.0-DOC4` — concept page: shared libraries and ABI.** Sonames, symbol versioning, what
  "ABI-compatible" means, why a library's soname bump forces its consumers to rebuild, and how
  C++ standard-library symbols leak through another library's exports, using libLLVM's std::
  re-exports as the worked case. drift.md and the toolchain guide's audit sections link it.
  *Priority: med · Effort: medium · Bump: patch*

- **`3.5.0-DOC5` — concept page: how an Arch package is built.** PKGBUILD and its arrays,
  makepkg and `makepkg.conf`, pkgbase vs. split packages, BUILDDIR and PKGDEST, `.SRCINFO`,
  `-git` packages and pkgver(), source signatures and checksums. The base most other pages
  assume.
  *Priority: med · Effort: medium · Bump: patch*

- **`3.5.0-DOC6` — concept page: compiler optimization basics.** What `-O2`/`-O3` change,
  `-march=native` and the portability it gives up, full vs. thin LTO, why the linker matters
  (lld, `-fno-plt`), and what the hardening flags cost and buy. Profiles in the README and
  `profiles.toml` comments link it.
  *Priority: med · Effort: medium · Bump: patch*

- **`3.5.0-DOC7` — concept page: bootstrapping an optimized compiler.** Why building a compiler
  with itself takes stages, what instrumented, training and optimized passes each produce, why
  the live toolchain must never be half-replaced, and how a PGO+BOLT build differs from a plain
  stage-2 bootstrap. The toolchain section of pgo.md links it.
  *Priority: low · Effort: medium · Bump: patch*

- **`3.5.0-DOC8` — concept page: kernel builds and boot basics.** Kconfig and
  `olddefconfig`, modules vs. built-in, initramfs, boot loaders and entries (systemd-boot
  sort-key vs. filename order), and why a fallback kernel matters. kernel.md and
  boot-entries.md link it.
  *Priority: low · Effort: medium · Bump: patch*

- **`3.5.0-DOC9` — concept page: measuring performance honestly.** Run-to-run noise, warm
  vs. cold caches, repeated runs and spread, what a before/after comparison needs to hold
  fixed, and `perf` basics. pgo.md's "Did it help?" and `bench toolchain` docs link it.
  *Priority: low · Effort: small · Bump: patch*

- **`3.5.0-DOC10` — glossary.** One page defining the terms the docs use (pkgbase, split
  package, PKGDEST, soname, LTO, profdata, training workload, pass, sentinel, drift), each with
  a link to the concept page or guide that covers it, so pages link a term instead of
  re-explaining it. Grows with each concept page.
  *Priority: low · Effort: small · Bump: patch*

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

### Open questions
