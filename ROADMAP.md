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

- **`2.2.0-B1` — Misleading (possibly spurious) external-install demotion message on
  update.** An `update` run logs e.g. `demoted 19 source-built package(s) reinstalled
  from the repo: cosmic-*-git, lib32-vulkan-icd-loader-git, vulkan-*-git, …` — but every
  named package is a `-git` AUR package with **no repo version** to be "reinstalled from
  the repo" from, so both the wording and the demotion itself look wrong. Reported symptom:
  the message "doesn't seem correct and also doesn't change update behaviour." Investigate
  `_reconcile_external_demotions` (`update.py:341`) and the self-install sentinel diff
  (`primitives/install_reconcile.external_install_targets`): determine whether the sentinels
  are capturing packages sysforge itself built/installed (false positives) rather than true
  external `pacman -S` reinstalls, and whether demoting a source-built `-git` package to a
  `pacman` marker is ever correct. Fix the demotion trigger and/or re-word the message.
  *Priority: medium (touches the source-built ⇄ pacman state authority; a false demotion
  silently stops a package from being rebuilt on future updates).*

- **`2.2.0-B2` — Spurious kernel/toolchain "changed since last sysforge run" reminders on
  update.** An `update` run surfaces the pacman-hook sentinel reminders
  (`Kernel package(s) changed…` / `Toolchain package(s) (llvm/clang/gcc) changed…`) even when
  the only thing that changed the kernel/toolchain was **a prior `sysforge update` itself** —
  so the nag fires for work sysforge already did. Mechanism: PostTransaction libalpm hooks drop
  `/var/lib/sysforge/sentinels/{kernel,toolchain}`; `_consume_pacman_hook_sentinels`
  (`update.py:309`) surfaces+unlinks pre-existing sentinels at start-of-run (line 380) and does a
  `silent=True` clear at end-of-run (line 1082) to swallow sentinels sysforge's own Phase 5
  (`pacman -U`) / Phase 6.5 (`pacman -Syu`) transactions just dropped. The end-of-run clear is a
  plain sequential call, **not guaranteed on every exit path** — any exception or early return
  after Phase 6.5 leaves sysforge's own sentinel on disk, and the next run reports it as an
  external change. Fix: make the end-of-run swallow run unconditionally (e.g. `finally`), or
  snapshot the sentinel set present at start-of-run and only ever warn on that set so mid-run
  self-drops can never be surfaced. *Priority: medium (a warning that fires for sysforge's own
  changes trains the user to ignore a genuinely useful rebuild/staleness signal — same
  trust-erosion failure mode as `2.2.0-B1`).*

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

- **`2.2.0-B4` — `doctor --gfxperf` warns about a nonexistent package (`nvidia-vaapi-driver`).**
  The VA-API video-decode check (`_check_vaapi_driver`, `primitives/gfxperf_probe.py:81`) gates on
  the installed-package name `nvidia-vaapi-driver`, but that is the *upstream project* name — the
  Arch package is `libva-nvidia-driver` (the one actually installed). So the check never finds its
  target on a correct system, permanently emits a WARN, and its remediation tells the user to
  `Install 'nvidia-vaapi-driver'`, a package that does not exist in the repos. Fix: match on the
  real package name (`libva-nvidia-driver`), and update the finding message + remediation string
  accordingly (the upstream name may still be worth a parenthetical for recognisability). Ships in
  the just-landed `1.2.0-F22` gfxperf axis. *Priority: medium (a permanent false positive that
  points at a phantom package — same advisory trust-erosion failure mode as `2.2.0-B1`/`B2`).*

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
  snapshot before a build runs; estimated build-time heuristic. (The
  ccache/sccache readiness check was split out to `2.2.0-F1` — it's a health
  check, not a configure mutation.) *Priority: low (candidate).*

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
