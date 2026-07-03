# sysforge (unreleased)

The optimization release: profile-guided optimization across mesa, the kernel, and
the LLVM toolchain; signed releases end to end; a GUI-capable installer; and the
PKGBUILD review trust gate — on top of a large modular refactor.

## Added

- **Profile-guided optimization suite** — `build <pkg> --pgo=record|use` PGO-optimizes
  mesa (and, generalized beyond mesa via a config warn-list, any package); the kernel
  gains sample-based FDO with `run kernel --autofdo=record|capture|use` (+ `--propeller`);
  the LLVM toolchain builds through a 4-pass PGO bootstrap. Instrumented builds coexist
  under a `-sysforge` rename and reuse durable profdata across runs.
- **PKGBUILD review gate** — before building any package whose source changed since the
  last accepted build, sysforge shows the full source-tree diff (PKGBUILD, `.install`
  files, patches, new sources) and prompts: view / accept / skip / abort, single keypress.
  `sysforge build` prompts by default; `sysforge update` auto-accepts with a logged notice
  so batch runs stay unattended (`--review` opts into the prompt). Disable with
  `--no-review` or `[build] review = false`.
- **Desktop environments boot into a GUI** (`1.2.0-F27`) — the curated desktop catalog
  grew from `gnome`/`kde` to eight environments (`gnome`, `kde`, `xfce`, `mate`,
  `cinnamon`, `lxqt`, `budgie`, `cosmic`), each bundling its session, display manager,
  and portals. Installing a desktop group now also **enables** its display-manager
  service, so a fresh install (or `sysforge packages add-group <de>`) lands at a graphical
  login instead of a TTY.
- **Interactive build-failure recovery** — on a build failure, an interactive menu offers
  editing the PKGBUILD or swapping the C/C++ compiler and linker for a retry; a chosen
  swap persists via the `[package_compiler_overrides]` table in `profiles.toml`.
- **Opt-in hardware-filtering of mesa drivers** — `[mesa] filter_drivers` restricts the
  built gallium/vulkan drivers to the detected hardware (software baseline always kept),
  the meson analogue of LLVM target filtering.
- **Per-build throttling knobs** — `nice`/`ionice`/`cpu_quota`/`jobs` controls bound a
  build's CPU and I/O footprint.
- **Kernel build toggles** — headers and docs subpackages can be disabled from
  `kernel.toml`/CLI; the kernel stage is interactive by default with a pause before
  `nconfig` so the assembled config can be reviewed.
- **`doctor` expansion** — redesigned as the primary debug/safety surface, with read-only
  system axes covering toolchain, boot, and runtime state, plus new network/connectivity
  and audio/sound-stack diagnostic axes.
- **Package groups** — `[group.<name>]` tables in packages.toml expand into per-package
  entries at load time, with optional group-level defaults inherited by members. Explicit
  `[[package]]` entries always win.
- **Runtime profiling** — `--timings` reports per-phase durations with proportional bars.
- **scdoc man page** — hand-written prose plus generated COMMANDS sections;
  `argparse-manpage` dropped entirely.
- **First-install bootstrap notice** and libalpm-hook provisioning surfaced through
  `setup`/`doctor`.
- **Config adoption tooling** — `sysforge config merge` interactively adopts `.sfnew`
  config drift; `make sync-config` add-only adopts newly-shipped defaults into the live
  config.
- **Signed releases end to end** — the release commit, annotated tag, and source tarball
  are GPG-signed; the stable PKGBUILD verifies the signature at install time via
  `validpgpkeys` + a detached `.asc` source. See also *Security*.
- **Terminal-aware output** — decorative glyphs downgrade to ASCII on non-UTF terminals.
- **Incremental release notes** (`1.2.0-F35`) — release notes are authored per-task into a
  running accumulator (`docs/release-notes/unreleased.md`); `make check-standards` lints it
  under the Keep a Changelog vocabulary, and `tools/release.sh` renames it to `vX.Y.Z.md`
  (title stamped with version + date) at release, reseeding a fresh accumulator. The
  `release-notes` skill now reconciles/lints the accumulated entries instead of authoring
  the file from scratch.
- **Kernel-stage upstream source tracking & local rename** (`1.2.0-F40`) — kernel.toml
  gains `upstream_pkgname`, decoupling what sysforge pulls (e.g. `linux-zen`) from what
  it builds/installs as (`pkgname`, defaulting to `upstream_pkgname`). A missing source
  tree is now bootstrapped by the source-sync scheduler (sync runs before the PKGBUILD
  path is required), `source` auto-resolves `local → repo → aur` when omitted (the
  phantom `git` value errors clearly), and when `pkgname` differs from the upstream the
  cloned PKGBUILD's pkgbase is patched to the local name (coexist rename — installs
  alongside the official package, stacking under the optimized-build `-sysforge`
  suffix). Pure-local configs are unaffected.
- **Same-variant toolchain drift detection** (`1.2.0-Q9`) — `update` now stamps a
  `toolchain_fingerprint` alongside `toolchain_variant` in `build_state.toml` and flags a
  package when the active toolchain was rebuilt since it was built, even when the variant
  name is unchanged (e.g. a fresh-profdata PGO rebuild with the same libLLVM soname). New
  `[toolchain] drift_detect` config key selects the method: `"fingerprint"` (default; fast
  path/size/mtime/version stat) or `"content_hash"` (hashes the resolved `libLLVM.so`).
  Advisory by default — surfaced in the drift summary and `--explain-drift`, rebuilt only
  under `--rebuild-on-toolchain-drift` / `--rebuild-on-drift`.
- **Repo checkouts pin to the sync-DB release** (`1.2.0-F41`) — `source=repo` checkouts
  are pinned to the release tag matching pacman's sync-DB version, so source builds
  match what pacman would install; set `[build] repo_track = "main"` in sysforge.toml
  for testing-track builds.
- **Configurable kernel kconfig targets** (`1.2.0-F37`) — the kconfig target sequence
  is configurable via `kernel.toml`
  `kconfig_targets` (UI/prompting/silent taxonomy, at most one UI target which
  always runs last, prompting targets rejected in non-interactive runs,
  `randconfig` excluded); the lsmod snapshot now accumulates across builds and
  `localmodconfig` is warned against as high-risk/low-reward.

## Changed

- **Toolchain & kernel stage stability overhaul** — both stages now run three safety gates
  (preflight, post-build audit, post-install verify) with the build split from the install,
  snapshot + auto-restore undo for the toolchain suite, and curated boot-critical kconfig
  checks for the kernel. Toolchain health is also probed up front by `sysforge update` so a
  broken compiler aborts the batch before any build. libLLVM soname bumps trigger a
  consumer rebuild, and system libLLVM always keeps `AMDGPU` so the desktop stays bootable.
- **Major modular refactor** — god modules decomposed into cohesive single-purpose modules;
  `build` and `update` now share one build engine (`build_core.py`); unified logging tags;
  DESIGN.md generated from modular `docs/design/` sources. `build_state.toml` is now the
  authority for what sysforge maintains, and the `source_built`/`build_from_source`
  vocabulary was disambiguated (legacy `profiled` aliased on read).
- **Effective-linker reconcile** — a PKGBUILD's hardcoded `-fuse-ld=` is reconciled against
  the effective linker (honoring `--ld`/profile) rather than silently overriding it.

## Removed

- **Breaking:** `converge` verb removed. Its build-state-wide flag-drift coverage is folded
  into `sysforge update` Phase 4.3: out-of-walk source-built entries are detected and
  reported, with a `sysforge build` hint for rebuilds. Use
  `sysforge update --offline --dry-run` for the read-only drift report.
- **Breaking:** `update_repo_profiled` alias removed. Use `update --repo-mode=build_from_source`.

## Fixed

- Desktop-catalog packages now honor `repo_mode` (`1.2.0-B12`) — every curated
  desktop-environment group stamps `source = "repo"` on its members, so with
  `repo_mode = "pacman"` desktop packages install from the official repos instead
  of being unconditionally source-built.
- Batch builds are now topologically ordered by intra-batch dependency edges (including
  provides/sonames) and freshly built siblings are installed before a dependent configures
  — fixing stale-sibling failures in large stack rebuilds.
- Repo runtime `depends` are pre-installed alongside makedepends/checkdepends, and freshly
  built explicit targets are always installed.
- ANSI/OSC escape stripping on pty-captured build output, so compiler diagnostics from
  terminal-aware compilers are matched correctly.
- `lld` and PGO flags are scrubbed from static-musl builds, fixing the `pacman-static`
  build (soname-bump / symbol-drift gates added).
- Split-package relocation, `provides`/`conflicts`/`replaces` for renamed split packages,
  and mesa driver fallbacks hardened.
- `doctor` no longer false-flags a healthy DKMS module (`1.2.0-B9`) — a module that
  `dkms status` reports as merely `built` (not `installed`) for the running kernel is
  cross-checked against its `.ko` in the kernel's `updates/dkms` tree, so a loaded,
  working module (newer dkms can leave it at `built`) is treated as present.
- `doctor` pacnew/pacsave advice no longer dead-ends (`1.2.0-B10`) — unmerged pacman
  config files are split by whether their base file still exists: those with a live base
  advise `pacdiff` (which can merge them), while orphaned `.pacsave` leftovers from removed
  packages (base file gone, `pacdiff` no-ops) advise manual review/removal instead.
- Interactive pager output is no longer mangled (`1.2.0-B5`) — a verb that
  painted a `ui.progress` status (e.g. `state orphans`' pre-scan phase) left its
  DECSTBM scroll region active when it handed off to the pager, clamping less to
  `[1, N-1]` so its alternate-screen redraws desynced (blank-open /
  scroll-up-only / looping-top). The shared `maybe_pager` seam now runs the
  pager inside `progress.suspended()`, releasing the region for the pager's
  lifetime and restoring it after — fixing `log`, `state list`, `state orphans`,
  and `state failed` at once. It also drops `less -X` from its fallback (that
  flag suppresses the alternate-screen switch entirely) and parses `$PAGER` as a
  shell word list, so `PAGER="less -RF"` is honored instead of being treated as
  one un-spawnable token.
- Kernel interactive-review pause now fires at the right moment (`1.2.0-B6`) — it
  moved out of the stage (where it ran before `makepkg`, i.e. before the base-seed
  and fragment merges that assemble the final `.config` inside `prepare()`) and into
  the patched PKGBUILD's `prepare()`, immediately after those merges and just before
  `make nconfig` opens. The operator now confirms against the *merged* config instead
  of a pre-merge plan. The injected pause is TTY-guarded and errexit-safe, so
  unattended / no-TTY / dry-run builds are unaffected.
- Disabling the kernel `-docs` subpackage now actually suppresses it (`1.2.0-B7`) —
  `patch_kernel_subpackages` also rewrites `pkgname+=(...)` appends (the form modern
  Arch kernel PKGBUILDs use to add optional subpackages), so a `-docs` added via an
  append is no longer missed and left to build and install. Doc-build neutralization
  additionally handles a mixed `make all htmldocs` line by stripping only the `*docs`
  goal (→ `make all`) instead of skipping the line, so the doc compile is dropped
  without dropping the real build.
- Source sync now honors a PKGBUILD's origin classification during a build (`1.2.0-B8`) —
  `makepkg_wrapper.run()`'s pre-build sync built its `SyncRequest` without a `source`, so it
  always defaulted to `aur`. A hand-maintained (`local`) kernel PKGBUILD (e.g. a stock PKGBUILD
  with a modified `pkgbase`) triggered a spurious AUR RPC that could abort the build, and a
  git-hosted PKGBUILD repo was never fetched — building from a stale PKGBUILD. The sync now
  passes `options.source` through, so `local` short-circuits cleanly and `git`/`repo` fetch the
  right remote.
- Various dep-resolution, path-resolution, VM-testing, and build fixes.
- `run kernel --cleansrc-force` could rebuild a stale source version (`1.2.0-B14`) —
  `--cleansrc` now also purges the package's cached SRCDEST tarballs, and repo checkouts
  re-pin to the current sync-DB release on every sync.

## Security

- **Release provenance** — every release is GPG-signed end to end (commit, annotated tag,
  and source tarball). The release preflight hard-fails if signing is not usable, so an
  unsigned or unpublishable release can never be produced; downstream `makepkg` verifies
  the maintainer signature at install time.
