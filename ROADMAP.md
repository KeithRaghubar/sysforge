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
