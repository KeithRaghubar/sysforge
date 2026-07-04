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

- **`2.0.1-B2` — Build-failure recovery prompt omits the failing package name.** The
  recovery menu and surrounding failure messages never say *which* package failed —
  in a batch update the `[SYSFORGE][UI][MAKEPKG] Recover:` prompt is ambiguous. The
  menu builder (`makepkg_invoke._recover_menu_choices` / `_run_recovery_menu`)
  already receives `pkgbase`; thread it into the `Recover:` header line (e.g.
  `Recover <pkgbase>:`) and audit the sibling failure-summary lines for the same
  omission. One home — the fix lives in the existing recovery-menu seam, no
  parallel prompt. *Priority: medium (users act on this prompt mid-failure;
  ambiguity invites acting on the wrong package).*

### Features

- **`2.0.1-F3` — Integrate archinstall for the bootstrap disk/base-install slice
  (promoted from Q1).** Replace sysforge's hand-rolled partitioning + base-install
  logic (`partition.py` ~411 lines, `base_install.py` ~131 lines) with a
  **translation layer** that maps sysforge's bootstrap config onto archinstall's
  scriptable, headless API — validated by the 2026-07-03 spike:
    - **Disk** → `archinstall.lib.disk.device_handler.DeviceHandler` (singleton:
      `partition()`, `format()`, `wipe_dev()`, `load_devices()`,
      `create_btrfs_volumes()`), driven by the `DiskLayoutConfiguration` /
      `DeviceModification` / `PartitionModification` / `FilesystemType` models.
    - **Base install** → `Installer(target, disk_config, base_packages, kernels,
      silent=True)` (context manager: `mount_ordered_layout()`,
      `minimal_installation()`, `genfstab()`, `add_bootloader()`, `set_mirrors()`,
      `set_locale()`, `set_timezone()`, `create_users()`, `enable_service()`).
  Constraints: (a) archinstall is a **live-ISO-only** dependency (pulls
  `pyparted`/`cryptography`) and must not leak into the normal package-build
  install path — gate it to the bootstrap entry point; (b) pin archinstall and
  add a compat shim since its models move across releases; (c) `configure.py`
  (~759 lines) exceeds archinstall's coverage and stays sysforge's; (d) all disk
  mutation stays behind the existing sentinel-gated verb. First slice: a
  proof-of-concept `partition` stage that emits a `DiskLayoutConfiguration` and
  calls `DeviceHandler.partition/format`, validated in the VM harness, before
  porting base-install. *Priority: medium (removes the highest-risk
  re-implemented surface: disk/boot setup).*

- **`1.2.0-F9` — Respect a PKGBUILD's per-package `options=()` (spun off Q6).** At
  conf-emit time, honor the parsed `globals["options"]`: **`!lto`** → strip profile
  LTO flags (`-flto*`) from CFLAGS/CXXFLAGS/LDFLAGS (author declared LTO breaks it —
  the cosmic-edit/onig mold-failure class); **`!buildflags`** → makepkg ignores conf
  build flags entirely, so suppress optimization *and* prevent Phase-4.3 flag-drift
  (`flag_drift.resolve_flag_drift`) from false-triggering a rebuild. Reuse the
  `emit_makepkg_conf(is_lib32=…/is_musl_static=…)` scrub seam — one home, no parallel
  rule. *Priority: medium (don't inject flags the PKGBUILD opts out of).*

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

- **`1.2.0-F24` — `build --pgo` should imply makepkg `-Cc` (clean build).** Both
  `--pgo=record` and `--pgo=use` should force a clean build/package
  (makepkg `-C`/`-c`), so instrumentation and profile-use builds never reuse stale
  object files from a differently-instrumented prior run. Apply at the build-flag
  seam, not per-package. *Priority: medium (correctness — stale objects silently
  corrupt a PGO build).*

- **`1.2.0-F26` — FDO-instrumented kernel must not overwrite the production sysforge
  kernel.** An AutoFDO/Propeller *instrumented* (record-pass) kernel build currently
  collides with the production sysforge kernel install. Give the instrumented kernel
  a *separate* boot entry, overwriting an existing one only when sysforge created it
  (ownership-gated, like the `owner_stage` + coexist-rename discipline). Reuse the
  `primitives/kernel_fdo.py` seam and the existing boot-entry path — no parallel
  writer. *Priority: medium (safety — silently replacing the production kernel is a
  boot-stability risk).*

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

_(none open — resolved questions are either promoted to a Feature/Bug entry
above or recorded under Abandoned below.)_

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
