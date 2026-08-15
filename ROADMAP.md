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
`1.2.0-Q1` (open question), `1.2.0-STD1` (standards). The version prefix is the
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
right after a release. Run `make next-id TYPE=F` (or `B`/`Q`/`STD`) — it reads the
current `pyproject.toml` version, scopes to that cycle's counter, and prints the next
free ID (e.g. `2.4.0-F1`). `make check-standards` also flags collisions and
active-cycle sequence gaps.

Within each subsection, entries are kept in **ascending ID order** (by type
counter, then version) — sort on every add so the list stays scannable. Every
entry opens `- **`<ID>` — <title sentence>.**` and is separated from its
neighbour by a `---` rule, so a single item is readable on its own rather than
running into the next one. `docs/release-notes/` entries use the same two rules.

**Open questions (`Q`) must be resolved before any implementation.** A `Q`
entry is undecided by definition; investigation/spikes to inform the decision
are fine, but before writing production code the question must first be either
**promoted** to a proper `F`/`B`/`STD` entry (which then follows the normal
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
| `3.0.0-B1` | stage-owned advisory is blind to pinned repo checkouts | med | small | patch |
| `3.0.0-B3` | a killed bootstrap leaves the target with passwordless root | med | small | patch |
| `3.0.0-B5` | the ungated-source warning never fires on stage builds | low | small | patch |
| `2.5.1-F3` | state failed --clear-all emits no SYSFORGE_TARGET | low | small | patch |
| `2.6.1-F28` | artifact review --all: bulk-adopt every offerable candidate | low | small | patch |
| `2.6.1-F29` | colour-code the update version-check verdicts | low | small | patch |
| `2.6.1-F27` | Install stage target-root change summary | low | medium | patch |
| `3.0.0-F1` | Preflight the Rust toolchain when the kernel fragment requests CONFIG_RUST | low | medium | patch |
| `2.6.1-F21` | one home for replacing an existing config file | low | large | patch |
<!-- END roadmap-table -->

### Features

- **`3.0.0-F1` — Preflight the Rust toolchain when the kernel fragment requests `CONFIG_RUST`.**
  The kernel stage never authors kconfig itself — it merges hardware, device and manual
  `[[kconfig]]` entries into `sysforge.config` and lets the PKGBUILD's `prepare()` overlay them
  (`stages/kernel.py` module docstring). That is the right division of labour, and `CONFIG_RUST=y`
  should stay a user decision, not a sysforge default: the in-tree Rust drivers cover none of the
  tested-hardware scope (§Tested hardware — x86_64/Zen 3/NVMe), and the Nvidia path here is an
  out-of-tree DKMS module that kernel Rust does not touch. What is missing is the *precondition*
  check. `CONFIG_RUST` is unique among kconfig symbols in depending on host tooling rather than on
  the kernel source: `scripts/rust_is_available.sh` demands a `rustc` inside the version window the
  tree pins plus a matching `bindgen`, and when it fails kconfig drops the symbol during
  `olddefconfig` — the silent-loss shape the shipped `kconfig_plan.VERIFY` slot (`2.6.1-F23`)
  *detects*. This item is the other half: refuse to start the build rather than discover the loss
  from a warning after it. Two things make the
  failure likelier here than upstream assumes — `RUSTC_WRAPPER=sccache` and a rustup install that
  shadows the distro `rust` (the live workstation case, already documented in
  `primitives/rust_probe.py`), so the `rustc` the kernel resolves is frequently not the one the
  operator thinks. The seam exists: `primitives/toolchain_preflight.py` already probes rustc with
  pin awareness (`_probe_rust_native`, `rust_toolchain_pins`), and `doctor.py`'s `rust` axis already
  reports rustup-shadow provenance. Add a requirement token (`rust:kernel`) contributed when the
  merged fragment carries `CONFIG_RUST=y`, resolving to a `rustc` + `bindgen` presence and
  version-window probe, and surface the shadow provenance in the failure message rather than a bare
  "not found". Design decisions: **where the version window comes from** (parse the tree's
  `scripts/min-tool-version.sh` / `rust_is_available.sh`, versus a table sysforge maintains and must
  chase — the former is the only one that stays correct across kernel versions) and whether this is
  a hard preflight failure or a loud warn, given the stage's existing preference for warning over
  hard-failing a curated-table entry. Dual-toolchain parity applies: the check must behave
  identically on the gcc and llvm kernel paths, since `CONFIG_RUST` is orthogonal to `LLVM=1`.
  *Priority: low · Effort: medium · Bump: patch* — no behaviour change unless a fragment actually
  requests `CONFIG_RUST`; it converts a post-build silent drop into a pre-build refusal.
  **Standards home on adoption:** none new — this extends the existing toolchain-preflight seam.

---

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

- **`2.5.1-F3` — `state failed --clear-all` emits no `SYSFORGE_TARGET`.** Follow-up to `2.4.0-F1`,
  which gave every sentinel-gated verb a `journal_target` override. `StateFailedVerb.journal_target`
  keys only on `args.clear` (single pkgbase → `pkg:<name>`), so a `--clear-all` invocation — which is
  equally sentinel-gated because it rewrites `build_state.toml` — falls to `None` and emits the verb
  name alone. This is the one spot where 2.4.0-F1's "every mutating verb supplies a meaningful target"
  goal isn't met. Emit a subjectless `mode:` target (e.g. `journal.mode_target("failed-clear-all")`)
  on the clear-all path, and add the gcc/llvm-independent dual test (clear-all → `mode:...`, alongside
  the existing single-clear and no-clear cases in `test_journal.py`). No new seam; the standards row 20
  scope note already covers per-verb `SYSFORGE_TARGET`. *Priority: low · Effort: small · Bump: patch* — observability
  completeness; additive, no correctness impact.

---

- **`2.6.1-F21` — one home for replacing an existing config file.** Six sites hand-roll the same
  stage-to-temp-then-install shape with three different spellings and two different privilege
  idioms: `env_persist.apply_write`, `reconfigure._save_sysforge_toml_ui`, the `makepkg.conf` writer
  in the same module (line ~1181), `makepkg_conf.py`, `pacman_hooks.py`, and `artifacts.write_live`
  — the last of which alone uses the §22 seam correctly (`run_privileged` + `install -Dm`) while the
  first three call `subprocess.run(privileged_argv(["cp", …]))` raw. None of them is atomic: every
  one truncates-then-writes, so a kill or power loss mid-write leaves a truncated file. Add
  `primitives/atomic_write.py` as the sole seam for *replacing a file that already exists*: resolve
  symlinks first (`write_text` writes through a symlink, `os.replace` would clobber a dotfile-manager
  link with a regular file), stat the destination for mode/owner, stage the temp **in the
  destination's directory** so `rename(2)` stays same-filesystem, then `os.replace` on the direct
  path and `run_privileged(["install", "-m/-o/-g", …])` + rename on the escalated one. Convert the
  first four writers together — fixing one and not the others leaves the weaker guarantee in place
  while implying otherwise — and fold their raw `privileged_argv` calls into `run_privileged`.
  Deliberately **out of scope: `artifacts.write_live`**, which creates new files with a class-defined
  mode rather than replacing user-owned config, so it has no destination mode to preserve and no
  symlink to honour; folding it in would force the helper to grow a second mode. Add the
  symlink-preservation and mode/owner-preservation regression tests none of these writers has today.
  Severity note for whoever implements: the file carrying irreplaceable content here is the **user's
  shell rc**, not `/etc/environment` — pam_env parses the latter line-oriented and skips malformed
  lines with a warning, so a truncated `/etc/environment` silently loses env vars rather than
  blocking login. *Priority: low · Effort: large · Bump: patch* — six call sites plus coverage that
  does not exist yet; behaviour-preserving on every success path. **Standards home on adoption:**
  none new — §22's privilege seam row already covers the `run_privileged` conversion.

---


- **`2.6.1-F27` — Install stage target-root change summary.** Builds on `2.6.1-F24`; **built last,
  and deferrable.** The install stage pacstraps into a target root via `archinstall --silent`, so
  it has no before-state and every row is an addition (`— → ver`) — a manifest plus a total size.
  Two obstacles: `pacman.get_all_installed_packages()` (`pacman.py:735`) queries the live root with
  no `root=` parameter and needs an optional one; and **no target-root path is modelled anywhere in
  sysforge** — `archinstall_config.py` describes only per-partition `mountpoint` values, and neither
  `install.py` nor `archinstall_invoke.py` records the mount root, so resolution falls to an explicit
  `--target-root`/config value or a `findmnt` probe at stage end. The real risk is timing rather than
  path resolution: the after-snapshot must run while the target is still mounted, and the current
  code does not establish whether archinstall leaves it mounted when `install.py` returns. On any
  failure this emits F24's `UNKNOWN` outcome with an explicit reason — never a silent no-op, never a
  fabricated count. If the mount lifetime proves hostile, drop this item; F24–F26 stand alone.
  *Priority: low · Effort: medium · Bump: patch* — the one piece carrying implementation
  uncertainty, isolated here so it cannot hold up the rest.

---

- **`2.6.1-F28` — `artifact review --all`: bulk-adopt every offerable candidate.** Adoption is
  one-file-at-a-time: `artifacts.adopt` takes a single `src`, and `ArtifactReviewVerb.execute`
  reaches it only through a per-candidate `prompt_choice` loop. Pointing sysforge at a directory
  that is already a curated set — `~/.local/bin` on a workstation that has accumulated dozens of
  hand-written scripts — therefore costs one keystroke per file, with no way to say "all of these,
  I wrote them". Add `--all` to `artifact review`: iterate the same `iter_offerable(registry,
  ignore)` result and call `adopt` on each, printing the adopted name/class per line and a final
  count. Reusing that one composition point is the whole point — the exclusion rules (package-owned,
  sysforge-owned, already-managed, declined-and-unchanged) stay in a single home and cannot drift
  between the interactive and bulk paths. Three specifics the implementation must honour: (1)
  `--all` **skips `OWNER_UNKNOWN` candidates** unless `--include-unknown` is also passed —
  `owner == unknown` means `pacman.owners_of` returned no verdict for that path, so the file may in
  fact be package-owned and a blind bulk adopt is exactly where that mislabel does damage; the
  interactive path can surface `[ownership unknown]` and let a human decide, the bulk path cannot.
  (2) It **respects the `IgnoreList`**, same as the interactive loop — a recorded decline is a
  decision, and `--all` is a convenience over the offer set, not an override of it. (3) It runs
  off-TTY, replacing today's list-and-hint branch when the flag is given, since it needs no prompt.
  Per-candidate `ArtifactError` logs and continues, mirroring the existing loop. Low risk by
  construction: adoption is copy-only (`requires_sentinel = False`), touches no live file, and is
  undone by `artifact remove --purge`. Tests: bulk adopt across mixed classes/roots; unknown-owner
  skipped by default and adopted with `--include-unknown`; ignored candidate not adopted; off-TTY
  bulk path; a mid-run adopt failure not aborting the rest. Completions (`_sysforge` + bash) and
  the manpage move in the same change. *Priority: low · Effort: small · Bump: patch* — additive
  flag on an existing verb, no new seam and no change to the per-file adopt contract.
  **Standards home on adoption:** none new.

---

- **`2.6.1-F29` — colour-code the `update` version-check verdicts.** A `sysforge update --devel`
  run over a large `-git` set produces one summary line per package, and nothing distinguishes the
  handful that are actually eligible to rebuild from the wall that are not — the reader parses
  `NEEDS_REBUILD`/`UP_TO_DATE`/`DOWNGRADE` as text. Colour the action tag in
  `update_summary._print_summary`: green for `NEEDS_REBUILD`/`NEEDS_PACMAN_UPGRADE`, yellow for
  `UP_TO_DATE`/`DEVEL`, red for `DOWNGRADE` and the failure actions (`DEVEL_EVAL_FAILED`,
  `PULL_FAILED`, `RATE_LIMITED`, `PURGE_REFUSED`, `SKIPPED_NO_CHECKUPDATES`). The colour map is a
  second dict keyed by the same action strings as `_ACTION_FORMATS`, applied through
  `render.tag_header` (an optional `color=` argument) so the `[TAG]` gutter keeps its one home and
  no site hand-writes an escape — §Logging/Colour's `log.use_color()` gate then applies for free.
  **The gutter is the only correct surface, and the reason is load-bearing.** The obvious target —
  the `[VERSION]` INFO line at `primitives/version.py:26` — cannot take colour: `log.info` builds
  its file-log text as `plain = f"[SYSFORGE][INFO]{tag} {message}"` from the caller's own message,
  and only `_format_line` (level + tag decoration) consults `use_color()`. Colour embedded in a log
  *message* is therefore ungated and lands verbatim in `sysforge-update.log`, which is why every
  existing `log.green`/`red`/`dim` call site sits on a `ui()`/`print()` path and none on
  `info()`/`warn()`. `_print_summary` uses bare `print()` and never reaches a file, so it is
  colour-safe by construction. Two adjacent fixes belong in the same change: (1) that same
  `version.py` INFO line logs the two operands *before* comparing and names no package, so it is
  near-useless in a log — give it its result (`vercmp 'a' 'b' -> 1`); (2) the invariant this item
  discovered is unwritten — `docs/design/12-logging.md`'s Colour section says `log.py` is the single
  colour authority but not that **message bodies passed to `error`/`warn`/`info`/`debug` must stay
  uncoloured, because the file-log path bypasses the gate**. State it there. Tests: a colour-forced
  summary asserting the per-action SGR, and `NO_COLOR` yielding the current plain output byte for
  byte. *Priority: low · Effort: small · Bump: patch* — presentation only, one dict and one
  optional argument; no change to the action taxonomy or to what `update` decides.
  **Standards home on adoption:** none new — row 5 (`NO_COLOR`/`FORCE_COLOR`) already governs the
  gate this rides on.

### Bugs

- **`3.0.0-B5` — the ungated-source warning never fires on stage builds.**
  `warn_ungated_sources` (`primitives/net_policy.py`) is wired only at `build_core.py:836`, so a
  frozen run warns about `source=()` entries the freeze cannot mediate for ordinary packages but
  says nothing for toolchain and kernel builds, which reach `makepkg_wrapper` without passing
  through `build_core`. Those are precisely the builds where an unmediated `source=()` fetch
  matters most — they run longest, pull the most code, and are the ones a user is most likely to
  leave running unattended under `--frozen` believing the freeze covers them. The gap is silent:
  the freeze itself still holds at all five gated seams, so nothing fails; the user simply is not
  told what remains uncovered. Fix by hoisting the call to a seam both paths cross, rather than
  duplicating it at each stage — a second call site would drift the same way the frozen-exit check
  did before `3.0.0-F2` centralised it.
  *Priority: low · Effort: small · Bump: patch* — advisory only, no enforcement change.
  **Standards home on adoption:** none new.

---

- **`3.0.0-B3` — a killed bootstrap leaves the target with passwordless root.**
  `pipeline/stages/configure.py:370-373` writes `/etc/sudoers.d/99-sysforge-bootstrap-build`
  granting the build user `NOPASSWD: ALL`, because `makepkg -s` must sync makedeps
  non-interactively while building sysforge itself inside the target. The `finally` at `:423`
  removes it, which covers exceptions and non-zero exits — but not `SIGKILL`, an OOM kill, or
  power loss mid-build. Those leave a freshly installed system carrying an unconditional
  passwordless-root rule for its primary user, in a file the user has no reason to look for.
  Low likelihood, maximum blast radius, and the failure is silent: the install otherwise looks
  incomplete rather than insecure. Two independent fixes, both cheap and worth taking together —
  scope the rule to what it actually needs (`NOPASSWD: /usr/bin/pacman`) so even a leaked drop
  is not root-equivalent, and sweep for stale `99-sysforge-bootstrap-*` drops at bootstrap entry
  so a `--resume` after a kill cleans up its predecessor. A `pre_check` refusal is the wrong
  shape here: the sweep must run on the path that would otherwise re-create the file.
  *Priority: med · Effort: small · Bump: patch* — narrow trigger, but the residue is a
  privilege-escalation primitive and the fix touches one stage.
  **Standards home on adoption:** none new — the privilege seam (`primitives/privilege.py`)
  does not cover sudoers *provisioning*, only invocation.

---

- **`3.0.0-B1` — stage-owned advisory is blind to pinned repo checkouts.**
  `update._detect_stage_owned_updates` exists to tell the user when a package it skipped
  (`owner_stage` set) has upstream movement waiting on `run toolchain` / `run kernel`. It resolves
  the candidate version through `_check_one_pkgbase`, which for a `repo_class = "source"` entry
  compares the installed version against `pkgbuild_ver` — the version parsed from the **local**
  PKGBUILD. Stage-owned packages are excluded from the walk and therefore from `_sync_sources`, so
  for a `source = "repo"` checkout that tree is a detached-HEAD pin (`source_sync._pin_repo_checkout`)
  which only ever advances *inside* the owning stage's own run. Installed version and local
  `pkgbuild_ver` are the same value by construction, the check never yields `NEEDS_REBUILD`, and no
  advisory is emitted — the one case the feature was built for. Observed live: `spirv-llvm-translator`
  sat at 22.1.2-1 while `extra` carried 22.1.5-1, and the intervening `update` run printed only the
  `info`-level "skipping 8 toolchain-stage package(s)" line with no "Stage-owned updates available"
  block. The user-visible signal degrades to pacman's own `warning: … ignoring package upgrade`,
  which names the symptom but attributes it to `IgnoreGroup = sf-build` rather than to a stage that
  has not run. AUR-sourced stage-owned packages are unaffected (RPC supplies the upstream version
  without a sync). Fix in the advisory path only — for stage-owned entries classified `source =
  "repo"`, read the candidate from pacman's sync DB (`source_sync.get_pacman_sync_version`, already
  the authority the pin itself targets) instead of the un-synced local PKGBUILD; leave the walk's
  exclusion and the pin's lifecycle untouched, since syncing stage-owned sources from `update` is
  exactly the double-processing the `owner_stage` skip exists to prevent. Test both origins (repo
  entry → advisory from sync DB; AUR entry → advisory from RPC, unchanged).
  *Priority: med · Effort: small · Bump: patch* — advisory-only; no build or install behaviour changes.
