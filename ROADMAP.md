# SysForge Roadmap

Planned features/changes and the rationale for purposely-excluded or abandoned
ideas. This is the single tracked home for forward-looking work; **`DESIGN.md`
describes only implemented design** and never carries roadmap IDs.

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
(it records the cycle the item originated in, not its target). IDs appear only here
and in release notes.

Within each subsection, entries are kept in **ascending ID order** (by type
counter, then version) — sort on every add so the list stays scannable.

**Open questions (`Q`) must be resolved before any implementation.** A `Q`
entry is undecided by definition; investigation/spikes to inform the decision
are fine, but before writing production code the question must first be either
**promoted** to a proper `F`/`B`/`STD` entry (which then follows the normal
landing flow) or moved to **Abandoned** with a rationale. Never implement
straight off a `Q`.

---

## Planned

### Bugs

- **`2.1.0-B1` — Build-failure recovery compiler-swap prompt allows a mismatched
  cc/cxx toolchain and hides available options.** The compiler-change retry prompt in
  the recovery menu (`makepkg_invoke._run_recovery_menu`) lets the user pick a `cc` and
  `cxx` from *different* toolchains (e.g. `gcc` cc + `clang++` cxx), which produces an
  incoherent override, and it doesn't surface the full option set. Fix: present the
  toolchain choice as a coherent unit (e.g. `gcc`/`clang`) so cc and cxx always come
  from the same toolchain, and enumerate all available options in the prompt. Persist
  the resulting swap via the existing sole writer
  `profile_writer.write_package_compiler_override` — no parallel path. *Priority: medium
  (an incoherent swap can make the retry fail confusingly).*

- **`2.1.0-B8` — kernel docs subpackage is still built and installed with no-docs the
  default.** With no `docs` key set (default off), the kernel stage still builds and
  installs the `-docs` subpackage (`linux-sysforge-docs-*` observed in a real install).
  The `_resolve_subpackages` / `patch_kernel_subpackages` path is meant to drop docs
  unless opted in; something isn't disabling the docs target (or the disable isn't
  reaching the PKGBUILD's docs `package_*()` split). Fix and add a regression asserting
  the docs subpackage is absent from the built set when the key is unset. *Priority:
  medium (wasted build time + an unwanted installed package).*

- **`2.1.0-B10` — `lib32-vulkan-icd-loader` fails under clang with a misleading
  "compiler not valid" error.** Building the package with the llvm toolchain fails; a
  gcc per-package override works, but the surfaced error reads as though the compiler
  itself is invalid rather than naming the real failure (a lib32/multilib clang flag or
  a package-specific incompatibility). Investigate the actual failure and either fix the
  lib32 clang path or, if genuinely gcc-only, detect it and emit an accurate message
  (and auto-suggest the override) instead of the generic "not valid". Dual-toolchain:
  ship both a gcc-path and llvm-path test. *Priority: medium.*

- **`2.1.0-B11` — `doctor` progress indicator is stuck on "starting…" for the whole
  system-package audit.** The longest doctor axis (installed-package check) shows
  "starting…" for its entire duration with no advancement. Surface the in-progress
  package name if cheaply available, or at minimum a phase-accurate message
  ("auditing installed packages"). Progress messaging only — the axis stays read-only.
  *Priority: low.*

- **`2.1.0-B12` — `update` warns `mesa-sysforge: no sync-DB candidate` after mesa was
  reinstalled from the Arch repos.** `[SYNC] mesa-sysforge: no sync-DB candidate —
  leaving checkout on main (testing-track)` fires even though mesa is now the stock repo
  package. Either the external-reinstall demotion
  (`BuildState.reconcile_external_installs`) isn't firing for mesa, or the sync path
  looks up the wrong DB name for a `-sysforge`-suffixed source tree. Reconcile so a
  repo-reinstalled package stops emitting the no-candidate sync warning. *Priority: low
  (noise, but signals stale tracking state).*

- **`2.1.0-B13` — self-install marker write fails with EACCES during
  `update --devel --install-only`.** `[RECONCILE] could not record self-install marker
  (non-fatal): [Errno 13] Permission denied:
  '/var/lib/sysforge/sentinels/self-install'`. The sentinels dir is provisioned
  `root:sysforge` setgid 2775 via `fs_provision.py`, so a group-member write should
  succeed — either the dir/mode drifted or the process isn't in `sysforge`. Confirm the
  provisioning invariant holds for this path and make the marker write land (or
  downgrade the message if the marker is legitimately root-only here). *Priority: low
  (explicitly non-fatal, but points at a provisioning gap).*

- **`2.1.0-B14` — toolchain `reuse_unchanged = true` doesn't skip an unchanged
  rebuild.** Two consecutive `run toolchain --cleansrc` runs both execute the final
  build stage; run 2 should compute the same `build_fingerprint` and skip. Either the
  fingerprint isn't stable across runs (a volatile input — check `_SCHEMA` inputs and
  whether `--cleansrc` perturbs the hash) or the reuse gate isn't consulted on this
  path. Fix so an unchanged second run reuses the built artifacts. *Priority: medium
  (defeats the whole reuse optimization on the most expensive stage).*

- **`2.1.0-B15` — toolchain stage numbering is inconsistent between docs and progress
  output.** Some docs/log strings use the internal `1a/1b, 2, 3` numbering while the
  progress indicator and other sections use `1, 2, 3, 4`. Consolidate on the user-facing
  `1, 2, 3, 4` scheme across the progress indicator, `kernel`/`toolchain` log strings,
  and the design source (`docs/design/*` + `make design`), keeping any internal
  sub-pass detail as a parenthetical rather than a competing scheme. *Priority: low
  (confusing but cosmetic).*

- **`2.1.0-B17` — interactive kernel build proceeds without ever showing the `nconfig`
  menu.** With `interactive = true` (the kernel-stage default) the build starts and runs
  to completion with no kconfig prompt — the operator never gets the intended
  review/edit menu. This is a *behavior* bug, not the documentation issue originally
  filed here: the design is that on the interactive path
  `pkgbuild_patcher.patch_kernel_kconfig_apply` (called from `makepkg_wrapper.py:488`)
  injects a `make nconfig` step (guarded by a TTY `read` pause) into the PKGBUILD's
  `prepare()` whenever the PKGBUILD has no interactive target of its own — see
  `add_nconfig = interactive and not _INTERACTIVE_KCONFIG_RE.search(text)` at
  `pkgbuild_patcher.py:808`. So the mechanism to guarantee a prompt *exists*; something
  is defeating it in practice. Candidate causes to investigate: (a) the injected
  `if [ -t 0 ]` / `make nconfig` block isn't reaching a real TTY because the makepkg
  subprocess's stdin isn't the controlling terminal on this path (the ncurses UI needs
  the tty, not just inherited stdout/stderr); (b) `_INTERACTIVE_KCONFIG_RE` is matching a
  *commented-out* or otherwise inert interactive target in the stock PKGBUILD, so
  `add_nconfig` computes False and nothing is injected; (c) a configured
  `kconfig_targets` sequence (which suppresses the nconfig injection) is set unexpectedly;
  or (d) the anchor search returns None so the whole fragment/nconfig block is skipped
  with only a warning. Reproduce against the real stock kernel PKGBUILD, identify which
  branch drops the prompt, fix so an interactive run actually opens the menu, and add a
  regression asserting the injected `make nconfig` (or the operator's own interactive
  target) survives into the patched `prepare()` for the no-configured-sequence
  interactive path. *Priority: medium (the interactive default silently doesn't work —
  operators can't review the kernel config).*

### Features

- **`2.1.0-F1` — Collision-proof roadmap ID allocation with release-notes visibility.**
  ROADMAP has no view into `docs/release-notes/`, so a shipped ID (e.g. a landed
  `2.1.0-B2`) and a still-open ROADMAP ID can silently reuse the same number — which is
  exactly what happened: open `2.1.0-B2`/`B3` collided with *shipped* `2.1.0-B2`–`B7`
  and had to be hand-renumbered to `2.1.0-B16`/`B17` during triage.
  Add tooling (fold into `make check-standards` or a new `tools/` check) that scans both
  ROADMAP.md and the release-notes accumulator/archive, computes the true high-water mark
  per `<version>-<TYPE>`, and flags any duplicate or gap-jumping ID. Optionally expose a
  "next free ID" helper so triage allocates monotonically. *Priority: medium (prevents
  recurring bookkeeping corruption).*

- **`2.1.0-F2` — Kernel config key to force-enable all hotpluggable device drivers.**
  When minimizing the kernel (e.g. `localmodconfig`), USB and other hotplug-class
  devices absent at build time get dropped, so a device plugged in later is unsupported.
  Add a `kernel.toml` opt-in that re-enables the hotpluggable driver classes as modules
  after minimization, so a slimmed kernel still supports later-attached hardware.
  Boot-safety stays authoritative (this only *adds* modules). Distinct from the
  decided-against proactive `=n` filter (`1.2.0-Q11`). *Priority: medium.*

- **`2.1.0-F3` — Before/after package versions for pacman `-Syu` packages in the update
  summary.** The summary renderer already shows version deltas for source-built
  packages; extend it to include old→new versions for the repo packages pulled in by the
  pacman `-Syu` portion of an update. *Priority: low.*

- **`2.1.0-F4` — Log file for every non-pipeline verb and standalone pipeline stage.**
  There's no log artifact for a `doctor` run (or other non-pipeline verbs), and
  standalone pipeline stages aren't logged either. Extend the unified run-log
  (`log.open_unified_log`; `Verb.unified_log_basename`) so every verb — and a directly
  invoked stage — writes a discoverable log. *Priority: medium (no post-hoc record for
  most verbs today).*

- **`2.1.0-F5` — Global flags to bypass build throttling and to raise priority.** Add a
  global flag that ignores the configured throttle (nice/ionice/cpu_quota/jobs) for this
  run, and a second, stronger flag that runs at *higher* than default priority. Both
  route through the one throttle home (`build_throttle.resolve_throttle`) — no parallel
  throttle path. *Priority: low.*

- **`2.1.0-F6` — Relative `cpu_quota` as a fraction of available cores.** `cpu_quota`
  currently takes an absolute percentage (e.g. `800%`). Allow a fractional form (e.g.
  `0.5`) that sysforge translates against the host's total capacity (16 cores →
  `800%`), so the config is portable across machines. Resolve inside
  `build_throttle.resolve_throttle`. *Priority: low.*

- **`1.2.0-F14` — `doctor`: warn on installed instrumented/incomplete-PGO builds of
  *any* package.** Since `--pgo` works on any package (F5), an instrumented
  (record-stage) build can be left live for any target, not just mesa. Add a
  read-only doctor check that flags a live instrumented build and points at the next
  step (`build <pkg> --pgo=use`, or roll back to the repo package). Detect via the
  existing provenance trail — `build_state.toml` `build_mode` cross-checked against
  store state (a bare `.profraw` with no merged `.profdata` = record-only) — not from
  binaries. *Priority: medium (record-stage builds are unoptimized and transient).*

- **`1.2.0-F15` — `doctor` package-cache axis (from the doctor maintenance-gap audit).** Warn when the
  pacman package cache (`/var/cache/pacman/pkg`, resolve via the proper config home
  rather than hardcoding) exceeds a size threshold, with `paccache -r` /
  `paccache -ruk0` remediation. Distinct from `cache_probe.py`, which only reports
  *build* caches (ccache/sccache). New read-only axis or fold into the `pacman`
  axis. *Priority: medium (the most common real-world disk reclaim).*

- **`1.2.0-F16` — `doctor` mirror-freshness check (from the doctor maintenance-gap audit).** Warn when
  `/etc/pacman.d/mirrorlist` is stale (file age, or newest server's last-sync age)
  so partial-upgrade/slow-mirror situations surface. Read-only, no network call
  (don't probe mirror latency live — that flaps). *Priority: low.*

- **`1.2.0-F17` — Promote the disk-space check into a `doctor` axis (from the doctor
  maintenance-gap audit).** The disk-space check currently lives only in the reconfigure stage
  (`pipeline/stages/reconfigure.py`), so an ad-hoc `sysforge doctor` run misses it.
  **Move** the logic to a probe primitive and consume it from both the stage and a
  new doctor axis (one home — don't duplicate). *Priority: medium.*

- **`1.2.0-F18` — `doctor` fstab integrity (from the doctor maintenance-gap audit).** Flag
  `/etc/fstab` entries whose UUID/label or device no longer resolves (stale mount).
  Read-only. *Priority: low.*

- **`1.2.0-F19` — Broaden the journal scan beyond firmware (from the doctor maintenance-gap audit).**
  `runtime_probe` only greps `journalctl -k -b` for "Direct firmware load … failed".
  Extend it (or add a sibling check on the services/boot axis) to surface
  failed-boot / core-dump / repeated-unit-failure errors. Read-only, current-boot
  scoped. *Priority: low.*

- **`1.2.0-F20` — Rule priority auto-calculation (from the DESIGN roadmap).**
  Auto-calculate a baseline specificity score from rule conditions (mirrors CSS
  specificity: more AND'd conditions = higher weight), with manual `priority`
  override for ties. Deferred until enough real rules exist to validate whether
  auto-priority causes ordering problems in practice. *Priority: low (candidate, not
  a commitment).*

- **`1.2.0-F21` — Configure-stage additions (from the DESIGN roadmap).** btrfs
  snapshot before a build runs; ccache/sccache initialisation check; estimated
  build-time heuristic. *Priority: low (candidate).*

- **`1.2.0-F22` — Graphics runtime debugging refinement (from the DESIGN roadmap).**
  Tighten the graphics/doctor diagnostics surface (exact scope TBD). A candidate
  when revisiting graphics-related code; not blocking. *Priority: low (candidate).*

- **`1.2.0-F28` — User-owned artifact inventory primitive.** Track the user-owned
  system artifacts now scattered across `~/scripts`, `/etc/systemd/system/`,
  `/etc/pacman.d/hooks/`, etc.: a tracked-file inventory, a repo-controlled
  source-of-truth dir, an install/sync command, and drift detection vs the
  filesystem, tied into the existing config/profile/manifest layers. Anything that
  mutates the system stays behind a sentinel-gated verb. Stays inside the boundary in
  DESIGN.md §Scope & Non-Goals (this is steady-state health of a managed system, not
  backup/config-management). *Priority: low (strategic — coarse; decompose before
  building).* **Sub-thread to design: opt-in offering of user-owned systemd
  services and pacman hooks.** The user keeps custom units and hooks on the live
  system; sysforge should be able to *offer* (not force) them — open question whether
  the surface is the `setup` stage, a dedicated verb, or a sync mode of this
  inventory. Discuss the UX before committing; it is a concrete first slice of this
  primitive.

- **`1.2.0-F29` — Basic package management verbs (uninstall / search / revert-to-stock).**
  Sysforge is build-focused but lacks everyday lifecycle verbs. `search` and
  `uninstall` are largely pacman passthroughs, but must account for the build/install
  paths a sysforge package can take: an `uninstall` has to clear the `build_state.toml`
  entry (`state forget`) and any coexist-renamed `-sysforge` package, not just
  `pacman -R`. **`revert-to-stock`** is the genuinely sysforge-specific part — undo a
  source-built/optimized package back to the repo version: reverse coexist-rename,
  forget build state, reinstall the stock pacman package, and reconcile drift gates.
  Route through the Verb framework (`requires_sentinel=True` for the mutating verbs);
  reuse `BuildState.reconcile_external_installs` / `state forget` rather than a
  parallel demotion path. *Priority: medium (rounds out the lifecycle; revert-to-stock
  is a real recovery need).*

- **`1.2.0-F31` — `make install` / `make uninstall` via venv + config symlinks.** Add
  verbose Make targets that install sysforge from the git checkout (venv entry point +
  symlinked config/hooks/completions/manpage) and a matching uninstall, for quick
  trial or fresh dev-env setup **without** going through the AUR and **without**
  permanent system changes. Each target must print every file it creates/removes and
  whether the operation succeeded (idempotent, reversible). Document the option in
  README.md (user-facing install path). Cross-check `make check-shipped` parity so the
  symlinked set matches the packaged set. *Priority: medium (lowers the bar to try
  sysforge / stand up a new workstation).*

- **`1.2.0-F36` — Finish the logging re-levelling audit (interactive stages).** The
  configurable default verbosity (`[log] verbosity` + global `--quiet`) shipped, along with
  the level rubric (DESIGN.md §Logging) and a re-levelling of the day-to-day `build`/`update`
  path (packages stage narration → info; build_core dep-failure cautions → warn) guarded by a
  golden-output test. **Remaining:** sweep the interactive/bootstrap-time stages against the
  same rubric — `reconfigure.py` (94 `ui()`), `configure.py` (47), `kernel.py` (42),
  `toolchain.py` (60), `hardware.py` (19), `partition.py` (14) — demoting progress narration
  to `info()` while keeping prompts/plan-tables/results as `ui()`. Extend the golden guard to a
  representative stage run. *Priority: low (bootstrap-time output, not the day-to-day
  regression, which is resolved).*

### Open questions

- **`2.1.0-Q1` — `doctor`'s `sudo ldconfig` remediation doesn't actually clear the
  warnings it's attached to.** Most benign doctor findings advise running
  `sudo ldconfig` (which rebuilds the `ld.so` shared-library cache from
  `/etc/ld.so.conf` + trusted dirs), but running it did not resolve the warnings.
  Question: what are these findings really detecting, and is `ldconfig` the correct
  remediation at all? If the warning persists after a cache rebuild, the finding is
  keying off something `ldconfig` doesn't touch (stale symlink, a path outside the
  cache's search dirs, or a detection that doesn't re-check post-fix) — so the advice is
  wrong or the check is. Investigate the specific findings before changing code; then
  promote to a Bug (fix the check or the remediation text) or Abandon with rationale.
  Never implement straight off this Q. *Priority: medium (mis-advises the user on nearly
  every benign finding).*

---

## Abandoned / decided against

- **`1.2.0-Q11` — proactive kernel driver-class filter — decided against 2026-07-03.**
  The question was whether the kernel stage should *proactively* exclude host-irrelevant
  drivers (deriving `=n` for built-in `=y` options from `hardware_profile`), covering the
  two gaps F37 left: `localmodconfig` touches only unloaded *modules* (`=m`), not built-in
  `=y` options, and its filtering is reactive (keyed off the build machine's loaded module
  set). Decision: **not worth the boot-safety risk for the marginal benefit.** A built-in
  driver compiled into the kernel costs image size and a little build time but is inert at
  runtime; forcing it `=n` from an inferred hardware profile is exactly the kind of
  proactive exclusion that can silently drop a driver the machine needs at *next* boot
  (new hardware, a hotplugged device, a rescue scenario), and the kernel stage's whole
  discipline is that Gate-1/Gate-2 boot-safety stays authoritative. F37's opt-in
  target-sequence plus the accumulating (union) lsmod snapshot already lets a user who
  wants a slimmer kernel opt into `localmodconfig` reactively, which is the safe side of
  the trade. If a concrete boot-size or build-time problem ever motivates revisiting this,
  it would reopen under a new ID — not resume here.

- **`-sysforge` suffix on the PGO-built toolchain — scrapped 2026-06-24.** The PGO
  toolchain keeps installing under stock names (`clang`, `llvm`, `llvm-libs`, …),
  consistent with the CLAUDE.md invariant that the toolchain stage is the in-place
  system replacement and never threads `optimization_build_mode`. The rename would
  have introduced regressions in five exact-pacman-name lookups
  (`_verify_llvm_install`/`_probe_cc` skew arms, `_installed_libllvm_soname` →
  soname-bump gate, BOLT Pass 4a, `collect_llvm_state` provenance) plus a B5 rework,
  for provenance-cosmetic benefit on a default-`enabled=false` path. Not worth the
  risk on the highest-stakes path in the repo.

- **`[env_precedence]` config table — design cancelled.** The original design
  proposed a priority stack (wrapper profile = 100, makepkg.conf = 80, shell
  passthrough = 20, PKGBUILD export = 10) and an `[env_precedence]` TOML table to
  configure it. Superseded by a simpler, more predictable model: build-tool vars
  (`CC`, `CFLAGS`, `LDFLAGS`, etc.) are stripped from the inherited shell env in
  `invoke_makepkg` before makepkg runs — the temp conf is the sole authority for all
  makepkg-managed keys. Shell-env bleed-through is not a configurable priority; it is
  prevented entirely. SysForge bootstrap vars (`SYSFORGE_STATE_DIR`,
  `SYSFORGE_CONFIG_DIR`) are exempt (SysForge's own interface, not build-tool vars).
  The `[env_precedence]` table will not be implemented. (The env-stripping model that
  replaced it is documented in DESIGN.md.)
