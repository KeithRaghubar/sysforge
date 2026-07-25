# sysforge (unreleased)

A kernel-stage correctness release: kernel builds now install exactly the
packages they built and never degrade to the full package cache, the interactive
kconfig review can no longer be silently skipped, and `SYSFORGE_TARGET` journald
correlation is extended to every mutating verb under one namespaced scheme.

## Added

- **`2.4.0-F1`** — `SYSFORGE_TARGET` is now emitted for every sentinel-gated
  mutating verb (`uninstall`, `revert-to-stock`, `state forget`,
  `state failed --clear`, `state repair`, `state orphans --prune`), not just
  `build`. Values are namespaced `pkg:<names>` for package subjects and
  `mode:<subcommand>` for subjectless state operations, so
  `journalctl SYSFORGE_TARGET=pkg:<pkg>` correlates the operations most worth
  reviewing during an incident. Prefixes are formatted in one place
  (`journal.pkg_target`/`journal.mode_target`).
- **`2.4.0-F3`** — `doctor --rust`: advisory, read-only axis reporting the Rust
  toolchain a build will actually use — the effective cargo/rustc owner (rustup
  channel or distro `rust` package), a warning when the active rustup default is
  non-stable (nightly/beta/pinned), and, for named package targets, a
  `rust-toolchain.toml` pin plus whether that toolchain is installed (uninstalled
  = a mid-build network fetch). Never rewrites a pin; never fails a run. Opt-in.

## Changed

- **Breaking (journald):** `build`'s `SYSFORGE_TARGET` value is now namespaced
  `pkg:<names>` (was the bare `<names>`), bringing it into the same scheme as the
  other mutating verbs. Migration: update any `journalctl SYSFORGE_TARGET=<pkg>`
  filter for `build` to `journalctl SYSFORGE_TARGET=pkg:<pkg>`. (2.4.0-F1)
- Toolchain LLVM source pre-flight now annotates split-off members (e.g.
  `llvm-libs`, built from the `llvm` PKGBUILD) as `(split of llvm)` instead of
  showing empty `origin=missing`/`sync=missing` source columns for a tree they
  don't own. The pre-flight guards source trees (pkgbases), so a split binary's
  per-member row was pure noise. (2.5.1-B2)
- One AlreadyBuilt policy seam (`2.5.1-F2`): makepkg exit 13 is now interpreted
  by a single policy helper (`primitives/already_built.resolve_already_built`)
  routed from all three catch sites — kernel (unchanged B5 prompt semantics),
  build_core (unchanged silent reuse), and toolchain, where a stale PKGDEST
  artifact previously crashed the pass with an unhandled error and now reuses
  the artifact.

## Fixed

- Toolchain Gate-3 no longer false-rolls-back on a pkgrel-only LLVM suite skew:
  an independent `llvm` rebuild (e.g. `-2`) beside `clang`/`lld`/… at `-1` is a
  legitimate state, not an interrupted install. `detect_suite_skew` now enforces
  pkgver lockstep across the suite plus full `pkgver-pkgrel` lockstep only within
  the shared-pkgbase pair `llvm`/`llvm-libs`. (2.5.1-B1)
- Pass-4 toolchain build-reuse cache no longer misses on a post-install sanity
  re-run. The input fingerprint folded in the *installed* versions of the LLVM
  suite (`llvm`/`llvm-libs`/…), but Pass 4 links the *staged* libLLVM
  (`--nodeps`), so installing the just-built suite moved those versions and
  invalidated every otherwise-identical package. Build-set members are now
  excluded from `makedep_versions` (the staged libLLVM is still pinned via the
  Merkle-chained `staged_dep_fps`); external build-deps still invalidate.
  Fingerprint schema bumped to v3. (2.5.1-B2)
- Kernel-stage install no longer installs the wrong packages — in the worst
  case the *entire shared PKGDEST*. Three faults compounded: (1) the built-file
  parser anchored on a bare `<pkgname>-` prefix, so pkgname `linux` matched
  `linux-custom`, `linux-sysforge`, `linux-steam-integration`, etc. (a lenient
  `rsplit` let the bogus tail parse as a valid version); (2) the build-time
  manifest that scopes a *renamed* kernel (`linux` → `linux-sysforge`) was
  captured only for builds with an extracted profile, so a rename-only kernel
  build never recorded one; (3) with no manifest and no pkgname match, artifact
  scoping degraded to the full PKGDEST union and handed hundreds of unrelated
  packages to `pacman -U`. Fixes: the parser now requires an exact
  `[epoch:]pkgver-pkgrel-arch` tail (three hyphen-delimited fields, per
  `PKGBUILD(5)`'s no-hyphens-in-pkgver rule); the manifest is captured for any
  name-affecting build (extracted profile **or** rename); and scoping never
  degrades to the full PKGDEST — an unscopable install now fails loudly rather
  than installing everything. (2.5.1-B3)
- Kernel-stage re-run no longer fails with "no built package found — nothing to
  install" when the renamed artifacts already exist in PKGDEST from a prior run.
  makepkg refuses to rebuild an already-built package and surfaces `AlreadyBuilt`,
  which the stage correctly treats as "proceed to install" — but the build-time
  manifest that scopes a *renamed* kernel (`linux` → `linux-sysforge`) was only
  captured on the fresh-build success path, so the `AlreadyBuilt` short-circuit
  left no manifest and pkgname scoping (reading the un-patched on-disk PKGBUILD)
  couldn't name the `linux-sysforge-*` artifacts. The manifest is now also
  captured on the `AlreadyBuilt` path, under the same rename/extracted-profile
  guard, so the decoupled install step can locate the existing artifacts. This
  completes 2.5.1-B3, which fixed the fresh-rename case but not the re-run one.
  (2.5.1-B4)
- An interactive kernel run no longer silently skips the promised kconfig
  review when makepkg short-circuits with "package already built" (a stale
  same-version package in PKGDEST — exit 13 skips `prepare()`, and the
  interactive `make nconfig` review lives inside it). The stage now warns that
  the review did not run and prompts: install as-built, rebuild with `-f` so
  the review actually happens, or abort (the default). Unattended runs
  (`--non-interactive`, `interactive = false`, or no TTY) keep the proceed
  behaviour. (2.5.1-B5)
- The kernel Gate-2 kconfig drift check now WARNs (was INFO) when it cannot
  run because no resolved `.config` exists in the build tree — the standing
  state on every "package already built" re-run, where the advisory audit
  silently never verified the merged options against the built kernel. The
  message now names the cause and the consequence. (2.5.1-B6)
- Kernel install failures are now diagnosable and the stale-package re-run
  loop is broken: `pacman -U` failures name the artifacts they tried to
  install (and, on interactive installs, note that pacman's output went to
  the terminal uncaptured and that a declined prompt also exits 1); when the
  failed install was of a previously built package (the "already built"
  path), the error points at the exit — fresh build via pkgver/pkgrel bump or
  removing the stale package(s) from PKGDEST. (2.5.1-B7)
- The kernel stage's interactive-kconfig gate now requires a real TTY, matching
  the stage's other prompts (config ∧ no `--non-interactive` ∧ TTY). Previously
  it consulted only config + flag, so a piped/captured run "promised" an
  nconfig review that could never render and silently EOF'd through it. A
  config-requested review downgraded by a missing TTY is now WARNed instead of
  silent. (2.5.1-B8)
- Kernel-stage builds now always get the kernel PKGBUILD patchers. The wrapper
  derived `kernel_build` solely from profile rule matching (`build_mode ==
  "kernel"`), so without a `[[rules]]` entry mapping the kernel package to the
  kernel profile — the shipped default ships that rule commented out — every
  kernel patcher silently no-oped: no `sysforge.config` fragment merge, no
  `kconfig_targets` sequence, no interactive `make nconfig`, while the stage
  logged the full kconfig plan as if it applied. The stage's
  `owner_stage="kernel"` stamp is now authoritative; profile derivation remains
  for rule-routed builds. (2.5.1-B9)
- The `check-roadmap-table` guard's own tests no longer silently stop testing
  anything when `ROADMAP.md` is reworded. Four tests cloned the live roadmap and
  mutated it with `str.replace()` keyed on an exact prose substring; because
  `str.replace()` cannot fail, a reword (and the shipping of the entry they keyed
  on) turned every mutation into a no-op, so the generator was handed an
  unmodified file and the "drift is detected" assertions failed. They now build a
  synthetic roadmap fixture and retag entries by **ID**, asserting the edit
  actually changed something — so a fixture that stops matching fails loudly as a
  fixture error instead of degrading into a vacuous test. (2.5.1-B11)
