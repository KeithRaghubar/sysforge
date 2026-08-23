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
| `3.1.0-F4` | a first run should confirm before it changes anything, and setup should offer to persist that posture | high | medium | major |
| `3.0.0-B1` | stage-owned advisory is blind to pinned repo checkouts | med | small | patch |
| `3.0.0-B3` | a killed bootstrap leaves the target with passwordless root | med | small | patch |
| `3.1.0-B4` | a timed-out sudo prompt at install time is reported as a kernel-stage failure | med | small | patch |
| `3.1.0-B5` | the kernel stage has no sudo keepalive, so its final install prompt goes stale | med | small | patch |
| `3.1.0-F2` | no supported way to feed last run's failures back into a retry | med | small | minor |
| `3.1.0-F5` | the first run toolchain / run kernel on an install changes the system with no nudge to read the config that drives it | med | small | patch |
| `3.1.0-F1` | a clean diagnostics axis reports nothing, so it reads as a broken axis | med | medium | minor |
| `3.1.0-F3` | no way to declare an AUR-free posture; update reaches for the AUR unconditionally | med | medium | minor |
| `3.0.0-B5` | the ungated-source warning never fires on stage builds | low | small | patch |
| `2.5.1-F3` | state failed --clear-all emits no SYSFORGE_TARGET | low | small | patch |
| `2.6.1-F28` | artifact review --all: bulk-adopt every offerable candidate | low | small | patch |
| `2.6.1-F29` | colour-code the update version-check verdicts | low | small | patch |
| `2.6.1-F27` | Install stage target-root change summary | low | medium | patch |
| `3.0.0-F1` | Preflight the Rust toolchain when the kernel fragment requests CONFIG_RUST | low | medium | patch |
| `3.0.0-F5` | itemize the flag-triggered pacman -Syu in the result summary | low | medium | patch |
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

- **`3.1.0-F1` — a clean diagnostics axis reports nothing, so it reads as a broken axis.**
  `sysforge doctor system --graphics` on a healthy NVIDIA/Wayland workstation prints one `[INFO]`
  line (`session_type`) and `1 finding(s), 0 error(s)` — indistinguishable from an axis whose
  probes all bailed out. That is not a graphics bug: every probe in
  `primitives/graphics_probe.py` returns `None` on success (`_check_nvidia_module_loaded`,
  `_check_multilib_enabled`, `_check_mesa_llvm_symbols`, …), and `_check_session_type` is the lone
  always-INFO probe, so the visible output is an accident of which check happens to be
  unconditional rather than a report. The shape is framework-wide — `diag.Axis`
  (`primitives/diagnostics.py:181`) is a bare callable returning findings, with no notion of which
  checks ran, which were vendor-gated out (`gpu_vendors` empty ⇒ silently skipped), and which
  passed; `render_axis` can therefore only print `clean_msg`. So the user cannot distinguish
  *checked and healthy* from *skipped because the probe found no `lspci`/`lsmod`/`pacman.conf`* —
  and the vendor-gated skips are the ones most likely to hide a real detection failure upstream of
  the check. Fix at the framework seam, not per-probe: let a probe report a `ran`/`skipped(reason)`
  outcome alongside its optional finding, have `Axis` accumulate the roster, and render it under
  `-v` (per the §Logging rubric — the roster is narration, the findings are the answer), leaving
  default output unchanged. Doing it in `graphics_probe.py` alone would fork the axis contract that
  `toolchain`, `hardware`, `cache`, `rust` and `gfxperf` all share.
  *Priority: med · Effort: medium · Bump: minor* — observability gap that is currently active on a
  real system; touches the shared `diagnostics` contract and every probe module's return type, but
  adds no new checks and changes no default-verbosity output.
  **Standards home on adoption:** none new — row 25 (logging levels) already governs where the
  roster prints.

---

- **`3.1.0-F2` — no supported way to feed last run's failures back into a retry.**
  `sysforge state failed` already knows the exact set a user wants to retry with different flags
  (`state_cmd.py:608`, `StateFailedVerb`), and both `build` and `update` take multiple positional
  package names — but the two cannot be composed, because `state failed` renders only a padded
  human table (PKGBASE / FAILED_AT / SIGNATURE / ERROR) through the pager. There is no
  machine-readable output mode anywhere in the CLI: no `--format`, `--json`, `--porcelain`, or
  `--plain` on any verb. The retry loop after a partial `update` — three `cosmic-*` packages fail
  on `makepkg` exit 4, and the user wants exactly those three rebuilt with `--makepkg=-f
  --interactive` — therefore has no supported spelling. The workarounds are both bad: scraping the
  table with `awk` couples a shell one-liner to column padding, and reading `build_state.toml`
  directly (`[packages.<pkgbase>]` entries carrying `failed_at`) couples it to a schema
  `primitives/build_state.py` documents as internal. Add a bare-names mode to `state failed` —
  pkgbases only, one per line, no header, no pager, implying `--no-pager` — so
  `sysforge build $(sysforge state failed --quiet)` is the supported spelling. Scope it to this
  verb rather than opening a CLI-wide `--format` axis: `state failed` is the one place whose output
  is *already* a set of package names, and a general machine-output standard is a much larger
  decision than this gap needs. The flag name matters — the global `--quiet` (verbosity 0) already
  exists, so reuse it only if the verb-local meaning composes with it, otherwise pick a distinct
  name rather than overloading it.
  *Priority: med · Effort: small · Bump: minor* — a gap that is currently active on a real system
  (three failures recorded right now with no supported retry path); new flag plus a render branch,
  no change to build_state or to the existing table.
  **Standards home on adoption:** none new — but if this ever grows into a CLI-wide machine-output
  mode, that *would* need a row; keep this one verb-local so it does not pre-empt that decision.

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

- **`3.0.0-F5` — itemize the flag-triggered `pacman -Syu` in the result summary.** Phase 6.5 has two
  routes into the same `pacman -Syu` since `3.0.0-F4` decoupled it from `repo_mode`, and only one of
  them can report what it did. The classified route builds `pacman_upgrade_pkgs` from results the
  version-check stamped `NEEDS_PACMAN_UPGRADE` (`update_version.py`), each carrying an
  `installed_ver`/`pkgbuild_ver` pair, so `update_summary._print_result_summary` renders one
  `pkgbase: old → new` line per entry. The flag/config route (`--sysupgrade` / `[build]
  system_upgrade`) deliberately does no widened walk and no `checkupdates` probe — it hands the whole
  transaction to pacman — so the only fact in `ResultSummary` is the `system_upgrade_ran` boolean and
  the renderer falls through to the single line `system upgrade (pacman resolved the transaction)`.
  That line is honest but strictly less useful than what a `-Syu`-by-hand shows, and it is the common
  case for anyone who turns `system_upgrade` on: the managed set rarely holds `build_mode = "pacman"`
  entries, so the classified list is usually empty precisely when the flag route fires. Add a
  reporting-only version capture for the flag route so both presentations itemize. Design decisions:
  **where the version pairs come from** — a `checkupdates` probe *before* the transaction (cheap, one
  subprocess, but it is a second resolver whose answer can disagree with what pacman actually did,
  and it reintroduces exactly the probe F4 removed), versus reading `pacman -Qi`-style state or the
  pacman log *after* the transaction (authoritative — it reports what happened rather than what was
  predicted — but needs a parse seam that does not exist yet); the latter is the better fit for a
  renderer whose contract is reporting facts. Also **whether the block stays capped or prints in
  full** (a stock `-Syu` can be hundreds of packages, unlike the managed classified list) and whether
  the capture is unconditional or earns a flag. The renderer stays presentation-only — the capture
  belongs in `_build_result_summary`'s assembler, feeding the existing `pacman_upgrade_pkgs` +
  version-map fields so the `elif system_upgrade_ran` fallback narrows to the genuinely-unknown case
  (offline, or a failed transaction with nothing to read back). `render.version_pair` already owns
  the `old → new` vocabulary; do not re-inline it. *Priority: low · Effort: medium · Bump: patch* —
  reporting only, no change to what the transaction does, and the existing single-line output stays
  as the fallback.
  **Standards home on adoption:** none new — extends the existing result-summary renderer.

---

- **`3.1.0-F5` — the first `run toolchain` / `run kernel` on an install changes the system with no
  nudge to read the config that drives it.** These are the two stages whose behaviour is decided
  almost entirely by their TOML rather than by the flags the user typed — `etc/sysforge/toolchain.toml`
  picks the compiler and the PGO/BOLT passes, `etc/sysforge/kernel.toml` picks the kconfig fragments
  and initramfs handling — so a user who ran `setup` and then types `sysforge run kernel` gets a
  kernel built from defaults they have never opened. The existing first-install advisory does not
  cover this: `primitives/init_notice.py` keys on a marker the PKGBUILD `post_install` scriptlet
  drops and names only the `reconfigure`/`hardware` bootstrap stages (`_REQUIRED_STAGES`), retiring
  itself the moment both are `done` — i.e. it clears *before* anyone reaches toolchain or kernel.
  Emit a one-time advisory when the stage has never completed on this install, naming the config
  file, the two or three settings most likely to surprise, and how to review them. No new state is
  needed: `PipelineState.stage_status()` (`pipeline/state.py:237`) already returns `"pending"` for a
  stage never seen, and both entry points (`run_stage_standalone`, `pipeline/runner.py:161`, and
  `run_pipeline`, `:217`) hold the state object before the stage executes. Two decisions to settle
  first: whether "first run" means never-*completed* (a failed first attempt re-advises — preferred)
  or never-*attempted*, and whether this rides `init_notice.py`'s module (one home for first-run
  advisories) or stays at the pipeline seam where the state already is. Keep it strictly advisory —
  print, never prompt, never block, never raise, matching `init_notice.py`'s stated contract; a
  prompt here would inherit every unattended-consent problem `3.0.0-F3` and `3.1.0-F4` are about.
  Relates to `3.1.0-F4`, which is the stronger version of this idea — a default-on gate that renders
  the resolved plan and asks once. If F4 lands, this is superseded, because a rendered plan *is* the
  config made visible; it is filed separately because F4 is a `major` bump blocked on the runtime
  TOML-writer seam, and this is a patch-bump advisory that can land immediately.
  *Priority: med · Effort: small · Bump: patch* — a gap that is currently active on a real system
  (nothing points a fresh install at `kernel.toml` before it builds a kernel); one advisory helper
  plus a call at the stage seam, no new state, no change to what either stage does.
  **Standards home on adoption:** none new — row 25 (logging levels) governs the level, and
  `init_notice.py` is the existing home for first-run advisory text if the helper lands there.

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

---

- **`3.1.0-B4` — a timed-out sudo prompt at install time is reported as a kernel-stage failure.**
  `pipeline/stages/kernel.py:2274` opens `sentinel_scope` around the install → mkinitcpio →
  bootloader mutation window, and the first statement inside it is `install_built_packages`
  (`primitives/makepkg_wrapper.py:351`), which runs `sudo pacman -U` with inherited stdio. When the
  operator leaves a multi-hour build unattended, sudo's `passwd_timeout` fires and **sudo exits
  non-zero without ever executing pacman** — nothing was installed, no file on the system was
  touched. `makepkg_wrapper.py:374` cannot tell that apart from a genuine pacman failure and raises
  `RuntimeError`, which propagates out of the scope; by contract (`stage_sentinel.py:371`) any
  exception leaves the sentinel in place, so the next invocation is blocked by
  `check_and_recover_stale_sentinel` demanding the recorded `recovery_cmd`
  (`_kernel_recovery_command()`, the mkinitcpio line). The operator must therefore run a pointless
  mkinitcpio purely to clear a sentinel guarding a mutation that never began, just to get back to
  the final install. The sentinel is behaving correctly — it simply cannot distinguish "the
  mutation window was entered and abandoned mid-way" (kernel installed, initramfs missing →
  unbootable, the case it exists for) from "authentication never succeeded, so the window was never
  really entered". Fix by acquiring credentials *before* entering the scope: probe with the
  allowlisted `sudo -v` auth probe (explicitly out of scope for the privilege seam per
  `primitives/privilege.py`'s docstring), and on failure abort cleanly with a message naming what
  happened and the plain re-run (`sysforge run kernel`) — no sentinel written, no recovery ritual.
  A successful probe caches the timestamp, so the `pacman -U` inside the scope does not re-prompt.
  Keep the classification narrow: only a pre-install auth failure is a clean abort; a `pacman -U`
  that actually ran and failed must keep leaving the sentinel. Pairs with `3.1.0-B5`, which stops
  the prompt from timing out in the first place; this entry is the safety net for when it still
  does. Test both branches (auth probe fails → no sentinel, distinct message; probe succeeds and
  pacman fails → sentinel retained, existing B7 stale-package guidance intact).
  *Priority: med · Effort: small · Bump: patch* — one stage, no change to the mutation window
  itself or to the sentinel contract.
  **Standards home on adoption:** none new — row 18 (privilege seam) already carves out auth
  probes; this adds a call site, not a mechanism.

---

- **`3.1.0-B5` — the kernel stage has no sudo keepalive, so its final install prompt goes stale.**
  `pipeline/stages/toolchain.py:1170` runs `_sudo_keepalive_daemon` — a background thread
  refreshing `sudo -v` every `_SUDO_KEEPALIVE_INTERVAL` — precisely so the install at the end of a
  2+ hour PGO sequence does not re-prompt an operator who has walked away. The kernel stage builds
  for just as long and installs the same way (`install_built_packages` calling sudo directly from
  the sysforge process, so the same timestamp entry applies), but has no equivalent: credentials
  authenticated at stage entry have long expired by the time the build finishes, so the run stops
  on a password prompt that then times out. `primitives/makepkg_invoke.py:690` shows how routinely
  this bites — it carries a whole interactive "Built packages found — build likely succeeded but
  install failed (sudo timeout?) … [s]udo re-auth and install" recovery path, which the kernel
  stage's split build/install shape bypasses entirely. Fix by giving the kernel build the same
  keepalive coverage, but lift the daemon out of `toolchain.py` into a shared primitive (one home)
  rather than copying it — a second copy is exactly the drift the one-home invariants exist to
  prevent, and a third caller (`build_core`'s long batch builds) is plausible later. The daemon
  must remain best-effort and non-fatal: a failed refresh warns, as it does today, and the stage
  still reaches `3.1.0-B4`'s clean abort if the prompt is ultimately unanswered.
  *Priority: med · Effort: small · Bump: patch* — behaviour-preserving extraction plus one new
  caller; no change to what is built or installed.
  **Standards home on adoption:** none new — `sudo -v` is an auth probe, already outside the
  privilege seam (row 18); the new module is the seam for *credential lifetime*, not escalation.
