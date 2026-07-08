# sysforge (unreleased)

<!--
Running accumulator for the next release. Every landing commit that COMPLETES a
ROADMAP item appends its entry here (in the same commit that drops the item from
ROADMAP.md), under the matching Keep a Changelog section — one of Added, Changed,
Deprecated, Removed, Fixed, Security, in that order. Reference the roadmap ID
inline, e.g. (1.2.0-F35). Flag breaking changes with a **Breaking:** prefix and
the migration path. At release time tools/release.sh (Phase 1) renames this file
to vX.Y.Z.md, stamps the `# ` title with the version and date, and reseeds a fresh
accumulator. Run the release-notes skill first to reconcile/lint the entries and
finalize the one-line summary below (drop this comment). Keep a Changelog:
https://keepachangelog.com/en/1.1.0/
-->

### Changed

- Bootstrap disk + base install + system identity now run through archinstall
  (generated from `bootstrap.toml` via its headless JSON config + `--silent`),
  replacing the hand-rolled partition/base-install/identity stages. The
  `configure` stage now applies only sysforge-specific tuning. (`2.0.1-F3`)

### Fixed

- The packages stage no longer narrates pacman installs as source builds. The
  progress bar hardcoded a `building` verb for every manifest entry, so a
  desktop package installed with `pacman -S` (e.g. `gnome-shell` after choosing
  GNOME) still showed `building · gnome-shell` even though nothing was compiled.
  The verb now tracks the branch actually taken — `installing` for repo packages
  in pacman mode, `building` for source-built (aur/git/local or
  `build_from_source`) entries. (2.1.0-B7)

- The live-ISO installer no longer writes a mirror country that crashes
  archinstall with `KeyError: 'CA'` during mirror selection. `iso-install.sh`'s
  country prompt accepted a bare ISO code (`CA`) because `reflector --country`
  takes codes *and* names — but archinstall's `mirror_regions` is keyed only by
  the full region names it scrapes from archlinux.org, so a code raised a
  `KeyError` deep in `set_mirrors`. The prompt still accepts either form but now
  normalizes to the canonical full name (`Canada`) before writing
  `bootstrap.toml`, the value both `reflector` and archinstall accept. (2.1.0-B6)

- The bootstrap install stage no longer generates an archinstall config that
  crashes partitioning with `Can't have the end before the start! (… length=0)`.
  The root partition was emitted with `size: {value: 0}` on the assumption that
  archinstall reads that (with `total_size: null`) as "fill the remaining
  disk" — but the 3.0.15 headless schema has no fill sentinel: it converts the
  size straight to a sector length, so `0` produced a zero-length partition
  parted rejected. The root size is now a concrete value computed from the real
  disk (probed with `lsblk`), filling the space after the ESP minus a 1 MiB GPT
  tail. A real run that can't probe the device fails fast instead of
  partitioning blind; `--dry-run` uses a nominal size to preview. (2.1.0-B5)

- The bootstrap pipeline no longer refuses to start as root on the live ISO.
  `sysforge run pipeline` blanket-blocked euid 0 because the pipeline verb was
  marked makepkg-bearing — but the bootstrap phase (install/hardware/configure/
  reconfigure) legitimately runs as root on the ISO, where root is the only
  account until the install stage creates the user. The no-root rule now lives
  on the three build stages (`toolchain`/`packages`/`kernel`) and is enforced
  per stage by the runner, so the root-run bootstrap phase proceeds and a
  post-reboot resume that re-enters as root still fails fast at the first build
  stage. (2.1.0-B4)

- Build-failure recovery compiler swaps now persist for single-package
  PKGBUILDs. After a recovery-menu compiler swap on a package without an
  explicit `pkgbase=` line (the common non-split case), the resulting override
  was silently dropped: `_run_build` keyed the persist call on the raw,
  absent `pkgbase` global, so it no-op'd on the empty-key guard and
  `[package_compiler_overrides]` in `profiles.toml` stayed empty — leaving the
  next `update` to re-trigger the same failure. The persist now uses the same
  pkgbase-or-pkgname key the recovery menu labels with and `resolve_profile`
  reads the override back with, so a known-good swap self-heals subsequent
  builds. (2.1.0-B2)

- Toolchain stage no longer refuses on a spurious "dirty (no upstream tracking
  branch)" LLVM blocker for release-tag checkouts. A `source=repo` LLVM tree
  pinned to a tag sits on a detached HEAD (no `@{u}`) whose commit is still
  reachable from `origin` — upstream's own history, not local work. The
  pre-flight report now applies the same `head_reachable_from_remote` test
  `purge_src` already uses, so such clean trees pass and no longer wedge the
  stage where no `--cleansrc-force` could ever clear the blocker. (2.1.0-B3)

- The kernel stage's final install no longer sweeps in unrelated and stale
  `linux*` packages. The "Installing built package(s)" step discovered
  artifacts by pkgname-scoping a shared PKGDEST, but a renamed kernel's patched
  PKGBUILD (`-sysforge` rename, dropped `-docs`) is cleaned up by install time,
  leaving only the upstream PKGBUILD (pkgbase `linux`) — whose name
  prefix-matches `linux-custom`, `linux-steam-integration`, and every stale
  `linux-sysforge-<oldver>`, so the install fell back to the whole PKGDEST and
  could downgrade the running kernel. The build now records the exact package
  filenames it emits (from `makepkg --packagelist` against the patched
  PKGBUILD, while it still exists) into a sidecar manifest, and the install
  matches against that set precisely, falling back to pkgname scoping only when
  no manifest is present. (2.1.0-B9)
