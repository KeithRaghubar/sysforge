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
convention) stays `STD`. A `DEV` item **files no release-note entry**: release notes
are user-facing and their sections drive the derived SemVer bump. Its squash-commit
subject carries the ID and is its only record, which `make next-id` and the gap
check read so a shipped `DEV` number is never reissued. `make check-standards`
rejects a `DEV`-filed release-note entry.

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
| `3.0.0-F3` | update's PKGBUILD review gate is silent in exactly the unattended case | high | medium | major |
| `3.1.0-F4` | a first run should confirm before it changes anything, and setup should offer to persist that posture | high | medium | major |
| `3.1.0-F3` | no way to declare an AUR-free posture; update reaches for the AUR unconditionally | med | medium | minor |
| `3.2.0-F18` | build inputs applied outside the resolved profile never register as drift, so changing them leaves stale packages installed silently | med | medium | minor |
| `3.2.0-F19` | an interactive build's failure reason is never logged | med | medium | minor |
| `3.4.0-F3` | an opt-in, DKMS-safe compile-time kernel hardening set | med | medium | minor |
| `3.4.0-F4` | a revert or forget is remembered, so update doesn't quietly re-adopt the package | med | medium | minor |
| `3.4.0-F5` | sixteen doctor axes still can't say which checks they skipped | low | medium | minor |
<!-- END roadmap-table -->

### Features

- **`3.0.0-F3` — `update`'s PKGBUILD review gate is silent in exactly the unattended case.** The
  review gate (`primitives/pkgbuild_review.py`) is the codebase's existing supply-chain control: it
  diffs the full source tree from the recorded `reviewed_commit` to HEAD — catching changes hiding
  in `.install` files, patches and new sources, not just the PKGBUILD — and asks for a decision.
  But it auto-accepts on two paths, and their intersection is the hole: non-interactive runs (stdin
  or stdout not a TTY) auto-accept so an unattended `sysforge update` cannot hang, and `update`
  itself passes `interactive=False` by default so routine batch updates stay unattended. The result
  is that a cron/timer-driven `update` builds and installs arbitrary changed sources having only
  *logged* the decision. That is the precise scenario the recent AUR malware incidents exploit, and
  `3.0.0-F2` does not cover it — a freeze stops code *arriving*, this is code that already arrived.
  The fix is not to prompt in a non-TTY (that reintroduces the hang the auto-accept exists to
  prevent) but to make unattended runs **refuse** rather than assume: a policy knob whose strict
  setting turns a would-be prompt into a per-package blocker, reported in the summary and exited
  non-zero, so the operator reviews and re-runs. Design decisions to resolve first: **whether the
  strict setting is the new default** (a behaviour change for existing unattended users, hence the
  major-bump framing) or opt-in beside `[security] freeze_sources`; whether `--no-review` should
  remain able to disable the gate wholesale under a strict policy, or be demoted to a per-run lift
  like `--thaw`; and whether the blocker reuses `pkgbuild_review`'s existing `DECISION_*` vocabulary
  or earns its own status. Should reuse `3.0.0-F2`'s blocker-reporting path rather than growing a
  second one. *Priority: high · Effort: medium · Bump: major* — under the strict default an
  unattended `update` that previously completed now stops on any changed source, which is a
  breaking behaviour change for automation; it is also the larger of the two holes, since it fires
  on code already on disk.
  **Standards home on adoption:** none new — extends the existing review-gate seam.

---

- **`3.1.0-F3` — no way to declare an AUR-free posture; `update` reaches for the AUR unconditionally.**
  The per-package machinery for a repo-and-local-only user is already complete and deliberate:
  `source` is a first-class classification (`"repo" | "aur" | "git" | "local"`) settable in
  `packages.toml`, persisted in `build_state.toml` rather than re-derived each run
  (`primitives/build_state.py:184`), and honoured by the scheduler — `source_sync.py:279`
  short-circuits `"local"` to `STATUS_SKIPPED_LOCAL` because a hand-maintained PKGBUILD has no
  remote to sync against. `repo_mode = "build_from_source"` covers repo packages, and one
  `sysforge build <pkg>` is enough to put anything under `update`'s maintenance without it. What is
  missing is the *posture*: nothing lets a user say "never touch the AUR" once. The `[security]`
  section offers only `freeze_sources`, a blanket network freeze that also denies repo checkouts and
  source fetches — the wrong instrument; `[aur]` (`sysforge.toml:48`) tunes politeness
  (`min_fetch_interval_ms`, `rate_limit_abort_s`) but never abstention; and `--offline` gets there
  only by disabling every version check too. Worse, `update.py:646` calls `fetch_aur_name_cache()`
  on every non-`--offline` run *before* inspecting what is actually managed, so a user with zero
  AUR packages still generates AUR RPC traffic on every update. The policy half has a seam that
  already anticipates this: `net_policy.py:46-48` splits `KIND_AUR_CLONE` from
  `KIND_REPO_CHECKOUT` precisely because "a future policy may permit one while denying the other" —
  an AUR-free posture is that policy, not a new mechanism. The eager name-cache warm is the harder
  half, since it sits upstream of `NetPolicy.check()` entirely; gate it on whether anything managed
  actually carries `source = "aur"` (build_state already knows) rather than routing it through the
  policy. Filing this commits sysforge to the AUR-free user as a supported persona, which is the
  real decision here — the code is largely already behaving as if it were.
  *Priority: med · Effort: medium · Bump: minor* — additive posture plus one gating condition; no
  existing default changes and no package's routing behaviour moves.
  **Standards home on adoption:** none new — the egress-kind vocabulary in `net_policy.py` is the
  existing home, and this extends it rather than adopting an external spec.

---

- **`3.1.0-F4` — a first run should confirm before it changes anything, and `setup` should offer to persist that posture.**
  A new user's first `sysforge update` can rebuild and reinstall an arbitrary number of packages
  with no upfront confirmation. Nothing in the CLI gates it: `--dry-run` shows the plan but is a
  separate invocation the user has to know to run first, and the `[build] review` gate
  (`packages.toml:90`, default `true`) is a *per-package* prompt that fires only when a package's
  source tree changed since the last accepted build — it is a supply-chain diff review, not a
  "here is the whole batch, proceed?" gate, and it says nothing about the packages whose sources
  are unchanged but which will still be rebuilt and reinstalled. Note also that `--interactive`
  is already spoken for and means two *different* things: on `build` it strips `--noconfirm` and
  hands stdout/stderr to makepkg's terminal (`cli.py:312`), on `update` it pauses on build
  failures for manual correction (`cli.py:441`). Neither is a confirmation gate, and a third
  meaning must not be hung on that flag name.
  Add a real gate — default on — that prints the resolved plan (what will be rebuilt, what will be
  installed, what the batched `pacman -Syu` will touch) and prompts once before any mutation, with
  the existing `--dry-run` output as its body since that computation already exists. Pair it with
  flipping `[security] freeze_sources` from `false` (`sysforge.toml:243`) to `true`, so the
  out-of-the-box posture is "ask before changing, and do not fetch unmediated sources" and the
  permissive behaviour is what a user opts into (`--no-frozen`, and a `--yes`/`--noconfirm`-style
  bypass for the gate).
  The second half is the escape hatch that makes the posture liveable: `setup` (and the
  `reconfigure` stage) should *ask* whether to persist the answers globally and write them to the
  live `sysforge.toml`, so an experienced user turns both off once instead of passing flags
  forever. Two constraints make that non-trivial and are why it belongs here rather than as a bare
  "add a prompt" item. **(a) There is no runtime TOML writer.** tomlkit is a dev-only dependency
  pulled into an ephemeral overlay by `tools/sync_config.py`; the only runtime precedent is
  `config.set_default_toolchain` / `_rewrite_profiles_default_toolchain`
  (`primitives/config.py:752-814`), an anchored line rewrite that exists specifically to avoid
  tomlkit at runtime. A second such key wants that generalised into one seam, not copy-pasted —
  the same "one home" argument as `2.6.1-F21`. **(b) The deprecation registry does not model a
  changed default.** `primitives/deprecations.py` has kinds for removed *surfaces* (`config_key`,
  `state_token`); a default whose *value* changed while the key remains valid has no kind, so
  either the registry grows one or the `freeze_sources` flip is carried by a release-note
  `## Changed` entry plus the prompt alone. Decide that before implementing, not during. `setup`
  currently takes only `--pacman-conf` and does no prompting at all, so the prompt seam is new
  even though `primitives/prompt.py` supplies the primitives (`prompt_choice`, `is_interactive`).
  Relates to `3.0.0-F3`, which fixes the *review* gate's silence under automation — the same
  "unattended runs must not silently consent" principle, one layer down.
  *Priority: high · Effort: medium · Bump: major* — a default-on confirmation gate and a flipped
  `freeze_sources` both change behaviour for every existing install on upgrade; the `setup` prompt
  is the migration path, not a separate convenience, which is why the parts ship together.
  **Standards home on adoption:** none new — but the runtime config-write seam from (a) is a
  candidate "one home" row if a third key ever needs it.

---

- **`3.2.0-F18` — build inputs applied outside the resolved profile never register as drift, so
  changing them leaves stale packages installed silently.** Flag drift (Phase 4.3) compares only
  `serialize_effective_flags`, and `SYSFORGE_KEYS` rightly excludes the scheduling/throttle keys
  that cannot change output. But several settings *do* change the built artifact and reach the build
  through seams the profile never sees: `sysforge.toml [build] python` (the interpreter pinned by
  `makepkg_env.resolve_build_python` — a Python package built against one minor version installs
  into that version's `site-packages`, so a pin change or a system Python bump breaks it with no
  signal), `[mesa] filter_drivers`/`gallium`/`vulkan` and `toolchain.toml [llvm] targets` (both
  patched into the PKGBUILD by `_maybe_patch_mesa_drivers` / `_maybe_patch_llvm_targets`, and both
  auto-detected from the hardware profile, so a GPU swap changes them too — the hardware stage
  reports the diff but nothing links it to a rebuild), and the PGO flag spliced in via
  `effective_flags_extra` (`--pgo=use` / store reuse), which bypasses `flags_string`. Record a
  second per-package field, `build_inputs`, at `_record_build_state` — the resolved values
  themselves, not a hash, so `--explain-drift` can render a per-key diff in `flag_drift.diff_flags`'
  `+added` / `-removed` vocabulary — and re-derive it at drift time through the same resolvers
  (`resolve_build_python`, `resolve_or_detect_mesa_drivers`, `resolve_or_detect_llvm_targets`), the
  B11 one-function-both-sides rule. For Python, record the interpreter's `major.minor` rather than
  its path so a patch-level system update does not drift every package. Surface it as a sub-axis of
  flag drift (same reporting, same `--rebuild-on-flag-drift` promotion) rather than a fourth CLI
  axis; a missing field on pre-existing entries reads as "unknown, not drifted" so the upgrade does
  not flag the whole build_state at once. Out of scope: `kernel.toml` kconfig inputs (stage-owned,
  rebuilt explicitly by `run kernel`, and already diffed request-vs-result by
  `_gate2_kconfig_drift`) and the LLVM PGO/BOLT settings the toolchain stage owns; LLVM's `targets`
  lands only in the build-state-wide fold, reported with the owning-stage hint like any other
  stage-owned entry. Tests: record/re-derive parity per input, the missing-field no-drift upgrade
  path, python `major.minor` normalisation, and one gcc-path + one llvm-path case for the PGO input.
  *Priority: med · Effort: medium · Bump: minor* — med because each case is a silent-stale-artifact
  class, the python one reachable by an ordinary distro update; medium because four inputs each need
  a record site and a re-derive site, plus a build_state schema field and the fold/explain plumbing.

---

- **`3.2.0-F19` — an interactive build's failure reason is never logged.** In interactive mode
  makepkg's stdout/stderr go straight to the terminal, so neither the unified run-log
  (`sysforge-run-kernel.log`) nor the per-package log (`sysforge_<pkg>.log`) records why a build
  failed. They record only `Build failed — leaving patched PKGBUILD in place` and `Kernel stage FAILED
  before applying changes`. The 2026-09-28 `3.2.0-B36` failure could only be diagnosed after the
  fact by reading `.config` generations in the build tree. A 2026-08-15 kernel run in the same log
  failed with no reason at all. Tee makepkg's combined output into the per-package log while keeping
  the terminal attached: run makepkg under a pty (the `pty_runner` seam the non-interactive path
  already uses) and copy the stream to the log, so prompts and `nconfig` still work. On failure, repeat
  the last N lines (after `strip_ansi`) in the unified log under `[BUILD]`, and feed them to
  `build_diag._MATCHERS` as the non-interactive path does. Needs a test that a failing interactive
  build leaves its last output lines in both logs.
  *Priority: med · Effort: medium · Bump: minor* — diagnosability; every interactive-stage failure
  currently needs forensic reconstruction.
  **Standards home on adoption:** none.

- **`3.4.0-F3` — an opt-in, DKMS-safe compile-time kernel hardening set.** Promoted from `3.1.0-Q1`,
  which asked whether sysforge should take a position on kernel hardening. The scope boundary is now
  decided. sysforge owns the **compile-time `CONFIG_*` half**, because the kernel stage already decides
  kernel config. The **runtime half stays out of scope**: `/etc/sysctl.d`, kernel command-line
  hardening, and `kptr_restrict`/`unprivileged_bpf_disabled`-style sysctls are system policy, which
  `docs/design/20-scope.md` already excludes. Add a top-level `kernel.toml` key (for example
  `hardening = false`; placed above `[fdo]` per the file's table-header rule, with
  `_KNOWN_TOP_KEYS` extended in `tools/check_shipped.py`). When it is on, the stage merges a curated
  hardening set into the `sysforge.config` fragment at the **lowest** precedence, so manual
  `[[kconfig]]` entries and hardware-profile entries override it with the usual conflict warning, and
  so the set survives `localmodconfig` minimisation like any other fragment entry.
  **The set is DKMS-safe by construction:** it never contains module-signature enforcement
  (`MODULE_SIG_FORCE`) or a lockdown mode that blocks unsigned modules, because on the tested
  hardware (NVIDIA through DKMS) those leave the next boot without a graphics driver. Add a guard on the
  *merged* result, so it also catches a manual entry: if the resolved config enforces module
  signatures or lockdown while `kernel_safety.list_dkms_modules()` is non-empty, refuse before the
  build and name the modules. This follows the `3.0.0-F1` precedent of preflighting a
  `CONFIG_*` requirement rather than proceeding silently. Pick the token list at implementation time from the
  kernel's own self-protection recommendations, checked against the current Arch config. Tokens Arch
  already enables still belong in the set, to protect them from minimisation. Tokens with a
  measurable runtime cost (for example `INIT_ON_FREE_DEFAULT_ON`) go in with a comment, which is
  why the whole set is opt-in. The guide and `kernel.toml` comments state the runtime half is
  deliberately not covered. Tests: the key off leaves the fragment unchanged; the key on merges at lowest
  precedence and a manual entry wins; the DKMS guard refuses on a merged sig-force result with
  modules present and passes without them; and an assertion that the curated set contains no
  sig-enforcement or lockdown token.
  *Priority: med · Effort: medium · Bump: minor* — an additive opt-in with no default change; the
  effort is the merge precedence plus the DKMS guard and its tests, not the token list.
  **Standards home on adoption:** a scope citation for the Arch wiki
  [Security](https://wiki.archlinux.org/title/Security) page in `docs/design/20-scope.md` next to
  `System_maintenance` and `General_recommendations`, which also narrows that file's "security
  hardening as a whole" exclusion to the runtime half. It is **not** a `21-standards.md` row,
  because the page is too broad to enforce as a whole.

- **`3.4.0-F4` — a revert or forget is remembered, so `update` doesn't quietly re-adopt the
  package.** Promoted from `3.2.0-Q2`, which asked what the unit of "stop maintaining this" is.
  The mechanical half has shipped. `3.2.0-B15` fixed the reconcile key, and conflict-mode `revert`
  is already pkgbase-atomic: `plan_revert` collects every installed split member and swaps them all
  in one `pacman -S`, and `verbs/shared.forget_packages` forgets the whole pkgbase. What is still
  missing is a durable **decision**. Today "stop maintaining" means only that the build_state
  record is gone, and `update_assemble` rebuilds the target set every run from independent sources,
  two of which re-adopt a reverted package with no notice: a non-inert `packages.toml` override
  (`behavior_overridden`), and `repo_mode = "build_from_source"`, which pulls in every installed
  repo package and so re-targets anything just reverted to stock. Persist an opt-out record in the
  state dir (its own file, following the `artifacts-ignored.toml` precedent, keyed by pkgbase so a
  split set opts out together). `revert` and `state forget` write it. The update assembly subtracts
  it from `target_names` after every adoption source has been applied, and reports each suppressed
  name once at `info` level, naming the source that would have re-adopted it (for example
  "packages.toml override" or "repo_mode build_from_source") so the user can remove that policy.
  **Only an explicit adoption clears the record**: `build <pkg>`, `update <pkg>` (the
  `explicit_set` path) and `packages add <pkg>`. Passive sources never do. `state list` shows
  opted-out entries so the record can be inspected. `uninstall` writes no record, because a
  removed package is outside the installed set and a later reinstall is a fresh decision.
  **Rejected alternatives:** pkgbase-atomic revert (already shipped as above, so not a choice any
  more) and pkgname-precise revert that refuses when siblings survive (it would hand the user a
  manual step and leave the policy path open). Completions and the man page pick up any new
  `state list` column or flag in the same change. Tests: revert and then update with a non-inert
  override suppresses the package and reports it; the same with `repo_mode =
  "build_from_source"`; an explicit `build`/`update <pkg>`/`packages add` clears the record and
  re-adopts; a split set opts out and clears as one pkgbase; and `uninstall` leaves no record.
  *Priority: med · Effort: medium · Bump: minor* — med because both re-adoption paths are reachable
  by ordinary use (`packages add`, or the source-everything repo mode); medium for a new persisted
  file, the assembly subtraction, three clearing sites and the reporting.
  **Standards home on adoption:** none new; `docs/design/` gains the opt-out record under the
  update-assembly and revert sections.

---

- **`3.4.0-F5` — sixteen doctor axes still can't say which checks they skipped.** `3.1.0-F1` added
  the ran/skipped roster (`diag.Skip`, `Roster`, `AxisResult`) and migrated `graphics` and
  `gfxperf`; every other axis returns a bare finding list, so `-vvv` prints `roster: not reported
  by this axis` for it and a probe that bailed out still looks the same as one that passed.
  Migrate the remaining axes (`toolchain`, `rust`, `cache`, `hardware`, `pacman`, `state`, `boot`,
  `restart`, `storage`, `services`, `audio`, `network`, `integrity`, `distro`, `abi`) one module
  at a time: `_check_*`-shaped probes change only their "could not check" returns to
  `diag.Skip`; inline collectors build a `Roster` directly.
  *Priority: low · Effort: medium · Bump: minor* — observability only; each axis is independent,
  so it can land piecemeal.
  **Standards home on adoption:** none new — row 25 governs where the roster prints.

### Bugs

### Documentation

### Open questions
