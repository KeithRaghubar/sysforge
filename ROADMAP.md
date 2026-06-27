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

- **`1.2.0-F27` — Sync the desktop catalog to the Arch wiki's DE list.** Keep
  `pkg_catalog.DESKTOP_CATALOG` (the one home for the desktop catalog) in sync with
  the desktop environments enumerated on the Arch wiki's General-recommendations
  page, rather than an ad-hoc subset (add a DE via `DESKTOP_CATALOG` +
  `bootstrap.toml [desktop]` + both completions). *Priority: low.*

- **`1.2.0-F28` — User-owned artifact inventory primitive.** Track the user-owned
  system artifacts now scattered across `~/scripts`, `/etc/systemd/system/`,
  `/etc/pacman.d/hooks/`, etc.: a tracked-file inventory, a repo-controlled
  source-of-truth dir, an install/sync command, and drift detection vs the
  filesystem, tied into the existing config/profile/manifest layers. Anything that
  mutates the system stays behind a sentinel-gated verb. Stays inside the boundary in
  DESIGN.md §Scope & Non-Goals (this is steady-state health of a managed system, not
  backup/config-management). *Priority: low (strategic — coarse; decompose before
  building).*

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
