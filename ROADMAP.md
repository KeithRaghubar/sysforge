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

- **`2.2.0-B3` — `check-shipped` manpage guard couples to the local scdoc version.**
  The `manpage` check in `tools/check_shipped.py` asserts that committed `man/sysforge.1`
  byte-matches fresh `make man` output on the current machine. But `make man` runs `scdoc`,
  whose rendering changes between versions (e.g. 1.11.4→1.11.5 re-escaped every `-` as `\-`
  and re-stamped the `.TH` date), so the committed artifact is silently pinned to whichever
  scdoc version last regenerated it. A contributor (or CI) on a different scdoc version sees
  the inverse reflow — hundreds of cosmetic `\-`↔`-` lines — and the guard fails on changes
  they never made, conflating "the CLI surface changed" with "the renderer version changed".
  The escaping is functionally inert (roff renders `\-` and `-` identically). Fix: normalize
  the comparison in `check_shipped.py` — strip the generator/date comment line and canonicalize
  hyphen escaping before diffing — so the guard verifies man-page *content* (does it match the
  argparse tree) rather than scdoc-version-specific byte output. *Priority: low (cosmetic churn
  + contributor/CI friction; no runtime or rendered-output impact).*

- **`2.2.0-B5` — `record_self_install` bypasses `fs_provision`, so the self-install
  sentinel silently never records.** `install_reconcile.record_self_install` creates
  `/var/lib/sysforge/sentinels/` with a bare `d.mkdir(...)` + `os.open(...)` instead of
  routing through `fs_provision.ensure_writable_dir`. The sentinels dir is written by
  *two* actors at different privilege levels: root (the libalpm hooks drop
  `buildstate`/`kernel`/`toolchain`) and the *unprivileged* sysforge process (this
  function appends `self-install` from the `pacman -U` chokepoint in `pacman.py`). Because
  root's hooks create the dir first, it lands `0755 root:root` and is never healed to the
  `2775 root:sysforge` that `fs_provision` designs for exactly this coexistence — so the
  unprivileged append fails with `EACCES` and the marker is never written. Observed live:
  dir is `drwxr-xr-x root root`, no `self-install` file exists, yet the build user *is* in
  the `sysforge` group (the group-write path would work if the mode were correct). The
  `2.1.0-B13` fchmod-heal only repairs the *file* once created; it cannot heal a
  root-owned parent the user can't write into. **Impact (trust-erosion, same family as the
  shipped B1/B2/B4 cluster):** with `self-install` perpetually empty,
  `external_install_targets = buildstate − ∅` misclassifies every package sysforge itself
  installs via `pacman -U` as an external `pacman -S`, risking spurious `build_mode`
  demotion on the next `update` reconcile. The recurring `[RECONCILE] could not record
  self-install marker (non-fatal): [Errno 13] Permission denied` INFO line is only the
  surface symptom. Fix: route the sentinel-dir creation through
  `fs_provision.ensure_writable_dir` (or provision the sentinels dir group-writable in
  `setup`) so both the root hooks and the unprivileged append share a `2775 root:sysforge`
  tree. *Priority: medium (silent reconcile mis-classification + visible per-install
  warning; best-effort catch means no crash).*

- **`2.2.0-B6` — `fs_provision.ensure_writable_dir` fast path skips group/mode
  enforcement, leaving user-created dirs `user:user` instead of `root:sysforge`.** The fast
  path (`fs_provision.py:132-138`) does `mkdir(parents=True)` then `if os.access(path,
  os.W_OK): return path` — so **any already-writable directory is returned untouched**, and
  the `SYSFORGE_GROUP` / setgid `SYSFORGE_DIR_MODE` (`2775`) are only ever applied on the
  sudo *slow* path (root-owned ancestor). Consequence: a cache/state dir first created by
  the unprivileged build user lands `user:user 0755` with **no setgid bit**, so every child
  created under it also fails to inherit the `sysforge` group. Observed live:
  `/var/cache/sysforge/llvm-pgo/` and all three of its files are `keith:keith`, while
  sibling stores that happened to hit the slow path (`pgo-mesa`, `propeller/linux-sysforge`,
  `autofdo/linux-sysforge`) are correctly `root:sysforge 2775`. This is the general form of
  `2.2.0-B5` (which is the sentinel-dir-specific instance): a second writer at a different
  uid — the libalpm hooks (root), or a later `sudo` rebuild — then can't write the
  user-owned tree, and the group-write coexistence the design promises silently doesn't
  hold. Fix: in the fast path, after landing a writable dir, **best-effort self-heal the
  group + setgid when the current user owns it and belongs to `SYSFORGE_GROUP`** (a plain
  `chgrp`/`chmod` needs no sudo in that case), falling through to the slow path only when it
  can't. Fixing this subsumes the ownership half of B5. Related environmental facet (track
  here, not a separate ID): the shipped `tmpfiles.d`/`sysusers.d` correctly declare `2775
  root sysforge`, but `dev_install.sh` skips symlinking them (`tools/dev_install.sh:64`) and
  the libalpm hooks then create `/var/lib/sysforge` + sentinel files as `root:root` before
  anything provisions the group — so a *dev-install* box never gets the group baseline a
  packaged install does. Consider having `dev_install.sh` install + `systemd-tmpfiles
  --create` the shipped configs, or a `doctor`/`setup` heal pass over the managed trees.
  *Priority: medium (silent group-ownership drift → cross-uid write failures in the
  best-effort paths; same trust-erosion family as B5).*

### Features

- **`2.2.0-F1` — ccache/sccache readiness doctor axis.** Split out of `1.2.0-F21`
  (Configure-stage additions), where it sat awkwardly among build-time *mutations*.
  This is a **read-only health check**, so it belongs in the doctor-axis family: a
  producer → `list[diagnostics.Finding]` that verifies ccache/sccache are installed,
  on `PATH`, and configured with a sane cache dir/size before a build relies on them.
  Reuse the existing passive probes in `primitives/cache_probe.py`
  (`probe_ccache()`/`probe_sccache()`) rather than adding a parallel reader. Register
  in `doctor.py` + `cli.py` + both completions + manpage + `_patch_axes_clean` in the
  same change (per the doctor-axis one-home invariant). *Priority: low (candidate).*

- **`1.2.0-F20` — Rule priority auto-calculation (from the DESIGN roadmap).**
  Auto-calculate a baseline specificity score from rule conditions (mirrors CSS
  specificity: more AND'd conditions = higher weight), with manual `priority`
  override for ties. Deferred until enough real rules exist to validate whether
  auto-priority causes ordering problems in practice. *Priority: low (candidate, not
  a commitment).*

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

- **`1.2.0-F43` — Logging re-levelling audit for interactive/bootstrap stages.**
  Follow-up to the shipped `1.2.0-F36` slice (configurable `[log] verbosity` + `--quiet`,
  the DESIGN.md §Logging rubric, and the re-levelled day-to-day `build`/`update` path
  under a golden-output test). **Remaining:** sweep the interactive/bootstrap-time stages
  against the same rubric — `reconfigure.py` (94 `ui()`), `configure.py` (47), `kernel.py`
  (42), `toolchain.py` (60), `hardware.py` (19), `partition.py` (14) — demoting progress
  narration to `info()` while keeping prompts/plan-tables/results as `ui()`. Extend the
  golden guard to a representative stage run. *Priority: low (bootstrap-time output, not the
  day-to-day regression, which is resolved under F36).*

---

## Abandoned / decided against

- **`2.2.0-Q1` — build-system cohesion audit — decided against 2026-07-10.** The question was
  whether the kernel, toolchain, and package stages had diverged from a shared build system and
  warranted consolidation. Investigation found the premise doesn't hold: there are two seams, and
  the load-bearing one is already the single home. The **low seam** (`makepkg_wrapper.run` /
  `primitives/makepkg_invoke.py`) — where the real one-home invariants live (flag scrubs, build
  throttle, PGO/FDO/BOLT rename, review gate, recovery menu) — is used by *every* surface: `build`,
  `update`, and all three stages. The **high seam** (`build_core.build_and_install`: dep-resolve →
  batch-order → bulk-install) is used only by `build` and `update`; `packages.py`/`kernel.py`/
  `toolchain.py` call `makepkg_run` directly, but that divergence is **intentional**, not drift:
  `toolchain.py` is a 5-pass staged build with no system-install for passes 1–3 (routing it through
  `build_and_install`'s resolve→build→bulk-install assumption would be wrong), and `kernel.py` is a
  single interactive-by-default package with an `nconfig` pause, post-install steps, and local
  pkgbase rename. The only genuine candidate — `packages.py`'s per-package loop partially
  re-implementing `build_and_install` — is bootstrap-time (not the day-to-day path), carries its own
  stage resume/progress state, and touches the build-state authority, so the net simplification is
  marginal against the risk. If that duplication ever becomes a real maintenance cost it reopens as a
  narrow `F` scoped to the `packages` stage — not a surface-wide audit.

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
