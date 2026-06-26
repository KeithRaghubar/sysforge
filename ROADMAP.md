# SysForge Roadmap

Planned features/changes and the rationale for purposely-excluded or abandoned
ideas. This is the single tracked home for forward-looking work; **`DESIGN.md`
describes only implemented design** and never carries roadmap IDs.

**Shipped work is not recorded here** — it lives in `docs/release-notes/` and git
history (the commit that lands an item is its record).

## ID scheme

IDs are `<version>-<TYPE><n>`, e.g. `1.2.0-F1` (feature), `1.2.0-B1` (bug),
`1.2.0-Q1` (open question), `1.2.0-STD1` (standards). The version prefix is the
current `pyproject.toml` version. The per-release, per-type counter **resets to 1
on every release** (when `tools/release.sh` bumps the version); the version prefix
keeps IDs globally unique. An item still open at release time keeps its existing ID
(it records the cycle the item originated in, not its target). IDs appear only here
and in release notes.

---

## Planned

### Bugs

- **`1.2.0-B1` — PGO-built split packages: `provides`/`conflicts` attribution is
  busted.** Observed on mesa: the conflict lands on the wrong split member —
  `mesa-docs-sysforge` declares the conflict with stock `mesa` while
  `mesa-sysforge` declares no conflict at all, then fails at install because stock
  `mesa` still owns the files. The `provides`/`conflicts` rewrite (conflict-mode
  `patch_package_suffix`) is wrong across all built split members, forcing a manual
  rollback to stock mesa. Needs a fix to the per-`package_<name>()`
  provides/conflicts attribution **and** much more thorough testing of mesa PGO
  builds and general (`--pgo`) builds. *Priority: high (a profiled build that can't
  install is a hard regression on the optimization path).*

### Features

- **`1.2.0-F24` — `build --pgo` should imply makepkg `-Cc` (clean build).** Both
  `--pgo=record` and `--pgo=use` should force a clean build/package
  (makepkg `-C`/`-c`), so instrumentation and profile-use builds never reuse stale
  object files from a differently-instrumented prior run. Apply at the build-flag
  seam, not per-package. *Priority: medium (correctness — stale objects silently
  corrupt a PGO build).*

- **`1.2.0-F9` — Respect a PKGBUILD's per-package `options=()` (spun off Q6).** At
  conf-emit time, read the parsed `globals["options"]` (already captured by
  `parse_pkgbuild`) for the target and: (1) **`!lto`** → strip profile LTO flags
  (`-flto*`) from CFLAGS/CXXFLAGS/LDFLAGS — the package author has declared LTO
  breaks it (the cosmic-edit/onig mold-failure class); (2) **`!buildflags`** →
  recognize that makepkg will ignore conf CFLAGS/CXXFLAGS/LDFLAGS entirely, so
  optimization is a no-op AND Phase-4.3 flag-drift
  (`flag_drift.resolve_flag_drift`) must not false-trigger a rebuild. Implement as
  the same conf-emit scrub seam as `emit_makepkg_conf(is_lib32=…)`/`is_musl_static=…`
  (reuse the strip helpers; one home — don't add a parallel rule). Lower-stakes
  tokens (`!strip`/`debug`/`!makeflags`) optional. *Priority: medium (correctness —
  prevents injecting flags the PKGBUILD explicitly opts out of).*

- **`1.2.0-F13` — `doctor`: detect a partially-installed PGO toolchain.** When
  `toolchain.toml` defines a PGO toolchain (`[llvm] enabled = true`,
  `compiler = "llvm"`, `pgo = true`) but the installed LLVM suite is a *mix* of
  profiled and stock-repo components, `doctor` currently reports nothing. Observed:
  repo `llvm` + `llvm-libs` were temporarily reinstalled to fix a mesa runtime
  issue, leaving an inconsistent suite, and no axis flagged it. Add a read-only
  check (likely on the existing toolchain/graphics axis or a dedicated PGO axis)
  that cross-references the declared PGO intent against per-component provenance —
  reuse `llvm_state.collect_llvm_state` / `detect_toolchain_config_mismatch` (the
  provenance home) rather than adding a third checker (see CLAUDE.md "Toolchain
  health: exactly two checkers" + `llvm_state` invariants). Emit a `warn` naming
  which components are stock vs profiled and pointing at `run toolchain` to
  reconcile. *Priority: medium (correctness — a silently-mixed toolchain is exactly
  the state the gates exist to prevent).*

- **`1.2.0-F14` — `doctor`: warn on installed instrumented/incomplete-PGO builds of
  *any* package.** Since `--pgo` now works on any package (F5), an instrumented
  (`-fprofile-generate`, record-stage) build can be left installed for any target,
  not just mesa. `doctor` should detect when an instrumented build is the live
  package and `warn` with next steps: complete the profiled build
  (`build <pkg> --pgo=use`) or roll back to the repo package. Detection should lean
  on the existing provenance trail — `build_state.toml` `build_mode` (the
  `pgo`/`pgo_mesa` record-stage markers via `mesa_pgo.build_mode_for`) cross-checked
  against store state (`mesa_pgo.resolve_store` — a bare `.profraw` with no merged
  `.profdata` is the record-only signal) — rather than re-deriving from binaries.
  Read-only, no system mutation, consistent with the doctor-axis Finding framework.
  *Priority: medium (correctness — record-stage builds are unoptimized and meant to
  be transient).*

- **`1.2.0-F15` — `doctor` package-cache axis (spun off F11 audit).** Warn when the
  pacman package cache (`/var/cache/pacman/pkg`, resolve via the proper config home
  rather than hardcoding) exceeds a size threshold, with `paccache -r` /
  `paccache -ruk0` remediation. Distinct from `cache_probe.py`, which only reports
  *build* caches (ccache/sccache). New read-only axis or fold into the `pacman`
  axis. *Priority: medium (the most common real-world disk reclaim).*

- **`1.2.0-F16` — `doctor` mirror-freshness check (spun off F11 audit).** Warn when
  `/etc/pacman.d/mirrorlist` is stale (file age, or newest server's last-sync age)
  so partial-upgrade/slow-mirror situations surface. Read-only, no network call
  (don't probe mirror latency live — that flaps). *Priority: low.*

- **`1.2.0-F17` — Promote the disk-space check into a `doctor` axis (spun off F11
  audit).** The disk-space check currently lives only in the reconfigure stage
  (`pipeline/stages/reconfigure.py`), so an ad-hoc `sysforge doctor` run misses it.
  **Move** the logic to a probe primitive and consume it from both the stage and a
  new doctor axis (one home — don't duplicate). *Priority: medium.*

- **`1.2.0-F18` — `doctor` fstab integrity (spun off F11 audit).** Flag
  `/etc/fstab` entries whose UUID/label or device no longer resolves (stale mount).
  Read-only. *Priority: low.*

- **`1.2.0-F19` — Broaden the journal scan beyond firmware (spun off F11 audit).**
  `runtime_probe` only greps `journalctl -k -b` for "Direct firmware load … failed".
  Extend it (or add a sibling check on the services/boot axis) to surface
  failed-boot / core-dump / repeated-unit-failure errors. Read-only, current-boot
  scoped. *Priority: low.*

- **`1.2.0-F11` — Roadmap alignment with the Arch wiki (guiding principle + concrete
  first cut).** Treat the Arch wiki's
  [System maintenance](https://wiki.archlinux.org/title/System_maintenance) and
  [General recommendations](https://wiki.archlinux.org/title/General_recommendations)
  pages as the north star for what sysforge should help **set up, monitor, and
  debug** — the roadmap should stay aligned with their contents rather than
  diverging. This is a *meta* item that seeds smaller specs; it should not be
  implemented as one mega-change. **Concrete first cut:** drive the GUI package
  groups from the desktop-environment list on General recommendations — i.e. keep
  `pkg_catalog.DESKTOP_CATALOG` (the one home for the desktop catalog, per CLAUDE.md)
  in sync with the wiki's enumerated DEs rather than an ad-hoc subset. Most
  System-maintenance topics (orphan removal, paccache, failed units, journal errors,
  mirror freshness, `.pacnew`/`.pacsave` handling, fstab/UUID checks) overlap with
  existing `doctor` axes — audit which are already covered and file the gaps as their
  own items rather than scope-creeping this entry. *Priority: low (strategic —
  spawns scoped follow-ups; not a single deliverable).*
  **Progress (2026-06-26):** the System-maintenance audit strand is done — coverage
  matrix in `docs/superpowers/specs/2026-06-26-doctor-maintenance-gap-audit.md`
  (local, gitignored); gaps filed as F15–F19 above. The desktop-catalog ↔ wiki first
  cut and the broader General-recommendations alignment remain.

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

- **`1.2.0-F23` — System-maintenance scope expansion (from the DESIGN roadmap).**
  Grow sysforge beyond build/package management into a unified system-maintenance
  helper: track and manage user-owned system artifacts that currently live ad-hoc
  across `~/scripts`, `/etc/systemd/system/`, `/etc/pacman.d/hooks/`, etc. Candidate
  primitives: inventory of tracked files, source-of-truth dir under repo control,
  install/sync command, drift detection vs filesystem, integration with the existing
  config/profile/manifest layers.

  *Scoping pass (which Arch-wiki maintenance topics fit):* SysForge's lane is
  **build/package optimization and the health of what it builds** — not a
  general config-management or backup tool.
  - **In scope (covered or a clean fit):** full-system upgrade / partial-upgrade
    avoidance (the `update` verb already owns this); orphans / unused packages /
    paccache (extends the `cache` layer + a read-only `doctor` axis);
    `.pacnew`/`.pacsave` handling (analogous to the `.sfnew` config-merge verb;
    a `doctor` axis surfacing pending merges); failed systemd units / journal
    errors (read-only `doctor` Finding axis); mirror/keyring freshness (a `doctor`
    axis — it directly affects what sysforge builds/installs).
  - **Out of scope (not sysforge's job):** backups, snapshots-as-policy, disk-space
    *strategy*, user-data hygiene (the user's btrfs/timeshift/borg tooling — the
    only adjacency is the optional pre-build snapshot under F21); the broader
    *General recommendations* territory (networking, user management, desktop/locale/
    input config, security hardening as a whole) — outside a package-builder's remit.

  The actionable near-term slice is a set of read-only `doctor` axes (orphans,
  `.pacnew`, failed units, mirror/keyring freshness — several already filed as
  F15–F19) plus the artifact-inventory primitive sketched above. Anything mutating
  stays behind an explicit verb with the sentinel/gate discipline the rest of
  sysforge uses. *Priority: low (strategic).*

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
