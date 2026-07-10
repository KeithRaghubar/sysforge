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

## Added

- `doctor`: new `storage` axis — build-dir free-space and `/etc/fstab` integrity
  checks (`--storage`); `shutil.disk_usage` now has a single home
  (`storage_probe.probe_free_space`), shared with the reconfigure disk step
  (1.2.0-F17, 1.2.0-F18).
- `doctor` `pacman` axis: package-cache size and mirrorlist-freshness warnings,
  thresholds via the new `[doctor]` config section (1.2.0-F15, 1.2.0-F16).
- `doctor` `state` axis: flags a live record-stage PGO build (bare `.profraw`,
  no merged `.profdata`) and points at `--pgo=use` or a repo rollback
  (1.2.0-F14).
- `doctor` `services` axis: broadened the current-boot journal scan beyond
  firmware to surface failed-start / core-dump / filesystem / OOM errors
  (1.2.0-F19).
- `revert-to-stock` verb: undo a source-built or optimized package back to its
  stock repo version — reinstalls the stock repo package, atomically replacing a
  conflict-mode `-sysforge` build (a lone `pacman -S`, so reverse deps stay
  satisfied) or removing-then-reinstalling a coexisting one (kernel FDO), then
  forgets the build-state entry so `update` stops rebuilding it. (1.2.0-F29)
- `make dev-install` / `make dev-uninstall`: install sysforge from a git checkout
  (editable venv entry point + system symlinks mirroring the package layout) for a
  quick trial or fresh dev-env, without going through the AUR. Verbose, idempotent,
  and fully reversible; the symlink mapping is parity-checked against the packaged
  set. (1.2.0-F31)
- `check-standards`: new `roadmap_ids` group cross-checks ROADMAP.md against
  `docs/release-notes/` — flags an open ID reusing a shipped number, an ID in
  both Planned and Abandoned, and a shipped `Q`-typed ID; warns on sequence gaps
  in the active version prefix. Adds `--next-id <version>-<TYPE>` to allocate the
  next free ID monotonically (2.1.0-F1).
- Global `--no-throttle` / `--turbo` flags: `--no-throttle` ignores the
  configured build throttle for a run; `--turbo` runs at higher-than-default
  priority (negative niceness, best-effort IO, no cap). Both route through the
  one throttle home via a run-scoped override (2.1.0-F5).
- `[build] cpu_quota` now also accepts a decimal fraction of the host's total
  cores (e.g. `0.5` → `800%` on a 16-core box), translated against
  `os.cpu_count()` so the same config is portable across machines (2.1.0-F6).
- `[kernel] keep_hotplug_drivers` (and `--keep-hotplug-drivers` /
  `--no-keep-hotplug-drivers`): re-enable hotplug driver classes (USB,
  USB4/Thunderbolt, MMC/SD, hot-plug PCI/CardBus, hot-plug HID) as modules after
  config minimization, via a dedicated post-minimization kconfig fragment so
  `localmodconfig` can't strip them. Off by default. (2.1.0-F2)

## Fixed

- Build-failure recovery menu `[c]` now offers the compiler as a coherent
  toolchain unit (`gcc` = `gcc`/`g++`, `clang` = `clang`/`clang++`) instead of
  two independent free-text prompts, so a retry can no longer end up with a
  mismatched `cc`/`cxx` pair; the menu enumerates only installed toolchains and
  keeps `[m]` as a manual escape hatch (2.1.0-B1).
- Regression test locks in the kernel `-docs` subpackage drop surviving the
  `-sysforge` coexist rename, with the doc build (`make htmldocs`) neutralized.
  (2.1.0-B8)
- Postflight diagnostics now recognize the lib32 clang + `ld.lld` `-lgcc_s`
  failure (`toolchain:lib32-clang-libgcc`): a 32-bit build where lld can't find
  the `libgcc_s` clang implicitly links, which CMake/meson mis-reports as the
  compiler being "broken". The diagnosis names the real cause and suggests a
  per-package `--cc gcc --cxx g++` override instead of the generic message
  (2.1.0-B10).
- `doctor` progress indicator no longer sits on "starting…" for the whole run —
  it now advances per axis and per audited package (2.1.0-B11).
- `update` source-sync no longer warns `no sync-DB candidate` for a coexist
  `-sysforge`-renamed repo package (e.g. `mesa-sysforge` from `--pgo=use`): the
  repo pin now queries pacman with the stock upstream base (`origin_pkgbase`,
  threaded via `SyncRequest.sync_db_name`) instead of the renamed name pacman
  never knew (2.1.0-B12).
- Self-install sentinel is now created group-writable so a `sysforge update`
  run under a different uid in the `sysforge` group (e.g. after a prior
  `sudo sysforge` run) can append to it instead of failing with EACCES; a
  pre-existing umask-644 file is healed on the next owner write. The setgid
  state dir only grants group ownership, not the group-write bit (2.1.0-B13).
- `run toolchain` input-fingerprint reuse (`reuse_unchanged` / `--reuse-built`)
  now actually fires on the profdata-reuse resume path. The Pass-4 fingerprint's
  compiler dimension was keyed on the clang binary's path + size + mtime, so the
  staged stage-2 clang of the profgen run never matched the `/usr/bin/clang` of a
  resume — guaranteeing a cache miss and a full rebuild of the heaviest packages.
  It now keys on the compiler `--version` line only (the trained toolchain is
  already pinned by the profdata hash), and the reuse cache moved to a sibling of
  `pgo_store` so a fresh 4-pass run's startup purge no longer wipes it. Fingerprint
  `_SCHEMA` bumped to 2 (2.1.0-B14).
- Interactive `sysforge run kernel` now shows the kconfig review menu even when the
  PKGBUILD uses `make oldconfig` (a resolve step was wrongly treated as an operator
  review, suppressing the injected `make nconfig`). (2.1.0-B17)
- `doctor` no longer advises `sudo ldconfig` for an unsatisfied soname whose owner
  is already installed — that branch is only reached once the library is confirmed
  absent from every directory ldconfig scans, so the cache rebuild cannot help. It
  now points at refreshing the files db (`sudo pacman -Fy`) or rebuilding the
  dependent package (2.1.0-B18, promoted from 2.1.0-Q1).
