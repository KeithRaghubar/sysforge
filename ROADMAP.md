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

---

## Planned

### Features

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

- **`1.2.0-F30` — Config default for rebuild-on-drift.** `update` currently requires
  `--rebuild-on-drift` (and the per-axis `--rebuild-on-toolchain-drift` /
  `--rebuild-on-flag-drift`) every run. Add an `[update]` config key (e.g.
  `rebuild_on_drift = true`, with per-axis overrides) so a user who always wants
  drift auto-resolved doesn't repeat the flag. CLI flag still wins when passed;
  resolve through the existing `getattr(args, ...)` seam in `update.py` Phase 4.3 — no
  parallel drift switch. *Priority: medium.*

- **`1.2.0-F31` — `make install` / `make uninstall` via venv + config symlinks.** Add
  verbose Make targets that install sysforge from the git checkout (venv entry point +
  symlinked config/hooks/completions/manpage) and a matching uninstall, for quick
  trial or fresh dev-env setup **without** going through the AUR and **without**
  permanent system changes. Each target must print every file it creates/removes and
  whether the operation succeeded (idempotent, reversible). Document the option in
  README.md (user-facing install path). Cross-check `make check-shipped` parity so the
  symlinked set matches the packaged set. *Priority: medium (lowers the bar to try
  sysforge / stand up a new workstation).*

- **`1.2.0-F32` — `update`: advise on stage-owned packages with available updates.**
  `update` skips toolchain/kernel stage-owned packages by design (`owner_stage`), but
  it should still *detect* that a newer upstream version exists for them and advise the
  user to run the owning pipeline stage (`run toolchain` / `run kernel`) to rebuild.
  Detection only — no rebuild from `update`. Surface in the summary, keyed off
  `stage_ownership.owner_of`. *Priority: medium (silent staleness on the highest-stakes
  packages otherwise).*

- **`1.2.0-F33` — `update` summary formatting.** The end-of-run summary lacks clear
  separation between the header, the built/failed/pacman sections, and the package
  names within each section. Tighten the layout in `_print_summary` (grouped headers,
  per-section spacing/columns, readable package lists) honoring the Unicode/`use_color`
  gates. *Priority: low (polish, but it's the primary user-facing output of the most
  common verb).*

- **`1.2.0-F34` — `doctor` mesa-failed finding should also advise manual rollback via
  `state forget`.** When mesa is source-built (`mesa-sysforge`) and installed, the doctor
  mesa check still reports the stock `mesa` as failed; the message currently only says the
  warning clears on rebuild. Extend the finding's remediation text to also offer the manual
  path — `sysforge state forget mesa` (drop the build-state tracking entry) — for users who
  intentionally reverted. Read-only finding; advice-text only, no new mutation. Keep it in
  the existing mesa doctor axis — no new producer. *Priority: low (UX — the current advice
  is technically right but dead-ends a legitimate workflow).*

- **`1.2.0-F36` — Audit logging verbosity levels + configurable default verbosity.** Warning/info
  messages added over successive features have crept into the default verbosity level, eroding the
  meaning of the levels (default output is now noisy). Audit every logging call site to confirm the
  level matches intent (info vs warning vs debug), and add a configurable default-verbosity key
  (e.g. `[log] verbosity` in `sysforge.toml`, or the appropriate config home) so users can opt into
  a quieter or more verbose baseline. CLI `-v/-q` flags still win when passed. Note: the user
  personally prefers `info` as a default but it's too noisy to force on everyone — so the fix is
  *correct levelling* plus a *user-settable* default, not changing the shipped default. Route
  through the existing `log` seam / `log.use_color`-style gating — no parallel verbosity switch.
  *Priority: medium (the primary UX regression in day-to-day output).*

- **`1.2.0-F37` — Kernel stage: configurable kconfig target(s) via `kernel.toml`.** The kernel
  stage should accept a configurable config-generation target chosen from the kernel's `make`
  kconfig targets (`menuconfig`, `nconfig`, `oldconfig`, `olddefconfig`, `localmodconfig`,
  `localyesconfig`, `defconfig`, `allmodconfig`, `alldefconfig`, `savedefconfig`, `listnewconfig`,
  `mod2yesconfig`, etc.). Allow **at most one interactive** target (`config`/`nconfig`/`menuconfig`/
  `xconfig`/`gconfig`) but **multiple non-interactive** targets, with sysforge defining and
  documenting the execution order of a multi-target run. Wire the toggle through `kernel.toml` and
  reuse the existing kernel-stage kconfig invocation seam (coordinate with the F25 pause-before-
  kconfig helper / B6 positioning). *Priority: medium (makes the kernel-config workflow flexible
  without hand-editing the stage).* **Sub-question:** does `randconfig` have any practical reason
  to be offered? If not, exclude it from the allowed-target set (document the exclusion rationale).

- **`1.2.0-F38` — `update`: report installed dependencies as their own summary category.** The
  end-of-run `update` summary should surface dependency packages that were installed as a build
  prerequisite (via `prepare_deps`) as a distinct category, separate from the built/failed/pacman
  sections, so users can see what was pulled in on their behalf. Fold into `_print_summary`
  alongside the F33 summary-formatting work (honour the Unicode/`use_color` gates). *Priority:
  low (visibility into implicit installs).*

---

## Bugs

- **`1.2.0-B8` — Kernel and toolchain stages don't pull latest source before building.** The
  kernel stage built without first fetching the newest upstream revision, producing a stale
  build. Both the kernel and toolchain stages should always sync to latest before building,
  the same way `update` does for ordinary packages — routed through the source-sync scheduler
  (`source_sync.get_scheduler().request(...)`, full-history `git_fetch_and_compare`), never a
  bare `git pull`. *Priority: medium (the highest-stakes stages can silently rebuild stale
  source).*

---

## Open questions

- **`1.2.0-Q10` — Can repoctl be restricted to versions present in the official repos?**
  When pulling package versions via repoctl, builds often pick up versions newer than what
  `pacman` itself would install — the suspicion is that it sees `[testing]`/`[*-testing]`
  versions. *Open question: is there a way to constrain the lookup to the stable official
  repos only (filter by enabled-but-non-testing repos, or compare against `pacman`'s
  resolved candidate), and is this a repoctl-config matter or a sysforge-side filter?*

---

## Abandoned / decided against

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
