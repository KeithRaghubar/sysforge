<!-- GENERATED FILE -- do not edit directly.
     Source of truth: the modular files under docs/design/ (see
     docs/design/index.md and docs/design/_manifest). Edit those, then run
     `make design`; `make check-design` guards against drift. -->

# SysForge Design Document

SysForge is an Arch Linux build and maintenance suite with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles, tracks build state for update detection, and automates the full build lifecycle — from fetching PKGBUILDs to installing profiled packages. Pacman owns the package database; SysForge owns the build configuration layer above it.

SysForge manages the profiled AUR-helper surface (install, update, and manage AUR and custom packages with system-tuned profiled builds) and a full bootstrap pipeline (stages 1–4: partition, base install, hardware detection, configure) that automates a fresh Arch install from the ISO. Current release is **<!--version-->v1.2.0<!--/version-->**; per-release changes are recorded in `docs/release-notes/`.

---

## Table of Contents

1. [Philosophy](#philosophy)
2. [Distribution Model](#distribution-model)
3. [Architecture Overview](#architecture-overview)
4. [Directory Structure](#directory-structure)
5. [Package Manifest](#package-manifest)
6. [Config Layer](#config-layer)
7. [Pipeline Layer](#pipeline-layer)
8. [CLI Verb Framework](#cli-verb-framework)
9. [Verbs](#verbs)
10. [Primitives Layer](#primitives-layer)
11. [Flag Profile System](#flag-profile-system)
12. [Makepkg Wrapper](#makepkg-wrapper)
13. [Logging](#logging)
14. [Man Pages](#man-pages)
15. [Hardware Detection](#hardware-detection)
16. [Cache Management](#cache-management)
17. [Graphics Stack Build Order](#graphics-stack-build-order)
18. [Release Process](#release-process)
19. [Drift detection](#drift-detection)
20. [Known Gaps](#known-gaps)
21. [Roadmap](#roadmap)
22. [Standards & Specifications](#standards-specifications)

---

## Philosophy

SysForge was motivated by source-based distros' compile-time control and performance tuning, without their fragility and maintenance overhead. The core insight is that source-based systems conflate several concerns that are better separated:

- **Hardware profiling** — what the machine has
- **Compiler flags** — how to build for it
- **Feature selection** — what to enable

SysForge separates these into distinct config layers and produces a standard mutable Arch system as output. It is not a distro. There is no ISO, no divergence from upstream Arch, no custom package ecosystem.

---

## Distribution Model

SysForge ships as an AUR package. The PKGBUILD points at the GitHub repo as its source.

**Bootstrap path:**
1. Boot vanilla Arch ISO
2. Install SysForge from AUR
3. Run SysForge

SysForge lives in the Arch ecosystem and produces a standard Arch system as output.

**Installed paths:**
- `/etc/sysforge/` — system defaults (owned by the package)
- `~/.config/sysforge/` — user overrides
- `/usr/bin/sysforge` — CLI entry point

---

## Architecture Overview

Three layers:

```
┌─────────────────────────────────────────┐
│  Config                                 │
│  TOML profiles + hardware overlays      │
├─────────────────────────────────────────┤
│  Pipeline                               │
│  Python DAG orchestrator                │
│  checkpoint/resume across stages        │
├─────────────────────────────────────────┤
│  Primitives                             │
│  PKGBUILD parser, makepkg wrapper,      │
│  dep analysis, flag extraction          │
└─────────────────────────────────────────┘
```

**Import direction:** `cli.py` → `verbs/runner.py` → command modules (`update.py`, `packages_cmd.py`, `resolve.py`, …) → `primitives/*`. Each command module defines a `*Verb(Verb)` subclass alongside its existing helpers; the runner dispatches uniformly across them. No command module imports from another command module. See [CLI Verb Framework](#cli-verb-framework).

---

## Directory Structure

### Development (local repo)

```
sysforge/
├── sysforge/
│   ├── __init__.py
│   ├── cli.py                         # CLI entry: argv preprocessing + argparse parser construction + verb dispatch (verb classes live in their per-command modules)
│   ├── log.py                         # structured logging (stderr + optional file output)
│   ├── ui/
│   │   ├── __init__.py
│   │   ├── headers.py                 # shared visual primitives (welcome banner, stage banners, stage list, closing rule)
│   │   └── progress.py                # bottom-anchored batch progress indicator (TTY scroll region + plain fallback)
│   ├── resolve.py                     # sysforge resolve subcommand
│   ├── update.py                      # sysforge update subcommand
│   ├── build_core.py                  # shared build engine behind `build` + `update` (dep prep, build loop, install)
│   ├── build_cmd.py                   # sysforge build subcommand (BuildVerb; routes through build_core)
│   ├── run_cmd.py                     # sysforge run namespace verbs (pipeline/hardware/reconfigure/toolchain/packages/kernel)
│   ├── env_cmd.py                     # sysforge env subcommand (read-only env-chain inspector)
│   ├── completions_cmd.py             # sysforge completions data sink (consumed by _sysforge)
│   ├── doctor.py                      # sysforge doctor subcommand (ABI/linkage health check)
│   ├── fetch.py                       # sysforge fetch subcommand (download PKGBUILDs, no build)
│   ├── packages_cmd.py                # sysforge packages namespace (list/add/remove)
│   ├── state_cmd.py                   # sysforge state namespace (list/repair) — build_state.toml
│   ├── setup_cmd.py                   # sysforge setup subcommand (pacman IgnoreGroup = sf-build guard)
│   ├── verbs/
│   │   ├── base.py                    # Verb ABC + PreCheckResult/ExecResult result types
│   │   ├── runner.py                  # run_verb dispatch + sentinel wrapping
│   │   └── helpers.py                 # shared verb helpers (load_config_with_overrides)
│   └── primitives/
│       ├── paths.py                   # config path constants + resolve_packages_path()
│       ├── stage_ownership.py         # stage→package ownership registry (update skip bootstrap)
│       ├── config.py                  # TOML config loading, path constants, system conf parsing
│       ├── pacman.py                  # pacman queries, batch install, makedep helpers
│       ├── profile.py                 # profile resolution, rule matching, consumes
│       ├── pkgbuild_meta.py           # static PKGBUILD parser (read-only)
│       ├── pkgbuild_patcher.py        # PKGBUILD mutation + flag extraction
│       ├── prompt.py                  # shared interactive-prompt helpers (every stage uses these)
│       ├── makepkg_wrapper.py         # build execution: emit conf, invoke makepkg
│       ├── makepkg_flags.py           # makepkg flag-string transforms (owns [FLAG] tag)
│       ├── makepkg_artifacts.py       # built .pkg.tar* discovery + filename→version parse (pure, no tag)
│       ├── makepkg_pgo.py             # PGO profdata state resolution (pure, no logger)
│       ├── makepkg_env.py             # subprocess env resolution + effective build dir (owns [ENV] tag)
│       ├── makepkg_conf.py            # temp makepkg.conf emission ctx-mgr (owns [CONF])
│       ├── makepkg_invoke.py          # makepkg subprocess invocation + sudo-timeout retry (owns [MAKEPKG])
│       ├── pty_runner.py              # spawn subprocess on a pty (preserves cargo's live progress bar)
│       ├── aur_resolve.py             # recursive AUR dependency resolution + topo sort
│       ├── dep_analysis.py            # pre-build soname dependency checks
│       ├── abi_check.py               # post-build versioned-symbol ABI check (.so cross-ref)
│       ├── provides_lookup.py         # reverse-lookup soname → package (pacman -Fq)
│       ├── diagnostics.py             # unified Finding framework (severity, adapters, axis runner, renderer)
│       ├── graphics_probe.py          # system-state graphics checks (NVIDIA, Wayland, Steam)
│       ├── device_probe.py            # full PCI/USB inventory + driver-coverage (modalias→module→CONFIG)
│       ├── kernel_safety.py           # kernel boot-safety: .config audit, fallback/boot-artifact/DKMS checks
│       ├── system_probe.py            # doctor pacman axis: -Dk, db.lck, .pacnew, orphans (read-only)
│       ├── state_probe.py             # doctor state axis: build failures, stale sentinel, state drift
│       ├── runtime_probe.py           # doctor services axis: failed units, missing firmware
│       ├── failure.py                 # failure scenario handling (shared)
│       ├── resource_guard.py          # controller RLIMIT_AS cap + lift_for_child() for subprocesses
│       ├── cache_probe.py             # passive ccache/sccache monitoring ([CACHE] tag)
│       ├── aur.py                     # AUR RPC v5, git clone, pkgctl checkout, GPG key import
│       ├── rate_limit.py              # shared RPC + git fetch rate limiter (RateLimiter, RateLimited)
│       ├── source_meta.py             # per-package AUR RPC + git HEAD cache (source_meta.toml)
│       ├── source_sync.py             # process-wide SourceSyncScheduler (RPC-first, sequential)
│       ├── build_state.py             # per-package build metadata persistence (build_state.toml)
│       └── version.py                 # vercmp wrapper + version string formatting
│   └── pipeline/
│       ├── __init__.py
│       ├── runner.py                  # stage sequencing, checkpoint/resume
│       ├── state.py                   # pipeline_state.toml read/write
│       └── stages/
│           ├── __init__.py            # STAGES ordered list
│           ├── base.py                # Stage base class, RunOptions dataclass
│           ├── _bootstrap.py          # shared bootstrap config loader (BootstrapConfig dataclass)
│           ├── partition.py           # stage 1: GPT partitioning, mkfs, mount
│           ├── base_install.py        # stage 2: pacstrap + genfstab
│           ├── hardware.py            # stage 3: CPU/GPU/NVMe detection + PCI/USB inventory → hardware_profile.toml
│           ├── configure.py           # stage 4: hostname, locale, timezone, bootloader, user, services (arch-chroot)
│           ├── reconfigure.py         # stage 5: pre-build checkpoint
│           ├── toolchain.py           # stage 6: LLVM/GCC toolchain build (optional 4-pass PGO)
│           ├── packages.py            # stage 7: package builds
│           └── kernel.py              # stage 8: kernel build
├── tests/
│   ├── conftest.py
│   ├── data/
│   │   ├── PKGBUILDs/                 # htop, llvm, lib32-llvm, cosmic, vulkan-headers-git
│   │   │   ├── complex.PKGBUILD  →  vulkan-headers-git.PKGBUILD  (symlink alias)
│   │   │   ├── complex2.PKGBUILD →  lib32-llvm.PKGBUILD           (symlink alias)
│   │   │   └── simple.PKGBUILD   →  htop.PKGBUILD                 (symlink alias)
│   │   ├── test_profiles.toml
│   │   ├── etc/sysforge/
│   │   │   ├── profiles.toml
│   │   │   ├── packages.toml
│   │   │   ├── toolchain.toml
│   │   │   └── kernel.toml
│   │   └── user/.config/sysforge/
│   │       └── profiles.toml
│   ├── test_append_merge.py
│   ├── test_consumes.py
│   ├── test_dep_analysis.py
│   ├── test_env_pass.py
│   ├── test_aur.py
│   ├── test_cache_probe.py
│   ├── test_cli.py
│   ├── test_failure.py
│   ├── test_log.py
│   ├── test_parser.py
│   ├── test_patcher.py
│   ├── test_pipeline.py
│   ├── test_pipeline_runner.py
│   ├── test_pipeline_state.py
│   ├── test_reconfigure.py
│   ├── test_resolve.py
│   ├── test_stage_bootstrap.py
│   ├── test_stage_kernel.py
│   ├── test_stage_packages.py
│   ├── test_system_conf.py
│   ├── test_update.py
│   ├── test_build_state.py
│   ├── test_version.py
│   └── test_wrapper.py
├── completions/
│   └── _sysforge                      # zsh completion script
├── tools/
│   ├── iso-install.sh                 # bootstrap helper: installs sysforge on live ISO, writes bootstrap.toml
│   └── vm/
│       ├── boot.sh                    # launch QEMU VM (--iso, --snapshot modes)
│       └── bootstrap.toml             # VM-specific bootstrap.toml for testing
├── PKGBUILD
├── pyproject.toml
├── Makefile
└── DESIGN.md
```

### Installed

```
/etc/sysforge/
    profiles.toml                    # flag profiles, [[rules]], conflict groups, consumes inference
    packages.toml                    # default build-rule overrides
    bootstrap.toml                   # written by iso-install.sh (or hand-copied from the example)
/usr/share/sysforge/
    bootstrap.toml.example           # starter template for manual hand-edit setups
/usr/bin/sysforge
~/.config/sysforge/                  # $XDG_CONFIG_HOME/sysforge
    profiles.toml                    # user overrides (optional; merges with system via extends_system)
~/.cache/sysforge/                   # $XDG_CACHE_HOME/sysforge
    aur-packages.txt                 # AUR name cache (regenerable, refreshed every 24h)
~/.local/state/sysforge/             # $XDG_STATE_HOME/sysforge
    (state files)                    # fallback runtime state when /var/lib/sysforge is not writable
/var/cache/sysforge/                 # regenerable build cache (override via SYSFORGE_PGO_STORE); provisioned 0777 by tmpfiles.d, sudo-created at runtime if absent
    llvm-pgo/                        # LLVM PGO profraw/profdata store (written by unprivileged makepkg)
/var/lib/sysforge/
    pipeline_state.toml              # pipeline checkpoint state (created at runtime)
    build_state.toml                 # per-package build metadata (created at runtime, by sysforge build/update)
    source_meta.toml                 # per-package AUR RPC + git HEAD snapshot used by the source_sync scheduler
    sysforge.log                     # unified log (created at runtime, cleared on success)
```

#### makepkg-owned paths (not sysforge dirs)

The PKGBUILD source tree, build workspace, and package output directory are **makepkg's domain**, not sysforge XDG/FHS dirs, so they fall outside the `paths.py` discipline of §21-standards:

- **`pkgbuild_src_dir`** (`~/src` by default, `[paths] pkgbuild_src_dir` in `profiles.toml`) — production PKGBUILD checkouts. FHS-style user source code, deliberately *not* under `$XDG_*` (it is not regenerable cache).
- **`BUILDDIR`** (`$HOME/builds` by default, set in `[profiles.bare]`) — makepkg's build workspace. Kept as a top-level `~/builds` rather than `$XDG_CACHE_HOME/sysforge` on purpose: it is a **shared** workspace the user also builds in manually (sysforge does not own it), and `~/src`/`~/builds` form a matched pair of user working dirs. It is fully user-overridable via the `BUILDDIR` profile key or `/etc/makepkg.conf`; sysforge resolves the effective value through `pacman.get_builddir()` (env → system conf), never assuming the default.
- **`PKGDEST` / `SRCDEST` / `LOGDEST`** — left to makepkg/`/etc/makepkg.conf`; sysforge never writes a default and reads them via `pacman.get_pkgdest()` / `get_srcdest()` / `get_logdest()` when it needs to locate an artifact, source, or log.

The optional `[makepkg]` table in `bootstrap.toml` (`packager`, `makeflags`) lets an unattended install stamp `PACKAGER` / `MAKEFLAGS` into the target `/etc/makepkg.conf` during the configure stage; on a running system the `reconfigure` makepkg step offers the same interactively.

---

## Package Manifest

Two files split the responsibility cleanly, and keeping them distinct is what
makes the model legible:

- **`build_state.toml` — the registry of what sysforge maintains.** It is the
  **authority** for steady-state tracking. Any installed package sysforge built
  from source (its record has `build_mode != "pacman"`) is maintained — i.e.
  `sysforge update` rebuilds it from source as upstream advances — with no
  packages.toml entry required. `build_mode = "pacman"` records are inert
  install-markers written by `sync_with_installed` for everything else installed.
- **`packages.toml` — the declared manifest** of *intent*: the bootstrap install
  set, package groups, and per-package **build overrides**. It does **not** drive
  steady-state tracking; that is build_state's job.

`packages.toml` plays two roles depending on context:

1. **Bootstrap (pipeline `run packages` stage):** every entry is installed. The manifest *is* the install list, because the system has nothing installed yet beyond the pacstrap base.
2. **Steady-state (`sysforge update`, `sysforge build`):** entries act as **build-rule overrides** applied to the live install set. Pacman owns the install set; `build_state.toml` mirrors it and is the tracking authority. An entry whose package is not currently installed is an inert rule, not a "missing" item.

This dual role is intentional: the manifest captures your declared intent, but at steady-state we respect the live system rather than reconciling against the manifest.

### What `sysforge update` maintains (steady-state scope)

In one sentence: **sysforge maintains what it built.** Concretely, `update`'s
walk is the union of —
- everything sysforge source-built (build_state `build_mode != "pacman"`) — this
  is what makes `sysforge build mesa` *durable*: the optimized repo build is
  rebuilt from source on every update instead of being frozen behind
  `IgnoreGroup = sf-build`;
- every foreign package (`pacman -Qm` — AUR/local installed outside or before
  sysforge), with default rules and no overrides;
- repo packages explicitly opted in via a packages.toml override
  (`pkgbuild_patch` / `cache` / `reason`) or globally via `repo_mode = "profiled"`.

Stage-owned packages (kernel/toolchain) are excluded from the default walk
(`--include-stage-owned` or naming them overrides). To **stop** maintaining a
package, drop its record with `sysforge state forget <pkg>` — the installed
artifact is left in place (still pinned by the `sf-build` group), so reverting to
the stock repo binary is a separate `pacman -S <pkg>`. Uninstalling a package
also auto-stops tracking (`sync_with_installed` prunes its record).

The orthogonality of the roles means:
- An entry for `mesa-git` can stay in the manifest even if you've rolled back to repo `mesa` — it's an inert rule at steady-state, but the next pipeline bootstrap of a fresh system would still install it.
- An installed AUR package without an entry uses default rules — `sysforge update` still walks it via `pacman -Qm`, just with no overrides applied.
- `profiles.toml` and the manifest stay orthogonal: sourcing/patching choices vs. compiler flag tuning.

Each entry overrides at most these fields (all optional except `name`):
- `source` — `repo` (pacman) vs `aur`/`git`/`local`. A **routing hint**: it tells the bootstrap/build paths how to obtain the package, falling through to pacman / AUR RPC inference if omitted. It does **not** by itself put a package under steady-state tracking — tracking comes from sysforge having built the package (build_state), not from a manifest entry. So an entry with only `name` + `source` has no override effect at steady-state, and `sysforge packages add` rejects it (pair it with a behavior-changing field — `pkgbuild_patch`, `cache`, `reason` — for the entry to override anything). When sysforge builds a package, it records the resolved `source` in build_state, so the registry is self-describing without re-inferring origin later.
- `pkgbuild_patch` *(bool)* — if `true`, the PKGBUILD patching library runs on this package before build.
- `cache` *(bool)* — `false` disables ccache/sccache for this package (required for PGO stages).

```toml
[build]
pkgbuild_src_dir = "~/src"   # PKGBUILD source tree; auto-cloned if absent
repo_mode = "profiled"       # default for repo packages: "pacman" | "profiled"

[[package]]
name = "mesa-git"
pkgbuild_patch = true        # override: patch flags before building

[[package]]
name = "llvm"
cache = false                # override: never cache instrumented PGO objects
```

An entry with only `name` and no override fields has no effect on the build. `sysforge packages add` rejects such calls.

### `[build]` global section

- `pkgbuild_src_dir` — directory holding pre-cloned PKGBUILDs (`<pkgbuild_src_dir>/<name>/PKGBUILD`). Missing AUR clones are auto-fetched here on demand.
- `repo_mode` — controls how the **bootstrap** (`run packages`) builds repo-source entries: `"pacman"` (install via `pacman -S --needed`) or `"profiled"` (build from PKGBUILD with sysforge flag profiles); per-package `pkgbuild_patch = true` forces profiled regardless. At **steady-state** `repo_mode = "profiled"` is a *bulk drift-surfacing* switch: it pulls **every** installed repo package into `sysforge update`'s walk so repo-side version drift is reported alongside AUR drift. It is **not** how you get a repo package source-built going forward — that happens automatically once sysforge has built it (build_state authority; `sysforge build mesa` is the natural entry). Of the bulk set, only the overridden / already-source-built subset is rebuilt from source; the remainder takes a fast pacman path (`checkupdates` for upgrade detection, one terminal `sudo pacman -Syu` after the source-build loop). This avoids a per-package `pkgctl repo clone` for every installed repo package and is what makes the "track everything" mode tolerable on a maintained workstation.

### Package groups

`[group.<name>]` tables declare named sets that expand into `[[package]]`-equivalent entries at load time, so a desktop stack (e.g. 20+ git packages) can be tracked without enumerating every member as its own block:

```toml
[group.cosmic]
packages = ["cosmic-session-git", "cosmic-comp-git", "cosmic-settings-git"]
# Optional defaults inherited by every member:
# pkgbuild_patch = true
```

Expansion semantics (single expansion point: `primitives/config.expand_package_groups` — every manifest consumer routes through it; do not re-expand `[group.*]` elsewhere):

- Each member becomes a synthetic entry carrying its group defaults plus `group = "<name>"` marking its origin.
- An explicit `[[package]]` entry for the same name wins **outright** over the group entry — no field merge — so a member can be individually overridden.
- The first group to claim a name wins over later groups.
- Bootstrap (`run packages`): members are installed like any entry. Steady-state (`sysforge update`): members participate as overrides; a member with no group defaults is legitimately inert (its meaning is the bootstrap set) and is exempt from the inert-override warning that hand-written entries get.
- `packages list` shows groups as written in the file (name, member count, defaults, members), after the explicit-entry table. Groups are hand-edited TOML, **or** written by the guided desktop selection below; `packages add`/`remove` manage explicit `[[package]]` entries only.

#### Curated desktop catalog

`primitives/pkg_catalog.py` ships a small curated catalog of desktop-environment groups (currently `gnome` and `kde`) and is the single home for the catalog, the guided selection prompt (`select_desktop`), and the `[group.*]` writer (`write_desktop_group`). It lives in the primitives layer because three surfaces consume it:

- **`sysforge packages add-group <gnome|kde>`** — writes the chosen catalog group into `packages.toml` (idempotent: re-running replaces the same-named group block; other `[[package]]` blocks and tables are preserved byte-for-byte). Creates the file with the standard header if absent.
- **Configure stage (bootstrap, stage 4)** — after copying config into the target, `select_desktop` resolves the choice: `bootstrap.toml [desktop] environment` wins non-interactively (unattended installs); otherwise a TTY run prompts ("Install a graphical desktop? → numbered menu"); a non-TTY run with no preselection skips. The group is written into the *target's* `packages.toml` so the later packages stage installs it.
- **Reconfigure step `desktop`** — offers the same guided selection on a live system, writing to the live manifest.

The writer only adds `[group.*]` text; expansion stays in `config.expand_package_groups`. The catalog is intentionally minimal (a core session + display manager per entry) — users extend their own group afterward.

### Manifest lifecycle commands

`sysforge packages` is a small namespace for managing override entries:

- **`packages list`** (default when no subcommand) — tabulates entries: name and any override fields set. `--orphans` lists entries whose package is not currently installed (informational only; entries are still valid rules).
- **`packages add <pkg> [--source ...] [--pkgbuild-patch] [--no-cache] [--reason TEXT]`** — adds or updates an override entry. Requires at least one of `--pkgbuild-patch`, `--no-cache`, `--reason` (the *behavior-changing* override fields); calls with only `<pkg>` or `<pkg> --source` are rejected. `--source` is metadata that pins routing (`repo` vs `aur`) — it doesn't satisfy validation on its own, since classification arrives at the same value automatically. Entries with no behavior-changing override are auto-pruned on the next `packages.toml` write-back (`add` or `remove`).
- **`packages add-group <gnome|kde>`** — writes a curated desktop-environment group (see *Curated desktop catalog* above) into `packages.toml`. Idempotent; the group installs via `sysforge run packages`.
- **`packages remove <pkg>`** — removes the `[[package]]` block for the named entry using line-level manipulation; preserves all surrounding comments and section headers.

All subcommands accept `--packages FILE` to target a specific file (default: `/etc/sysforge/packages.toml`).

`build_state.toml` inspection and repair has its own namespace — see `sysforge state` (`state list`, `state repair`).

Valid per-entry fields: `name`, `source`, `pkgbuild_patch`, `cache`. Unknown fields are ignored.

### `-march=native` strategy

SysForge uses `-march=native` rather than hardcoding CPU-specific flags. Optimization becomes a compile-time concern — it works across CPU families without separate logic. If a package is incompatible with native tuning, a higher-priority rule pointing to the `bare` profile overrides `-march` for that package only.

---

## Config Layer

### Config file hierarchy

- System default: `/etc/sysforge/profiles.toml`
- User override: `~/.config/sysforge/profiles.toml`

`profiles.toml` is a single file holding flag profiles, `[[rules]]`, `[append_conflict_groups]`, and `[consumes_inference]`. By default the user file **fully replaces** the system file. To layer on top instead, add `extends_system = true` at the top of the user file — user values take priority on all conflicts. User rule priorities are bumped by 100 on merge (range 100–199) to always outrank system rules (range 0–99).

### Adopting new shipped defaults

The non-`profiles.toml` configs (`packages.toml`, `toolchain.toml`, `kernel.toml`, `sysforge.toml`) are read from a single resolved path with no per-key fallback to shipped defaults, so a live config does **not** automatically gain keys/sections added by a new release. On an installed system pacman's `backup=()` + `.pacnew` reconciliation covers this (and `doctor --pacman` warns about unmerged `.pacnew`). In a from-repo dev setup — where `SYSFORGE_CONFIG_DIR` points at a working tree and pacman never touches the config — `make sync-config` (`tools/sync_config.py`) fills the gap.

**Config-dir resolution.** `SYSFORGE_CONFIG_DIR`, when set, is the directory that *directly contains* the TOML files (e.g. `~/sf-config/kernel.toml`) — it is **not** an FHS root prefix, mirroring how `SYSFORGE_STATE_DIR` holds state files directly. When unset, the config dir is the FHS system path `/etc/sysforge`. The single resolution home is `primitives/paths.py` (`CONFIG_DIR` + the `*_PATH` constants); `tools/sync_config.py` and `tests/conftest.py` mirror it. (The installed-system path is unchanged: with the env unset, everything still resolves under `/etc/sysforge`.)

`sync-config` is an **add-only**, comment-preserving merge from `etc/sysforge/*.toml` into the live config dir (`$SYSFORGE_CONFIG_DIR` itself, else `/etc/sysforge`, or `--target DIR`): it injects keys, tables, and their leading comment blocks the live file is missing, and never overwrites a value the live file already sets (even if the shipped default changed). Arrays-of-tables (`[[package]]`) are user content and are left untouched. Bare keys are spliced before the first table header (TOML adjacency rule); new tables are appended. The merge is **key-anchored**, so it can only carry comments that lead an active key it injects — pure documentation comments and *commented-out example* settings (`# interactive = true`) have no key to anchor to. When a shipped file gains such content the live file lacks, the shipped file is written verbatim beside the target as `<name>.sfnew` (pacnew-style) for the operator to diff and adopt; a stale `.sfnew` is removed once the drift is resolved. `tomlkit` is a **dev-only** dependency (ephemeral `uv run --no-sync --with tomlkit`), never added to `pyproject.toml`. `--dry-run` reports without writing. `bootstrap.toml` is excluded (per-host, no live counterpart).

**Adopting the `.sfnew` residue — `sysforge config merge`.** Hand-merging the `.sfnew` companions is the only remaining manual step, so `sysforge config merge` (verb `config-merge`, `config_cmd.py`) is a pacdiff-style interactive driver over them. It scans the resolved config dir (`--config-dir` override, else `paths.CONFIG_DIR`) for `*.sfnew` — plus pacman's own `*.pacnew`/`*.pacsave` for sysforge config files on a packaged install — and for each presents: `[v]iew` (a `difflib` unified diff through `$PAGER` via `maybe_pager`), `[m]erge` (launch the resolved diff/merge tool with `live new`, then re-loop), `[s]kip`, `[r]emove` (drop the companion once the live file is satisfactory), `[o]verwrite` (copy the companion over the live file verbatim — guarded by a confirm and a "discards your local values" warning, never the default, intended for the `.pacnew` accept-maintainer case), and a[b]ort. Because a `.sfnew` is the *verbatim shipped file*, there is **no blind "accept theirs"** as a primary action — the safe path is merge-then-remove. The verb edits config files in place but never builds/installs, so it carries **no sentinel**. The diff/merge tool resolves through one home shared with the reconfigure editor chain — `primitives/editor.resolve_merge_tool` (`SYSFORGE_MERGE` > `sysforge.toml [ui].merge` > `$DIFFPROG` > `vimdiff`) launched via `run_tty_argv` (the `/dev/tty` passthrough, also used by `resolve_editor`'s callers). `--list`/`--dry-run` reports the companion→target pairs without prompting (scripting/CI).

### Dev fixtures vs. personal config

`tests/data/etc/sysforge/` is the **git-tracked test fixture set** wired in `tests/conftest.py` (which *forces* `SYSFORGE_CONFIG_DIR` to that dir directly, so a developer shell exporting its own value cannot leak into the suite). It is kept in shipped↔fixture parity by `make check-shipped`. A developer's **personal live config** is a separate, untracked dir (e.g. `~/sf-config`, holding the TOML files directly) that the shell's `SYSFORGE_CONFIG_DIR` points at and that `make sync-config` services — keeping personal config out of the tracked tree while leaving the fixtures deterministic.

### Directory layout

SysForge uses FHS-correct system roots and XDG Base Directory-correct user roots. The user side honours `$XDG_CONFIG_HOME`, `$XDG_CACHE_HOME`, and `$XDG_STATE_HOME` when set, falling back to their spec defaults.

| Location | Purpose |
|----------|---------|
| `/etc/sysforge/` | Shipped config (read-only, package-owned) |
| `/var/lib/sysforge/` | Runtime state (build_state, pipeline_state, source_meta, hardware_profile, sysforge.log) |
| `/var/cache/sysforge/` | Regenerable build cache (LLVM PGO profdata store; override via `SYSFORGE_PGO_STORE`) |
| `$XDG_CONFIG_HOME/sysforge/` (default `~/.config/sysforge/`) | User config overrides |
| `$XDG_CACHE_HOME/sysforge/` (default `~/.cache/sysforge/`) | AUR name cache (refreshed every 24h) |
| `$XDG_STATE_HOME/sysforge/` (default `~/.local/state/sysforge/`) | Fallback for runtime state when `/var/lib/sysforge` is not writable |

On first run, `sysforge` migrates the legacy consolidated dirs (`~/.config/sysforge/{cache,state}`) into their XDG-correct homes (`$XDG_CACHE_HOME/sysforge`, `$XDG_STATE_HOME/sysforge`). Migration is idempotent and best-effort — a failure logs a warning but does not block startup.

### State directory

Pipeline state is written to `/var/lib/sysforge/` by default. Override via the `SYSFORGE_STATE_DIR` environment variable or `--state-dir` CLI flag; CLI takes priority. Both are logged when present. `SYSFORGE_STATE_DIR` is a SysForge bootstrap var and is intentionally not subject to the build tool env isolation rule.

The configure stage creates a `sysforge` system group and sets the state directory to `root:sysforge` with mode `02775` (setgid: files written into the dir inherit the `sysforge` group). The recursive chown also normalises any state files written earlier in the same pipeline (`sysforge.log`, `pipeline_state.toml`) — without it, those files stay `root:root` and block the post-reboot `--resume` for the primary user. `open_unified_log` further `chmod 0o664`s the log on creation so future appends by other group members succeed. The builder user is added to the group during bootstrap; additional admin users can be added via `usermod -aG sysforge <user>`. If `/var/lib/sysforge` is not writable (e.g. standalone usage without bootstrap), the state dir falls back to the XDG state dir (`$XDG_STATE_HOME/sysforge`, default `~/.local/state/sysforge`).

### Profile conf override

Both `sysforge build` and `sysforge pipeline` accept `--profile-conf FILE` to substitute an alternate `profiles.toml` at runtime, bypassing the default user/system search paths. The override carries flag profiles, conflict groups, and consumes inference together (all sections live in the one file). If the specified file sets `extends_system = true`, the standard system config is still merged underneath it via the normal `extends_system` logic.

### Global settings (`sysforge.toml`)

`/etc/sysforge/sysforge.toml` holds global settings that don't belong in flag profiles or package manifests. Loaded by `load_sysforge_toml()` in `config.py`; returns `{}` if the file is missing.

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `[ui]` | `editor` | — | Editor for reconfigure stage (overridden by `SYSFORGE_EDITOR` env) |
| `[ui]` | `merge` | — | Diff/merge tool for `sysforge config merge` (`.sfnew` adoption). Resolved `SYSFORGE_MERGE` env > this > `$DIFFPROG` > `vimdiff`; accepts args (`"nvim -d"`, `"meld"`). Shares one home with the editor chain in `primitives/editor.py` (`resolve_merge_tool`/`resolve_editor`/`run_tty_argv`) |
| `[git]` | `fetch_timeout` | `30` | Seconds before a `git fetch` times out during source sync (0 = no limit). Legacy alias: `pull_timeout` |
| `[git]` | `clone_timeout` | `60` | Seconds before `git clone` / `pkgctl repo clone` times out (0 = no limit) |
| `[build]` | `python` | `system` | Python interpreter for PKGBUILD `build()` steps, pinned ahead of any pyenv/asdf/conda shim on `PATH` so a bare `python` resolves to the interpreter its `python-*` makedepends were installed against. `system` / unset → `/usr/bin/python`; a bare version like `3.12` → `/usr/bin/python3.12`; or an absolute path. Resolved choice logged at DEBUG; an unusable value warns and falls back to the system python |
| `[aur]` | `min_fetch_interval_ms` | `500` | Minimum gap between consecutive git fetches against aur.archlinux.org (millisecond resolution) |
| `[aur]` | `rate_limit_abort_s` | `300` | If AUR returns a `Retry-After` ≥ this many seconds, the remaining sync batch is aborted rather than waited out |
| `[mesa]` | `filter_drivers` | `false` | Opt-in master switch for hardware-filtering mesa's gallium/vulkan drivers (the meson analogue of LLVM target filtering). Off → mesa builds every upstream driver. On → `mesa_drivers.resolve_or_detect_mesa_drivers` trims `-D gallium-drivers=` / `-D vulkan-drivers=` to the detected GPU vendors, always keeping the mandatory software baseline (gallium `llvmpipe`/`softpipe`/`zink`, vulkan `swrast`/lavapipe) |
| `[mesa]` | `gallium` | — | Optional explicit gallium-driver override list (non-empty pins the axis, still baseline-enforced; absent → autodetect). Tokens must be valid meson gallium drivers |
| `[mesa]` | `vulkan` | — | Optional explicit vulkan-driver override list (same semantics) |

### Hardware overlays

The hardware detection stage emits `hardware_profile.toml` which feeds kconfig automation and gates hardware-specific packages in `packages.toml`. Key machine-specific caveats (Ryzen 7 5800X3D + RTX 5070):

- Explicit disable of `nouveau`
- CPU-specific flags: `CONFIG_MZEN3`, `CONFIG_X86_AMD_PSTATE`

---

## Pipeline Layer

Python DAG orchestrator with checkpoint/resume. Stages run in order:

1. **partition** — fully implemented (GPT, ESP + root, mkfs, mount)
2. **base_install** — fully implemented (pacstrap minimal base, genfstab)
3. **hardware** — fully implemented (CPU/GPU/NVMe detection → hardware_profile.toml)
4. **configure** — fully implemented (hostname, locale, timezone, mirrorlist, systemd-boot, user creation + sudo, sshd config, shell dotfiles, passwords via arch-chroot)
5. **reconfigure** — fully implemented (pre-build checkpoint: config review, disk/network/gpg checks, build preview)
6. **toolchain** — fully implemented (LLVM/GCC, optional 4-pass PGO bootstrap, compiler propagation to packages/kernel)
7. **packages** — fully implemented
8. **kernel** — fully implemented

Stages 1–4 are **bootstrap-only** — they run once from a live install environment. Stages 5–8 are **repeatable** and run on the installed system. Use `sysforge run pipeline --start-from reconfigure` to run the pre-build checkpoint on a live system; use `--start-from packages` to skip straight to builds. Stages 5–8 are also available as standalone `sysforge run <stage>` commands for repeated, out-of-pipeline use (e.g. `sysforge run packages`). The toolchain (6) and kernel (8) stages default to `enabled = false` because building a custom toolchain or kernel is an opt-in decision; users who want the stock system compiler and pacman kernel leave them disabled.

### Bootstrap workflow (stages 1–4)

Stages 1–4 run from a live Arch install environment (booted from the install ISO). The state dir must be set to the target system so pipeline state persists across the reboot:

```bash
# From the live environment — iso-install.sh sets this up automatically
sysforge run pipeline --state-dir /mnt/var/lib/sysforge
```

When stage 4 (configure) completes, the reconfigure stage detects it is running on the live ISO (via `/run/archiso`) and raises `BootstrapRebootRequired`. The runner catches this as a clean stop (exit 0), saves state, and prints the resume command. After rebooting into the installed system:

```bash
sysforge run pipeline --resume
```

**`iso-install.sh`** (`tools/iso-install.sh`) automates the live-ISO setup steps: checks connectivity, installs sysforge from the AUR (`sysforge` by default; pass `--git` to install `sysforge-git` instead), and prompts for all required bootstrap values with validation (timezone checked against `/usr/share/zoneinfo/`, passwords entered silently with confirmation). Writes a complete `bootstrap.toml` and prints the pipeline command when done. Builds the AUR package as a temporary unprivileged user (`aurbuild`) since `makepkg` refuses to run as root; the user and its sudoers drop-in are removed on exit.

**`bootstrap.toml`** (`/etc/sysforge/bootstrap.toml`) configures stages 1–4. The package does not install this file directly — it ships a starter template at `/usr/share/sysforge/bootstrap.toml.example`. `iso-install.sh` writes the live file from interactive prompts; for hand-edit setups, copy the example to `/etc/sysforge/` first.

```toml
target = "/mnt"          # mount point for the new system

[partition]
device       = "/dev/sda"   # required — block device to wipe and partition
esp_size_mib = 512          # EFI System Partition size in MiB (default: 512)
root_fs      = "ext4"       # "ext4" | "btrfs" (default: "ext4")

[system]
hostname           = "archlinux"    # required
locale             = "en_US.UTF-8" # required
timezone           = "UTC"          # required
keymap             = "us"           # optional (default: "us")
parallel_downloads = 5              # pacman ParallelDownloads (default: 5)
root_password      = "secret"       # optional — set via chpasswd; warn if absent
username           = "builder"      # optional — primary user (default: "builder")
user_password      = "secret"       # optional — user password; warn if absent

[mirror]
countries = ["Canada"]  # reflector --country (optional)
protocol  = "https"
age       = 12                 # reflector --latest N hours

[desktop]
environment = "gnome"   # optional — "gnome" | "kde"; installs a curated
                        # desktop package group. Unset + a TTY → the configure
                        # stage prompts; unset + no TTY → no desktop.
```

**Configure stage (stage 4)** runs all one-time system identity steps inside `arch-chroot`:
- Hostname (`/etc/hostname`), locale (`locale-gen`), timezone (`ln -sf /usr/share/zoneinfo/...`), keymap (`/etc/vconsole.conf`), `ParallelDownloads` in `pacman.conf`
- Reflector mirrorlist (skipped gracefully if `reflector` absent in chroot)
- Pacman db refresh against the fresh mirrorlist: `pacman -Sy` then `pacman -Fy` (`_sync_pacman_dbs`, best-effort — a transient mirror failure warns but never aborts configure). Seeding the **files** db here lets the reconfigure editor picker map an editor binary to its package on first boot without a separate sync.
- systemd-boot: `bootctl install`, `loader.conf`, `entries/arch.conf` (uses `root=LABEL=root`)
- `systemctl enable NetworkManager` + `systemctl enable sshd`
- `PermitRootLogin yes` in `/etc/ssh/sshd_config`
- `useradd -m -G wheel <username>` + `/etc/sudoers.d/wheel` drop-in
- Shell dotfiles: `.bashrc` + `.zshrc` for root (red prompt) and primary user (green prompt)
- Root and user passwords via `chpasswd` (warns if absent from bootstrap.toml)
- Desktop environment (optional): after copying config into the target, `pkg_catalog.select_desktop` resolves `[desktop] environment` (non-interactive) or prompts on a TTY, then writes the chosen `[group.*]` into the target's `packages.toml` so the packages stage installs it. The only interactive point in an otherwise non-interactive stage; non-TTY runs with no preselection skip silently. See Package Manifest → *Curated desktop catalog*.
- sysforge install in target via `makepkg -si` from the source tree's PKGBUILD, run as the build user with a temporary `NOPASSWD` sudoers drop-in (removed after install). The configure stage stages the source as `sysforge-$pkgver.tar.gz` so makepkg uses the local copy instead of fetching, runs with `--skipchecksums --skipinteg` since the tarball is locally produced, and ends with sysforge owned by pacman (`pacman -Q sysforge`). This replaces the earlier `uv pip install --system` path, which left files unowned and forced `pacman -U --overwrite='*'` on the first AUR-driven update.

The hardware stage (stage 3) needs no config — it auto-detects and writes `hardware_profile.toml` to `state_dir`. After reboot the file is at its natural path (`/var/lib/sysforge/hardware_profile.toml`) and the kernel stage picks it up automatically.

**Full device inventory.** Beyond the scalar CPU/GPU/NVMe summary, the stage enumerates every PCI and USB device via `primitives/device_probe.enumerate_devices()` and appends a `[[devices]]` array-of-tables to `hardware_profile.toml` (bus, address, modalias, class, description, bound driver, expected modules, suggested `CONFIG_*`). The device→module link is resolved against a complete **reference kernel**'s `modules.alias` (newest installed stock kernel, excluding any `custom` modules dir) — a custom kernel that omitted a driver can't resolve the modalias it lacks, so resolving against the reference surfaces the gap. The module→`CONFIG_*` step is widened beyond `device_probe`'s curated table by the cached `kbuild_map` (harvested by the kernel stage's Gate 2 from the last built tree), loaded from the state dir when present. The union of present devices' `suggested_kconfig`, minus anything the heuristic `[kconfig]` table already owns, is emitted as a `[kconfig_devices]` table (all `=m`) for the kernel stage's fragment merge. Any present, functional device with no driver bound is WARNed at the stage and pointed at `sysforge doctor --hardware`. The `[[devices]]` block is emitted after the scalar `[hardware]`/`[kconfig]`/`[kconfig_devices]` tables; existing readers (`tomllib`) are unaffected.

### Runner

`run_pipeline(config, options, stages)` sequences stage execution:
- Validates `depends_on` references before running
- Reads checkpoint state to determine start index
- Calls `stage.run()`, marks done/failed, saves state after each stage
- On `NotImplementedError`: prints `--start-from` guidance and exits
- On `BootstrapRebootRequired`: saves state, prints reboot + resume instructions, exits 0 (clean stop, not failure)
- On `RuntimeError`: saves state and exits with resume instructions
- `--dry-run`: logs what would run without calling `stage.run()`

Guard against accidental state clobber: if a state file exists and neither `--resume` nor `--start-from` is passed, the runner exits with instructions rather than overwriting. Both flags are supported on `sysforge run pipeline`.

**User-facing output.** The runner emits a welcome banner (sysforge version + ordered stage chain) and a status snapshot (`✓ done`, `▸ running`, `· pending`, `↳ skipped_to`) before the loop, a stage banner before each stage (`[N/M] name` between two `═` rules), a `✓ name complete` line after each stage, and a closing rule on success. All of this routes through `log.ui` so it reaches both stderr and the unified log regardless of `-v` level. Visual primitives live in `sysforge/ui/headers.py` and share the `═` rule + bold-cyan style with `tools/iso-install.sh` (parallel `_double_rule` / `_step` / `_field` helpers in shell). Step counters are 1-based against the full stage list, so `--start-from configure` shows `[4/8]`, not `[1/…]`.

### Checkpoint state

`pipeline_state.toml` is the authoritative checkpoint record. Written atomically (write-then-rename) after every state transition. Human-readable TOML for manual recovery.

Per-stage status: `pending` → `running` → `done` / `failed` / `skipped_to`

Intra-stage package progress (packages stage only):

```toml
[stages.packages.progress]
built     = ["llvm", "clang", "lld"]
failed    = ["mesa-git"]
skipped   = []
remaining = ["cosmic-comp-git", "cosmic-panel-git"]
```

On resume with failed packages, the user is prompted to retry or skip each (or `--force-retry` bypasses the prompt).

### Kernel stage (stage 8)

Builds a custom kernel from a PKGBUILD. The stage is a clean no-op if `/etc/sysforge/kernel.toml` is absent or has `enabled = false`, so systems using a stock pacman kernel skip it without needing `--start-from`. Opt-in by design — users who want a stock kernel leave the stage disabled.

**`kernel.toml` structure:**

```toml
pkgname          = "linux-sysforge"  # shipped default; must match the PKGBUILD pkgbase
pkgbuild_src_dir = "~/src"       # parent dir; PKGBUILD is at <pkgbuild_src_dir>/<srcdir>/PKGBUILD
srcdir           = "linux"       # source directory name if different from pkgname (optional)
bootloader       = "systemd-boot"    # systemd-boot | grub | none  (default: systemd-boot)
interactive      = true              # default: true — interactive kconfig (make nconfig)
compiler         = "llvm"            # "gcc" | "llvm" — kernel-stage compiler (optional)
base_config      = "pkgbuild"        # "pkgbuild" (default) | "running" | <path> — base .config source
build_headers    = true              # default: true — build the -headers subpackage (DKMS needs it)
build_docs       = false             # default: false — drop the -docs subpackage from the build
source           = "local"           # "local" (default) | "aur" | "git"
                                     # "local" = hand-maintained PKGBUILD, no remote sync.
                                     # "aur"/"git" = PKGBUILD is a clone of an AUR/git remote.

# Boot safety (defaults shown; see §Kernel stage boot-safety):
require_fallback_kernel = true       # refuse to install a custom kernel as the only kernel
boot_audit              = true       # run /boot-space + pre-install resolved-.config audit
min_boot_free_mb        = 200        # minimum free MiB on /boot before building
capture_lsmod_snapshot  = true       # capture lsmod for `make localmodconfig`
kconfig_merge           = true       # write the sysforge.config fragment at all (false also disables the drift check)
device_kconfig          = true       # merge hardware_profile [kconfig_devices] into the fragment

[[kconfig]]                      # manual kconfig overrides (optional, repeatable)
option = "CONFIG_HZ_1000"        # must match CONFIG_[A-Z0-9_]+
value  = "y"                     # y | m | n | non-empty string
```

`srcdir` is needed when the PKGBUILD directory name differs from `pkgname` (e.g. `pkgname = "linux-sysforge"` but the repo is cloned as `~/builds/linux`). Defaults to `pkgname` if omitted.

**Kernel-stage compiler override:** `compiler = "gcc" | "llvm"` is independent of the toolchain stage. A system that keeps gcc system-wide can still build the kernel with LLVM (or vice versa). Resolution order: `--compiler` CLI flag > `kernel.toml compiler` > toolchain-stage pipeline state (cc/cxx set by stage 6) > profile defaults. When set to LLVM, the standard `LLVM=1 LLVM_IAS=1` env vars are injected by `makepkg_wrapper` automatically — no extra PKGBUILD changes needed. Note: `compiler = "llvm"` builds the kernel *with* clang but does **not** apply PGO profdata — the profdata trains the clang binary, not the linux target, so there is no kernel-PGO path here.

**Headers/docs subpackages (`build_headers` / `build_docs`):** the kernel image is always built; the optional `-headers` and `-docs` subpackages are toggled independently. `build_headers` defaults **on** (DKMS and any out-of-tree module build need the matching kernel headers to compile); `build_docs` defaults **off** (the docs subpackage is slow to package and rarely needed) — note this drops the `-docs` subpackage that a stock kernel PKGBUILD would otherwise build. Resolution per toggle: CLI flag (`--headers`/`--no-headers`, `--docs`/`--no-docs`, `argparse.BooleanOptionalAction`, default unset → `None`) > `kernel.toml` (`build_headers`/`build_docs`) > hard default. The single home for the resolve is `_resolve_subpackages`. Disabling a subpackage works by **dropping its entry from the PKGBUILD's `pkgname=(...)` array** — `pkgbuild_patcher.patch_kernel_subpackages` (called from `makepkg_wrapper` for every kernel build, after `patch_kernel_btf_guard`) removes any array token whose dequoted value ends with `-headers`/`-docs`. Standard Arch kernels synthesize each `package_$_p()` via `for _p in "${pkgname[@]}"; do eval …`, so an entry absent from the array is never packaged; the `_package-headers()`/`_package-docs()` helper bodies are left untouched. The patcher needs no sentinel (removal is self-idempotent), is a no-op when both are kept or the targeted subpackage is absent, and preserves both the single-line and one-token-per-line array layouts. When `build_headers` is disabled, Gate 1 escalates the standing DKMS reminder into a hard warning: no `<pkg>-headers` will be installed, so out-of-tree and DKMS modules (nvidia-open-dkms → black screen) cannot rebuild and won't load on reboot; any present DKMS modules are named.

**Resolution summary.** After resolving compiler (+ its origin), variant, bootloader (+ whether the chosen one is detected installed), source, interactive mode, kconfig counts, the headers/docs subpackage toggles (`subpkgs:` line), and the boot-safety gate settings, the stage emits a single labelled "Kernel build plan:" block (`_log_resolution_summary`). It prints on every run (useful before a multi-hour build) and is the readable core of `--dry-run`, replacing decisions previously scattered across the log. The standalone interactive default also emits a one-line nudge pointing at `--non-interactive` for unattended runs.

**Variant-inheritance nudge.** When `compiler` is unset (neither CLI nor `kernel.toml`) and the toolchain-stage variant is `pgo_llvm`, the stage emits a WARN naming the inherited variant and recommending that the operator persist `compiler = "llvm"` in `kernel.toml` so the choice survives a future toolchain-stage disable (which clears `[stages.toolchain.result]`). `stock_llvm` gets the same nudge at INFO level. `gcc` and `system` variants are silent — gcc is the safe default and `system` means there's no opinion to project.

**Configured-vs-installed toolchain mismatch.** The variant nudge above reflects what the toolchain *stage* registered in pipeline state; this check reflects on-disk reality. When `toolchain.toml` requests a custom LLVM toolchain (`enabled = true`, `compiler = "llvm"`) but the installed LLVM is stock repo (`install_origin == "repo"` — a custom build is never in a sync DB) or its PGO profdata is version-skewed, the stage emits a WARN before the build. The check is suppressed when `skip_build = true` (which intentionally registers the installed compiler as-is — stock-vs-custom is then a deliberate choice, mirroring the `compiler = "gcc"` register-only short-circuit). It uses `llvm_state.detect_toolchain_config_mismatch`, which is built strictly on `collect_llvm_state` (the sanctioned LLVM-inspection entry point) — this is **provenance reporting**, deliberately *not* a third toolchain *health* probe (those remain `_verify_llvm_install` and `toolchain_preflight._probe_cc`). The same detector backs `sysforge doctor --toolchain`. There is intentionally no persisted "toolchain is correct" flag: it would go stale the moment pacman replaces LLVM out-of-band, so the mismatch is computed on read from current install state.

**Per-kernel toolchain-drift check.** Stage entry compares the installed kernel's recorded `toolchain_variant` (from `build_state.toml`) against the active variant. On mismatch (e.g. installed kernel was built under `stock_llvm`, active is `pgo_llvm`), the stage emits a WARN before the build runs. This mirrors `sysforge update`'s drift sweep but covers the kernel package, which `update` excludes via the stage-ownership skip. Back-compat: no recorded variant → silent (older builds preceded the field).

**Bootloader-installed preflight.** Stage entry probes for systemd-boot (`/boot/loader/loader.conf`) and grub (`/boot/grub/grub.cfg`); falls back to `pacman -Qq systemd grub` when neither marker is present. When the resolved `bootloader` (≠ `none`) isn't in the detected set, a single non-fatal WARN surfaces the mismatch *before* the build runs — so a user on a grub-only system who left the default `systemd-boot` configured gets an early signal instead of a post-install `bootctl update` failure. False negatives on exotic setups (UKI, custom loaders) don't block the build; the post-install branch still tolerates the bootloader-update failure.

**Pkgname/pkgbase consistency check.** After the source sync, the stage static-parses the PKGBUILD via `parse_pkgbuild` and confirms the parsed `pkgbase` (or `pkgname` for non-split packages) matches `kernel.toml pkgname`. A typo or a cloned PKGBUILD whose `pkgbase` has drifted from the directory name raises a clear `RuntimeError` at stage entry instead of failing late at `makepkg --install` after a multi-hour build.

**Pkgname repo-collision check.** Immediately after the consistency check, the stage tests `kernel.toml pkgname` against the pacman sync DBs via `aur.is_repo_package` (one `pacman -Si`). A custom kernel should carry a unique name; if the name matches an official package (e.g. `linux`, `linux-lts`), building and installing it would overwrite the stock package on `pacman -U`. Interactive runs prompt for confirmation (`prompt_choice`, default no); unattended runs (`--non-interactive` or no TTY) abort; `--dry-run` warns without prompting.

**kconfig fragment:**

Hardware-driven kconfig entries come from `hardware_profile.toml [kconfig]` (emitted by the hardware stage). The kernel stage locates that file via `config["hardware_profile"]` when present, else falls back to `state_dir / "hardware_profile.toml"` — the path the hardware stage actually writes — so a standalone `run kernel` after a standalone `run hardware` picks up the profile even though no in-process pipeline state carried the path (mirrors the resolution in the reconfigure hardware-profile review). These include both positive `=y` enables (CPU/GPU/NVMe-driven) and architecture-disable `=n` umbrellas — when the host is x86_64, the hardware stage writes `# CONFIG_ARM64 is not set`, the same for RISC-V/PowerPC/MIPS top-level keys and a curated set of ARM64 SoC families, culling unreachable subtrees from `make nconfig`. See §Hardware Detection → *Architecture-aware kconfig disable* for the registry. Device-driven entries come from `hardware_profile.toml [kconfig_devices]` (modular drivers, `=m`, for devices present on the machine — see §Hardware Detection → *Device-driven kconfig*), gated by `kernel.toml device_kconfig` (default true). Manual overrides from `kernel.toml [[kconfig]]` are merged on top — precedence is manual > hardware > device; manual wins on conflict with a `[WARN]` (including arch-disable entries — a cross-compile use case can re-enable `CONFIG_ARM64=y` per the override path), while device entries are machine-derived advisories that hardware/manual override silently. The combined result is written to `<pkgbuild_src_dir>/<srcdir>/sysforge.config` before `makepkg` runs, each entry annotated with a `# source: manual|hardware|device` line. sysforge **patches the merge into the PKGBUILD** so a stock kernel PKGBUILD applies the fragment without cooperation: `pkgbuild_patcher.patch_kernel_kconfig_apply` (called from `makepkg_wrapper` for every kernel build) finds the kconfig-setup anchor in `prepare()` (a `make olddefconfig`/`oldconfig`/`defconfig` line, else the `.config` seed) and injects, right after it, a file-guarded base-config seed (`cp "$startdir/sysforge.base.config" .config` + `make olddefconfig`, see *Base config* below) followed by a guarded `scripts/kconfig/merge_config.sh -m .config "$startdir/sysforge.config"` + `make olddefconfig` (a bool-only symbol receiving `=m` is normalized by `merge_config.sh`/`olddefconfig`). Both blocks are wrapped in `if [ -f … ]`, so when a source produced no file the step is a runtime no-op. A PKGBUILD that already calls `merge_config.sh` (or references the fragment) is detected and left untouched — no double-injection; a PKGBUILD with no anchor at all is warned about (the fragment can't be placed). The whole merge is gated by `kernel.toml kconfig_merge` (default true) — set it false to skip the fragment entirely; when disabled, any stale `sysforge.config` from a prior run is removed so the PKGBUILD doesn't merge a leftover (the `device_kconfig` toggle, by contrast, gates only the device-driven sub-source).

Manual override validation: `option` must match `CONFIG_[A-Z0-9_]+`; `value` must be non-empty (`n` to disable); duplicates within `kernel.toml` are an error.

If no source provides any kconfig entries, no fragment is written. The fragment is written *after* the source sync (so a `--cleansrc` re-clone doesn't wipe it) and *after* compiler resolution, so its banner carries a toolchain-provenance line (`# toolchain variant: <variant>  cc: <path>`) giving a `.config` diff between two builds a trail of which toolchain produced it.

**Base config (`base_config`):**

The fragment is an *overlay* — it does not define the build's starting `.config`. `base_config` selects that base: `"pkgbuild"` (default, no-op — the PKGBUILD provides its own base), `"running"` (the running kernel's config, read via `dep_analysis.read_running_kconfig_text` from `/proc/config.gz` then `/boot/config-$(uname -r)`), or a path to a `.config` file. Resolution order: the `--base-config` CLI flag > `kernel.toml base_config` > the `"pkgbuild"` default (mirroring `--compiler`/`--bootloader`). For `"running"`/`<path>`, sysforge writes the resolved config to `<pkgbuild_src_dir>/<srcdir>/sysforge.base.config` before the build (dry-run aware) and `patch_kernel_kconfig_apply` injects the `cp sysforge.base.config .config` + `make olddefconfig` seed **before** the fragment merge (same injection point) — so a stock PKGBUILD honours `base_config` without cooperating. sysforge never mutates tracked source files. The seed is file-existence guarded, so the `"pkgbuild"` default (which writes no `sysforge.base.config`) is a runtime no-op. A `"running"` source that resolves to nothing (no `/proc/config.gz`, no `/boot/config-*`) warns and falls back to the PKGBUILD base; an unknown non-path value raises. The resolved source appears in the "Kernel build plan:" summary (`base cfg:` line).

**lsmod snapshot:**

Before the build, `lsmod` output is captured to `<state_dir>/lsmod.snapshot` (unless `capture_lsmod_snapshot = false`). This lets the PKGBUILD run `make localmodconfig` reproducibly using a fixed module set from the running system rather than whatever is loaded at build time. `localmodconfig` strips drivers for hardware *not loaded at snapshot time* — Gate 1 warns about this and Gate 2 (below) is the backstop that catches a dropped root-path driver before install.

**Interactive kconfig (kernel-stage default):**

`sysforge run kernel` is interactive by default — the kernel stage passes `interactive=True` into `BuildOptions`. On the interactive path `patch_kernel_kconfig_apply` injects `make nconfig` (after the fragment merge) into the PKGBUILD's `prepare()` so the user reviews and edits the resolved config before the build proceeds — sysforge supplies the interactive target itself rather than depending on the PKGBUILD having one (a stock PKGBUILD that only runs `make olddefconfig` would otherwise show no menu). It does **not** add a second `nconfig` when the PKGBUILD already has an interactive target. The makepkg subprocess inherits the parent TTY in interactive mode (`makepkg_invoke`), so the ncurses UI renders on the controlling terminal. The default can be flipped via `kernel.toml interactive = false` or the `--non-interactive` CLI flag; the non-interactive path skips the `nconfig` injection and additionally runs `patch_noninteractive_kconfig`, which rewrites any *existing* interactive target (`oldconfig`, `nconfig`, `menuconfig`, `xconfig`, `gconfig`) to `make olddefconfig` for unattended runs. `olddefconfig` applies defaults for all new symbols without terminal interaction; VAR=val arguments before the target (e.g. `ARCH=x86_64`) and trailing comments are preserved. `--noconfirm` only controls makepkg's own prompts and has no effect on interactive make targets inside the PKGBUILD.

Note: when other verbs (`sysforge build`, `sysforge update`) build a kernel PKGBUILD with `build_mode = "kernel"` on the resolved profile, those paths still default to *non-interactive* — interactive-by-default is a kernel-stage-only contract because the stage is the user-driven kernel build entry point.

**Post-build kconfig drift (advisory):** after the build, beside Gate 2 (pre-install, outside the sentinel), `_gate2_kconfig_drift` compares the options sysforge merged into `sysforge.config` against the resolved `.config` and warns on any that didn't survive — disabled (`y/m`→`n`/absent), changed (built-in↔module, or a string/int value change), or re-enabled (`n`→`y/m`). On the interactive path a drop usually means the operator toggled it off in `make nconfig`; otherwise it's typically `make olddefconfig` dropping a request whose dependencies are unmet. sysforge can't distinguish the two without a full dependency solve, so this **never blocks** the build — unlike the boot-critical Gate 2 audit it sits next to. The diff itself is a pure fact in `kernel_safety.diff_requested_kconfig` (returning `KconfigDrift` records over the same `parse_kconfig_text` representation both files share — a missing resolved option normalises to `n`, so a requested `n` that ends up absent is correctly not flagged); the stage owns only the logging. The check keys off the fragment's existence, so it is on exactly when `kconfig_merge` produced a fragment (both on or both off).

**Source sync via the scheduler:**

The kernel stage routes its source refresh through `source_sync.get_scheduler().request(SyncRequest(..., source=<kernel.toml source>))` ahead of the build, the same path as the toolchain stage. With the default `source = "local"`, the scheduler short-circuits (no RPC, no clone, no fetch) — only `--cleansrc` / `--cleansrc-force` would attempt a purge, but a hand-maintained tree has no remote to re-clone from, so users on the `local` path leave cleansrc unset. For `source = "aur"` / `"git"`, the normal sync runs: `--cleansrc` purges and re-clones (refusing on dirty/ahead/no-upstream clones); `--cleansrc-force` overrides that guard; cleansrc forces a sync even when `--no-update` is also set. `STATUS_FAILED` / `STATUS_RATE_LIMITED` / `STATUS_PURGE_REFUSED` raise.

`STATUS_DIVERGED` (upstream advanced but the local tree can't fast-forward — local commits or a dirty tree) gets stronger handling in the *kernel* stage than the plain warning the other verbs use, because building a kernel off stale or hand-edited source is exactly the easy-to-miss footgun. `_warn_and_confirm_diverged` enriches the WARN with ahead/behind counts (`classify_head_vs_upstream`) so the "upstream has new commits but the local repo is dirty" case is spelled out, then **gates the build**: an interactive run must confirm (`prompt_choice`, default no), and an unattended run (`--non-interactive` or no TTY) aborts. Either decline raises, leaving nothing built (the sync runs before the sentinel). `--cleansrc` to discard local edits is the suggested escape hatch.

The source sync (including the `--cleansrc` purge, which `purge_src` does as a non-atomic `shutil.rmtree`) runs **outside** the boot sentinel by design: it mutates only the src tree, nothing boot-critical, so wrapping it in the sentinel — whose `recovery_cmd` is `sudo mkinitcpio -P` — would be semantically wrong. The atomicity contract is "purge, then clone"; an interrupted purge leaves a missing/partial PKGBUILD that fails **loudly** at `_pkgbuild_path` on the next run (with a hint to re-run `--cleansrc` to re-clone), not a silent brick. No sentinel is needed because the running kernel was never touched.

**Stage-ownership stamp:**

After a successful build, the kernel stage stamps `owner_stage = "kernel"` and `source = "local"` (or the configured value) into `build_state.toml` via `BuildOptions`. `sysforge update` honours that marker and skips the kernel package by default — the canonical update path is `sysforge run kernel`, not a sweep through `update`. Before the first kernel-stage build has written that stamp, the config bootstrap fallback in `primitives/stage_ownership.py` (consulted by `update.py`) reads `kernel.toml`'s `pkgname` and applies the same skip; split sub-packages collapse to the kernel `pkgbase` via the same `get_pkgbase()` lookup that handles other custom-built split packages. `--include-stage-owned` overrides the skip; naming the package explicitly on the `sysforge update` command line is treated as an opt-in for that run.

**Kernel stage boot-safety.**

The kernel stage must never leave the machine unbootable. Three gates wrap the build/install, backed by `primitives/kernel_safety.py` (the policy — what aborts vs warns — lives in the stage; the facts live in the primitive). Brick-class findings (`is_brick=True`) hard-fail; everything else warns.

To make a *pre-install* hard-fail possible, the build is **split from the install**: the stage builds with `BuildOptions.no_install=True` (the profile's `-i`/`--install` flags are stripped via `INSTALL_FLAGS`), audits the resolved `.config`, then installs the produced artifact via `makepkg_wrapper.install_built_packages()` (a `sudo pacman -U` of the built `.pkg.tar*`). The artifact is located via `_find_artifacts`, which searches the union of `pacman.get_pkgdest()` (makepkg's configured `PKGDEST`, env- or conf-resolved) and the PKGBUILD dir — so a non-default `PKGDEST` doesn't strand the install looking in the wrong directory (the same resolver path the ABI report and `build_core` use). Because `PKGDEST` is shared across every build, `install_built_packages` first scopes that union down to *this* PKGBUILD's own pkgnames (`_artifacts_for_pkgbuild`: parse the PKGBUILD, keep only files whose name `_parse_built_pkg_filename`-matches a declared pkgname) — otherwise a populated `PKGDEST` would drag every previously-built package into the kernel's `pacman -U`. The scoping falls back to the full union when the PKGBUILD can't be parsed for pkgnames (or nothing matches), so a static-parse limitation degrades to the old locate-everything behaviour rather than installing nothing. Separately, `pkgbuild_patcher.patch_kernel_config_install` injects a `/boot/config-<release>` install into the kernel's `package()` (preferring the split `package_<pkgbase>()`, falling back to a bare `package()`, then to the unsuffixed `_package()` helper used by the standard Arch kernel PKGBUILD's `eval`-loop split-package idiom — which synthesizes the real package functions at runtime and is therefore invisible to a static parser) when the PKGBUILD doesn't already ship one, so the resolved `.config` is pacman-tracked under `/boot`; it locates the build tree via `include/config/kernel.release` and is skipped idempotently when `/boot/config` is already installed. A third sibling patcher, `pkgbuild_patcher.patch_kernel_btf_guard` (also called from `makepkg_wrapper` for every kernel build), gates the stock PKGBUILD's bpftool `vmlinux.h` step: `make -C tools/bpf/bpftool vmlinux.h` and the `package()` install of the produced `vmlinux.h` both hard-require a `.BTF` section in `vmlinux`, which only exists when `CONFIG_DEBUG_INFO_BTF=y`. A BTF-off resolved `.config` — e.g. `base_config="running"` seeded from a lean, debug-info-free kernel — would otherwise fail the build at `failed to find '.BTF' ELF section`. The patcher wraps the build step in, and reduces the `vmlinux.h` install to, a runtime `if [[ $(scripts/config -s CONFIG_DEBUG_INFO_BTF) = y ]]` guard (the same idiom the PKGBUILD already uses for `CONFIG_DEBUG_INFO_BTF_MODULES`), evaluated against the real resolved config — so a BTF-on build keeps both steps and a BTF-off build skips them, with no sysforge-side BTF prediction. It is idempotent (a `# sysforge: BTF guard` sentinel) and a no-op when the step is absent (commented out / already removed). Without the split, Arch's pacman hooks (`kernel-install`/mkinitcpio) would build the initramfs and boot entry *at install time* — before any audit could run. The build mutates nothing and runs **outside** the sentinel, so a Gate 2 abort leaves the system completely untouched (nothing installed, no sentinel set).

- **Gate 1 — preflight (before the build).** Cheap, read-only. Hard-fails on a missing **fallback kernel** (no stock `linux`/`linux-lts` with a boot image — installing a custom kernel as the only kernel has no recovery path; override with `--allow-no-fallback` / `require_fallback_kernel = false`) and on a **missing/too-full `/boot`** (`min_boot_free_mb`; part of `boot_audit`). Captures the root topology (FS / storage transport / crypt-LVM-RAID from `/proc/mounts` + `lsblk -s` + `/etc/crypttab` + `/proc/mdstat`) for Gate 2. Advisory warnings: localmodconfig strip, DKMS rebuild reminder, mkinitcpio `HOOKS` vs root topology. In `--dry-run` the hard-fails downgrade to warnings.
- **Gate 2 — resolved-`.config` audit (after build, before install).** Reads the resolved `.config` from the build tree and runs `kernel_safety.audit_resolved_config(config, topology, devices)`. This is the only placement that sees post-merge / post-`olddefconfig` / post-`nconfig` state, so it's the single catch for a **Kconfig dependency cascade** (e.g. `CONFIG_SND_PCI=n` silently dropping `CONFIG_SND_HDA_INTEL`). Brick-class drops — root filesystem, root storage controller, core boot infra (`CONFIG_MODULES`/`BLK_DEV_INITRD`/`DEVTMPFS`/…), systemd prerequisites, crypt/LVM/RAID stacking — **abort before install** (override: `--skip-boot-audit` / `boot_audit = false`). Device-driver gaps (present PCI/USB device with no enabled driver, from `device_probe`) and console/framebuffer drops are advisory. Gate 2 is also the **kbuild-map harvest point**: the resolved `.config`'s parent is the version-exact source tree, so the stage runs `kbuild_map.parse_kbuild_tree` there, hands the fresh map to the device audit (near-total module→`CONFIG_*` coverage instead of the curated table alone), and caches it to `<state_dir>/kbuild_module_map.json` for the hardware stage's `[kconfig_devices]` fold. The parse is best-effort — any failure degrades to the curated-only audit, never blocks the gate.
- **Gate 3 — boot-readiness (after install + mkinitcpio + bootloader).** `verify_boot_artifacts` confirms `vmlinuz-<pkg>` + `initramfs-<pkg>.img` are present, non-trivial, and referenced by ≥1 boot entry (systemd-boot loader entry or `grub.cfg`) — a missing entry means the kernel installed but cannot be selected (the `bootctl update` ≠ boot-entry trap). `check_dkms_for_kernel` flags DKMS modules not rebuilt for the new release (nvidia → black screen, zfs root → unbootable). Brick findings raise; running inside the sentinel, that leaves the sentinel set so the next run is prompted to recover.

**CLI surface (`sysforge run kernel`):**

`--dry-run`, `--no-update`, `--cleansrc`, `--cleansrc-force`, `--non-interactive`, `--compiler {gcc,llvm}`, `--bootloader {systemd-boot,grub,none}`, `--base-config {pkgbuild,running,<path>}`, `--allow-no-fallback`, `--skip-boot-audit`, `--headers`/`--no-headers`, `--docs`/`--no-docs`, `--no-pkg-logs`, `--persist-log`, `--log-dir`, `--cache-report`, `--abi-check`, `--state-dir`, `--profile-conf`.

**Post-install steps** (run after the artifact is installed):
1. `sudo mkinitcpio -P`
2. Bootloader update: `bootctl update` (systemd-boot, default), `grub-mkconfig -o /boot/grub/grub.cfg` (grub), or skipped (`none`). The selection comes from `kernel.toml bootloader`, overridable per-invocation via `--bootloader`.
3. Gate 3 boot-readiness verification (above).

**Interrupted-install protection.** The artifact install (`pacman -U`), the post-install `mkinitcpio -P`, the bootloader regen, and Gate 3 are wrapped in `sentinel_scope(state_name="kernel", recovery_cmd="sudo mkinitcpio -P", …)`. (The build runs *before* this scope.) An interruption anywhere in that window leaves the sentinel in place so the next sysforge invocation blocks at the CLI-entry recovery prompt and offers to regenerate the initramfs — the step whose absence makes the system unbootable. See §Toolchain stage → *Interrupted-install protection* for the shared primitive.

**Concurrency lock.** The build → Gate 2 → install window is additionally wrapped in `primitives.build_lock.build_lock(state_dir / "kernel-build.lock", label="kernel")` so two concurrent `sysforge run kernel` runs sharing a state dir can't clobber `~/builds/<pkgbase>` (the second `nconfig`/makepkg would step on the first's `.config`). This is the **same shared primitive** the toolchain stage's PGO lock (`_pgo_lock`) delegates to — distinct from the sentinel: the *lock* is transient mutual exclusion held only for the run, while the *sentinel* persists an interrupted boot-critical mutation across runs. The kernel lock lives under `state_dir` (not `/var/tmp` like the PGO lock, whose staging dirs are genuinely global), so per-state-dir test runs stay isolated. Skipped in `--dry-run` (nothing is built).

### Packages stage (stage 7)

Walks `packages.toml` in order:
- `source = "repo"` → `sudo pacman -S --needed --noconfirm`
- `source = "aur"` / `"git"` → `_resolve_pkgbuild()` → `makepkg_wrapper.run()`. PKGBUILD lookup order: `packages.toml [build] pkgbuild_src_dir` → `profiles.toml [paths] pkgbuild_src_dir` → AUR clone.
- Non-fatal per-package failures: build continues, failures recorded in state
- Summary at end: `Total | Built | Failed | Skipped`

The AUR-dep build and per-package install loop are wrapped in `sentinel_scope(state_name="packages", …)` (no `recovery_cmd` — there's no single shell command that restores a partially-installed package set; the operator verifies with `pacman -Dk` and re-runs `sysforge run packages`). Per-package `RuntimeError` is caught and reported via the state machine; only an interruption or unexpected exception inside the scope preserves the sentinel.

### Toolchain stage (stage 6)

**Opt-in:** stage is a clean no-op if `/etc/sysforge/toolchain.toml` is absent or has `enabled = false`. Systems that skip this stage use whatever compiler is already installed; packages and kernel stages proceed normally.

**`toolchain.toml` structure:**

```toml
enabled     = true   # must be true to activate the stage
compiler    = "gcc"  # "gcc" (default when key absent) or "llvm"; LLVM is opt-in
pgo         = true   # only meaningful when compiler = "llvm"; ignored for gcc
skip_build  = false  # skip build; just register compiler paths in pipeline state

# Staging prefixes. Pass 1 outputs land in stage1 (system /usr never touched);
# Pass 2 outputs land in stage2 and are used as CC/CXX in Pass 3; Pass 3 stages
# the freshly-built OPTIMIZED libLLVM into stage3 so the non-pgo suite links
# against the libLLVM that ships (ABI coherence — see Pass 3 below).
pgo_staging1 = "/var/tmp/sysforge-llvm-stage1"
pgo_staging  = "/var/tmp/sysforge-llvm-stage2"
pgo_staging3 = "/var/tmp/sysforge-llvm-stage3"

# PGO data dir: profraw files written here during Pass 2, merged to clang.profdata
# (FHS default /var/cache/sysforge/llvm-pgo; override via toolchain.toml or SYSFORGE_PGO_STORE)
pgo_store   = "/var/cache/sysforge/llvm-pgo"

# Build-safety Gate 1 (LLVM path only; see Build-safety gates below)
min_build_free_gb = 40    # min free GiB per build filesystem (override: --skip-build-space-check)
require_multilib  = true  # require [multilib] enabled when any lib32-* is in scope

# Package lists — all have sane defaults, override only if needed
[packages]
pgo     = ["llvm", "llvm-libs", "clang", "lld"]
non_pgo = ["polly", "compiler-rt", "openmp", "spirv-llvm-translator"]
# lib32 defaults to EMPTY — lib32 LLVM packages are not built by the toolchain
# stage (see *lib32 is not toolchain-managed* below). Opt back in by listing them
# here; the target-filter and PGO exemptions keep that path correct.
lib32   = ["lib32-llvm", "lib32-llvm-libs", "lib32-clang", "lib32-spirv-llvm-translator"]
```

When `compiler` is unset (or set to `"gcc"`), the toolchain stage is **register-only**: it writes the system `/usr/bin/gcc` and `/usr/bin/g++` paths into pipeline state and returns without building anything. Stock `gcc-libs` from pacman's `base-devel` provides the runtime. The 4-pass PGO architecture below only kicks in for the explicit `compiler = "llvm"` path. Building GCC from source has no meaningful performance gains and is error-prone, so the stage doesn't own that path — use `pacman -S gcc gcc-libs` (already in `base-devel`) if you need to (re)install it.

**`skip_build = true`:** registers the system compiler paths in pipeline state without building anything. Downstream stages (packages, kernel) will use the system compiler. Useful when the system compiler is already optimized and no rebuild is needed.

**Profile-default propagation.** On a *successful* register/build — the GCC register-only short-circuit, the `skip_build` path, or the LLVM final pass after Gate 3 — the stage writes `profiles.toml [defaults] toolchain` to match `toolchain.toml`'s `compiler` via `config.set_default_toolchain` (`_propagate_default_toolchain`, the single home). This makes `toolchain.toml` the upstream source of truth: the package-compiler default (`flag-profile-system` → *Toolchain field*) always tracks the compiler the stage just registered, so a system that enables the stage with `compiler = "gcc"` builds packages with gcc, and flipping to `"llvm"` propagates to every package without a second edit. It writes only the **user live** `profiles.toml` (the file the resolver reads), only on success (a failed LLVM build returns before this point, so the default never flips to an uninstalled clang), and never on `--dry-run`; an unwritable config is a WARN, not a stage failure. This is *not* `toolchain_variant` (which records what was *built*, in `build_state.toml`).

**Build-safety gates + build/install split (kernel-parity).** The LLVM path mirrors the kernel stage's three-gate / build-install-split structure so a broken or doomed build can never leave the live `/usr` toolchain inconsistent. The pure, unit-testable facts live in `primitives/toolchain_safety.py` (`ToolchainFinding(severity, check_id, message, remediation, is_brick)`); the toolchain stage owns the abort/warn *policy*. `toolchain_safety` imports `LLVM_LOCKSTEP_SUITE` from `toolchain_preflight` (both primitives — no layering issue) and is **not** a third health-check entry point: `_verify_llvm_install` (pipeline, post-install) and `toolchain_preflight._probe_cc` (primitives, update path) remain the only two, and `_verify_llvm_install`'s skew arm now draws from `toolchain_safety.detect_suite_skew`.

- **Gate 1 — pre-build preflight** (`_gate1_preflight`, outside the sentinel, runs for both PGO and non-PGO). Brick-class aborts *before any build time is spent*: PKGBUILD pkgver skew across the lockstep suite (`check_pkgver_lockstep`; `spirv-llvm-translator` + `lib32-*` are excluded so their legitimately-different versions don't false-positive — the bug the old whole-set `_check_pkgver_consistency` had); a non-functional clang / missing lld (`smoke_test_compilers`, now run on the non-PGO path too); insufficient build-filesystem headroom (`check_build_space`, deduped by `st_dev`); `[multilib]` disabled while a `lib32-*` is in scope (`check_multilib_enabled`). Each brick is overridable (`--allow-version-skew`, `--skip-build-space-check`, `require_multilib = false`) and **downgraded to a warning in `--dry-run`**. Advisory (warn-only): residual `-fprofile-generate` instrumentation; an incomplete rollback snapshot.
- **Soname gate — pre-build, between Gate 1 and the build** (`_gate_soname_consumers`, calls `toolchain_safety.assess_libllvm_soname_impact`). PGO/optimization never changes `libLLVM`'s `SOVERSION` or exported symbols, so a same-version rebuild is ABI-identical and silent; but a libLLVM **version bump** changes the soname (`libLLVM.so.22.1` → `libLLVM.so.23.0`), and every installed package that links the old soname (mesa's radeonsi/radv/llvmpipe, and others) is left with a dangling `NEEDED` until rebuilt. The gate derives the target soname from the resolved llvm PKGBUILD pkgver, reads the installed soname authoritatively from `llvm-libs`'s on-disk file list, and — when they differ — enumerates the installed consumers (every package whose recorded `%DEPENDS%` carries a `libLLVM.so=<old major.minor>` soname dep, via `dep_analysis._SONAME_RE`), collapsed to pkgbase and **excluding** the LLVM lockstep suite + the packages being built this run (they're rebuilt here already). The `rebuild_soname_consumers` mode (CLI `--rebuild-soname-consumers` > `toolchain.toml` > `"prompt"`) decides: `prompt` (default) warns + lists + asks, approving captures the consumers for rebuild after Gate 3 and declining is a clean abort (non-TTY aborts — never silently break the system, pointing at `=auto`/`=off`); `auto` approves without prompting; `off` builds the toolchain but rebuilds nothing, printing the manual `sysforge build <consumers>` command. `--dry-run` previews the impact without prompting or rebuilding. The facts function is pure (guarded reads degrade to "no impact"); the stage owns the prompt/abort policy.
- **Build (outside the sentinel).** All passes build with `install=False`; the build functions return the built package map. A build-pass failure therefore mutates nothing and leaves **no sentinel** (matches kernel).
- **Gate 2 — pre-install ABI audit** (`_gate2_audit`, outside the sentinel, both paths). Scans the built `.pkg.tar*` for the `_ZNSt*@LLVM_*` hazard via `toolchain_safety.scan_abi_hazards`, **and** for a **graphics-consumer symbol shortfall** via `toolchain_safety.check_system_consumer_symbols`: it extracts the about-to-be-installed `libLLVM.so.*` from the built packages (lib32 excluded) and, for each installed external consumer that links that exact soname (mesa's `libgallium`/DRI/Vulkan drivers, curated in `_SYSTEM_LIBLLVM_CONSUMER_GLOBS`), checks every `LLVMInitialize*@LLVM_x.y` symbol the consumer imports is exported by the new libLLVM. A miss means a reduced `LLVM_TARGETS_TO_BUILD` dropped a backend the consumer needs (e.g. AMDGPU) — installing it black-screens the desktop while the kernel/KMS stay healthy. Either brick aborts before any `pacman -U` — nothing installed, no sentinel. This is the gate that `scan_abi_hazards` (intra-build only) never covered: it checks *already-installed* external consumers, the gap that let a target-reduced libLLVM ship.
- **Snapshot.** Right before the install, `cached_pkg_files_for(lockstep suite ∪ built names)` (in `primitives/pacman.py`) locates each currently-installed member's `.pkg.tar*` in the pacman cache. This is the offline-undo source. Gate 1 warns up front when any member's archive is missing (auto-undo will fall back to `pacman -S`).
- **Gate 3 — post-install verify, inside the sentinel** (`_verify_llvm_install`). Its `expected_targets` (for the `built ⊇ expected` `llvm-config --targets-built` check) is the **actually resolved** target set — `resolve_or_detect_llvm_targets(TOOLCHAIN_PATH, <state_dir>/hardware_profile.toml)`, the same value the build patched in — **not** just `toolchain.toml [llvm] targets`. On an autodetect host that key is unset, so the old form resolved to `None` and skipped the check entirely; sourcing the resolved set makes it actually assert (after the Part-1 baseline, that includes AMDGPU). A **post-install graphics arm** then folds `toolchain_safety.check_installed_consumer_symbols()` (the same symbol diff as Gate 2, but vs the *now-installed* `/usr/lib/libLLVM.so.*`) into the verify issues. On any failure, the stage **auto-restores** the prior-good toolchain from the snapshot in one `pacman -U` transaction (`_rollback_to_snapshot` → `batch_install_pkgs`): if restore succeeds the live `/usr` is whole again, so the sentinel is **cleared** and a `RuntimeError` is raised telling the user to investigate; if restore fails or the snapshot was incomplete, the sentinel is **kept** with `recovery_cmd` set to the snapshot restore (offline `pacman -U <cached>`, falling back to `pacman -S <suite>`). So a target-reduced libLLVM that somehow reached install is reverted automatically while rollback is still armed, rather than surfacing as a black screen at next login.

- **Consumer rebuild — after Gate 3, outside the sentinel** (`_rebuild_soname_consumers`). When the soname gate captured consumers (mode `prompt`-approved or `auto`), the now-installed-and-verified toolchain is followed by a rebuild of those consumers through the shared `build_core.build_and_install` engine (resolved via `find_pkgbuild` — auto-cloning repo packages like mesa — and the user's normal profile, so they re-link the new libLLVM). It runs **outside** the toolchain sentinel deliberately: a consumer rebuild failure must **not** roll back the intended toolchain bump — it surfaces as an actionable `RuntimeError` naming the failed packages and the manual `sysforge build` command, with the new toolchain left healthy.

The mutation window is therefore exactly install → Gate 3 → (rollback), followed by the out-of-band consumer rebuild. The concurrent-run lock (`_pgo_lock`) wraps the whole build → audit → snapshot → install window, like the kernel stage's `kernel-build.lock`. A consolidated resolution summary (`_log_toolchain_resolution_summary`) prints the compiler/pgo/variant, package counts, staging paths, gate settings, and snapshot availability — the readable core of `--dry-run`.

**PKGBUILD resolution:** follows `find_pkgbuild` lookup order (local `pkgbuild_src_dir` → `pkgctl repo clone`) for every package. After path resolution, the stage routes each unique resolved `pkgbuild_dir` through `SourceSyncScheduler.sync_many()` so missing trees are cloned and pre-existing trees are refreshed against AUR/repo upstream — same RPC short-circuit, rate-limit, and dirty-tree handling as `sysforge update`. Pass `--no-update` to skip the sync step (use whatever is on disk verbatim). Blocker statuses (`STATUS_FAILED`, `STATUS_RATE_LIMITED`, `STATUS_PURGE_REFUSED`) abort the stage; `STATUS_DIVERGED` is a warning. Resolved paths are then displayed in a table and the user is prompted to confirm or abort. On abort, the resume command is printed (`sysforge pipeline --resume --state-dir <dir>`) so they can make manual modifications and return.

**LLVM PGO bootstrap (only when `pgo = true`):**

Every PGO pass runs makepkg with `--cleanbuild --force` so the prior pass's `.pkg.tar.zst` in PKGDEST never short-circuits the next build (each pass produces a different artifact at the same pkgver). `makepkg` runs without `--install` — sysforge controls when (and where) Pass outputs land. **Only the final Pass 3 install touches `/usr`.** Earlier passes go to staging prefixes; the live system is never made ABI-incoherent mid-run. A sudo keepalive thread refreshes credentials every 60 seconds throughout the sequence (the final `pacman -U` still needs root). `llvm-profdata` is invoked with `RLIMIT_AS` lifted (`resource_guard.lift_for_child`) so it is not constrained by the sysforge controller's 2 GiB virtual address space cap.

The sequence is **four builds** (Pass 1a, Pass 1b, Pass 2, Pass 3) across three on-disk staging prefixes (`pgo_staging1` → `pgo_staging` → `pgo_staging3`) before the final install. Pass 3 is itself split into coherent sub-passes (3a → 3b → 3c):

1. **Pass 1a** — instrumented build of the pgo packages (`llvm`, `llvm-libs`) with the system compiler + `-fprofile-generate=<pgo_store>/`. Every output is **extracted to `pgo_staging1`** (no `pacman -U`, no live-root mutation), including the cmake-config / static-lib `llvm` package — Pass 1b's `find_package(LLVM)` needs those configs. The instrumented `.a` archives that land alongside surface `__llvm_profile_*` link errors for anything that consumes LLVM component targets; Pass 1b and Pass 2 work around that by **force-loading** the clang profile runtime into LDFLAGS **and selecting lld** (see below). Spurious profraw from CMake feature probes is purged before later passes begin.

2. **Pass 1b** — **non-instrumented** build of the non_pgo packages (`clang`, `lld`, `compiler-rt`, `polly`, `openmp`, `spirv-llvm-translator`) against stage1. The Pass 1b environment sets `CMAKE_PREFIX_PATH=<staging1>/usr` **and** injects `-DLLVM_DIR="<staging1>/usr/lib/cmake/llvm"` (same `cmake_llvm_dir` mechanism as 3b — see Pass 3 for why the env var alone is insufficient) so `find_package(LLVM)` resolves stage1's headers and configs; the resulting binaries link against stage1's `libLLVM.so` and are ABI-coherent with it (which keeps the Pass-2 training compiler coherent too). **LD_LIBRARY_PATH is deliberately NOT set** for Pass 1b — that would force the host `/usr/bin/clang` to load stage1's libLLVM and recreate the version-skew failure mode this design exists to prevent. `linker_flags_extra = _profile_runtime_ldflag()` force-loads the runtime via `-Wl,--push-state,--whole-archive <runtime_dir>/libclang_rt.profile-x86_64.a -Wl,--pop-state` so the instrumented static archives' `__llvm_profile_*` references resolve at link time. Crucially, Pass 1b passes `toolchain_variant="pgo_llvm"` so the variant-driven linker default (below) injects `-fuse-ld=lld`: the PGO bootstrap runs under a `CC=gcc` profile whose makepkg.conf defaults to **bfd**, and bfd's strict left-to-right archive resolution would otherwise drop a non-force-loaded runtime *before* the instrumented archives reference it — the historical Pass 1b `undefined reference to __llvm_profile_*` failure. lld is order-independent, and the `--whole-archive` form is order-proof under either linker (belt-and-suspenders). Pass 1b outputs are extracted into the same `pgo_staging1`, making it **self-sufficient**: stage1 now has a working clang and a working libLLVM, both built from the in-tree LLVM source, both ABI-coherent.

3. **Pass 2** — training run. CC is `<staging1>/usr/bin/clang` (built in Pass 1b), and the Pass-2 environment redirects dyld / cmake at stage1 via `LD_LIBRARY_PATH=<staging1>/usr/lib:…`, `CMAKE_PREFIX_PATH=<staging1>/usr:…`, `PATH=<staging1>/usr/bin:…`. The running clang and the libLLVM it loads are guaranteed coherent because they were built together — no possibility of version drift against `/usr`. Pass 2 builds pgo + non_pgo packages; the act of running stage1's clang against stage1's instrumented libLLVM generates profraw as a side effect. `LLVM_PROFILE_FILE` uses `%m_%p` (per-module-hash + per-PID) so parallel `make -j` clang processes each write their own `.profraw` file rather than contending on one; `CCACHE_DISABLE` and `SCCACHE_DISABLE` are set so neither cache tool bypasses the instrumented compiler. `linker_flags_extra` carries the same force-loaded profile-runtime LDFLAGS, and Pass 2 likewise sets `toolchain_variant="pgo_llvm"` (lld), so its non_pgo `find_package(LLVM)` builds against stage1's instrumented `.a` archives still link cleanly. A background daemon merges profraw into `clang.profdata` every 15 seconds using adaptive batch sizing (starts at 128 files; halves on OOM; minimum batch 8). No install. After the build, Pass 2 binaries are extracted to `pgo_staging` (stage2). The stage2 outputs are **non-instrumented** since Pass 2 doesn't apply `-fprofile-generate`.

4. **Pass 3** — final optimized build of pgo + non_pgo (+ lib32 only if explicitly opted in; empty by default — see *lib32 is not toolchain-managed*) with `-fprofile-use=<clang.profdata>`. It is split into coherent sub-passes so the non-pgo suite links against **the exact libLLVM that ships**, not the live `/usr` one:

   - **3a — pgo** (`llvm`, `llvm-libs`): the optimized PGO build. CC selection is conditional on whether `<pgo_staging>/usr/bin/clang` exists: when the PGO set includes `clang` (so Pass 2 produced a staged clang), 3a uses `CC=<pgo_staging>/usr/bin/clang` and redirects dyld / cmake at stage2 (`LD_LIBRARY_PATH`/`CMAKE_PREFIX_PATH`/`PATH`) — the staged clang's NEEDED libLLVM is stage2's, so the redirect is ABI-coherent. The shipped default only PGO-builds `llvm`/`llvm-libs` (clang is `non_pgo`, lives in stage1, never stage2), so the **system-clang fallback** applies: `CC=/usr/bin/clang` with the stage2 redirect **suppressed** (pointing stock clang at stage2's `LLVM_TARGETS_TO_BUILD`-restricted libLLVM would recreate the Pass 1b version-skew failure — missing target-init symbols like `LLVMInitializeBPFTarget`).
   - **stage3 extract** — the just-built optimized `llvm`/`llvm-libs` `.pkg.tar*` are extracted into `pgo_staging3` (via `_extract_pass2_to_staging`), so `<pgo_staging3>/usr` holds the libLLVM + headers + cmake configs that the install will ship. **Both split members must be staged**: `llvm-libs` carries `libLLVM.so*` while the `llvm` (dev) package carries `usr/lib/cmake/llvm/LLVMConfig.cmake` and the headers — without the latter, 3b's `find_package(LLVM)` finds nothing under `CMAKE_PREFIX_PATH` and silently falls back to the system `/usr` libLLVM (defeating the whole split → Gate-3 brick). Because the artifact lookup matches per-pkgname, the glob is **version-anchored** (`{name}-[0-9]*-*.pkg.tar*`, same idiom as `pacman.cached_pkg_files_for`) so the shorter `llvm` pkgname does not swallow the `llvm-libs-…` sibling artifact. As a belt-and-suspenders guard, `_assert_staging_has_llvm_cmake(pgo_staging3)` runs immediately after extraction and **aborts before install** if `LLVMConfig.cmake` is absent — converting a multi-hour build + bricked install into an immediate, actionable error.
   - **3b — non_pgo** (`clang`, `lld`, …): steered at the just-built optimized libLLVM via **both** `CMAKE_PREFIX_PATH=<pgo_staging3>/usr` (build env) **and** a `-DLLVM_DIR="<pgo_staging3>/usr/lib/cmake/llvm"` injected into the PKGBUILD `cmake` line (`pkgbuild_patcher.patch_llvm_dir`, threaded as `cmake_llvm_dir` through `_build_pass` → `make_build_options` → `_run_build`). The env var alone proved **insufficient**: `find_package(LLVM CONFIG)` silently resolved the live `/usr` libLLVM despite `CMAKE_PREFIX_PATH` (env-var search precedence lost to the system prefix), so clang/lld linked the wrong libLLVM and bricked at Gate 3. `LLVM_DIR` is the highest-precedence config-mode override — a cmake **cache** variable, checked before any prefix search and persisted in `CMakeCache.txt` across the PKGBUILD's repeated `cmake ..` calls (so injecting after the first invocation is sufficient, mirroring `patch_llvm_targets`). Both cmake-arg injectors append the new `-D` arg at the **true end of the (possibly `\`-continued) cmake statement** (`_cmake_statement_end`), not the first newline — so `-DLLVM_TARGETS_TO_BUILD` (injected first, for `clang`/`lld`/`compiler-rt`) and `-DLLVM_DIR` compose on one command. Inserting at the first newline instead splices the second arg into the first's continuation chain and orphans an arg, which bash then runs as a command (`-DLLVM_TARGETS_TO_BUILD…: command not found`, `build()` exit 4). Both injectors also anchor on the cmake **configure** invocation via `_find_cmake_configure_anchor`, **not** a bare `^cmake\b` line match: the anchor requires a same-line argument (so a lone `cmake` element in a multi-line `makedepends=(...)` array — e.g. `spirv-llvm-translator` — is never chosen; the loose match spliced `-DLLVM_DIR` into the dependency array and makepkg rejected it, exit 12) and skips action-mode invocations (`cmake --build`/`--install`/`-E`/…) which ignore `-D` cache args. **Post-patch validation gate:** after all injectors run and before makepkg, `pkgbuild_patcher.validate_patched_pkgbuild` (called from `makepkg_wrapper._run_build`, gated to the injection path) re-parses the patched PKGBUILD and fails fast on the two corruption classes that previously surfaced only hours into a build — **G1** requires the parsed dependency/identity globals (`depends`/`makedepends`/`checkdepends`/`provides`/… via `pkgbuild_meta.parse_pkgbuild`) to be unchanged from upstream (catches a `-D` arg landing in a dep array), and **G2** requires each managed injected token (`-DLLVM_TARGETS_TO_BUILD=`/`-DLLVM_DIR=`) to ride a `cmake` command's continuation-joined logical line (catches an orphaned arg, while ignoring legitimate `-D …` *array elements* like `cmake_options=(-D FOO=ON)` since only the two managed tokens are checked). A violation raises `PkgbuildPatchError` and aborts before anything is built. **Why this matters:** the std::-symbol re-export profile flips between build modes — stock/instrumented libLLVM emits an out-of-line weak copy of e.g. `std::string::_M_assign`, which the `LLVM_X.Y { global: *; }` version script globs into the LLVM namespace (so libLLVM *exports* `_M_assign@@LLVM_X.Y`), while the `-fprofile-use` build inlines it away and imports it from libstdc++ (`@GLIBCXX_*`). If clang were built against an *exporting* libLLVM (stock `/usr`, stage1, or stage2) but the run ships the *non-exporting* PGO libLLVM, `libclang-cpp` would dangle `_ZNSt*@LLVM_X.Y` and the live clang would brick at the first symbol lookup. Building 3b against stage3 makes clang record its true ABI (`@GLIBCXX_*`), coherent with the shipped libLLVM. As in Pass 1b, **only `CMAKE_PREFIX_PATH` is set in the env, never `LD_LIBRARY_PATH`** (the host clang compiles the source; it must not be forced to *load* the staged libLLVM — `-DLLVM_DIR` steers only the cmake config lookup, not the runtime loader). **Post-build link assertion:** immediately after 3b (still pre-install), `_assert_pass_links_shipped_libllvm` reruns `toolchain_safety.scan_abi_hazards` on the just-built `clang`/`lld` `.pkg.tar*`; a correctly-steered build yields **zero** `_ZNSt*@LLVM_*` undefined refs, so a non-empty result means the steering was *still* defeated and the stage aborts **before install** (no sentinel, no rollback), naming the offending pass — the earliest, most specific catch.
   - **3c — lib32** (only when explicitly opted in): same `CMAKE_PREFIX_PATH=<pgo_staging3>/usr` + `-DLLVM_DIR` steering and the same post-build link assertion.

   Across all sub-passes `LLVM_PROFILE_FILE` is cleared so any inherited Pass-2 training env can't leak, and `linker_flags_extra` is left unset (stage3's LLVM is non-instrumented — no `__llvm_profile_*` references, and no Pass 1b/2 residual flag leaks into the final binaries). The install — the **only** `sudo pacman -U` against `/usr` — runs after the Gate-2 audit on the built packages. Staging prefixes (stage2 *and* stage3) are removed only **after** the post-install verify passes (see below), so a failed verify keeps the stage prefixes on disk for diagnostic inspection. Profdata is **preserved** at `<pgo_store>/clang.profdata`; a version sidecar `clang.profdata.version` (LLVM major integer, e.g. `22`) is written alongside it so `sysforge update` can check compatibility before reusing the profdata.

**Pre-install ABI hazard check (Gate 2).** Between the Pass-3 build and the final `sudo pacman -U`, `_gate2_audit` extracts each built `.pkg.tar*`'s shared libraries and scans their `nm -D` output via `toolchain_safety.scan_abi_hazards` (which uses `abi_check._undefined_versioned`). Any UND versioned symbol whose mangled name is in the C++ stdlib namespace (`_ZNSt*`) and whose version starts with `LLVM_` is a hard block: it means a built binary (typically `libclang-cpp.so`) recorded a `std::string` (or similar) requirement against the `LLVM_X.Y` version namespace — i.e. clang was linked against a libLLVM that *re-exports* the C++ stdlib while the run ships one that does not. Installing those binaries would leave the live toolchain unable to resolve `std::string` methods at runtime (`symbol lookup error: libclang-cpp.so: undefined symbol _ZNSt..., version LLVM_22.1`). The Pass-3 coherent sub-pass split (3b builds the non-pgo suite against the shipped libLLVM in stage3) is the **primary** prevention; the 3b/3c post-build link assertion (above) is the earliest catch; Gate 2 is the final **backstop** that refuses to install if a hazard slips through anyway. Gate 2 runs *outside* the sentinel, so a hazard aborts with the live `/usr` intact and **no sentinel**; the user is told to restart with `--rebuild-profdata`. (This scan moved out of `_pgo_install` — which now only installs — and is shared with the non-PGO path.) Gate 2 (and the 3b/3c assertion) depend on `abi_check._extract_sos` actually extracting: it now `mkdir`s its destination before `bsdtar -x -C` (`scan_abi_hazards` extracts each package into a per-package subdir to avoid same-named `.so` collisions, and that subdir does not pre-exist — without the `mkdir`, `bsdtar` failed with *"could not chdir to …"*, the audit saw zero shared objects, and the gate passed **vacuously**, which is exactly how the brick reached install).

**ABI-safety invariant (Path B).** The live `/usr` is observably coherent before and after every step except the single final `pacman -U`, and even that is now reversible: Gate 3 verifies the result and auto-rolls-back to the snapshot on failure. A run that aborts before install (build failure, Gate 1, or Gate 2) leaves the system exactly as `sysforge run toolchain` found it — nothing installed, no sentinel. A run whose install verifies-bad restores the prior-good suite from the pacman cache. No half-installed instrumented `libLLVM.so`, no orphaned `/usr/bin/clang` that can't resolve `LLVMInitializeBPFTarget@LLVM_22.1`. The only role `/usr/bin/clang` plays in the run is **as a bootstrap host compiler in Pass 1b** (compiling source into objects, never loading a different-version libLLVM); version drift between the in-tree LLVM source and the installed system packages is therefore no longer a failure mode.

**Stage ownership (`sysforge update` skip).** The install-bearing final pass (Pass 3, or the single pass when `pgo = false`) stamps `owner_stage = "toolchain"` into `build_state.toml` via `BuildOptions` — mirroring how the kernel stage stamps `owner_stage = "kernel"`. `sysforge update` honours that marker and skips the LLVM suite by default, pointing the user at `sysforge run toolchain` instead of rebuilding `llvm`/`clang`/`lld`/`compiler-rt` mid-sweep. Intermediate PGO passes (1a/1b/2) leave the marker unset so their transient, soon-overwritten staging writes don't claim ownership. Before the first toolchain-stage build has written that stamp — and for build_state entries written by older sysforge versions that predate the field — the config bootstrap fallback in `primitives/stage_ownership.py` (consulted by `update.py` via `load_stage_ownership()`) reads `toolchain.toml` and applies the same skip, but **only when** the stage is `enabled` *and* `compiler = "llvm"` (the default/unset `gcc` path is register-only and owns no LLVM, so stock pacman LLVM stays pacman-class and is left alone). Ownership is the **union** of `is_llvm_pkgbase` (prefix match: `llvm`/`clang`/`compiler-rt`/`lld`) **excluding `lib32-*`** and the explicit `toolchain.toml [packages]` lists captured in the same snapshot. The `lib32-*` exclusion is deliberate: the toolchain stage doesn't build lib32 by default (see *lib32 is not toolchain-managed*), so claiming implicit ownership would make `update` skip them with nothing building them; they're toolchain-owned only when explicitly listed in `[packages] lib32`. The configured set is what catches members `is_llvm_pkgbase` doesn't match by prefix — notably `spirv-llvm-translator` (and any custom-listed package) — so they're skipped too, not just the prefix set. `--include-stage-owned` overrides the skip; naming an LLVM package explicitly on the `sysforge update` command line is an opt-in for that run. This is the exact analogue of the `kernel.toml` bootstrap fallback (see the kernel stage's stage-ownership note).

**lib32 is not toolchain-managed.** `[packages] lib32` defaults to **empty** (`_DEFAULT_LLVM_LIB32 = []`); the toolchain stage builds no lib32 packages unless a user explicitly opts them back in. The reason is a target-set asymmetry: lib32 LLVM packages ship no headers of their own and compile against the all-target 64-bit `/usr/include/llvm` headers, but the toolchain stage's host-driven `LLVM_TARGETS_TO_BUILD` filter (e.g. `X86;NVPTX`) would reduce lib32-llvm's target set. lib32-clang's GPU-offload tools (`clang-nvlink-wrapper`, `clang-sycl-linker`) call `InitializeAllTargets()` — resolved from those all-target headers — and then fail to link against the reduced lib32 libLLVM (`ld.lld: undefined symbol: LLVMInitializeAArch64AsmParser`, etc.). PGO adds nothing either: the profile is trained on the x86_64 clang self-build and is discarded by the i686 (`-m32`) build. lib32 packages instead come from `sysforge update` (repo, full targets, no PGO). Two guards keep an explicit opt-in correct anyway: `makepkg_wrapper._maybe_patch_llvm_targets` never injects a reduced target set for `lib32-*` (always all targets, matching the shared headers), and `makepkg_conf`'s lib32 scrub strips `-fprofile-use` (via `makepkg_flags._strip_pgo_flags`) so no foreign-arch profile reaches the i686 build. `build_diag` recognises the reduced-target link failure (`toolchain:lib32-reduced-target`) and points at the real fix rather than a spurious "version skew".

**Pass 1b skipped when `non_pgo` is empty.** Minimal configs (tests, intentionally-narrow rebuilds) can set `[packages] non_pgo = []`. In that case stage1 has no clang, and Pass 2 falls back to `/usr/bin/clang` — recreating the bootstrap-host-clang behaviour, where the user is responsible for keeping system clang ABI-coherent with the in-tree LLVM source. The non-empty default (clang/lld/compiler-rt/...) is the supported path.

**Dep resolution for staged passes.** Pass 1a builds against the live `/usr` and keeps the profile-supplied `--syncdeps`, so missing build tools (cmake, ninja, python, z3, libffi, …) are pacman-installed normally. Pass 1b, Pass 2, and Pass 3 build against a stage prefix; `CMAKE_PREFIX_PATH=<staging>/usr` makes `find_package(LLVM)` see the staged headers and cmake configs, but pacman has no knowledge of those staged packages. `_build_pass(staged_deps=True)` therefore strips `--syncdeps`/`-s` (via the shared `SYNC_FLAGS` constant from `makepkg_wrapper.py`, the same set `pacman.BATCH_STRIP_FLAGS` removes for batch builds) from the resolved profile's makepkg flags and appends `--nodeps` for those three passes. Without that, makepkg's pre-build dep check would invoke `sudo pacman -S llvm=<pkgver>` and fail with "target not found" (the just-built version is not in any repo). The non-llvm build deps stay required — they're expected to already be on the system from Pass 1a's `--syncdeps` install.

**Concurrent-run lock.** `ToolchainStage.run` acquires an advisory `flock(2)` (`_pgo_lock`, the shared `build_lock` primitive) on `_pgo_lock_path(staging1)` = `<pgo_staging1>.parent/sysforge-pgo.lock` (typically `/var/tmp/sysforge-pgo.lock`) around the whole build → audit → snapshot → install window — not just the PGO passes, so the non-PGO path is guarded too (mirroring the kernel stage's `kernel-build.lock`). The sentinel scope guards re-entry on the state-dir but not the `/var/tmp` staging dirs or `~/pgo`, both of which two concurrent runs would corrupt. The lock file holds the owner's PID, so the loser surfaces "another sysforge PGO build is running (pid N)" rather than a confusing mid-flow failure. The path is in `staging1.parent` rather than inside `pgo_store` so the Pass-1 purge cannot delete it. Skipped in `--dry-run` (the lock file would be a side effect).

**Post-install libLLVM resolution check.** `_verify_llvm_install` runs `ldd /usr/bin/clang` and `ldd /usr/bin/lld` and asserts that any `libLLVM*.so` lines resolve under `/usr/lib`. A `/var/tmp/sysforge-llvm-stage*` path appearing in `ldd` of an installed binary means Pass 3 packaged a bad RPATH or the install is incomplete — `/usr` looks consistent until `/var/tmp` gets cleaned, at which point the live toolchain silently breaks. The verify-stage check catches that before the sentinel clears.

**Verify-failure diagnostic dump.** On a `_verify_llvm_install` failure, `ToolchainStage.run` calls `_dump_stage_dynsym_evidence(staging3, state_dir)` before the recovery prompt. The brick is a C++ stdlib symbol bound to libLLVM's `LLVM_<ver>` version node, so the actionable evidence is the **set difference** between what the *installed* consumers (`libclang-cpp` / `liblldCommon`) demand under `@LLVM_*` and what the *installed* `libLLVM` provides — read straight from the live `/usr` files that bricked. The dump lists those missing symbols (the brick cause) and, when the kept `stage3` libLLVM *does* export them, notes that the shipped libLLVM diverged from what Pass 3b linked against — pointing directly at an incomplete/incoherent stage3. It is written to `<state_dir>/llvm_abi_hazard.log`. (This replaced an earlier dump of `stage2`'s `libLLVM` defined exports, which on the profdata-reuse fast path captured a stale/empty prefix that never contained the brick symbols.) The installed libLLVM is selected to **match the soname the consumers actually link** (`_newest_so` keyed on the consumer's `lib*.so.<major>.<minor>` version), not a lexical-first glob — otherwise a compat package's older runtime (e.g. `llvm21-libs`'s `libLLVM.so.21.1` sitting beside `llvm-libs`'s `libLLVM.so.22.1`) is picked, its `@LLVM_21.1` exports satisfy nothing the `@LLVM_22.1` consumers demand, and the diff reports a false *"0 NOT provided"* all-clear that hides the brick. Staging removal is deferred until verify passes, so the `stage3` prefix survives the failure path for this contrast. The log path is surfaced in the WARN block alongside the suggested recovery command.

**Profdata reuse:** before purging `pgo_store`, the stage checks for an existing `clang.profdata` + version sidecar. The sidecar's LLVM major version is compared against the `pkgver` in the pgo PKGBUILDs (not the installed version — the toolchain stage builds a *new* version). If compatible (same major), passes 1a–2 are skipped entirely and only the optimized build (Pass 3) runs, using system clang as CC (which, after a prior successful run, is already PGO-optimized). Stage1/stage2 are not needed in this path, but **stage3 still is**: Pass 3 stages the freshly-built optimized libLLVM there and builds the non-pgo suite (3b) against it for ABI coherence — exactly as on the full path. `--rebuild-profdata` forces a full 4-pass build regardless, e.g. after upstream codegen changes within the same major version.

**Pass-3 input-fingerprint reuse (opt-in).** Profdata reuse skips the *training* (passes 1a–2) but still rebuilds every Pass-3 package. When a *late* Pass-3 package fails (e.g. `spirv-llvm-translator` in 3b), a rerun would needlessly re-optimise the identical `llvm`/`llvm-libs` (3a, the heaviest target) and other unchanged packages. The opt-in input-fingerprint cache (`primitives/build_fingerprint.py`, CLI `--reuse-built` > `toolchain.toml reuse_unchanged` > off) skips a Pass-3 package whose **inputs are unchanged and whose built artifact is still on disk**. The fingerprint folds everything that determines the output: the upstream PKGBUILD content hash, the source-tree git HEAD, the compiler identity (`clang_identity` — path + size + nanosecond mtime + `--version`), the injected/profile flags (`compiler_flags_extra`/`linker_flags_extra`/`cmake_llvm_dir`/`extra_flags`), a `config_digest` (`hash_obj` of the flag-relevant config — `profiles`/`rules` + `toolchain.toml`, the latter because `[llvm] targets` drives the `LLVM_TARGETS_TO_BUILD` patch the upstream-PKGBUILD hash can't see), the `clang.profdata` content hash (hashed once per run), and the installed build-dep versions. Sub-passes are **Merkle-chained**: 3b/3c fold in 3a's current fingerprints (`staged_dep_fps`), so if a rebuilt libLLVM's fingerprint shifts, every consumer's does too and they rebuild — no stale libLLVM can ride through a cache hit. The cache is keyed by `(pass_id, pkgbase)` and stored at `<pgo_store>/build_cache.json`, so it is **wiped on a fresh 4-pass start** (the purge branch) but **survives a profdata-reuse resume** (the dominant rerun-after-failure case). It is **always written** (so a first, non-opted-in run populates it) but only **consulted** when opted in. The whole mechanism is fail-safe — a missing/changed artifact (size or nanosecond mtime), a fingerprint mismatch, a config edit, a schema bump, dry-run, or the GCC register-only path all force a normal rebuild; it never reuses a stale build. `--rebuild-profdata` (full 4-pass + `pgo_store` purge) also bypasses it. The logic lives in `_build_pass` (per-package skip/record, returning `{pkgbase: fingerprint}` for the chain) with the cache primitives in `build_fingerprint`; do not add a second cache or skip path.

**Sidecar write timing.** The version sidecar is written **right after Pass 2 completes** (after the final profraw merge produces `clang.profdata`, before Pass 3 starts) — not after a successful Pass 3 install. The sidecar's only invariant is "this profdata is for LLVM major N", which is determined entirely by what Pass 2 instrumented; Pass 3 success has no bearing on it. Writing it post-Pass-2 means a Pass-3 failure (e.g. a transient toolchain bug, an aborted run) still leaves recoverable profdata that the next invocation can reuse via `_check_existing_profdata` rather than being forced into a full 4-pass rebuild. The major itself is derived from the in-tree PGO PKGBUILD `pkgver` (`_pgo_target_major`), matching the value `_check_existing_profdata` will later compare against — symmetric with the reuse check, and correct across major bumps where `pacman -Q llvm` would report a stale value.

**Confirmation gating (PGO).** Unlike the rest of sysforge (which is automation-focused), the LLVM PGO sub-flow is fragile enough that wrong profdata silently mis-optimises the resulting compiler. Four decision points in `_build_llvm_pgo` therefore prompt the user before destructive or long-running work, all sharing a single `_pgo_confirm` helper:

1. **Reuse vs rebuild** — when compatible profdata is found, prompt `[Y/n]` to reuse; declining triggers a full 4-pass rebuild (and continues into prompts 2–3).
2. **Purge `staging/` and `pgo_store/`** — prompt `[y/N]` before `rmtree`; declining aborts PGO. After the purge, `pgo_store` is recreated via `_ensure_pgo_store_writable`: the FHS default lives under root-owned `/var/cache`, and the unprivileged makepkg passes write `.profraw` directly into it, so when a plain `mkdir` hits `EACCES` the helper falls back to `sudo mkdir -p` + `sudo chown -R <user>:` (the installed `tmpfiles.d` provisions `/var/cache/sysforge` 0777 for package installs, but a run-from-repo dev setup has no tmpfiles, hence the runtime fallback). A user-writable / env-override `pgo_store` takes the direct path and never touches sudo.
3. **4-pass start** — prompt `[y/N]` before launching the ~2–3 hour 4-pass sequence; declining aborts PGO.
4. **Suspicious Pass-2 profdata size** (`< 10 MiB`) — prompt `[y/N]` to continue into Pass 3; declining aborts before Pass 3 so the user can investigate instrumentation.

`--auto-pgo` (added on `run toolchain`) bypasses all four prompts and falls through to the prior automated behaviour. **Non-interactive without `--auto-pgo`** — the prompt's `eof_default="n"` fires and the PGO path aborts with a clear error directing the user to pass `--auto-pgo` or run with a TTY. The other existing prompts (residual-instrumentation, GCC-anyway, main "Proceed with toolchain build?", LLVM blockers) are unchanged.

**`pgo = false` path:** single build pass, all packages built and installed together. No profdata, no staging, no daemon.

**GCC path (`compiler = "gcc"`):** no build. Registers `/usr/bin/gcc` and `/usr/bin/g++` in pipeline state and returns. `pgo`, `skip_build`, `[packages]`, and the LLVM safety preflight are all skipped — none of them apply.

**Compiler propagation:** on completion the toolchain stage writes the resolved compiler paths into pipeline state:

```toml
[stages.toolchain.result]
cc      = "/usr/bin/clang"   # or "/usr/bin/gcc"
cxx     = "/usr/bin/clang++" # or "/usr/bin/g++"
ld      = "lld"              # llvm only; absent for gcc
variant = "pgo_llvm"         # gcc | stock_llvm | pgo_llvm
```

The packages and kernel stages read these values and inject them into the build environment, overriding any profile-level `CC`/`CXX` defaults. If the toolchain stage was skipped, these keys are absent and stages fall back to the profile.

**`variant` is the canonical toolchain-identity signal** for downstream conditional behaviour. Consumers should read it via `pipeline.state.get_toolchain_variant(state)` — do not derive it from the `cc` path or by re-parsing `toolchain.toml`. The fallback `"system"` is returned when the toolchain stage has never run on this state dir. The `skip_build = true` path reflects on-disk reality: if `pgo_store/clang.profdata` and its version sidecar exist, the installed clang is the result of a prior PGO build and `variant = "pgo_llvm"`; otherwise `"stock_llvm"`. Variants flow into `build_state.toml` per-build via `BuildOptions.toolchain_variant`, where `sysforge update` reads them back to detect toolchain drift (see §Toolchain-variant drift detection below).

**Variant-driven per-package env overlay.** `profile.variant_env_overlay(pkgbase, variant) -> dict[str, str]` returns extra env vars to inject for specific pkgbases when sysforge owns the LLVM toolchain. `_run_build` applies the overlay AFTER profile-derived env vars and AFTER `injected_env`, but only fills keys that aren't already set — overlays are defaults, not overrides, and a stage explicitly setting `MESA_WHICH_LLVM` (or any other overlay key) still wins. Today the only entry is `mesa` / `mesa-git` / `lib32-mesa` / `lib32-mesa-git` → `MESA_WHICH_LLVM=4` under `stock_llvm`/`pgo_llvm`. Reason: those PKGBUILDs use a case selector to pick which LLVM tree they link against, defaulting to `4` (`extra/llvm`) — which is the same package name sysforge installs after the toolchain stage. Setting it explicitly makes the build reproducible even if the user's shell exports a different value (e.g. `MESA_WHICH_LLVM=1` pointing at `llvm-minimal-git`). Skipped for `gcc` and `system` variants so sysforge doesn't override the user's shell preference when it has no LLVM opinion.

**Variant-driven linker soft default.** `emit_makepkg_conf` injects `-fuse-ld=lld` into `LDFLAGS` when (a) `toolchain_variant in {"stock_llvm", "pgo_llvm"}`, (b) no explicit `ld_override` was passed, (c) the resolved `LDFLAGS` (profile first, then system conf) declares no `-fuse-ld=` linker, (d) `lld` is on `PATH`, and (e) this is not a kernel build. The defaults-not-overrides rule is the key invariant: a profile that already declares `LDFLAGS="… -fuse-ld=mold"` keeps mold, and an explicit `BuildOptions.ld_override` still wins (hard override beats soft default). Effect: any build path that flows through `BuildOptions.toolchain_variant` — `packages` stage, `sysforge update`, `sysforge build`, and the **PGO bootstrap's own Pass 1a/1b/2/3** — picks up the toolchain's linker without each caller having to repeat the propagation. The PGO consumer passes (1b/2) depend on this specifically: they run under the `CC=gcc` profile (default bfd), and the `pgo_llvm` variant is what flips them to lld so the force-loaded profile runtime resolves regardless of archive order. The kernel build path is opt-out because kernel linker selection is controlled by `LLVM=1`, not LDFLAGS. `gcc` and `system` variants skip the injection so sysforge doesn't override the user's makepkg.conf when it has no LLVM opinion.

**Stale-state wipe (disabled / absent stage).** When `toolchain.toml` is absent or has `enabled = false`, the stage clears any prior `[stages.toolchain.result]` from pipeline state before returning. This prevents the failure mode where a user runs `compiler = "llvm"`, disables the stage, and subsequent `packages`/`kernel` stages keep using the stale `cc=/usr/bin/clang`/`ld=lld` overrides — the disable opts out of all downstream LLVM propagation, not just the build.

**Interrupted-install protection.** Three layers wired into the LLVM build path (the GCC path is register-only and skips all three). Note the sentinel now wraps only the install → Gate-3 → rollback window (the build runs before it, outside the sentinel — see *Build-safety gates* above):

1. **Stage sentinel** (`primitives/stage_sentinel.py`) — writes `<state_dir>/stage_in_progress.toml` just before the `sudo pacman -U` of the run and clears it after Gate-3 verification passes (or after a successful auto-rollback, since the system is then whole again). Schema records `stage`, `started_at`, `compiler`, `pgo`, and a `recovery_cmd` string. The recovery command is **snapshot-aware** (`_snapshot_recovery_cmd`): when every suite member's prior `.pkg.tar*` is cached it's an offline `sudo pacman -U <cached files>`; otherwise it falls back to the online `sudo pacman -S <suite>`. On every subsequent sysforge invocation, `cli.main()` calls `check_and_recover_stale_sentinel()` before dispatching install-bearing commands (`build`, `update`, `run *`, `setup`) — the gate is centralised in `cli._gate_sentinel_check(args)`, which also skips read-only invocations (`--dry-run`) so users can inspect the system without first running recovery. If a sentinel is found, the operator is prompted to auto-run the recovery command (`[y/N]`). **TTY-only prompt:** when stdin is not a TTY (background sessions, scripts, IDE wrappers), the prompt would silently auto-decline; `check_and_recover_stale_sentinel` instead emits an explicit error naming the sentinel file path and the recovery command, then returns False. **Verify-after-clear:** after `sentinel.clear()` runs, the recovery path checks that `sentinel.path.exists()` is False before printing "Recovery completed". A still-present file means the recovery cleared a different path (state-dir mismatch, namespace/chroot surprise) — the path is logged loudly so the operator can investigate instead of trusting a false-positive cleared message. Refusing recovery exits with status 2 and leaves the sentinel in place; success clears the sentinel and proceeds.

2. **Post-install verification (Gate 3)** — after the `pacman -U` of the LLVM run, `_verify_llvm_install()` checks: (a) `pacman -Q` versions across `_LLVM_VERSION_MATCH_SET` (which *is* `LLVM_LOCKSTEP_SUITE` from `toolchain_preflight` — `llvm`/`llvm-libs`/`clang`/`lld`/`compiler-rt`/`polly`/`openmp`) all agree, via `toolchain_safety.detect_suite_skew` (the canonical interrupted-install symptom — a mismatched `llvm-libs` is the exact failure mode that produces a broken GUI), (b) `clang --version` and `ld.lld --version` invoke cleanly without missing-symbol errors (probing `ld.lld`, the GNU-compatible flavor `-fuse-ld=lld` resolves to — bare `lld` is the generic multiplexer driver and always exits 1 without a flavor), (c) `ldd` of installed clang/lld resolves libLLVM under `/usr/lib` (`_check_llvm_link_resolution`), (d) when `[llvm] targets` is configured, `llvm-config --targets-built` is a superset. On failure the stage **auto-rolls-back** to the pre-install snapshot rather than prompting (the kernel-parity overhaul replaced the old interactive `_prompt_llvm_recovery`): a successful restore clears the sentinel and raises "prior toolchain was restored"; a failed/incomplete restore keeps the sentinel with the snapshot recovery command. This verification is comprehensive and fatal, but only runs inside `run toolchain`; if a toolchain run is interrupted before it (or a later partial pacman transaction reintroduces a skew), the broken state can still reach an everyday `sysforge update`. That gap is closed by the `cc:<name>` compiler-health probe in `toolchain_preflight` (see §`toolchain_preflight.py`), which re-detects the suite-wide pkgver skew / non-runnable clang before any package builds — deliberately a lightweight independent check sharing the `LLVM_LOCKSTEP_SUITE` constant rather than importing this pipeline-layer verifier into the primitives layer.

3. **Clean-exit SIGINT scope** (`primitives/interrupt.py`) — wraps the LLVM build dispatch in an `InterruptScope` context. The first Ctrl-C flips a flag without raising; the build code checks the flag at safe boundaries (between `makepkg` runs, between PGO passes) and raises `CleanExitRequested` to exit at the next safe point, sentinel intact. A second Ctrl-C falls through to default `SIGINT` handling (immediate termination) — the operator explicitly chose the unsafe path. `CleanExitRequested` subclasses `BaseException` so it propagates through `except Exception:` blocks without being silently swallowed.

The sentinel-installation and clean-exit machinery is exposed as a shared `sentinel_scope()` context manager in `primitives/stage_sentinel.py` (see Verb Framework below) so the same install-bearing protection used by the toolchain stage is also available to other stages and standalone CLI verbs.

**Sentinel coverage map.** The primitive is now used at every install-bearing stage entry, not just the LLVM toolchain. Current callers:

| Caller | `stage_name` | `recovery_cmd` | Notes |
|---|---|---|---|
| `pipeline/stages/toolchain.py` (LLVM path) | `toolchain` | snapshot-aware: offline `sudo pacman -U <cached suite>`, else `sudo pacman -S <suite>` | Sentinel scoped to install → Gate-3 verify → auto-rollback (build + Gates 1–2 run outside it). Full three-layer protection (sentinel + verify + clean-exit). |
| `pipeline/stages/kernel.py` | `kernel` | `sudo mkinitcpio -P` (regenerates initramfs — the boot-critical step) | Wraps `makepkg --install`, `mkinitcpio -P`, and the bootloader regen. |
| `pipeline/stages/packages.py` | `packages` | _none_ (no single command restores a partially-installed package set) | Wraps AUR-dep build + per-package install loop. Per-package failures are state-tracked and don't preserve the sentinel; only an interruption / unexpected exception does. |
| `pipeline/stages/reconfigure.py` (`_try_install_editor`) | `reconfigure-editor` | _none_ | Single-package install; sentinel is cheap consistency with the larger stages. |
| `verbs/runner.py` (any verb with `requires_sentinel=True`) | _verb name_ | per-verb (`verb.sentinel_recovery_cmd(args, pre)`) | Currently `build`, `update`, `state repair`, `state orphans --prune`, `state failed --clear`/`--clear-all`. |

The kernel and packages stage sentinels close the audit gap where an interrupted `pacman -U linux-custom` followed by an unfinished `mkinitcpio -P` could leave the system unbootable: the sentinel now persists across the makepkg → mkinitcpio → bootloader window, and the next sysforge invocation blocks at the CLI-entry recovery prompt with `sudo mkinitcpio -P` queued for auto-execution.

---

## CLI Verb Framework

Every top-level CLI verb (`build`, `update`, `fetch`, `doctor`, `resolve`, `env`, `setup`, `log`, `completions`, `packages …`, `state …`, `config …`, `run …`) is a `Verb` subclass — the `Verb` ABC and the `PreCheckResult`/`ExecResult` result types live in `sysforge/verbs/base.py`, while each concrete verb lives in its own per-command module (`build_cmd.py`, `run_cmd.py`, `env_cmd.py`, `completions_cmd.py`, `update.py`, `packages_cmd.py`, …). Verbs are dispatched through `run_verb()` in `sysforge/verbs/runner.py`. The framework is intentionally thin: three phases, two result types, one runner, one shared sentinel primitive. Argparse wiring in `cli.py` attaches the verb class via `parser.set_defaults(verb_cls=XVerb)` (never a `func=` callback), and `main()` resolves it via `sys.exit(_dispatch(args.verb_cls, args))` — a thin wrapper around `run_verb` that adds the optional cProfile harness (see *Global profiling flags* below).

**Three-phase contract.** Each verb implements:

- `pre_check(args) -> PreCheckResult` — validate args, load config, run preflights (LLVM safety, dirty-state guards, sudo checks). No state mutation. Returns one of three terminal shapes:
  - **proceed**: `skip_reason=None, blocker=None`, optional `ctx` dict carried into later phases.
  - **skip** (success short-circuit): `skip_reason="…"` — verb exits 0 with the reason logged.
  - **block** (failure short-circuit): `blocker="…", exit_code=N` — verb exits non-zero with the message logged.
- `execute(args, pre) -> ExecResult` — do the work. May mutate state. `ExecResult.exit_code` propagates to the process; `ExecResult.artifacts` is a free-form dict for `post_validate` to read.
- `post_validate(args, pre, result) -> None` — verify post-conditions, write final state, raise `RuntimeError` on failure. Default is a no-op.

**Result types** (`PreCheckResult` and `ExecResult`) are plain dataclasses with `ctx` / `artifacts` dicts; the runner does not inspect their contents. This keeps phase boundaries loose enough for ad-hoc data flow within a verb without inventing a per-verb context class.

**Sentinel handling.** Verbs whose `execute` mutates the live system set `requires_sentinel = True`. The runner wraps `execute + post_validate` in `sentinel_scope(state_dir, verb.name, recovery_cmd=…, retry_cmd=…, **metadata)` from `primitives/stage_sentinel.py`. On entry, the sentinel writes `stage_in_progress.toml`; on normal completion (both phases pass), it clears. On `RuntimeError` or `CleanExitRequested`, the sentinel is left in place so the next sysforge invocation blocks at the CLI-entry recovery prompt. `sentinel_scope` also installs an `InterruptScope`, so verbs participate in the same first-Ctrl-C-defers-to-safe-boundary behaviour as the toolchain stage. The toolchain pipeline stage uses the same primitive — there is one implementation, shared.

**Read-only verbs** (`env`, `resolve`, `log`, `state list`, `state orphans` without `--prune`, `state failed` without `--clear`/`--clear-all`, `packages list`, `doctor` without `--apply`) implement `execute` (the work is printing) and return `ExecResult()`; `post_validate` defaults to no-op and `requires_sentinel = False`. They use the same dispatch path as mutating verbs — no second code path.

**Error model.**
- `RuntimeError` raised from any phase → `_log.error(msg)`, return 1. Sentinel preserved if active.
- `SystemExit` → propagate verbatim (lets tests and signal handlers see the raw exit).
- `CleanExitRequested` → caught inside `sentinel_scope`, logged, re-raised as `RuntimeError` with the verb's `retry_cmd` and `recovery_cmd` in the message; sentinel preserved.

**Per-verb phase mapping** (current shape; phases reflect where each piece of work lives, not which primitives it calls):

| Verb | pre_check | execute | post_validate | sentinel |
|------|-----------|---------|---------------|----------|
| `build` | load config + LLVM preflight + `--cleansrc` validation | `build_core.build_and_install` (dep prep → build loop → install) | (build_state written by makepkg_wrapper) | yes |
| `update` | `--install-only` conflict check + config + state load + pacman-hook sentinel consumption | assemble → sync → vercmp → summary → `build_core.build_and_install` | (build_state written inline) | yes |
| `fetch` | load config + LLVM preflight | scheduler sync per pkg | non-blocker SyncResult statuses verified | no |
| `doctor` | load config + target expansion | depends/soname/ABI scan | invoke `BuildVerb` flow when `--apply` | delegated |
| `resolve` | load config | match rules + print | null | no |
| `env` | null | collect + format + print env chain | null | no |
| `setup` | read pacman.conf | check + patch IgnoreGroup | re-read confirms write | no |
| `log` | null | resolve unified/per-pkg log path; page through `$PAGER` | null | no |
| `packages {list,add,remove}` | load packages.toml + validate override fields | rewrite TOML | null | no |
| `state {list,repair,orphans}` | load state dir | inspect / repair / prune | null | `repair` only |
| `config merge` | null | scan config dir for `.sfnew`/`.pacnew`; pacdiff-style view/merge/remove loop | null | no |
| `run …` namespace | build `RunOptions` | delegate to `pipeline.run_pipeline` / `run_stage_standalone` | pipeline framework | (pipeline owns it) |

### Global profiling flags

Three top-level flags expose sysforge's own runtime performance (stdlib only, no new dependencies). All are position-independent: `_hoist_global_flags` in `cli.py` (a sibling of `_hoist_verbosity_flags`, run in the same argv-preprocessing pipeline) moves them — including `--py-profile-out`'s value token and its `=FILE` form — before the subcommand so argparse accepts them anywhere.

- **`--py-profile`** — `_dispatch` wraps `run_verb` in `cProfile.Profile()` and prints the top 25 functions by cumulative time to stderr at exit (stderr so piped stdout stays clean; the progress bottom-row is cleared first). The profiler stop/report sits in a `finally`, so verbs that `sys.exit()` from inside `execute` still emit stats. Only `run_verb` is wrapped — argv preprocessing and parser construction stay out of the profile. Known limitation: cProfile is main-thread-only, so `update`'s threaded version check shows up as join-wait, and subprocess work (makepkg/pacman/git) appears as wait time — wall-clock phase costs are `--timings`' job.
- **`--py-profile-out FILE`** — additionally `dump_stats(FILE)` for pstats/snakeviz; implies `--py-profile`. A separate flag (rather than an optional argument) so `sysforge --py-profile update` can't swallow the subcommand as a filename.
- **`--timings`** — promotes the wall-clock phase report to UI output after `build`/`update` runs. The phases are recorded unconditionally via `primitives/timing.PhaseTimer` (see §Primitives Layer → `timing.py`) and always written to the log at info level; the flag only changes where the report surfaces. `update` times source sync, version check, drift detection, and `pacman -Syu` around the engine; `build_core.build_and_install` records `dep prep`, per-package `build: <pkgbase>`, just-in-time `install deps: <pkgbase>` (when an intra-batch dep is installed ahead of a dependent), and `install` onto the caller's timer (or its own, exposed as `BuildOutcome.phase_records`).

A fourth global flag, **`--color=auto|always|never`**, is hoisted the same way (it carries a value token). It feeds the colour authority described in §Logging → Colour: `cli._resolve_color_mode` resolves `--color` flag > `[ui] color` config > `"auto"` and calls `log.set_color_mode()` once at startup. `auto` honours `NO_COLOR`/`FORCE_COLOR` and TTY detection; `always`/`never` force the decision (so colour survives a pager pipe, e.g. the coloured PKGBUILD review diff).

**Why not unify with the pipeline `Stage` contract?** Stages already presume multi-stage DAG semantics, per-stage checkpoints, and an opinionated `PipelineState`. Most CLI verbs are single-shot and don't want a pipeline state file. The verb framework reuses `sentinel_scope` for install-bearing protection but otherwise stays independent, so `sysforge env` is not paying for pipeline machinery it doesn't need. The `run` namespace verbs are exactly the thin shim from CLI → pipeline.

### Shared build engine (`build_core.py`)

`build` is a strict subset of `update`: both route their actual building through one engine in `sysforge/build_core.py`, so the two paths cannot drift the way they once did (a `build` that left makepkg's `-s`/`--syncdeps` in place would have makepkg run `pacman -S` on an AUR-only dependency and fail, while `update` stripped those flags and pre-resolved every dep itself). `update` extends the shared core with the things that are genuinely its own — version checking, source-sync scheduling, `--install-only`, toolchain pre-flight, the bulk `pacman -Syu`, and the run summary — but the dependency prep, the per-package makepkg invocation, and the install are identical code. Multi-package `build` runs end with their own `Build complete:` totals block (`build_cmd._print_build_summary`, mirroring `update`'s built/failed/skipped/pgo-skipped lines from the `BuildOutcome`); single-package runs skip it since the per-package narration already tells the whole story.

- **`build_and_install(targets, *, sync_source, …) -> BuildOutcome`** — the engine. Runs `prepare_deps`, then a per-package build loop, then `install_built`, returning the built/failed/pgo-skipped lists and the install-failed flag. Each makepkg call uses `strip_flags = BATCH_STRIP_FLAGS` (`{-s, --syncdeps, -i, --install}`) and `force_batch` when non-interactive, so makepkg never resolves deps via pacman and never installs inline — sysforge owns both. `targets` is any object exposing `pkgbase`/`pkgnames`/`pkgbuild_path`/`source` (`update._UpdateResult` qualifies directly; `build` builds a `BuildTarget` from the parsed PKGBUILD via `target_from_pkgbuild`). When the caller doesn't pass `pkgdest`, the engine resolves it from the system makepkg.conf (`pacman.get_pkgdest`) — artifacts land in `PKGDEST` when one is set, so the post-build snapshot, the `AlreadyBuilt` artifact scan, and the just-in-time install must all search there, not the PKGBUILD dir (the `build` verb relied on the caller default and silently installed nothing on PKGDEST systems; 2026-06-12 fix).
- **Intra-batch dependency ordering + just-in-time install** — before the build loop, `_order_targets_by_intra_deps` topo-sorts the batch (stdlib `graphlib`) by edges from each target's `depends` + `makedepends` + `checkdepends` matched against the other members' `pkgname`s **and `provides`** (version constraints stripped; soname provides like `libvulkan.so` participate; the parse is purely intra-batch — nothing external is queried; a dependency cycle warns and keeps the original order). The build loop then installs a freshly built member's artifacts (via `install_built`) *before* a dependent member's makepkg call, so the dependent configures against the new version instead of the stale installed one; the final bulk install skips files the just-in-time path already handled. Rationale: `prepare_deps`' AUR resolver only orders *missing* deps — a batch sibling already installed at a stale version never creates an edge there, so an alphabetical batch could build a loader against old headers whose new version sat unbuilt later in the same batch (the Vulkan 1.4.354 failure, 2026-06-12). A failed intra-batch dep only warns: the dependent still builds against the installed version and records its own failure normally.
- **`prepare_deps(pkgbuild_paths, config, *, building_names, …)`** — pre-installs missing repo *build deps* in one `pacman -S` transaction (`batch_install_makedeps`) and builds AUR/local deps in topo order (`resolve_aur_deps_batch` + `build_resolved_deps`), excluding the packages about to be built themselves. The repo arm collects `depends` + `makedepends` + `checkdepends` (`pacman.collect_builddeps`), **not just makedepends**: the per-package makepkg call runs with `-s` stripped, and makepkg checks runtime `depends` before building too, so a missing repo runtime dep would abort the build with exit 8 ("Could not resolve all dependencies"). It **filters the missing set to sync-repo packages first** (`aur.repo_packages`, the same classifier the AUR resolver uses) — an AUR-only dep mixed into the `pacman -S` transaction makes pacman abort with "target not found" and install *none* of the repo deps either, so AUR deps are excluded here and left to the AUR arm (which resolves `depends + makedepends`). Both arms are best-effort — a failure warns and lets the build proceed, surfacing a genuinely-missing dep as a per-package build failure with a diagnosis rather than aborting the whole batch up front.
- **`install_built(built_pkg_files, *, always_install=frozenset()) -> (files, install_failed)`** — dedupe, re-fetch the installed set (makedep/AUR pre-install may have expanded it), `filter_pkgs_to_installed` for split-pkgbase safety, then one `pacman -U`. The keep-set is the currently-installed pkgnames **union `always_install`** — the pkgnames the caller explicitly asked to build. `build_and_install` passes the build targets' pkgnames, so a fresh `sysforge build <new-pkg>` installs the package the user asked for instead of dropping it for not being installed yet; for `update` the union is a no-op (its targets are already installed). Reused by `update`'s `--install-only` artifact-scan branch (which keeps the default empty set).
- **`sync_source`** is the single deliberate caller difference: `update` passes `False` (Phase 2 already synced sources through the scheduler), `build` passes `not --no-update` to keep its inline per-package source sync (which itself routes through `source_sync.get_scheduler()` inside `makepkg_wrapper.run`). `_find_existing_artifacts` and `_record_build_failure` live here too (moved from `update.py`) since both the engine and `update`'s install-only scan use them.
- **`make_build_options(stage, options, **overrides) -> BuildOptions`** — the shared factory the three install-bearing pipeline stages (`kernel`/`toolchain`/`packages`) use instead of hand-assembling a `BuildOptions`. It maps the fields common to every stage's `RunOptions` (`no_pkg_logs` → `pkg_log`, plus `persist_log`/`state_dir`/`abi_check`, the last via `getattr` so a run-options object without it degrades to `False`), layers in the stage's constant defaults from `_STAGE_BUILD_DEFAULTS` (`kernel` → `owner_stage="kernel"` + `no_install=True`; `toolchain` → `pgo_managed=True`; `packages` → none), then applies the caller's per-call `overrides` (which win over both). Fields that differ per stage or per package — `profile_conf`, `log_dir`, `update`, `cc`/`cxx`/`ld_override`, `source`, `toolchain_variant`, `extra_flags`, … — are passed explicitly as overrides; anything a stage omits keeps its `BuildOptions` default (so `toolchain` not passing `log_dir` keeps it `None`). This is where a stage-wide default like the kernel build/install split lives, rather than being repeated at each call site.

---

## Verbs

The user-facing `sysforge` subcommands. Each is a `Verb` subclass dispatched
through the [CLI Verb Framework](#cli-verb-framework); the subsections below
document each verb's runtime behavior.

### `fetch.py`

Implements `sysforge fetch` — download one or more PKGBUILDs into `pkgbuild_src_dir` without building. Uses `find_pkgbuild` (auto-clones via `pkgctl_checkout` or `aur_clone` if not already present), then routes each already-cloned dir through `get_scheduler().request(SyncRequest(...))` for the shallow-fetch path (skipped with `--no-update`). `--force-fetch` sets `force_fetch=True` on the request, bypassing the RPC short-circuit for callers that want a guaranteed network check (e.g. to pick up a force-push that predates the cached RPC `last_modified`). Prints the resulting PKGBUILD directory path for each package. Exits 1 if any package failed.

Public API: `cmd_fetch(args)`.

### `resolve.py`

Implements `sysforge resolve` — inspect profile matching for a PKGBUILD without building it. Output goes to stdout.

Two modes:
- **Profile resolution** (default) — shows which profile and flags would apply to a package.
- **Dependency resolution** (`--deps`) — shows the full transitive dependency tree with build order. Displays AUR vs repo classification for each dep and marks already-installed packages. Dry-run only — does not build or install anything.

Public API: `cmd_resolve(args)`. Uses `find_pkgbuild` from `config.py` for PKGBUILD lookup (same search order as `sysforge build`). Internal helpers:
- `_get_profile_chain(profile_name, profiles)` — walks the `extends` chain and returns it root-last; stops on cycle or missing parent
- `_find_winner(matched_rules)` — returns the highest-priority rule that specifies a `profile` key
- `_format_conditions(rule)` — compact single-line summary of match conditions, omitting `profile`/`priority`
- `_print_resolve(...)` — formats and prints the resolve summary: package name, PKGBUILD path, all matched rules with winner marker, profile chain (`→` separated), build mode (if set), consumes, groups; with `--show-flags` expands the full resolved flag set with sysforge-internal keys separated under a comment

### `update.py`

Implements `sysforge update` — the update manager. The iteration scope is the **live install set**: every installed AUR package (`pacman -Qm`), plus every package sysforge source-built (`build_state.toml` is the tracking authority — `build_mode != "pacman"`), plus any repo packages selected by overrides or by `repo_mode = "profiled"`. `packages.toml` entries apply as overrides where present (see §Package Manifest). Organized into 7 phases:

**Phase 0 — Init.** Load BuildState, config, `packages.toml` overrides. Open unified log (always truncated). Refresh AUR name cache (skipped with `--offline` or `--install-only`).

**Phase 1 — Package set assembly** (`_assemble_package_set`). Build a unified `{pkgname: entry}` dict by walking the live install set: AUR (`pacman -Qm`) is always walked; every package sysforge source-built is always walked (`build_state.toml` `build_mode != "pacman"` — the authority that makes `sysforge build mesa` durable); repo packages are additionally walked when their `[[package]]` entry sets a behavior-changing field (`pkgbuild_patch`, `cache`, or `reason`), or when `[build] repo_mode = "profiled"` is set in `packages.toml` (the latter pulls every installed repo package into scope). Each repo entry is sub-classified into `repo_class = "source"` (has a behavior-changing override **or** was already source-built → goes through pkgctl-clone + makepkg) or `repo_class = "pacman"` (no override, never source-built → fast pacman path via `checkupdates` + a single terminal `pacman -Syu`). A source-built repo package must take the `"source"` path: a deferred `pacman -Syu` would be a no-op anyway (`IgnoreGroup = sf-build` shields the installed artifact), so rebuilding from source is the only way to refresh it. A bare `source = "repo"` entry is inert metadata (matches the `sysforge packages add` validator) and is *not* a trigger; the loader emits a warn line so it gets cleaned up. `packages.toml` entries are applied as override overlays (`source`, `pkgbuild_patch`, `cache`, `reason`); installed packages with no entry use defaults. Source classification is read from `build_state.toml`'s `source` field when present (set at build time) so a previously-built package keeps its origin across runs; falls through to override → pacman-foreign-inference for unrecorded packages. For AUR packages without a build_state record: bulk `aur_info` resolves the real `pkgbase` (split-package fix, e.g. `ob-xd-common` → pkgbase `ob-xd`). Apply positional PKG filter. Group by `pkgbase` to deduplicate split packages. Manifest entries whose package is not installed (e.g. a stored rule for `mesa-git` while repo `mesa` is installed) are not iterated — they are inert rules under the rules-not-install model.

**Phase 2 — Source sync** (`_sync_sources`). Ensures every iterated package has an up-to-date local PKGBUILD. Skipped entirely with `--offline` (and with `--install-only`, which forces offline since no rebuild will happen). VCS pkgbases (`-git`/`-svn`/`-hg`/`-bzr`) are filtered out of the request batch when `--devel` is **not** in effect — Phase 3 returns `DEVEL` for those packages without rebuilding, so the purge / clone / fetch / pkgver-resolve work is wasted; `--cleansrc` therefore never touches VCS checkouts unless `--devel` is also passed. Explicit positional pkgnames do not override this — the skip is uniform, mirroring the existing Phase 3 build-step skip. Pacman-class repo packages are likewise excluded (their upgrade detection runs through `checkupdates_map` in Phase 3 and the upgrade itself is dispatched as one terminal `sudo pacman -Syu`). The remaining requests delegate to the `source_sync.SourceSyncScheduler` singleton (`get_scheduler(...)`), issuing one `SyncRequest` per package — AUR sources go through the RPC short-circuit and `aur_clone`/`git_fetch_and_compare`, repo sources skip the RPC short-circuit (no AUR-RPC equivalent for the Arch packaging repo) and clone via `pkgctl_checkout` then refresh via `git_fetch_and_compare`; for repo sources only, a `STATUS_DIVERGED` outcome on a clean working tree triggers a hard-reset to `FETCH_HEAD` because pkgctl checkouts carry no user commits worth preserving (dirty trees are still respected and stay diverged):

1. **RPC-first.** The scheduler batches one `aur_info` call for all AUR packages in the run. For every package whose cached `rpc_version` / `rpc_last_modified` still matches the local HEAD metadata, the request short-circuits to `STATUS_UP_TO_DATE` — no `git fetch` executes at all.
2. **Clone on miss.** Missing / empty / non-git dirs hit `aur_clone` through the shared rate limiter.
3. **Shallow fetch + compare.** Everything else runs `git_fetch_and_compare` (depth-1 fetch, non-destructive HEAD compare). VCS packages (`-git`/`-svn`/`-hg`/`-bzr`) reaching the scheduler are always force-fetched regardless of RPC metadata — but this only happens under `--devel`, since `_sync_sources` filters them out otherwise (see above).
4. **Divergence is surfaced, not fixed.** A local-plus-upstream divergence (e.g. force-push, local commits) yields `STATUS_DIVERGED`; the build proceeds against the local PKGBUILD and an operator can opt into `--cleansrc` on the next run.
5. **Rate-limit aware.** AUR `Retry-After` is honoured; runs where the remaining penalty exceeds `[aur] rate_limit_abort_s` mark all pending packages `STATUS_RATE_LIMITED` rather than hanging for minutes.

Statuses treated as sync failures for the downstream buildability filter: `STATUS_FAILED`, `STATUS_RATE_LIMITED`, `STATUS_PURGE_REFUSED` (collected in `_SYNC_BLOCKING_STATUSES`, dispatched per-package via `_SYNC_STATUS_TO_ACTION`). `STATUS_DIVERGED` is a warning, not a blocker. `sync_failures` carries `(status, error_message)` so `_check_one_pkgbase` can map each blocking status to a distinct user-facing action: `STATUS_FAILED` → `PULL_FAILED`, `STATUS_RATE_LIMITED` → `RATE_LIMITED`, `STATUS_PURGE_REFUSED` → `PURGE_REFUSED` (cleansrc path).

**Phase 3 — Version check.** Parse PKGBUILD, look up installed version from `pacman -Q`, compare with `vercmp`. Produces a unified `_UpdateResult` per iterated package. Actions: `NEEDS_REBUILD`, `UP_TO_DATE`, `DEVEL`, `DEVEL_EVAL_FAILED`, `DOWNGRADE`, `PULL_FAILED`, `RATE_LIMITED`, `PURGE_REFUSED`. (`NOT_INSTALLED` is no longer emitted: under the live-install-set iteration model, only installed packages reach Phase 3.) For VCS packages (`-git`/`-svn`/`-hg`/`-bzr`) the static PKGBUILD `pkgver` is just a seed; without `--devel` the worker short-circuits *before* the pkgbuild_dir probe and PKGBUILD parse, returning `DEVEL` directly from the installed version (no `pkgbuild_ver`, no `pkgbuild_path`) — this matches the Phase 2 source-sync filter so both edges of the pipeline are silent on VCS pkgbases under the default mode. With `--devel`, the worker first attempts a cheap short-circuit: if `build_state.toml` carries a `built_upstream_commit` for the pkgbase, `vcs_pkgver.peek_upstream_commit` runs `git ls-remote` against the PKGBUILD's `source=()` URL and on a SHA match the package is reported `UP_TO_DATE` without ever touching makepkg. On a cache miss (no stored SHA, multi-git-source, ls-remote failure, or differing SHA) the canonical resolver `vcs_pkgver.evaluate_vcs_pkgver` runs `pkgver()` against the fetched upstream sources and the resulting `[epoch:]pkgver-pkgrel` is vercmp'd against installed (`NEEDS_REBUILD` / `UP_TO_DATE` / `DOWNGRADE`). Resolution failures are reported as `DEVEL_EVAL_FAILED` and skipped — the package is left untouched on the assumption that a missed update is preferable to wasted rebuilds when `pkgver()` is transiently broken (broken bash, dropped network mid-fetch, missing makedeps for `prepare()`).

**Phase 4 — Summary + dry-run gate.** Print per-package status. Exit if `--dry-run`. The summary header always lists per-action counts (`5 need rebuild, 30 up to date, 7 skipped (3 rate-limited, 2 devel, 1 pull failed, 1 purge refused)`); per-package detail is suppressed by default for non-actionable statuses (`UP_TO_DATE` / `DEVEL` / `DEVEL_EVAL_FAILED` / `RATE_LIMITED` / `PURGE_REFUSED` / `PULL_FAILED`) and surfaced with `-v`. `NEEDS_REBUILD` and `DOWNGRADE` always render per-package because they are actionable. The `_consume_pacman_hook_sentinels()` pass at `cmd_update` entry surfaces kernel/toolchain reminders dropped by the libalpm PostTransaction hooks (see §`pacman.py`); the buildstate sentinel is consumed silently.

**Phase 4.25 — Toolchain-variant drift.** Compare each result's recorded `toolchain_variant` (from `build_state.toml`) against the active variant resolved via `pipeline.state.get_toolchain_variant(PipelineState(state_dir))`. Drift means "the installed binary was produced under a different compiler identity than is active now" — e.g. you swapped `compiler = "gcc"` → `compiler = "llvm", pgo = true` and previously-built packages still carry `toolchain_variant = "gcc"`. Pure pacman-mode entries (`build_mode = "pacman"`) have no variant and are never drift candidates. Skipped entirely when the active variant is `"system"` (no toolchain stage has run). Always prints a one-line summary if any drift is detected. `--explain-drift` prints the full list and exits before Phase 5. `--rebuild-on-toolchain-drift` promotes drifted `UP_TO_DATE` results to `NEEDS_REBUILD` (off by default — drift is informational because most C/C++ packages don't measurably benefit from a re-stamp; the leverage is in libLLVM consumers, not blanket rebuilds). Drifted results that don't have a resolvable `pkgbuild_path` (typically pacman-class) are warned and skipped rather than crashing the build loop.

**Phase 4.3 — Flag drift.** Re-resolve the current profile for each iterated profiled package (`build_mode = "profiled"`) and diff the freshly serialized flags against the `flags_string` recorded in `build_state.toml` at build time; packages whose flags now resolve differently are *flag-drifted*. Detection is delegated to `primitives/flag_drift.resolve_flag_drift` — the single flag-drift engine. Patched-PKGBUILD and kernel builds carry their flags embedded in the PKGBUILD, so the embedded profile is extracted before resolution (matching the build-time path); a PKGBUILD that fails to parse is warned and skipped, never fatal. Flag-drift detection is **network-free** — it only re-resolves the local PKGBUILD + stored flags — so `sysforge update --offline --dry-run` is the read-only, no-network flag-drift report. Always prints a one-line summary if any drift is detected; `--explain-drift` lists every drifted package with a per-key flag diff (alongside toolchain drift) and exits before Phase 5. `--rebuild-on-flag-drift` promotes flag-drifted `UP_TO_DATE` results to `NEEDS_REBUILD` (off by default — one edit to a shared profile can drift *every* profiled package, so an unattended `update` must not silently trigger a full-system rebuild); `--rebuild-on-drift` is the umbrella that opts into both drift axes. Drifted results without a resolvable `pkgbuild_path` are warned and skipped. Promoted packages flow through the normal Phase 5/6 build+install, so the `filter_pkgs_to_installed` split-package guard applies unchanged. **Scope — build-state-wide fold (absorbed from the removed `converge` verb):** after the walk-driven pass, a second pass covers every profiled `build_state.toml` entry *outside* the run's package walk — now that a plain source-built package is *in* the walk (and directly promotable), the remaining out-of-walk cases are stage-owned (kernel/toolchain) entries filtered from the walk, or entries excluded by a positional `PKG` name filter. Fold entries respect the positional `PKG` filter, are reported and listed under `--explain-drift` like any other drift, but cannot be promoted to `NEEDS_REBUILD` (there is no walk entry to promote) — under a rebuild flag they get a warning pointing at `sysforge build <pkg>` (or the owning pipeline stage) instead. When the walk itself is empty but profiled build_state entries exist, `update` skips the "no packages in scope" early-exit and proceeds straight to the drift phases, so a drift-only run works on a system with no AUR packages.

**Phase 5 — Build.** Filter to buildable packages: `NEEDS_REBUILD`. Inside `build_core.build_and_install`, the **PKGBUILD review gate** (`primitives/pkgbuild_review.py`) runs first. `update` defaults to the gate's **auto mode**: each package whose clone HEAD differs from its recorded `reviewed_commit` is auto-accepted with a per-package `[REVIEW] auto-accepted` notice, so a plain `update` stays unattended. `--review` opts into the interactive mode (`build`'s default): a full source-tree diff prompt (view / accept / skip / abort, single keypress) before dep prep — skip drops the package (counted as skipped in the summary), abort ends the run with nothing built or installed. `--no-review` or `[build] review = false` skips the gate entirely (no notices). (Phase 3 already converted VCS packages to `NEEDS_REBUILD` / `UP_TO_DATE` / `DEVEL_EVAL_FAILED` under `--devel`, so no separate `DEVEL` union is needed at this stage.) Batch makedeps pre-install (single `sudo pacman -S`). AUR dep resolution + build. Single build loop for all packages. `--cleanbuild` (`-C`) prepended by default (suppressed by `--no-cleanbuild`). `--syncdeps`/`-s` and `--install`/`-i` stripped; packages installed in phase 6. `AlreadyBuilt` raised by `makepkg_wrapper.run` (PKGDEST already holds the matching `.pkg.tar`) is treated as a successful build: `_find_existing_artifacts` locates the matching files in pkgdest and queues them for install, instead of marking the pkgbase failed.

With `--install-only` the build loop is replaced wholesale: no makedep batching, no AUR dep resolution, no `makepkg` invocation. For each buildable result, `_find_existing_artifacts(pkgdest_or_pkgbuild_dir, pkgnames, pkgbuild_ver, installed_ver=...)` is called directly. Hits are queued for phase 6; misses log a `[SKIP]` line and are counted alongside the existing `skipped` total.

`_find_existing_artifacts` is a two-stage lookup. First it tries the strict glob `{pkgname}-{pkgbuild_ver}-*.pkg.tar.*` — the common path for non-VCS packages where the static PKGBUILD parse equals the filename version. If that returns nothing it falls back to a pkgname-only glob `{pkgname}-*-*-*.pkg.tar.*`, parses each filename's `(epoch, pkgver, pkgrel)`, and picks the newest by `vercmp`. The fallback is required for VCS (`-git`/`-svn`/...) packages, where `pkgver()` bumps the version dynamically at build time (PKGBUILD `pkgver=0.1.0` → artifact `0.1.0.r45.g1234567`) so the static `pkgbuild_ver` never matches the filename. When `installed_ver` is supplied (always under `--install-only`), the fallback further excludes any artifact not strictly newer than installed, preventing redundant reinstalls or downgrades. The `AlreadyBuilt` call site omits `installed_ver`: makepkg has already proved the artifact exists in PKGDEST, so the lookup just needs to find it.

**Phase 6 — Install + finalize.** Filter the built `.pkg.tar.*` files with `filter_pkgs_to_installed` so only files whose `pkgname` is already present in `pacman -Q` reach `pacman -U` — split `-git` pkgbases emit one file per split pkgname, and rebuilding must not silently add sub-packages the user never installed (e.g. `pipewire-full-git` emits 16 files; only the 2 installed ones get installed). New dependencies built via `build_resolved_deps` are handled on a separate path and are unaffected by this filter. Single `sudo pacman -U` for the kept set. Cache report, final summary, close unified log.

**Phase timing.** The body records wall-clock phase durations on a `primitives/timing.PhaseTimer` — `source sync`, `version check`, `drift detection`, and `pacman -Syu` around the engine, plus the `dep prep` / `build: <pkgbase>` / `install` records `build_core.build_and_install` appends to the same timer. The report (`render_report`, rendered by `_emit_timings` under `[UPDATE]`) is always written at info level (so it lands in the unified log) and is promoted to UI output with the global `--timings` flag. It is emitted at the final summary, at the "Nothing to rebuild" early exit, and on the `--explain-drift` / `--dry-run` exits, so `sysforge --timings update --dry-run` is the cheap way to see where a check-only run spends time.

Positional: `[PKG ...]` — optional package names to restrict the run to a subset of packages.

Flags: `--interactive`, `--packages`, `--dry-run`, `--devel`, `--offline`, `--install-only`, `--no-cleanbuild`, `--cleansrc`, `--state-dir`, `--profile-conf`, `--cache-report`, `--no-pkg-log`, `--persist-log`, `--log-dir`, `--makepkg`, `--review`, `--no-review`, `--explain-drift`, `--rebuild-on-toolchain-drift`, `--rebuild-on-flag-drift`, `--rebuild-on-drift`.

`--install-only` is mutually exclusive with the build-tuning flags `--makepkg`, `--no-cleanbuild`, `--cleansrc`, `--interactive`, and `--cache-report`; argparse rejects the combination. It implies `--offline`. Use it to install artifacts left in PKGDEST by a previous interrupted run, or by a manual `makepkg` invocation, without re-entering the build loop.

**Unattended full update.** `sysforge update` (no positional args) is the supported recipe for a hands-off "rebuild everything outdated" run: walks every installed AUR package, every package sysforge previously source-built (build_state), and any repo packages with profiled overrides, rebuilds those flagged `NEEDS_REBUILD`, and automatically clones any missing src dirs. Add `--cleansrc` to also discard divergent upstreams — this is destructive but per-package safe, since `purge_src` refuses any clone that holds uncommitted changes. `--cleansrc` also bypasses the RPC short-circuit so every AUR package in the run is re-cloned from scratch rather than trusting the cached metadata. VCS pkgbases are exempt from `--cleansrc` unless `--devel` is also passed (their checkouts are never touched in the default mode, since the build step skips them too). A refused package is counted as failed and skipped.

### `doctor.py`

Implements `sysforge doctor` — the unified diagnostic front-end for sysforge-managed system health. Its original axis is package depends + linkage drift (the class of breakage where a partial rebuild leaves an installed package referencing ABIs that no longer exist — e.g. graphics-stack drift: mesa, vulkan, libglvnd, GPU driver), but it now also runs a set of read-only **system-state axes** (toolchain provenance, hardware/boot readiness, graphics misconfiguration, pacman/system integrity, sysforge state integrity, boot/kernel runtime, services/runtime health). Read-only — never rebuilds or installs (except the `--apply` rebuild bridge), and the new axes never mutate state (no `pacman -Sy`, no `BuildState.save()`, no sentinel recovery). Bare `sysforge doctor` runs every system axis — the intended one-stop debug source for "is anything wrong with my sysforge-managed system".

**Unified `Finding` framework (`primitives/diagnostics.py`).** Every axis is a *producer* that returns `list[diagnostics.Finding]` — one finding shape (`category`, `severity`, `check_id`, `message`, `remediation`, optional `fix_cmd`/`auto_remediable`/`is_brick`) that subsumes the per-probe dataclasses that grew up independently (`GraphicsFinding`, `DeviceFinding`, `KernelFinding`, `ToolchainMismatchFinding`) plus the two outliers `ToolchainCheck` (`toolchain_preflight`) and `FixSuggestion` (`build_diag`). The probes keep their own dataclasses; `diagnostics.adapt` / `from_toolchain_check` / `from_fix_suggestion` convert at the boundary, so no probe is rewritten and the layering rule holds (the framework lives in `primitives` and never imports the `pipeline` layer — pipeline-layer checks are adapted by their callers). Rendering (`render_axis`), exit-code reduction (`error_count` — error-severity or `is_brick` ⇒ non-zero), severity normalisation (`normalize_severity` folds `"warning"` → `warn`), and exception-isolated axis running (`run_axes` — a raising probe degrades to one `*:probe_error` warning, never aborting the sweep) are centralised there. This is the backbone intended to become the single source for sysforge's internal error-checking/recovery; internal callers are migrated onto it incrementally (they currently keep their own entry points).

**Internal-caller adaptation status.** The two outlier internal shapes have correct, tested adapters into `Finding`: `from_toolchain_check` (`toolchain_preflight.ToolchainCheck`, used by `update`'s batch preflight + the toolchain stage) and `from_fix_suggestion` (`build_diag.FixSuggestion`, the build-failure diagnoser). Two live consumers already route internal state through the framework via `doctor`: the `state` axis surfaces persisted `build_diag` failures (`build_state.toml` `[failures]`) and the `toolchain` axis surfaces `llvm_state` provenance. The remaining migration — re-routing the *live* `update`-preflight and toolchain/kernel-stage render helpers through `render_axis` (which would change their on-screen output text) — is intentionally deferred: the adapters make it a mechanical follow-up, but it touches well-tested core install-path output and is out of scope for the doctor-focused work, so those callers keep their existing renderers for now.

For each target package, reads `/var/lib/pacman/local/<pkg>-<ver>/` directly: `files` for package-owned paths (filtered to `.so`/`.so.*`), `desc` for the `%DEPENDS%` array. Then runs two checks per package:

- **Depends check.** For each depends entry: versioned package deps verified via `pacman -T` + `vercmp`; `libfoo.so` and `libfoo.so=N` entries verified via `dep_analysis.soname_satisfied` against the `ldconfig -p` set.
- **ABI/linkage check.** Calls `abi_check.check_so_files` on the installed `.so` files — same symbol cross-check logic that `sysforge build --abi-check` runs, pointed at `/usr/lib/...` instead of a fresh archive.

Closure walk: by default, BFS over the target's `%DEPENDS%` transitively so one command covers the full dependency neighbourhood (the typical Steam-black-window pattern is a breakage one or two levels down from the root the user names). `--shallow` restricts to direct depends only. BFS dedupes on the resolved real pkgname from `pacman -Q` to collapse `provides`/virtual-package cycles. Output groups issues by the package the issue was found in, not by the root that triggered the walk, so overlapping closures from multiple roots produce one report per affected package.

Per-package headers and the final summary both tag each package with its installation origin — `[aur]` for foreign packages (`pacman -Qm`) and `[repo]` for non-foreign. Example: `== steam 1.0.0.79-1 [aur] ==` and `Affected: steam [aur] (62), mesa [repo] (3)`. The tag reflects where the *currently installed* copy came from, not where updates might be available; an AUR package that's also shipped by a repo still reads `[aur]`. This directly distinguishes the rebuild surface: `[aur]` findings are fixed by a rebuild through sysforge's own build path; `[repo]` findings require a `-Syu` that includes a maintainer rebuild. Not-installed roots read `(not installed)` without an origin tag.

`--graphics` expands to a curated stack: always `mesa[-git]`, `lib32-mesa[-git]`, `vulkan-icd-loader`, `lib32-vulkan-icd-loader`, `libglvnd`, `lib32-libglvnd`, `egl-wayland[-git]`, `xwayland[-git]` / `xorg-xwayland[-git]`, `wayland` + `lib32-wayland`, `libdrm` + `lib32-libdrm`, `libva` + `lib32-libva`, `libvdpau` + `lib32-libvdpau`, `gamescope`; plus per-vendor additions driven by the hardware overlay's `gpu_vendors` list (`nvidia` → active `nvidia-*` / `nvidia-open*-dkms` driver + `lib32-nvidia-utils` + `nvidia-settings`; `amd` → `vulkan-radeon`, `lib32-vulkan-radeon`, `libva-mesa-driver`; `intel` → `vulkan-intel`, `lib32-vulkan-intel`, `intel-media-driver`). The list is filtered against `pacman -Q` so only installed variants are actually verified — avoids false negatives on boxes that don't have lib32 counterparts. The expansion table lives in `doctor.py::GRAPHICS_BASE` / `GRAPHICS_BY_VENDOR` as reference data, not config.

`--graphics` also runs a second axis of checks — system-state probes from `primitives/graphics_probe.py` — after the package walk completes. These catch classes of graphics breakage that ABI/linkage walks cannot see: kernel-module parameters, NVIDIA driver version skew, session-type / compositor misconfiguration, missing Wayland explicit-sync protocol, Steam client config regressions. See `graphics_probe.py` below for the check inventory. Findings with severity `error` contribute to the exit code; `warn` and `info` do not.

`--hardware` runs a hardware/boot-readiness axis: it inventories all PCI/USB devices and flags any present device with no driver bound (`device_probe.check_unsupported_devices`), then audits the **running** kernel's `.config` (from `/proc/config.gz` or `/boot/config-$(uname -r)`) against the detected devices and root topology (`kernel_safety.audit_resolved_config`) — the on-the-spot diagnostic for "device X has no driver" / the `CONFIG_SND_PCI`-class trap. Unlike `--graphics`, `--hardware` needs no package targets and can be run on its own (`sysforge doctor --hardware`); it renders findings through `diagnostics.render_axis` in the `[SEV] check_id: message → remediation` format. `error`-severity findings (brick-class boot-config drops, carried via `is_brick`) contribute to the exit code; device-driver and degraded findings warn. Because the whole axis reads the **running** kernel, every finding is tagged with a reboot caveat (`_with_reboot_hint` in `doctor.py`): doctor re-probes live each run, but a kernel rebuilt/installed but not yet booted won't change these lines until reboot — so a freshly-applied fix correctly persists until the new kernel is the running one.

`--toolchain` runs a configured-vs-installed toolchain axis via `llvm_state.detect_toolchain_config_mismatch` (which wraps the sanctioned `collect_llvm_state` entry point — provenance reporting, not a third toolchain *health* probe). When `toolchain.toml` requests a custom LLVM toolchain (`enabled = true`, `compiler = "llvm"`) but stock repo LLVM is installed (`install_origin == "repo"`), or the PGO profdata is version-skewed, it reports the mismatch in the same `[SEV] check_id: message → remediation` format. The whole axis is suppressed when `skip_build = true` (that mode intentionally registers the installed compiler as-is, so stock-vs-custom is a deliberate choice, not a mismatch — like the `compiler = "gcc"` register-only path) and contributes `error`-severity findings to the exit code. Like `--hardware`, it needs no package targets and runs standalone (`sysforge doctor --toolchain`); the two can be combined (their exit codes OR together). This is the standalone surface of the same check the kernel stage emits before a build.

`--pacman` runs a pacman / system-integrity axis (`primitives/system_probe.py::collect_system_findings`) — all read-only against the *local* database (never `-Sy`): `pacman -Dk` local-db dependency consistency (missing deps → `error`), a lingering `/var/lib/pacman/db.lck` from an interrupted transaction (`warn`), unmerged `*.pacnew`/`*.pacsave` config files under `/etc` (`warn`), and `pacman -Qtdq` true orphans (`info`). Standalone (`sysforge doctor --pacman`).

`--state` runs a sysforge state-integrity axis (`primitives/state_probe.py::collect_state_findings`) — read-only inspection of sysforge's *own* persisted state: recorded build failures from `build_state.toml`'s `[failures]` table (each `warn`, surfacing the `build_diag` signature + `fix_cmd` when present), an interrupted stage sentinel via `StageSentinel.get_active()` (`error`, carrying the recorded `recovery_cmd` — it does **not** call the recovering `check_and_recover_stale_sentinel`), and build-state drift vs the live pacman db (`info`, zombie entries for uninstalled packages). The last source-sync `STATUS_*` is intentionally *not* surfaced: the source-sync scheduler cache is per-process, so a standalone `doctor` run has no sync results. Standalone (`sysforge doctor --state`).

`--boot` runs a boot/kernel-runtime axis (`doctor._collect_boot_findings`, reusing `primitives/kernel_safety`) — the running-system analog of the kernel stage's gates 1/3: per-bootable-kernel boot-artifact verification (`verify_boot_artifacts`: vmlinuz + initramfs + a boot entry; gaps are brick-class `is_brick` → exit code), a recovery-fallback check (`find_fallback_kernels`; only one bootable kernel → `info`), `/boot` free space (`check_boot_mount_space`), and DKMS modules for the running kernel (`check_dkms_for_kernel(running_kernel_release())`; these `dkms:*` findings carry the running-kernel reboot caveat, while the filesystem-live boot-artifact / `/boot`-space findings do not). The running-kernel `.config` device audit stays in `--hardware` (it is device-driver coverage, distinct from boot-artifact readiness), so there is no double-report. Standalone (`sysforge doctor --boot`).

`--services` runs a services/runtime-health axis (`primitives/runtime_probe.py::collect_runtime_findings`): failed systemd units (`systemctl --failed` → each `error`) and firmware a driver requested but could not load this boot (best-effort parse of `journalctl -k -b` for "Direct firmware load … failed" → `warn`; degrades silently when the journal is unreadable; the `missing_firmware` finding carries the current-boot reboot caveat, while live `failed_unit:*` findings do not). Standalone (`sysforge doctor --services`).

Vendor detection for `--graphics` prefers the hardware profile (`/var/lib/sysforge/hardware_profile.toml` → `[gpu] vendors`); when that file is absent, falls back to `lspci -nnk` scraping, extracting `nvidia`/`amd`/`intel`/`radeon` from VGA-class device strings. The `lspci` fallback is used for both the package-expansion vendor list and the graphics-probe vendor-gating.

**Invocation modes (which axes + which package walk).** Resolved by `_resolve_axis_names`:

- **Bare `sysforge doctor`** (no packages, no `--repo`, no axis flag) → runs **every system axis** (toolchain, hardware, graphics, pacman, state, boot, services) in `_SYSTEM_AXIS_ORDER` and **no** package walk. The fast "is anything wrong" default. (Previously bare was a usage error.)
- **`--all`** → every system axis **plus** the full per-package walk over `pacman -Q` (foreign and non-foreign). The exhaustive "is anything broken anywhere" sweep.
- **Explicit axis flags** (`--toolchain` / `--hardware` / `--graphics` / `--pacman` / `--state` / `--boot` / `--services`) → exactly those axes, in canonical order. `--graphics` additionally adds the graphics-stack closure to the package walk (it is both an axis flag and a package-walk trigger).
- **Package targets or `--repo`** without an axis flag → a focused package walk and **no** system axes. `--repo` narrows the walk to non-foreign packages (all of `pacman -Q` minus `pacman -Qm`).

The genuine usage error (exit 2, `nothing to check`) now only fires when nothing at all is selected — e.g. `--repo` on a system with no installed packages. Each axis renders its own `== <label> ==` section through `diagnostics.render_axis`; exit codes OR together across the package walk and all axes.

`--suggest` (`-s`) reverse-looks up lookup-able findings via `pacman -Fq` and prints a kind-tagged candidate line under each issue. Findings are split into four kinds — the distinction matters because they imply different remediations:

- `install` — the soname is **not present** on disk and the owning package is **not installed**. Installing it fixes the finding. Rendered as `      → install candidate: repo/pkg, …`. Covers:
  - Depends issues whose text matches `soname not found in ldconfig: libfoo.so[=N]`.
  - ABI issues of the form `… NEEDED lib 'libfoo.so.N' not found in ldconfig cache`.
  - The owning-package check uses `pacman -Q`: candidates already installed are dropped from this list (so `--suggest` never re-recommends a package the user already has). When the filter empties the list, the line becomes `      → all owning packages already installed; try \`sudo ldconfig\`, then re-run doctor` rather than the original `no candidate in files db`.
- `rebuild` — an `undefined versioned symbol` finding whose candidate owner **is installed and foreign** (locally built / in `pacman -Qm`). The actionable bucket: the fix is to **rebuild** that package against the current system. Rendered as `      → rebuild candidate: repo/pkg, …`.
- `repo_rebuild` — same shape as `rebuild`, but the candidate owner is an **installed repo package** (not in `pacman -Qm`). Surfaced separately as informational because the user can't fix repo-vs-repo drift through the foreign-rebuild flow — only by waiting for the repo to ship a newer build (or, rarely, `sudo pacman -S` to reinstall the current version). Rendered as `      → repo rebuild candidate (await repo update): repo/pkg, …`. Without this split the rebuild list filled up with noise like `core/glibc, core/libgcc, core/openssl, extra/libx11` whenever any repo package showed drift.
- `abi_drift` — same shape as `rebuild`, but only used when no `installed_names` filter is supplied (callers outside `cmd_doctor`). Rendered as `      → ABI-drift candidate (rebuild/upgrade): repo/pkg, …`. Inside `cmd_doctor` every `undefined versioned symbol` is partitioned three ways into `rebuild` (installed + foreign), `repo_rebuild` (installed + repo), and `install` (not installed) — so `abi_drift` is reserved for direct callers of `_collect_suggestions` that pass `installed_names=None`.

Keeping the kinds distinct avoids the reinstall-loop failure mode where a user reinstalls the surfaced packages and the same findings reappear (reinstalling the same `.pkg.tar.zst` archive cannot change the versioned symbols on disk).

**Stale ldconfig fallback.** `/etc/ld.so.cache` is only refreshed when `ldconfig` runs as root, typically from a package install hook. Between an install and the cache refresh — or when the install hook is absent — `ldconfig -p` reports a soname as missing even though the `.so` is on disk. To stop doctor from flagging these as false positives, `dep_analysis.soname_available` consults the resolved library directories (`/usr/lib`, `/usr/lib32`, plus absolute dirs declared in `/etc/ld.so.conf.d/*.conf`) when the in-cache check fails. Per-process `lru_cache` keeps the directory scan to one filesystem traversal per `lib32`/non-`lib32` axis. `soname_satisfied` remains the pure predicate; `soname_available` is the cache-aware variant.

`lib32` context is inferred from the owning pkgname prefix (`lib32-*` → query `usr/lib32/<soname>`). Requires a synced files db: if `/var/lib/pacman/sync/*.files` is absent, the command emits one warning (`run sudo pacman -Fy`) and runs the rest of the report with lookups skipped — findings still show, exit code unchanged.

End-of-run summary (when any candidates were collected): `Suggestions:` header with one line per affected package (`  <pkg>: install: cand-a, cand-b` and/or `  <pkg>: rebuild: cand-c` and/or `  <pkg>: repo-rebuild: cand-d` and/or `  <pkg>: abi-drift: cand-e`), followed by deduped lists across the whole run — `Install candidates: …`, `Rebuild candidates (foreign; ABI drift): …`, `Repo packages with ABI drift (await repo update or `sudo pacman -S` to reinstall): …`, and `ABI-drift candidates (rebuild or upgrade, not reinstall): …`. The actionable foreign-rebuild line and the informational repo-drift line are kept separate so a glibc/libgcc/openssl drift finding can't dilute the "what should I rebuild" list.

Foreign-package origin tags carry an extra `[untracked]` suffix when the pkgname is in `pacman -Qm` but absent from `build_state.toml` — same signal `sysforge state list` exposes. Header reads `== <pkg> <ver> [aur][untracked] ==` for these. Failure to read `build_state.toml` is non-fatal: the tag silently reverts to plain `[aur]` for backwards-compatible behaviour.

All report output (headers, issue lines, summary) flows through `log.ui` (→ stderr + unified log file) so external callers that scrape the unified log see doctor findings.

**`--apply` bridge.** `--apply` (implies `--suggest`) hands the REBUILD-classified candidates to `sysforge update` for actual rebuild. Drift-rebuild only: install candidates (not yet installed) are surfaced as `→ run: sysforge build <pkg>` informational lines but never invoked. Repo packages outside `sysforge update`'s scope (no behavior-changing override, no `repo_mode = "profiled"`) are surfaced as `→ run: sudo pacman -S <pkg>` and skipped. Foreign packages — and repo packages eligible under `repo_mode = "profiled"` — are gathered into a single eligible list, the user is prompted (`--no-confirm` skips), and `cmd_update` is invoked with that list as the positional pkgname filter. `--dry-run` reports the rebuild list without invoking the build. `--apply`'s exit code dominates the doctor exit — a successful rebuild produces exit 0 even if doctor surfaced issues. The bridge is intentionally thin: rather than extracting `update.py`'s build loop into a separate primitive, doctor synthesizes a `cmd_update` args namespace and reuses the existing path verbatim.

> **Real-world status (2026-05-02): unit-tested only.** The unit tests
> (`tests/test_doctor.py::test_apply_*`) mock `cmd_update` entirely, so the
> end-to-end "doctor finds drift → update rebuilds → install succeeds" path
> has not been exercised against a live system yet. Treat `--apply` as
> shipping behind tested-by-mock semantics; full integration verification
> is pending.

Public API: `cmd_doctor(args)`. Positional `[PKG ...]` and flags `--graphics`, `--hardware`, `--toolchain`, `--pacman`, `--state`, `--boot`, `--services`, `--all`, `--repo`, `--shallow`, `--quiet` (suppress clean lines, show only issues), `--suggest` / `-s` (inline + end-of-run candidate lookup via files db), `--apply` (drift-rebuild bridge), `--no-confirm`, `--dry-run`. New axes register in `_SYSTEM_AXIS_ORDER` / `_AXIS_FLAGS` / `_system_axes` with a `_collect_<axis>_findings` producer (looked up through module globals so tests can monkeypatch them).

Log tag: `[DOCTOR]` (was `[DOC]` before P3.4). Primitive lookup helper lives in `sysforge/primitives/provides_lookup.py` — see the `provides_lookup.py` subsection for the public API. NEEDED-soname extraction reuses `abi_check.needed_sonames` (public since doctor calls it directly for ABI-issue suggestions). System-state probes live in `sysforge/primitives/graphics_probe.py` — log tag `[GFX]`, public API `check_system_graphics(config, *, gpu_vendors=None)`; invoked from `cmd_doctor` when `--graphics` is set.

### `setup_cmd.py`

Implements `sysforge setup` — one-shot pre-flight that stops `pacman -Syu` from silently clobbering sysforge-built packages with upstream repo binaries. It inspects `/etc/pacman.conf` for `IgnoreGroup = sf-build` and, if missing, offers to add it (interactive prompt). Packages built by sysforge carry the `sf-build` group, so the IgnoreGroup line gates the whole rebuild surface behind a single policy knob rather than requiring a per-package `IgnorePkg`.

Public API: `cmd_setup(args)`. Flag: `--pacman-conf PATH` (default `/etc/pacman.conf`) for VM or chroot runs where the file lives elsewhere. No effect if the line is already present. Intended to be run once after first installing sysforge; safe to re-run.

### `state_cmd.py`

Implements `sysforge state` — a small read/repair namespace for `build_state.toml` (the live install-state mirror and the `[failures]` table). Separate from `sysforge packages` (which manages override rules in `packages.toml`); the split mirrors the rules-vs-state separation described in §Package Manifest.

- **`state list`** — tabulates `build_state.toml` entries: pkgbase, build_mode, last build, profile/flags. Read-only. Also appends an *Untracked foreign packages* section listing any installed `pacman -Qm` package that has no `build_state.toml` entry — those slipped past sysforge (installed manually) and won't be rebuilt by `sysforge update` from a known PKGBUILD without a fresh fetch.
- **`state repair`** — re-parses PKGBUILDs for entries whose stored fields contain unexpanded shell variables (e.g. `${pkgver}` literals from a buggy parse) and rewrites those rows. `--dry-run` previews fixes without writing.
- **`state orphans`** — scans `PKGDEST` for `.pkg.tar*` artifacts whose pkgname is installed AND whose version is strictly older than the installed version (the **superseded** category). Read-only by default; `--prune` deletes after a y/N prompt (`--no-confirm` skips). The detection primitive `pacman.detect_orphan_artifacts(pkgdest, installed)` returns `{"superseded": [...]}`.
  - **Files whose pkgname is *not* installed are intentionally NOT surfaced.** They could be a build kept on purpose (a kernel artifact whose source has local commits the user wants to keep available for later install, a test build, etc.) and we can't safely tell the difference. Per the load-bearing rule: if `--prune` wouldn't safely delete a file, don't list it. Users who want a broader view can use `paccache`/manual inspection.
  - Files whose `.PKGINFO` can't be read or whose filename doesn't parse are silently skipped — never deleted.
- **`state failed`** — lists the `[failures]` table of `build_state.toml`: pkgbase, failure timestamp, diagnosis signature, the diagnosed fix command (when `build_diag` matched a known signature), and the truncated error tail. Read-only by default. `--clear PKGBASE` removes one entry and `--clear-all` removes all (both rewrite `build_state.toml`, so they take the sentinel like `state repair`). Failures are recorded by `sysforge update`'s build fan-out (`_record_build_failure`) and auto-clear on the next successful build of the same pkgbase (`BuildState.record` pops the matching failure), so the list stays a live view of *currently-broken* packages.
- **`state forget PKG…`** — deletes the named packages' `build_state.toml` records so `sysforge update` stops maintaining them (the "hand it back to pacman" escape hatch for the durable-by-default tracking model — see §Package Manifest). A name matching a pkgbase forgets every split-package member sharing it (`BuildState.delete` per pkgname). The *installed* artifact is untouched and still carries the `sf-build` pacman group, so `pacman -Syu` won't replace it; reverting fully to the stock repo binary is a separate `pacman -S <pkg>`. Rewrites `build_state.toml`, so it takes the sentinel like `state repair`. (Uninstalling a package already auto-stops tracking via `sync_with_installed`'s prune; `forget` covers the keep-installed-but-unmanage case.)
- **Pagination.** `state list`, `state orphans`, and `state failed` pipe their output through `$PAGER` (or `less -RFX` / `more` as fallbacks) when stdout is a TTY. `--no-pager` disables. The pager wrapper `_maybe_pager(use_pager)` lives in `state_cmd.py` and degrades gracefully when no pager binary is available.

Public API: `cmd_state_list(args)`, `cmd_state_repair(args)`, `cmd_state_orphans(args)`, `cmd_state_failed(args)`, `cmd_state_forget(args)`. All except `orphans` accept `--state-dir`; `state orphans` reads PKGDEST from the layered system makepkg.conf via `pacman.get_pkgdest()`. `list`, `orphans`, and `failed` accept `--no-pager`; `failed` also accepts `--clear`/`--clear-all`; `forget` takes one or more `PKG` positionals.

## Primitives Layer

All modules independently testable. ~1280 pytest tests (`make test` from repo root).

### `log.py`

Structured logging module. Output goes to stderr (verbosity-gated) and optionally to log files (always full verbosity). File handles are module-level globals managed by `open_unified_log`/`close_unified_log` and `open_pkg_log`/`close_pkg_log`. All file write errors are silently swallowed so file I/O can never interrupt a build.

```
[SYSFORGE][LEVEL][TAG] message
```

Four levels: `error` (always shown), `warn` (`-v`), `info` (`-vv`), `debug` (`-vvv`). Set once at CLI entry with `log.set_verbosity(args.verbose)`.

Modules obtain a bound `Logger` instance via `log.get_logger("TAG")`, which stores the tag and exposes the same `ui`/`error`/`warn`/`info`/`debug`/`newline`/`prompt_prefix` interface as the module-level functions. Modules with multiple logging subsystems (e.g. `makepkg_wrapper.py`, `profile.py`, `aur.py`) create multiple named loggers at module level (`_conf_log`, `_build_log`, etc.). Module-level helpers (`open_unified_log`, `close_unified_log`, `open_pkg_log`, `close_pkg_log`, `set_verbosity`, `set_dry_run_mode`) are called directly on the `log` module.

**ANSI colour.** The LEVEL token is coloured by severity (bold red for `ERROR`, yellow for `WARN`, dim for `DEBUG`; `INFO` stays plain) and the TAG is cyan. Colour is applied only when the output stream is a TTY and the `NO_COLOR` environment variable is unset — so `sysforge … | cat`, redirections to files, and CI logs stay plain automatically. File logs (`sysforge.log`, `sysforge_<pkg>.log`) are never coloured regardless of terminal state. `ui/progress.py` consults the same `NO_COLOR` / TTY signals before engaging its scroll-region renderer.

**Viewing logs.** `sysforge log` (no args) pages the unified log at `<state_dir>/sysforge.log`; `sysforge log <pkg>` pages the per-package log at `<pkgbuild_src_dir>/<pkg>/sysforge_<pkg>.log`. Both pipe through `$PAGER` (default `less -RFX`) via the shared `primitives/pager.py:maybe_pager` context manager (also used by `state list` and `state orphans`). Missing files surface as a non-zero exit with the searched path — no AUR clone fallback, so a typo never causes a network operation. Tab-completion uses `sysforge completions local` (dirs under `pkgbuild_src_dir` containing a PKGBUILD).

### `ui/progress.py`

Bottom-anchored status line for batch operations (`[3/10] building htop`). Dual-mode renderer picked once at `progress.init()` (called from `cli.main` right after `log.set_verbosity`):

- **TTY mode** — DECSTBM scroll region (`ESC[1;N-1r`) reserves the last row; output scrolls above it and the bar stays pinned to row N **permanently — including during a subprocess build**. `SIGWINCH` is wired to re-establish the region and redraw the last status on resize. An `atexit` hook releases the region on interpreter shutdown. **A subprocess that does its own full-screen cursor addressing (`makepkg` → cargo/ninja/cmake live progress) is sized one row shorter than the terminal** — `pty_runner.run_with_pty(..., reserve_bottom_rows=progress.reserved_rows())` sets the child's pty winsize to `N - 1`, so the child confines its redraws/scrolling to the region above the bar and never touches row N (the standard status-line technique: tell the child the screen is one row shorter). Byte-forwarding is unchanged, so the child's live `\r` animation is preserved. This is why the bar survives a build instead of the child collapsing its output onto the bar row. (`progress.suspended()` — full release + restore — remains for a different case: a subprocess that takes the *real* terminal with inherited stdio and prompts on it, e.g. sudo/gpg, where the bar should yield entirely.) **Every region mutation is cursor-transparent** — setting and resetting the scroll region both home the cursor as a VT100 side effect, so `_establish_region`/`_release_region`/`_paint` each bracket their writes with DECSC/DECRC (`ESC7`/`ESC8`) and never park the logical cursor at the screen bottom. Without this, reserving the last row in a fresh shell (content at the top, empty rows below) drags the cursor to the bottom and strands the empty gap as scrollback blank lines once the region is released — visible on instant verbs since the universal startup `phase()` reserves the region for every command. **Establishing the region uses "scroll up one to reserve"** so the cursor ends up *inside* `[1, N-1]` regardless of where the shell prompt started: a DECSTBM region only scrolls for a cursor within its margins, so if the prompt began at the bottom of the screen the post-Enter cursor sits at row N (below the region) and output would pile onto the bottom row instead of scrolling. `_establish_region` therefore saves the cursor, jumps to the absolute bottom row, emits one index (`ESC D` — scrolls the whole screen up one, freeing row N for the bar), sets the region, restores the cursor and moves it up one line to undo the scroll — landing it back on its content, now inside the region. This fixes both the bottom-of-screen collapse and the fresh-shell gap with one sequence.
- **Plain mode** — selected when any of the following is true: `sys.stderr` is not a TTY, `_DRY_RUN`, `TERM=dumb`, `TERM=""`, `CI` set, or `NO_COLOR` set. Emits `[PROGRESS] [i/n] label` through `log.ui()` so the same data reaches logs and pipes without ANSI garbage.

Public API: `init()`, `shutdown()`, `render(current, total, label)`, `phase(label)`, `clear()`, `suspend_for_prompt()`, `suspended()` (a context manager that fully releases the region for an inherited-stdio interactive subprocess — sudo/gpg — and restores the bar after), `reserved_rows()` (0/1 — the row count a pty child must subtract from the terminal height so it stays above the bar), and a `tracker(total, prefix)` context manager that yields a `tick(label)` callable. The bar must be made safe before any `input()` prompt so the prompt doesn't collide with the reserved status row — this is handled **centrally** by the `primitives/prompt.py` helpers (`prompt_text` / `prompt_choice` / `prompt_key` each call `progress.suspend_for_prompt()` before reading input), so individual call sites no longer need to manage it. `suspend_for_prompt()` **blanks the bar line in place but keeps the scroll region** (the same `_paint`-style save → goto-bottom → clear → restore, which leaves the logical cursor untouched), so the prompt prints in the normal content flow where it is visible; the next `render()`/`phase()`/tick repaints the bar. This is deliberately *not* `clear()`: a full release resets the DECSTBM region (`ESC[r`), and the cursor-restore across that reset is unreliable on some terminals — it left prompts rendering off-view. (Code that runs an interactive *subprocess* with inherited stdio — rather than a `prompt.py` helper — still fully `clear()`s, since that path needs the region actually released.) Reservation is lazy: entering a tracker alone touches nothing — the first `tick()` call establishes the region.

`phase(label)` paints an **uncounted** phase status (`[SYSFORGE][PROGRESS] <label>`, no `[i/n]`) that persists across nested trackers — a tracker exiting while a phase is set repaints the phase instead of releasing the region, so the bottom line stays populated between counted batches. `phase(None)` clears the phase and releases the region. Plain mode emits one `[PROGRESS] <label>` line per phase *change* (repeats are deduped). This is what keeps the bottom line visible across the otherwise-blank stretches of `sysforge update` (startup, drift analysis, preflight, install).

**Universal startup coverage.** The indicator is guaranteed present for *every* command, not just `update`, via two centralized chokepoints: `verbs/runner.py::run_verb` paints a generic `<verb>: starting…` phase on dispatch (humanizing `run-toolchain` → `toolchain`) and clears it with `phase(None)` on every exit path, and `pipeline/runner.py` sets `phase(stage.name)` at each stage boundary in both `run_stage_standalone` and `run_pipeline`. Together these fill the previously-blank startup window of `run toolchain`/`run kernel`/`run packages`/the full pipeline and of read-only verbs (`doctor`, `resolve`, …). Verbs that paint their own richer phases (e.g. `update`) override the generic label seamlessly through the single shared phase state. Don't re-add a per-verb startup `phase()` — extend the chokepoints.

Integration sites: `sysforge/verbs/runner.py::run_verb` (universal `<verb>: starting…` startup phase + clear), `sysforge/pipeline/runner.py` (`phase(stage.name)` per stage in `run_stage_standalone` / `run_pipeline`), `sysforge/pipeline/stages/packages.py` (build loop), `sysforge/primitives/aur_resolve.py::build_resolved_deps` (AUR deps), `sysforge/update.py` (phase statuses for init/assembly/drift/preflight + trackers for source sync and the threaded version check), `sysforge/build_core.py` (phase statuses for source pre-sync, dep prep, and install + the build-loop tracker), `sysforge/fetch.py` (fetch loop). Interactive-prompt call sites in `pipeline/stages/packages.py`, `primitives/makepkg_wrapper.py`, and `build_core`'s review-gate block each call `progress.clear()` before prompting.

### `prompt.py`

Single shared interactive-prompt helper. Every stage that needs user input goes through `prompt.prompt_text()` (free-form), `prompt.prompt_choice()` (fixed-set), or `prompt.prompt_key()` (single keypress), so the behaviour on empty input, unrecognized input, and EOF is consistent across the codebase. `is_interactive()` wraps `sys.stdin.isatty()` for stages that need to gate prompts on a TTY.

Key contract:

- Every helper calls `progress.suspend_for_prompt()` before reading input, blanking the bottom-anchored bar so a prompt never collides with stale status text. This is the single home for that guarantee — call sites no longer manage the bar by hand before prompting (the forgotten clear at the toolchain confirm/preflight/PGO prompts was exactly this footgun). `suspend_for_prompt()` keeps the scroll region and leaves the cursor in place, so the prompt prints visibly in the content flow; the bar repaints on the next `render()`/`phase()`/tick. (It is *not* `clear()` — a full region reset's cursor-restore is unreliable on some terminals and rendered prompts off-view.)
- `prompt_choice` re-prompts with a visible warning on unrecognized input (so typos / jibberish never silently fall through to the default), unless the call site passes `retry_on_invalid=False` — used for destructive / mutate-confirm prompts where any non-confirming input must abort (partition.py's literal-`yes` confirmation; the `[y/N]` confirms in `state orphans --prune`, `doctor --apply` rebuild, and `auto_repair`'s checksum-mismatch `updpkgsums` retry). These three previously used a bare `input()` with a hand-rolled `EOFError` guard; routing them through `prompt_choice` also picks up the captured-stdin `OSError`-as-EOF handling for free.
- `eof_default` is a separate kwarg from `default`. Most sites pass neither (EOF returns `default`). `toolchain.py`'s GCC-build override and `_confirm_or_abort` deliberately set `eof_default="y"` to preserve their long-standing "EOF means proceed unattended" semantic.
- Both helpers catch `EOFError` *and* `OSError`, since pytest's captured stdin and other unreadable-stdin scenarios raise the latter.
- `prompt_key` reads one character in cbreak mode (`termios`/`tty`, restored in a `finally`) and echoes it plus a newline so the transcript still shows the answer — no Enter required. It degrades to line-based `input()` (first character of the stripped line) when stdin is not a TTY, `termios` is unavailable, or raw-mode setup fails, so tests and pipes keep working. Ctrl-C raises `KeyboardInterrupt` (cbreak delivers it as `\x03`); Ctrl-D/EOF/unreadable stdin raise `EOFError`; a bare Enter returns `""` ("no answer", distinct from EOF) so callers can re-prompt. Validation and re-prompt loops stay in the caller, matching `prompt_choice`'s contract. Used by the PKGBUILD review gate.
- Optional `tag`/`level` kwargs reuse `log.prompt_prefix(level, tag)` so prompts keep the standard `[SYSFORGE][LEVEL][TAG] ` format.

Call sites: `pipeline/stages/reconfigure.py` (11), `pipeline/stages/packages.py` (`_prompt_failed_packages`), `pipeline/stages/toolchain.py` (3), `pipeline/stages/partition.py` (1), `setup_cmd.py` (1), `primitives/makepkg_wrapper.py` (4). No stage may call `input()` directly.

### `paths.py`

Pure constants module — the canonical directory of every config file sysforge reads. `CONFIG_BASE` is derived from `$SYSFORGE_CONFIG_DIR` (default `/`, so system config resolves under `<base>/etc/sysforge/`, including `BOOTSTRAP_PATH`). User-side roots follow the XDG Base Directory Specification via the `_xdg_base(env, default)` helper: `USER_CONFIG_DIR` (`$XDG_CONFIG_HOME/sysforge`), `USER_CACHE_DIR` (`$XDG_CACHE_HOME/sysforge`), `USER_STATE_DIR` (`$XDG_STATE_HOME/sysforge`). The resolved path lists (`CONFIG_PATHS`, `CONFLICT_GROUP_PATHS`, `CONSUMES_INFERENCE_PATHS`) layer the user file (`$XDG_CONFIG_HOME/sysforge/…`) over the system file in `extends_system` order. Helpers: `resolve_packages_path(config)` returns the `packages.toml` path the rest of the codebase should use (honouring `--packages` overrides); `migrate_legacy_user_dirs()` does a one-shot best-effort move of the legacy consolidated `~/.config/sysforge/{cache,state}` dirs to their XDG homes. No I/O beyond the migration helper — just path strings.

### `config.py`

TOML config loading and path resolution. Public API:
- `load_config(config_paths=None)` — loads `profiles.toml`, merges user onto system via `extends_system`, validates rule priorities
- `load_conflict_groups(paths=None)` — extracts the `[append_conflict_groups]` table from `profiles.toml`
- `load_consumes_inference(paths=None)` — extracts the `[consumes_inference]` table from `profiles.toml`
- `find_pkgbuild(pkg, config=None)` — resolves a bare package name, directory path, or PKGBUILD path to an absolute PKGBUILD path. Search order: (1) direct path or directory (resolves `dir/PKGBUILD`), (2) `<cwd>/<name>/PKGBUILD`, (3) `<config [paths] pkgbuild_src_dir>/<name>/PKGBUILD`, (4) auto-clone if not found locally — repo packages via `pkgctl repo clone --protocol=https`, AUR packages are routed through `get_scheduler().request(SyncRequest(...))` so the clone is deduplicated with any concurrent update/fetch request and shares the same rate-limit budget. Used by `sysforge build`, `sysforge resolve`, and the packages stage.

`[paths] pkgbuild_src_dir` in `profiles.toml` is the user-configured root for local PKGBUILDs (`~/src` by default). Auto-clone also targets this directory.
- `resolve_pkgbuild_src_dir(config, build_cfg=None)` — the one home for the dual-key resolution: packages.toml `[build] pkgbuild_src_dir` wins over profiles.toml `[paths] pkgbuild_src_dir`. When both are set and point at different directories it warns once per run naming both values (the keys are allowed to differ, but a silent mismatch means builds and updates could read PKGBUILDs from different trees). Consumed by `update_assemble` and the packages stage's `_resolve_pkgbuild`; single-key readers (`build_cmd`, `log_cmd`, `completions_cmd`) read `[paths]` directly and don't need it.
- `parse_system_makepkg_conf(path=None)` — parses `/etc/makepkg.conf` into `{key: raw_value_string}` for use in temp conf generation. Handles backslash line continuation (e.g. `CFLAGS="... \\\n  -flag"`) and multiline bash array values (e.g. `VCSCLIENTS=(...)` spanning multiple lines) by tracking paren depth across lines. Merges user conf (`$XDG_CONFIG_HOME/pacman/makepkg.conf`, `~/.makepkg.conf`) on top of system conf.
- `set_makepkg_conf_keys(path, mapping, dest=None)` — the one home for *writing* makepkg.conf keys. Replaces an existing active `KEY=...` assignment in place, else uncomments a `#KEY=...` line, else appends; all other lines verbatim. Values are written quoted. `dest` defaults to `path` (in-place); a separate `dest` lets a caller read a root-owned `/etc/makepkg.conf` and stage the rewrite to a user-writable temp file for a later `sudo cp` (the reconfigure path). The configure stage's unattended `[makepkg]` write and reconfigure's interactive PACKAGER/MAKEFLAGS offer both route through it — don't hand-roll a second makepkg.conf writer. Pure transform exposed as `_rewrite_makepkg_conf_text(text, mapping)` for testing.

### `env_chain.py`

Snapshots and validates the runtime environment chain sysforge inherits, so any command can answer "what env do I see, where did it come from, and which sources disagree?" without manual `env`/`set` archaeology. Public API:

- `collect_env_chain()` — returns an `EnvChainSnapshot` carrying grouped runtime vars (sysforge/toolchain/makepkg/python/desktop/shell), the parent process chain walked via `/proc`, a shell-init-file presence map, a flat `sources` dict (one entry per parseable contributor), and the collection cost in ms.
- `compute_divergences(snap)` — returns `{var: {source_name: value}}` for every var whose declared values disagree across sources, or where a source declares a value the runtime doesn't carry.
- `format_env_chain(snap, *, verbosity=0)` — renders the snapshot as human-readable text. Always appends a `mismatches:` block. At `verbosity >= 2` (`-vv`) each grouped var carries an inline `[differs from …]` annotation alongside its value.
- `validate_env_chain(snap)` — non-fatal warnings (e.g. `SYSFORGE_STATE_DIR` unset, no `VIRTUAL_ENV`).
- `log_env_chain(level="debug")` — collect + format + emit via `log.get_logger("ENV")`. Called from `cli.main()` at every command's startup so the unified log always captures the env chain.

**Sources read** (parsed once per invocation):

| Source name        | Origin                                                 |
|--------------------|--------------------------------------------------------|
| `runtime`          | `os.environ` — the inherited view                      |
| `etc_environment`  | `/etc/environment` — bare `KEY=value` lines            |
| `pam_env_default`  | `/etc/security/pam_env.conf` DEFAULT field             |
| `pam_env_override` | `/etc/security/pam_env.conf` OVERRIDE field            |
| `system_zshenv` / `user_zshenv`     | `/etc/zsh/zshenv`, `~/.zshenv`        |
| `system_zprofile` / `user_zprofile` | `/etc/zsh/zprofile`, `~/.zprofile`    |
| `etc_profile` / `user_profile`      | `/etc/profile`, `~/.profile`          |
| `system_zshrc` / `user_zshrc`       | `/etc/zsh/zshrc`, `~/.zshrc`          |
| `system_zlogin` / `user_zlogin`     | `/etc/zsh/zlogin`, `~/.zlogin`        |
| `systemd_user`     | `systemctl --user show-environment` (skipped without `XDG_RUNTIME_DIR`) |
| `sysforge_config`  | resolved `[defaults]` profile from `profiles.toml`, flattened via `profile.merge_extends` + `profile.serialize_flags` |

Init-file parsing is regex-based (`export KEY=value`, `KEY=value; export KEY`, plus bare assignments only for `/etc/environment` and `pam_env`). Values containing `$(`, backtick, or `${VAR}` are tagged `<expansion: …>` and counted in `parse_caveats` per source — we do not source files in a subshell because `direnv` / ssh-agent loaders / similar would execute as side effects of every sysforge command.

**Cost.** Collection runs on every sysforge invocation, not only `-vvv` — verbosity gates console output, not collection. Total budget is ~35–75ms in a typical user session, dominated by the `systemctl --user show-environment` subprocess (~25–60ms); the rest is file reads + TOML decode + flag serialization. When `XDG_RUNTIME_DIR` is unset (cron, CI, non-graphical SSH), the subprocess is skipped entirely and the budget drops to ~10ms. `snap.cost_ms` is rendered on the final line of `format_env_chain` for ongoing measurement.

`sysforge env` is the explicit user-facing verb (prints the formatted snapshot at the active verbosity); the startup hook in `cli.main()` ensures the same snapshot lands in every unified log even when the user runs an unrelated verb. `toolchain.toml` is *not* a source — it declares LLVM/PGO stages, not env vars, so including it would invent semantics that aren't there.

### `pacman.py`

All pacman and batch-install shared operations. Public API:
- `get_pkgdest()` / `get_builddir()` / `get_srcdest()` / `get_logdest()` — resolve the corresponding makepkg path variable via the shared `_resolve_makepkg_path(key)` helper, which mirrors makepkg's own precedence: **environment first** (`os.environ`), then the layered `parse_system_makepkg_conf()` (`/etc/makepkg.conf` → user conf), quotes stripped and `~`/`$VARS` expanded, else `None`. These are the single home for "where did makepkg put / read this?" — any new code that needs to *locate* a built artifact, build tree, downloaded source, or build log must call them rather than assuming `~/builds` / the PKGBUILD dir / a hardcoded default (a recurring bug class). `BUILDDIR` resolution in particular feeds `makepkg_env._effective_build_dir` (side-car log diagnosis) and `kernel._resolve_built_config` (resolved `.config` discovery), both of which must honour a `BUILDDIR` set only in `/etc/makepkg.conf`. `PKGDEST` resolution feeds `makepkg_wrapper._find_artifacts` (the union-of-`PKGDEST`-and-PKGBUILD-dir locator behind `install_built_packages` + the post-build ABI report) and `build_core`'s artifact snapshot — so a non-default `PKGDEST` doesn't strand the install/ABI scan looking in the PKGBUILD dir. `install_built_packages` wraps that locator in `_artifacts_for_pkgbuild`, which scopes the union down to the PKGBUILD's own pkgnames (filename-matched via `_parse_built_pkg_filename`) so a shared `PKGDEST` doesn't feed every previously-built package into `pacman -U`; it falls back to the full union when the PKGBUILD yields no parseable pkgnames.
- `snapshot_pkg_dir(pkgdest)` — records the set of `.pkg.tar.*` files currently in pkgdest before a build
- `batch_install_pkgs(pkgdest, pre_snapshot, ...)` — diffs the post-build pkgdest against the snapshot and installs all new packages in a single `sudo pacman -U`
- `read_pkgname_from_file(path)` — extracts `pkgname` from a built `.pkg.tar.*` via `bsdtar -xOqf <path> .PKGINFO`; returns `None` on failure
- `filter_pkgs_to_installed(paths, installed)` — partitions pkg-file paths into `(keep, dropped)` by whether their `pkgname` is in the current installed set; used by `update` so split-pkgbase rebuilds don't add sub-packages the user never installed
- `collect_makedeps(paths)` (makedepends only) / `collect_builddeps(paths)` (`depends` + `makedepends` + `checkdepends`) / `filter_missing_deps(deps)` / `batch_install_makedeps(deps)` — build-dependency helpers. `prepare_deps`' repo arm uses `collect_builddeps`: the `-s`-stripped batch build needs runtime `depends` present too, not only makedepends. Both collectors share `_collect_dep_names`, which version-strips and skips un-evaluated shell tokens (`_looks_unresolved`)
- `get_installed_version(name)` — `pacman -Q <name>`; returns version string or `None`
- `get_all_installed_packages()` — `pacman -Q`; returns `{name: version}`
- `get_foreign_packages()` — `pacman -Qm`; returns names not from any sync DB
- `get_pacman_sync_version(name)` — `pacman -Si <name>`; returns version from sync DB or `None`

The five read-only queries above (`get_installed_version`, `get_all_installed_packages`, `get_foreign_packages`, `get_pacman_sync_version`, `filter_missing_deps`) check for an importable `pyalpm` and route through libalpm bindings when available — direct local-DB and sync-DB access is faster than spawning a `pacman` subprocess per call. The fallback path is the original subprocess shell-out, so installs without `pyalpm` are unaffected. `pyalpm` is shipped as `[project.optional-dependencies] extra` (`uv sync --extra extra`) or installed via the system package. `SYSFORGE_PACMAN_NO_PYALPM=1` forces the subprocess path even when pyalpm is present (used by `tests/conftest.py` so existing subprocess-mocking tests keep driving the query). Mutating paths (`pacman -U`, `pacman -S --needed`) and the `pacman -Fq` files-DB lookup in `provides_lookup.py` remain subprocess-based.

Constants: `BATCH_STRIP_FLAGS` (flags removed from per-build makepkg calls during batch install — composed as `SYNC_FLAGS | INSTALL_FLAGS`, the two flag families defined in `makepkg_flags.py`), `BATCH_EXTRA_FLAGS`. `SYNC_FLAGS` (`{--syncdeps, -s}`) is the single source of truth for the dep-sync strip — reused by the toolchain stage's staged-deps passes (§`run toolchain` Dep resolution) so the two sites that suppress makepkg's `pacman -S` share one name. Both `INSTALL_FLAGS` and `SYNC_FLAGS` live in `makepkg_flags.py` (their natural home — flag-family constants); `pacman`/`toolchain` import them from there, and `makepkg_wrapper` re-exports `INSTALL_FLAGS` for back-compat.

**Pacman PostTransaction hooks.** Three libalpm hooks under `/usr/share/libalpm/hooks/sysforge-{kernel,toolchain,buildstate}.hook` invoke `/usr/lib/sysforge/pacman-hook-helper.sh` to drop a sentinel into `/var/lib/sysforge/sentinels/`. Targets: kernel hook fires on `linux*` (inclusive of `linux-firmware` and `linux-headers`); toolchain hook fires on `llvm*`, `clang`, `lld`, `compiler-rt`, `gcc`, `gcc-libs` and the lib32 variants; buildstate hook fires on `*`. The helper is failsafe — every error path exits 0 so it cannot break a pacman transaction. `cmd_update` calls `_consume_pacman_hook_sentinels()` at entry: kernel/toolchain sentinels become `_log.warn` reminders, then unlink; buildstate is unlinked silently because the existing `BuildState.sync_with_installed()` already runs. The sentinel directory is created via tmpfiles.d and pre-created during the bootstrap configure stage; the consumer skips silently when the directory is absent so older installs that predate the hooks still work.

### `profile.py`

Profile resolution and rule matching. Public API:
- `merge_extends` — resolves the full `extends` chain into a flat profile dict, applying `[profiles.x.append]` token-level merges with conflict groups
- `match_rules` — evaluates all match fields against a parsed PKGBUILD. `pkgnames` rules match against both `pkgname` and `pkgbase` — split packages (e.g. kernels) set `pkgbase` to the canonical name and `pkgname` to an array of unexpanded sub-package names; matching on `pkgbase` ensures rules like `pkgnames = ["linux-custom"]` work correctly.
- `resolve_profile` — selects the winning rule by priority; optionally injects `pkgbuild_extracted` as the chain root
- `resolve_groups` — accumulates package groups from PKGBUILD, defaults, and all matched rules
- `resolve_consumes` — determines which conf types the build needs
- `serialize_flags(profile)` — serializes a resolved profile to a newline-separated `KEY=value` string for storage in `build_state.toml`
- `get_build_mode(profile)` — extracts the `build_mode` string from a resolved profile

Public constants: `CONF_KEY_MAP` (maps conf delivery channel → set of profile keys), `SYSFORGE_KEYS` (internal keys never written to any conf file), `KERNEL_CLEAN_KEYS` (flag keys stripped from makepkg.conf for kernel builds).

### `pkgbuild_meta.py`

Static parser for PKGBUILD metadata. Does **not** source or execute the PKGBUILD.

**Reliably parseable:** `pkgname`, `pkgver`, `pkgrel`, `epoch`, `groups`, `depends`, `makedepends`, `provides`, and all standard scalar/array globals. Function bodies extracted and stored under `functions`.

**Not statically parseable:** computed values (`pkgver=$(...)`, conditional metadata, `depends+=()` inside functions).

**Implementation notes:**
- Comment stripping respects quoting
- Function extraction uses brace-depth tracking; `${}` expansions skipped to prevent brace confusion
- Functions matched only at line boundaries; names support hyphens for split packages like `package_lib32-llvm`
- Known limitation: heredocs containing bare `{` or `}` will confuse the depth tracker; rare in PKGBUILDs, deferred

**Brace expansion** (`_expand_braces`, in `_parse_array_items`). Unquoted array tokens are bash-brace-expanded as they are parsed, so `makedepends=(python-{build,installer,wheel})` yields three items, not one bogus `python-{build,installer,wheel}`. Comma lists, sequence expressions (`{1..3}`, `{a..c}`), nesting, and multiple groups (cartesian product) are handled; a group with no top-level comma and no sequence stays literal (`{foo}`), as in bash. Quoted tokens are kept verbatim and `${...}` parameter expansions are skipped whole (their internal commas are never split) — variable expansion runs afterward (`_apply_var_expansion`). Without this, a brace-listed dependency reached `pacman -T`/`pacman -S` as one unresolvable name, which (combined with the repo/AUR makedep split in `build_core.prepare_deps`) is the proton-cachyos build-failure class this closes.

**Array-parameter expansion** (`_expand_array_refs` / `_apply_array_transform`). A dependency array may splice another array via bash parameter expansion — e.g. afdko's `_pydeps=(... ufonormalizer ...)` with `depends=(python "${_pydeps[@]/#/python-}")`. The static parser already captures `_pydeps` as its own array global, so this pass resolves the reference by symbol-table lookup (no shell sourcing): for any array item of the form `${name[@]<transform>}` (or `[*]`) it splices in `name`'s elements, applying the transform — `""` (verbatim), `/#/PREFIX` (prepend, the afdko case), `/%/SUFFIX` (append), and `/PAT/REPL` / `//PAT/REPL` (literal replace). An unknown array name, an unsupported transform (slices, anchored `/#PAT/`, glob metacharacters), or a non-array-ref token is **left verbatim** — the same conservatism as variable expansion below. Runs in the post-pass pipeline after `_merge_arch_arrays` (so arch-suffixed array-refs resolve too) and before `_apply_var_expansion` (so spliced items still get `$var` substitution). Without this, `${_pydeps[@]/#/python-}` survived as one bogus dependency token and `python-ufonormalizer` was dropped from the AUR dep graph, so a later `makepkg --syncdeps` aborted with "target not found"; any residual `${...}` token that this pass cannot resolve is detected downstream by `aur_resolve._looks_unresolved` (which both triggers the RPC-metadata rescue for discovery and keeps the junk token out of `pacman`/AUR queries).

**Arch-specific array merging** (`_merge_arch_arrays`). PKGBUILD(5) defines arch-suffixed variants of seven array families (`depends`, `makedepends`, `checkdepends`, `optdepends`, `provides`, `conflicts`, `replaces`) — e.g. `makedepends_x86_64=()`. The runtime appends these to the canonical array when CARCH matches; the static parser sees every variant unconditionally and merges them into the canonical key (dedup, order-preserving, plain entries first). The arch-suffixed key is retained for callers that need to read it. Without this pass, consumes inference and `match_rules` would silently miss entries declared only under an arch suffix (e.g. `lib32-rust` in `makedepends_x86_64`), so the i686 cross-probe and rust flag injection would not fire.

**Variable expansion** (`_apply_var_expansion`). After extracting globals the parser substitutes simple `$var` / `${var}` references using other scalar globals, iterating to a fixed point (bounded at 8 iterations). This handles common patterns like `_pkgname=foo; pkgname="$_pkgname-git"` and split packages written as `pkgname=("$pkgbase" "$pkgbase-headers")`. Shell parameter-expansion forms (`${var:-default}`, `${var%suffix}`, `${var#prefix}`, `${var//a/b}`) are intentionally **not** touched — the regex matches only `${name}` with a closing brace and no operators, so these expressions are preserved verbatim. Unresolved references (e.g. a `$var` whose definition lives inside a function body, not in globals) are also preserved verbatim rather than silently wiped. Without this pass, PKGBUILDs defining `pkgname` via a shell variable produced build_state entries keyed by the literal reference string (`["$_pkgname-git"]`), which made `sysforge update` silently miss those packages because `pacman -Q` knows them by their real names.

**Unresolvable `pkgver` fallback.** Some PKGBUILDs (`1password`, `openssl-1.0`, `openssl-1.1`) compute `pkgver` via bash parameter-expansion forms the static parser cannot evaluate (`pkgver=${_tarver//-/_}`, `pkgver=${_ver/[a-z]/.${_ver//[0-9.]/}}`). `format_version` then returns literal shell text, which `vercmp` sorts high against any real installed version — causing the package to be perpetually flagged `NEEDS_REBUILD`. `_check_one_pkgbase` in `update.py` detects this case (regex `[${}]` in the formatted string) and falls back to the AUR RPC `Version` cached in `source_meta.toml`, which is vercmp-ready (already `[epoch:]pkgver-pkgrel`). When no cached RPC version exists the package is skipped with a warning rather than compared against gibberish.

### `pkgbuild_patcher.py`

All PKGBUILD mutation. Active when `build_mode = "patched_pkgbuild"` or `"kernel"` on the resolved profile.

**Flag extraction** (`extract_pkgbuild_profile`) scans all function bodies and extracts bare, `export`, and `+=` assignments to known flag variables. Strips self-references (`$CFLAGS` in CFLAGS), skips complex bash expressions (e.g. `${CFLAGS/-g /-g1 }`), expands packed `-Wl,a,b,c` tokens into individual sub-tokens. Returns a synthetic profile dict used as the implicit chain root in `merge_extends` — forming the chain: `pkgbuild_extracted → bare → standard → optimized`.

**Conditional block handling** (`_extract_conditional_blocks`) finds `if...fi` blocks containing extractable key assignments using depth-tracked scanning. Entire blocks are removed from the patched PKGBUILD, never partially.

**Patching** (`apply_patch_pkgbuild`) writes `PKGBUILD.sysforge` with all managed flag assignments and conditional blocks removed. The original is untouched. Artifacts persist on build failure for diagnosis; `cleanup_patch_artifacts` removes them on success. On failure, the warning only mentions `pkgbuild_extracted_profile.toml` if it was actually written (non-empty extraction).

Inline `make VAR=val` and `cmake -DKEY=val` lines are only removed when the key is in `_EXTRACTABLE_KEYS` — keys that sysforge manages. This prevents accidental removal of kernel build commands like `make LOCALVERSION=...` or `make INSTALL_MOD_PATH=...` which are real build invocations, not flag assignments.

**Noninteractive kconfig patching** (`patch_noninteractive_kconfig`) replaces interactive kconfig targets (`oldconfig`, `nconfig`, `menuconfig`, `xconfig`, `gconfig`) with `make olddefconfig` in an already-patched PKGBUILD file. Called by the kernel stage after normal patching; modifies `PKGBUILD.sysforge` in place. Preserves `VAR=val` arguments before the target and trailing comments.

**Subshell toolchain env reset** (`patch_subshell_env_reset`) injects `unset CC CXX LD` at the top of every subshell function body (`funcname() (...)`) in `PKGBUILD.sysforge`. Subshell functions are isolated helper builds (musl bootstrap, embedded grub, wimboot, etc.) that should use the system-default compiler and linker, not the sysforge profile toolchain or inherited shell overrides. Without this, `CC=clang` from the profile and `LD=ld.lld` from the shell env leak into sub-builds that expect gcc/ld.bfd and produce broken toolchain wrappers or linker script failures. Considers two sources: profile toolchain keys (CC, CXX) and inherited shell env (CC, CXX, LD). Only injects when at least one key differs from the system default (gcc/g++/ld). Called from `_run_build` after PKGBUILD.sysforge is created, on all build paths (both patched and group-only).

**LLVM target filtering** (`patch_llvm_targets` + `is_llvm_pkgbase`) injects `-DLLVM_TARGETS_TO_BUILD="<list>"` into the cmake invocation of LLVM-toolchain PKGBUILDs (`llvm`, `clang`, `compiler-rt`, `lld` — gated by `is_llvm_pkgbase` on `pkgbase`). **`lib32-*` LLVM packages are exempted** at the `makepkg_wrapper._maybe_patch_llvm_targets` call site: they ship no headers of their own and compile against the all-target 64-bit `/usr/include/llvm` headers, so reducing their target set strands lib32-clang's offload tools (`clang-nvlink-wrapper`/`clang-sycl-linker`) on `InitializeAllTargets()` symbols the reduced lib32 libLLVM doesn't export (`ld.lld: undefined symbol: LLVMInitialize…AsmParser`). They always build the full set, matching the headers. The patcher is invoked at the end of `apply_patch_pkgbuild`. The target list is resolved by `primitives/llvm_targets.resolve_llvm_targets` in this order: `[llvm] targets` in `toolchain.toml` (explicit override; `targets = []` disables filtering) → `[hardware] llvm_targets` in `hardware_profile.toml` (autodetected from `uname -m` + `gpu_vendors`) → `None` (no filtering, build all targets). Idempotent: re-running on a PKGBUILD already carrying the same value is a no-op; replacing an existing `-DLLVM_TARGETS_TO_BUILD=` arg preserves the upstream PKGBUILD style. On a no-cmake-found PKGBUILD (upstream switched to meson), logs a warn and leaves the file unchanged. The `LLVM_EXPERIMENTAL_TARGETS_TO_BUILD` flag is intentionally untouched.

### `llvm_state.py`

Sole entry point for inspecting LLVM-toolchain source trees before a command touches them. Surfaces, per LLVM pkgbase in scope: variant, source origin (`repo` / `aur` / `user` / `missing`), dirty state with reason, divergence vs upstream (cheap path: compare HEAD against `SourceMetaCache.head_commit`; opt-in `probe_fetch=True` runs `git_fetch_and_compare`), pacman install origin + version, parsed PKGBUILD version, and the resolved `build_mode` from rule matching. PGO profdata mismatch is cross-checked via `makepkg_wrapper._resolve_pgo_state` for any pkgbase with `build_mode = "pgo_llvm_toolchain"`.

Public API: `is_llvm_in_scope(pkgnames)`, `collect_llvm_state(pkgnames, config, *, probe_fetch=False, offline=False)`, `render_preflight(report, *, verbose=False)`, `evaluate_strict(report, *, allow_dirty=False)`. Read-only — never clones, never mutates. PKGBUILD resolution mirrors steps 1-3 of `config.find_pkgbuild` without the auto-clone branch.

Wiring: `fetch` / `update` / `build` / `converge` render the report informationally (suppress with `--no-llvm-preflight` or `[safety] llvm_preflight = false`). `run toolchain` calls `evaluate_strict` after `_resolve_all_pkgbuilds` and refuses-by-default on dirty or diverged trees; bypass per-run with `--allow-dirty-llvm`, or actually overwrite the local trees with `--cleansrc-force` (which uses the new dirty classifier so trees in the `diverged_upstream` state — upstream force-pushed, no local commits authored by the local git user — are reported as clean and don't even need the bypass). The dirty-reason string distinguishes `"N commits ahead of upstream"` (`ahead`, real unpushed work) from `"diverged from upstream (N local / M upstream)"` (`diverged_user`, the histories have a common ancestor but the local user authored at least one commit on the local side). PGO profdata version mismatches are never bypassable — building against stale profdata silently corrupts the output.

Reuses (do not duplicate from caller code): `pkgbuild_patcher.is_llvm_pkgbase`, `aur.git_is_dirty`, `aur.git_fetch_and_compare`, `source_meta.SourceMetaCache.get`, `pkgbuild_meta.parse_pkgbuild`, `version.format_version`, `pacman.get_foreign_packages` / `get_installed_version`, `profile.match_rules` / `get_build_mode`, `makepkg_wrapper._resolve_pgo_state`. Log tag: `[LLVM]`.

### `toolchain_preflight.py`

Batch-time toolchain availability check, runs in `cmd_update` after `to_build` is finalised and before `batch_install_makedeps` (the helper is `update._toolchain_preflight_for_batch`). For every package in the batch the helper resolves the active `consumes` set (`profile.resolve_consumes` over `parse_pkgbuild` + `match_rules` + `resolve_profile`), then reduces those plus the lib32-* subset **and the set of resolved compilers** (`resolved["CC"]`/`["CXX"]`) to a required-toolchain token set via `collect_required_toolchains`. Token grammar: `rust:native`, `rust:cross:<target>`, `rust:cross:<target>@<toolchain>`, `cmake`, `meson`, `cc:<name>`.

**Compiler-health probe (`cc:<name>`).** `_probe_cc` verifies the resolved compiler actually *runs* (`<cc> --version`) and, for clang, that the whole LLVM lockstep suite shares one pkgver (`pacman -Q`). A half-installed / mismatched LLVM toolchain — clang built against a libLLVM that no longer exports a symbol it needs, or a partial upgrade that leaves some suite members behind — makes clang fail to even start with a dynamic-link symbol error (`undefined symbol: LLVMInitialize…Target`). Without this probe that surfaces only as N separate per-package `Unknown compiler(s): [['clang']]` failures with no captured cause; with it, the batch aborts up front with a `sudo pacman -Syu …` / `sysforge run toolchain` remediation. The skew arm sweeps **every installed member** of `LLVM_LOCKSTEP_SUITE` (`llvm`, `llvm-libs`, `clang`, `lld`, `compiler-rt`, `polly`, `openmp`) comparing pkgver only (pkgrel is stripped — a packaging bump like `lld 22.1.5-3` next to `clang 22.1.5-1` is not a skew), so a stranded `compiler-rt` is caught and the suggested `pacman -Syu` lists every member that needs resyncing rather than a hardcoded four. `spirv-llvm-translator` (its own version scheme) and `lib32-*` (separate multilib lineage / epoch) are deliberately excluded. `LLVM_LOCKSTEP_SUITE` is the single source of truth, shared with the toolchain stage's `_verify_llvm_install` (`_LLVM_VERSION_MATCH_SET` imports it) so the everyday-`update` probe and the post-install verifier never diverge — the probe stays a lightweight primitives-layer check rather than importing the pipeline-layer verifier. The pinned `@<toolchain>` form is emitted when the package's PKGBUILD exports `RUSTUP_TOOLCHAIN=<name>` inside `build()` / `check()` (regex scan over the parsed function body — `lib32-gstreamer` pins `stable` this way and would otherwise be probed against the workstation default). Currently only `rust:cross:i686-unknown-linux-gnu[@...]` is emitted (any lib32-* package with `rust` in consumes); other cross targets plug in at `collect_required_toolchains` when added.

Probes are sub-second: `rust:native` is `rustc --version`, `rust:cross:<target>[@<toolchain>]` writes a `fn main(){}` to a tempdir and runs `rustc --target <target> --emit=metadata` with `RUSTUP_TOOLCHAIN=<toolchain>` overlayed on the env when a pin is present. `--emit=metadata` skips codegen/linking but still requires the std crate, which is exactly the hurdle meson's own rust sanity check fails on when the i686 target is missing from the toolchain the build will use. Without a pin the probe uses `$RUSTUP_TOOLCHAIN` or `rustup show active-toolchain` for the effective toolchain name.

Auto-remediation: failures with `auto_remediable=True` (currently `rustup target add` only) get an interactive y/n prompt via `primitives.prompt.prompt_choice`; on accept the command is executed and the failed probe is re-run. Non-interactive runs (`--non-interactive`, `--noconfirm`, or no TTY) print the fix block and exit 1 instead. Per-package profile-resolution failures are caught and warned — preflight is best-effort and never blocks a build the real wrapper could still succeed at.

Public API: `collect_required_toolchains(per_pkg, lib32_pkgs, rust_toolchain_pins=None, compilers=None)`, `run_preflight(required)`, `render_preflight(report)`, `auto_remediate(report, *, non_interactive=False)`. Wiring: `update` only, behind `--no-toolchain-preflight`. The companion `primitives.build_diag.diagnose` runs in `makepkg_wrapper.invoke_makepkg` on non-zero exit and matches known failure signatures (E0463 missing std crate, gstreamer PTP-no-rust, meson "Unknown options" → stale build dir, `cuda:host-gcc-too-new` → nvcc rejecting a system gcc newer than the CUDA toolkit supports, `toolchain:lib32-reduced-target` → a link-time `ld.lld: undefined symbol: LLVMInitialize…` against a lib32 libLLVM (matched with an `-m32` / `lib32/libLLVM` signal) — lib32-clang's offload tools referencing all-target init symbols the reduced lib32 libLLVM lacks, pointing at building lib32 with the full target set rather than a spurious version skew; `toolchain:llvm-broken` → a clang/libLLVM mismatch where clang can't run, matched on `undefined symbol: LLVMInitialize…` / `symbol lookup error: …clang` / meson's `Unknown compiler(s): [['clang']]` (the lib32 reduced-target case is matched first and suppresses this generic one)) in the captured output and any `meson-logs/meson-log.txt` **or `CMakeFiles/CMakeError.log`** under the build directory; deduped on signature, never masks the real error. The CUDA matcher reads the toolkit's `crt/host_config.h` `#if __GNUC__ > N` gate and the highest installed `/usr/bin/g++-≤N` to emit a concrete `NVCC_APPEND_FLAGS='-ccbin …'` fix. Each `FixSuggestion`'s `signature`/`fix_cmd` is also carried up the exception (`.diagnosis`) and persisted to `build_state.toml`'s `[failures]` table by `sysforge update` (see §`build_state.py`). **Interactive builds** inherit the TTY so makepkg's stdout is never captured; on failure `invoke_makepkg` instead runs `diagnose([], _effective_build_dir(...))` over the side-car logs (resolving `$BUILDDIR/<pkgbase>` when `BUILDDIR` redirects the build out-of-tree) and threads the result through the user-abort RuntimeError, so `state failed` records a real signature rather than "Aborted by user". Log tags: `[PREFLIGHT]`, `[DIAG]`.

### `dep_analysis.py`

Pre-build dependency checks. Runs before `_run_build` in `makepkg_wrapper.run()`. Two check categories:

**Soname checks** (`check_soname_deps`): filters `.so` and `.so=N` entries from `depends`, parses ldconfig -p output, and checks presence and major version. `libcap.so=2` means libcap.so.2 must be present in ldconfig's cache. Version constraint checking (pacman -Q / vercmp) was intentionally omitted — makepkg already does this and any pre-check adds false-positive risk without meaningful value.

**Makedep runtime probes** (`check_makedep_runtime`): tests makedepends with known runtime requirements beyond package installation. Currently probes: `libguestfs` (appliance boot via `guestfish add /dev/null : run` with `LIBGUESTFS_DEBUG=1`). Each probe runs with a 15-second timeout; failure or timeout triggers diagnostic parsing. For libguestfs, `_diagnose_guestfs` parses the debug output for known patterns (e.g. "waiting for root UUID") and cross-references `/proc/config.gz` to identify the exact missing kernel config options (e.g. `CONFIG_SCSI_VIRTIO=m`). Version constraints on makedepends are stripped before lookup. Packages not in `_PROBED_MAKEDEPS` are silently skipped. Not-installed packages (FileNotFoundError) are skipped.

All functions accept injectable callables for testing. Non-fatal by default; configurable via `abi_mismatch` and `makedep_probe_failed` in `[failure_handling]`.

The soname match predicate (`soname_satisfied(entry, available_set)`) is exposed at module scope so `doctor.py` can reuse the `libfoo.so` / `libfoo.so=N` matching rules without duplicating them. `soname_available(entry, ldconfig_set, *, lib32=False)` wraps it with a filesystem fallback for stale `/etc/ld.so.cache`: when the in-cache check misses, the resolved library directories (`/usr/lib`, `/usr/lib32`, plus absolute paths from `/etc/ld.so.conf.d/*.conf`) are scanned (lru-cached per process) and the same predicate is applied. `check_soname_deps` and `doctor._check_depends` both use `soname_available` so a freshly-installed package isn't reported as missing while waiting on the post-install hook to rerun `ldconfig`.

`dep_analysis.py` validates shared-library ABI for packages that are already installed. Resolving what to install in the first place is the job of `aur_resolve.py` below.

### `aur_resolve.py`

Recursive AUR dependency resolution. `makepkg --syncdeps` installs missing `depends`/`makedepends` via `pacman -S`; pacman has no AUR visibility, so any AUR-only dep that is not already installed will fail the build. `aur_resolve.py` resolves the full dep tree up front, topologically orders it, and builds the missing AUR deps before the target.

Public API:
- `resolve_aur_deps(pkgbuild_path, config) -> list[ResolvedDep]` — full recursive resolution for a single package
- `resolve_aur_deps_batch(pkgbuild_paths, config) -> list[ResolvedDep]` — batch resolution for multiple packages (de-duplicated, single topo-sorted build order)
- `build_resolved_deps(deps, ...)` — build + install the resolved list in order; shared by every call site

Resolution algorithm:
1. Parse `depends` + `makedepends` from the PKGBUILD.
2. Strip version constraints (`>=`, `<=`, `=`, `>`, `<`).
3. Filter out already-installed packages (`pacman -T`).
4. Filter out repo-satisfiable packages (`repo_packages()` batch check).
5. Query AUR for the remainder (`aur_info()` batch).
6. For each AUR dep found: fetch its PKGBUILD (`find_pkgbuild`) so the build step has a local tree, then discover *its* deps and recurse from step 1. Discovery prefers the static parse of the cloned PKGBUILD, but falls back to authoritative AUR RPC `.SRCINFO` metadata (already fully shell-evaluated) when the static parse fails or still carries an un-evaluated token (`_deps_need_rpc_rescue` / `_looks_unresolved` — `$`, backtick, `[@]`, `$(`). This is what catches deps the static parser cannot expand, e.g. afdko's `depends=("${_pydeps[@]/#/python-}")` or any command-substitution dep; without the rescue the transitive AUR dep (`python-ufonormalizer`) is dropped and `makepkg --syncdeps` later aborts on it. The build still uses the local clone, so a locally-patched PKGBUILD is honoured.
7. Deps not found in AUR or repos → warn and let makepkg fail naturally. A token that is still un-evaluated shell syntax after step 6 is skipped by the same `_looks_unresolved` guard so it never reaches `pacman`/AUR as a bogus name (the guard is also applied in `pacman._collect_dep_names` — shared by `collect_makedeps`/`collect_builddeps` — for the top-level repo arm).
8. DFS topological sort with cycle detection (error on cycles).
9. Skip packages already installed at a satisfying version (`pacman -Q`) unless `-f`/`--force` is passed.

Build execution: iterate the topo-sorted list in order. Each dep gets full profile resolution (flag profiles, PKGBUILD patching) — same as any sysforge-managed build. Each dep is installed immediately after building so subsequent deps can link against it.

Integration points:
- **`sysforge build`** — resolve before building. `--track-deps` builds resolved AUR deps in topo order before the target.
- **`run packages` stage** — resolve before building each AUR/profiled package. `--track-deps` behaves the same.
- **`sysforge update`** — resolve after `collect_builddeps()`, before `batch_install_makedeps()`. AUR deps are built and installed first, then the main batch proceeds.
- **`sysforge resolve --deps <pkg>`** — standalone dry-run inspection. Shows the full dep tree with build order, AUR vs repo classification, and which deps are already installed.

### `abi_check.py`

Post-build ABI compatibility checker. For each shared library (`.so.*`) in a built package, cross-references strong undefined versioned symbol requirements (`nm -D` `U sym@VER`) against the exported versioned symbols of its NEEDED runtime libraries (`readelf -d`) as currently resolved by `ldconfig -p`. Catches ABI breakage at build time — e.g. a library built against `libfoo` exporting `sym@@FOO_2.0` when the installed `libfoo` still only exports `sym@@FOO_1.0`.

Two-layer API:
- `check_so_files(so_paths, *, benign_sink=None) -> list[str]` — pure .so-level core. Takes any list of on-disk shared libraries and returns warning strings for unsatisfied versioned symbols and missing NEEDED sonames. Used both by the build path (through `check_package_abi`) and by `doctor.py` on installed `.so` files. `benign_sink`, when a list is passed, accumulates `"<so>: <sym>@<ver>"` entries for demoted optional-symbol cases (see *symbol-version precision* below) so the caller can render one summary line instead of per-symbol noise.
- `check_package_abi(pkg_path: Path) -> list[str]` — archive wrapper. Lists `.so.*` members with `bsdtar -t`, extracts them with `bsdtar -x` to a temp dir, calls `check_so_files`. Invoked from `makepkg_wrapper.run()` when `--abi-check` is passed to `sysforge build`.

Symbol names are demangled through `c++filt` for readability. Missing NEEDED sonames (NEEDED lib not in `ldconfig -p`) produce a distinct warning from undefined versioned symbols. Packages with no shared libraries return an empty list (no-op).

**Symbol-version precision (false-positive control).** Resolution models the dynamic linker's actual semantics rather than a naive `@@`-only union, which previously produced thousands of spurious findings on a healthy lib32 stack:
- *Non-default exports.* `_parse_nm_exports` captures exports at **both** the default form (`sym@@VER`) and the non-default form (`sym@VER`, single `@`) — the glibc back-compat pattern (`realpath@GLIBC_2.2.5`, `dlopen@GLIBC_2.1`). The linker resolves an undefined `sym@VER` against any defined `sym@VER`; the old `@@`-only export regex under-reported real exports and flagged ~889 satisfied symbols on lib32 alone.
- *Verneed binding.* `_parse_verneed` reads `.gnu.version_r` (`readelf --version-info`), mapping each required version to the NEEDED soname(s) the linker recorded. A required version bound to **no** NEEDED soname is host/loader-provided (RTLD_GLOBAL, executable-provided plugin symbols) and is **not** flagged. Verneed drives the host/loader **skip** decision and missing-lib attribution only — *satisfaction is still checked against the union of all NEEDED exports*, because glibc-family libs (`libc`/`librt`/`libpthread`/`libdl`) have merged over time and a binary's recorded Verneed soname may no longer be the actual exporter.
- *Optional LLVM target-init demotion.* When the bound lib defines the required version node but not the specific symbol, and the symbol is an optional LLVM target-registration entry point (`_is_optional_llvm_target_init`: `LLVMInitialize<Target>{Target,TargetInfo,TargetMC,AsmParser,AsmPrinter,Disassembler}@LLVM_*`), it is demoted to `benign_sink` + an info log rather than reported. A libLLVM built with a reduced `LLVM_TARGETS_TO_BUILD` (e.g. a hand-built or older `lib32-llvm` restricted to X86/NVPTX) leaves Mesa gallium drivers referencing target-init symbols for un-built backends (AMDGPU, AArch64, ARM, …); each is lazily bound and only dereferenced when that GPU target is the active driver, so the absence is benign. (sysforge itself no longer reduces lib32-llvm's target set — see pipeline-layer → *lib32 is not toolchain-managed* — but the demotion still guards reduced libs from any source.) A genuine symbol-within-version break (e.g. a C++ stdlib `_ZNSt*@LLVM_*` from a PGO toolchain leak) does **not** match the pattern and stays a hard finding — see the *Pre-install ABI hazard check* under the toolchain stage, which guards that case independently.

**Arch-aware ldconfig lookups.** The ldconfig map is keyed by `(soname, ELF class)` rather than soname alone, because `ldconfig -p` lists both 32-bit and 64-bit variants of common sonames (e.g. `libc.so.6`) and first-hit-wins would collapse them. Each `.so` under check has its ELF class determined via `readelf -h` and NEEDED references are resolved against libs of matching arch. Without this, lib32 packages produce a flood of false-positive "undefined symbol" findings because their `unsigned int`-mangled requirements don't match the 64-bit `libc`'s `unsigned long`-mangled exports.

**Shim-library allowlist.** A small set of compat shims shipped by glibc (`libnsl.so.1`, `libc_malloc_debug.so`, `libc_malloc_debug.so.0`) are skipped by `_is_shim_lib`. Their "undefined" symbols are intentional: `libnsl`'s RPC API is implemented by `libtirpc` at runtime (not declared as NEEDED), and `libc_malloc_debug` uses weak-hook override patterns. Without this filter, every `doctor` run reports ~44 findings per glibc that bury the real signal.

**Vendored-binary package skip list.** `_ABI_CHECK_SKIP_PACKAGES` (public predicate `is_abi_check_skipped_package(pkgname)`) names packages that ship prebuilt vendored binaries which will never link cleanly against current system libs (e.g. `steam` carries its own CEF runtime, libcurl, etc. under `/usr/lib/steam/`). `doctor.py` skips the ABI/linkage check for these packages and emits a one-line `[ABI] skipped: vendored prebuilt binaries` note; the depends check still runs since depends drift on these is actionable. Applies at package granularity, not soname — a floor-level noise filter for `doctor --all` / `doctor -s <metapackage>` runs whose closures include these packages.

### `provides_lookup.py`

Reverse soname → package lookup backed by `pacman -Fq`. Used by `sysforge doctor --suggest` to convert a missing/broken soname (e.g. `libavcodec.so.62`) into the repo package(s) that would supply it. Public API:

- `files_db_present()` — true when `/var/lib/pacman/sync/*.files` is synced (from `pacman -Fy`). Callers short-circuit lookup when false.
- `sync_files_db()` — runs `sudo pacman -Fy`; returns success. **Install-bearing** (touches the system) — read-only callers (`doctor`) must gate on `files_db_present()` instead and must not call this. Its one caller is the reconfigure editor-install flow, which self-heals an unsynced files db before mapping an editor binary to its package.
- `suggest_for_soname(entry, *, lib32=False, installed_names=None)` — returns candidate `repo/pkg` strings for a soname entry, honouring `lib32` context (queries `usr/lib32/<soname>` vs `usr/lib/<soname>`). When `installed_names` is supplied, candidates whose bare pkgname (the part after the optional `repo/` prefix) is in the set are dropped — the load-bearing filter that stops `doctor --suggest` from re-recommending packages the user already has installed.

Log tag: `[PROV]`. The `suggest_*` / `files_db_present` queries are pure read-only; only `sync_files_db()` mutates (the editor-install flow's explicit opt-in). `doctor --suggest` stays read-only — it emits a single `run sudo pacman -Fy` warning when the files db is absent rather than syncing.

### `failure.py`

Cross-cutting failure scenario handler. Imported by `makepkg_wrapper` and `dep_analysis` to avoid circular imports.

`handle_failure(scenario, message, config, fallback=None)` dispatches to `abort`, `error`, `warn_and_fallback`, or `fallback` based on `[failure_handling]` config. `profile_missing` and `tempfile_write_failed` always abort regardless of config.

### `resource_guard.py`

Caps the sysforge controller process's virtual address space so a runaway long-running build session (days of pipeline work) cannot balloon memory. Public API:

- `install()` — called once at CLI entry. Sets `RLIMIT_AS` to 2 GiB on the Python process.
- `lift_for_child()` — returns a `preexec_fn` that restores the address-space limit to `RLIM_INFINITY` for a child process. Used as `preexec_fn=resource_guard.lift_for_child()` on `subprocess` calls whose children legitimately need more than 2 GiB (notably `llvm-profdata merge` in the toolchain stage, which mmaps the full profraw set).

The guard is applied to the controller only — makepkg itself is launched through normal subprocess invocation and inherits whatever the shell granted, so it is not affected unless explicitly opted in via `lift_for_child`.

### `makepkg_wrapper.py`

Build execution. Public API: `run(pkgbuild_path, options: BuildOptions | None = None)` where `BuildOptions` is a dataclass with all build options defaulted. Call sites construct `BuildOptions(field=value, ...)` with only what they need; adding a new option only requires a new field in `BuildOptions` and handling in `run()` — unrelated call sites don't change.

**Decomposition (complete).** The flag-string transforms (`expand_makepkg_flags`, the `-fuse-ld=` linker detect/inject/replace, full-LTO and lld-flag strips, lib32 `-march` scrub) live in `makepkg_flags.py`, which owns the `[FLAG]` tag. The built-artifact helpers (`_find_built_packages`, `_parse_built_pkg_filename`) live in `makepkg_artifacts.py` (pure, no tag) — the canonical post-build version source consumed by `build_core`/`pacman`/`vcs_pkgver`. The PGO profdata-state resolver (`_resolve_pgo_state`, `PGOBuildSkipped`, `_try_load_toml`, `_DEFAULT_PGO_STORE`) lives in `makepkg_pgo.py` (pure state, no logger — the conf/`run` PGO narration was collapsed into `[CONF]`/`[BUILD]` in P2b.6a/6c rather than migrated here, since the orchestration is interwoven with the build flow). The subprocess-env resolver (`resolve_env_vars`, `_effective_build_dir`) lives in `makepkg_env.py`, which owns the `[ENV]` tag (the three `[ENV]` sites still inside `invoke_makepkg` move with it at the invoke split). The temp-`makepkg.conf` emission context manager (`emit_makepkg_conf`) lives in `makepkg_conf.py`, which owns `[CONF]` and is the first relocation to *drop* a tag from the orchestrator (CONF gone from `makepkg_wrapper`). Its conf-assembly narration (linker guard, GCC+LTO guard, lib32 `-march` scrub, PGO/kernel flag adjustments) emits under `[CONF]` — these are decisions this module makes while assembling a correct conf; the pure flag transforms stay in `makepkg_flags` (called as `(cleaned, stripped)` returns), so the module is single-tag (P2b.6a collapse). The orchestrator re-imports `emit_makepkg_conf` (used by `_run_build`), which also re-exports it for the direct-import test surface. The makepkg invocation + sudo-timeout retry (`invoke_makepkg`, `_invoke_with_retry`, `_build_failed_error`, and the `ToolchainMismatchError`/`AlreadyBuilt` exceptions) lives in `makepkg_invoke.py`, which owns `[MAKEPKG]` — all its narration (build status, the inherited-shell-env scrub, the toolchain-mismatch note, the retry prompts) emits under `[MAKEPKG]` as the single job of launching makepkg and classifying its outcome (P2b.6b collapse); the pure transforms/resolvers it draws on stay in `makepkg_flags`/`makepkg_env`. The orchestrator re-imports all five (used by `_run_build`) and re-exports them. After these splits `makepkg_wrapper` is the ~770-line `[BUILD]` orchestrator (`_run_build`/`run`/`BuildOptions` + `install_built_packages`/`_maybe_patch_llvm_targets`), down from ~1960, emitting a single `[BUILD]` tag. P2b.6c collapsed its residual FLAG/GIT/KERNEL/PGO narration — the GCC-mismatch retry loop, the source-sync result, the kernel `LLVM=1` injection, and the PGO multi-pass coordination are all build-orchestration decisions, so they log under `[BUILD]`; the pure concerns keep their own homes (`makepkg_flags` `[FLAG]`, kernel stage `[KERNEL]`, `aur` `[GIT]`). `INSTALL_FLAGS`/`SYNC_FLAGS` moved to `makepkg_flags.py` to break the orchestrator↔invoke import cycle. `makepkg_wrapper` re-exports the public symbols (`expand_makepkg_flags`, `_parse_built_pkg_filename`, `_resolve_pgo_state`, `PGOBuildSkipped`, …) so all import sites are unchanged. The result is a thin `[BUILD]` orchestrator over six focused single-tag/pure modules — see the module tree.

High-level flow:
1. Parse PKGBUILD via `pkgbuild_meta.py`
2. Match rules, resolve profile (injecting `pkgbuild_extracted` root if patching)
3. Resolve consumes and groups
4. Import GPG keys via `aur.import_pgp_keys` (bundled `keys/pgp/*.asc` first, keyserver fallback)
5. Run pre-build soname dep analysis
6. If `patched_pkgbuild` or `kernel` mode: extract PKGBUILD flags, write extracted profile, apply patch
7. If `kernel` mode and not `interactive`: patch interactive kconfig targets in `PKGBUILD.sysforge` to `olddefconfig`
8. If `kernel` mode: detect effective CC; if clang, inject `LLVM=1 LLVM_IAS=1` into build env
9. Emit complete temp `makepkg.conf` (merged system conf + profile overrides; kernel mode omits `CFLAGS`/`CXXFLAGS`/`LDFLAGS`/`CPPFLAGS`/`DEBUG_*` profile overrides — system conf values preserved verbatim)
10. Resolve env vars for subprocess injection
11. Invoke `makepkg -p PKGBUILD.sysforge` with temp conf and injected env

**System conf merge:** `emit_makepkg_conf` reads `/etc/makepkg.conf` as a baseline and writes a complete self-contained temp conf — system keys pass through verbatim, profile keys override their counterparts inline, new profile keys are appended. No `. /etc/makepkg.conf` sourcing at runtime.

**Flag guards in `emit_makepkg_conf`:** before writing the conf, several scrubs run so a known-broken flag never reaches makepkg:
- **Linker guard** — when the effective linker (from `-fuse-ld=` in profile-then-system `LDFLAGS`) is not `lld`, lld-only tokens (`--icf=all/safe/none`, bare or inside `-Wl,…`) are stripped from *profile-override* `LDFLAGS` via `_strip_lld_flags` so configure-time link tests against the system linker don't break.
- **GCC+LTO guard** — GCC's `.gnu.lto_*` bitcode is incompatible with lld; when the effective compiler is GCC, `-flto=thin` is rewritten to `-flto`, and if the linker is lld, LTO is disabled entirely (clear `LTOFLAGS`, strip `-flto*`, flip `lto`→`!lto` in `OPTIONS`).
- **lib32 guards** — for `is_lib32=True` builds, 64-bit-only `-march=` tokens are scrubbed from `CFLAGS`/`CXXFLAGS` and lld `--icf=*` tokens from `LDFLAGS`, at **both** the profile-override site and the system-conf passthrough. The icf scrub is **unconditional on the effective linker** (unlike the linker guard above): 32-bit identical-code-folding breaks links for some lib32 packages (e.g. `lib32-lzo`) even when lld is active. This keeps the `bare` profile (priority-30 destination for `lib32-*`, silent on these keys) from letting the system conf's host-arch flags through to an i686 build — the guard lives at conf emission, not in a per-profile rule.

**Makepkg flag passthrough:** makepkg short flags can be passed directly on the command line (`sysforge build ventoy -sfCci`) or explicitly via `-m "-sfci"`. Implicit passthrough applies to `build` and `update` — the preprocessing layer (`_extract_implicit_makepkg_flags`) rewrites bare flags into `-m` form before argparse runs. Excluded from implicit passthrough: `-h`, `-V`, `-p`, `-m`, `-D` (conflict with sysforge flags or take a value argument; `-v` is already hoisted). Combined short flags are expanded: `-sfci` → `[-s, -f, -c, -i]`.

**Subprocess stdio:** the non-interactive branch routes makepkg's stdout+stderr through `pty_runner.run_with_pty` so child tools that gate live UI on `isatty()` (cargo's "Building [n/m]" bar, configure-script spinners) still emit their progress animation. Bytes are forwarded verbatim to `sys.stdout.buffer` when sysforge itself is on a tty so the user sees the animation alongside the bottom-anchored `[SYSFORGE][PROGRESS]` indicator. The same byte stream is decoded and split on `\n` into lines for failure classification (`prepare`/`build`/`package`), missing-dep collection (`target not found:`), already-built detection, and clang→GCC toolchain-mismatch pattern matching. Every classification (and the captured lines handed to `build_diag`) runs on `pty_runner.strip_ansi`-cleaned text: because the child sees a tty, compilers embed SGR/erase/OSC-8-hyperlink escapes *inside* diagnostic tokens — GCC ≥ 16 hyperlinks the quoted option name, which split `'-flto='` mid-token and silently defeated the mismatch patterns (gpu-burn regression, 2026-06-12) — and the matching is additionally curly-quote tolerant for localized GCC output. In verbose mode (`-vvv`) or when sysforge stdout is piped (`sysforge update | tee log.txt`), byte forwarding is suppressed; only the decoded lines reach the user, keeping captured logs free of `\r`/ANSI noise. A `MAKEPKG_HEARTBEAT_S`-cadence (default 30 s) idle callback writes `[heartbeat] <latest>` entries to the per-package log when no `\n` has crossed the boundary in that window — surfacing ninja's `\r`-redrawn `[X/Y] Building ...` status so a long compile phase doesn't look hung under `-vvv` / `sysforge log <pkg>`. **On a build failure at normal verbosity**, where the live stream only reached the terminal and the log holds only heartbeats, `invoke_makepkg` persists the last `MAKEPKG_FAILURE_TAIL_LINES` (80) of captured output to the per-package log at DEBUG (file, not console) — so the real failing block (e.g. ninja's `FAILED:` lines) is diagnosable after the fact without a `-vvv` re-run. Skipped at `-vvv` (every line is already logged) and for exit 13 (already-built, not a failure). The interactive branch still uses `subprocess.Popen` with inherited stdio so unbuffered prompts (sudo, gpg signing keys, pacman conflict resolution) reach the terminal immediately.

### `pty_runner.py`

Standalone helper: spawns a subprocess attached to a pty so child tools observe a tty on stdout+stderr. Reads raw bytes from the master fd, optionally forwards them verbatim to `sys.stdout.buffer` (preserving `\r`-based progress redraws), and delivers decoded lines to a callback for parent-side pattern matching. Splits lines on `\n` only — `\r` is left in place mid-line so cargo's redraws aren't shredded into spurious "lines". An optional `idle_callback` fires every `idle_timeout_s` seconds when no full line has been delivered; it receives `buf.split("\r")[-1]` (the latest in-place redraw segment) or `None` if the child is silent, without consuming the buffer — subsequent `\n` still delivers the original inter-newline content unchanged. The read loop wakes via `select(master_fd, …, idle_timeout_s)`, so the heartbeat is idle-driven (no spin). Sizes the child's pty winsize to `terminal_height - reserve_bottom_rows` (caller passes `progress.reserved_rows()`): when the progress bar holds the bottom row, the child believes the screen is one row shorter and keeps its full-screen redraws/scrolling above the bar, so the bar stays visible throughout the build instead of the child scrolling over it. Handles SIGWINCH (re-applies the shrunk winsize, then chains to the previously installed handler so `ui/progress._on_sigwinch` continues to fire), EIO on child exit, and UTF-8 codepoints split across read boundaries (incremental decoder with `errors="replace"`). stdin is inherited from the parent so TTY-only prompts (sudo) keep working. Used by `makepkg_wrapper.py`'s non-interactive build path; reusable for any subprocess where preserving child-side ANSI animation matters. Also exports `strip_ansi(text)` — removes OSC (BEL/ST-terminated, tolerating an unterminated sequence at a chunk boundary), CSI, and two-byte C1 escapes. Any parent-side substring matching on pty-captured output must run on `strip_ansi`-cleaned text, since escape bytes can sit inside the very token being matched (GCC ≥ 16 wraps quoted option names in OSC-8 hyperlinks).

### `cache_probe.py`

Passive monitoring of ccache/sccache/ThinLTO caches. Emits the `[CACHE]` log-tag lines that bracket each `makepkg` invocation with pre/post hit-miss deltas and, once per run, the ld.so cache mtime, pacman cache file count/size, and (per-package) the ThinLTO cache dir size extracted from `--thinlto-cache-dir=` in LDFLAGS. Never enables or disables caches — policy for that lives in `[cache]` of `profiles.toml`.

Public API covers three axes:
- **Per-build stats** — snapshot ccache/sccache counters before and after a build (`ccache --print-stats --format=tab`, `sccache --show-stats`), compute the delta, log hit rate when compilations occurred, say "no compilations recorded" when delta is zero.
- **System probes** — `emit_system_probes()` for the once-per-run ld.so / pacman cache measurements.
- **Session report** — the structured `--cache-report` summary accumulates per-package deltas and prints a totals block at end of run, regardless of verbosity (the only output that bypasses `-v` gating).

Each probe is skipped cleanly if the underlying binary is absent (e.g. sccache not installed).

**ThinLTO is probed per-build, not per-run — by design.** `emit_system_probes()` (ld.so mtime, pacman cache size) runs once at the start of each pipeline or build invocation, but ThinLTO cache size requires the resolved profile's LDFLAGS — those are per-package, not available at run start — so it is probed inside `_run_build()` and appears in `[CACHE]` lines once per package that configures `--thinlto-cache-dir=`. The emission lives in `cache_probe.report_thinlto_cache(ldflags)` — the single home for the `[CACHE]` ThinLTO line, called by both `emit_system_probes()` and the build orchestrator so `makepkg_wrapper` never spells the message itself. (P2a: the `[CACHE]`/`[ABI]`/`[PATCH]` tags are owned by `cache_probe`/`abi_check`/`pkgbuild_patcher` respectively; the build orchestrator delegates via `report_thinlto_cache` / `abi_check.report_post_build_abi` / `pkgbuild_patcher.warn_artifacts_left` and no longer holds those loggers.)

### `aur.py`

AUR RPC queries, package source detection, git/pkgctl clone helpers, and GPG key import. Network-facing primitives optionally accept a `RateLimiter` (from `rate_limit.py`) so the scheduler can throttle RPC and git-fetch traffic under a single budget.

- `repo_packages(names)` — single `pacman -Si name1 name2 ...` invocation; returns the subset of names present in any sync DB. Use for batch classification (O(1) subprocesses). Parses stdout for `Name : <pkg>` lines; packages not found produce errors to stderr only.
- `is_repo_package(name)` — single-name wrapper around pacman -Si; returns `True` if found in any sync DB. Used by `find_pkgbuild` to route auto-clone: repo packages → `pkgctl_checkout`, AUR → `aur_clone`.
- `aur_info(names)` — single batch `GET https://aur.archlinux.org/rpc/v5/info?arg[]=…` for all names; returns `{name: result_dict}`. Silent on network/JSON errors (returns `{}`).
- `aur_clone(name, dest, *, ref=None, depth=None)` — `git clone https://aur.archlinux.org/<name>.git <dest>`; optional `ref` / `depth` support shallow / branch-pinned clones. Raises `RuntimeError` on failure.
- `git_fetch_and_compare(pkgbuild_dir, *, timeout=30, limiter=None)` — **full-history** fetch of the tracked upstream (`git fetch <remote> <branch>`, adding `--unshallow` when the repo is shallow) followed by a HEAD compare, then an `--ff-only` merge when HEAD is an ancestor of `FETCH_HEAD`. A `--depth=1` fetch is deliberately *not* used: it grafts the fetched tip as a parent-less root (and marks the repo shallow), which makes the `merge-base --is-ancestor` check — and the `rev-list` counts in `classify_head_vs_upstream` — see no shared history, so every routine upstream advance would falsely report `diverged`. Packaging repos carry only PKGBUILD/metadata, so a full fetch is cheap. **Non-destructive**: never runs a reset/rebase; returns a `GitFetchOutcome(status, head_before, head_after, error)` where `status ∈ {"up_to_date", "fetched", "diverged", "failed", "skipped_no_tracking"}`. Divergence (a genuine non-fast-forward — local commits or a force-push upstream) is reported, not auto-recovered here — the scheduler decides whether to reset (see `source_sync.py`). Honours the limiter's `wait_before_fetch()` / `Retry-After` budget when supplied.
- `is_transient_git_error(stderr)` / `is_rate_limit_error(stderr)` — shared stderr classifiers used by both the scheduler and legacy retry paths.
- `_classify_head_vs_upstream(pkgbuild_dir)` — single classifier consumed by both `git_is_dirty` and `llvm_state._dirty_reason`. Returns `(state, n_local, n_upstream)` where `state ∈ {"not_a_repo", "no_head", "no_tracking", "clean", "behind", "ahead", "diverged_user", "diverged_upstream"}`. The two `diverged_*` states distinguish "upstream rewrote history (force-push), no local commits authored by the local git user" (`diverged_upstream` → not dirty) from "HEAD and upstream have a common ancestor but at least one of HEAD's divergent commits is authored by the local user" (`diverged_user` → dirty). The local user identity is read from `git -C <dir> config user.email` (with global fallback). `ahead` = HEAD is a strict descendant of upstream; `behind` = HEAD is an ancestor of upstream (the only "out of date but clean" case).
- `git_is_dirty(pkgbuild_dir, *, is_vcs=False)` — wrapper over the classifier: returns `True` for `no_tracking`, `ahead`, `diverged_user` (plus uncommitted tracked changes detected separately via `git status`); returns `False` for `clean`, `behind`, `no_head`, `not_a_repo`, `diverged_upstream`. Untracked files (build artifacts) are intentionally ignored. The `diverged_upstream` exemption fixes the false-positive on workstations whose Arch packaging clones get force-pushed every release. **`is_vcs=True`** (passed for `-git`/`-svn`/`-hg`/`-bzr` packaging repos) additionally filters out makepkg's `pkgver()` churn from the uncommitted-tracked check: a PKGBUILD whose diff is restricted to `pkgver=`/`pkgrel=` lines (the auto-bump), and **any** change to the generated `.SRCINFO` (treated as a build artifact — `pkgver()` rewrites its version-pinned `depends`/`provides` lines too, so a line-level filter can't distinguish a mechanical bump from a real edit; deliberate edits live in PKGBUILD, which is still checked). Deliberate edits to other PKGBUILD lines / other tracked files still count as dirty.
- `purge_src(pkgbuild_dir, *, force=False)` — `rm -rf` the directory after a `git_is_dirty` safety check. Raises `RuntimeError` if the clone holds local work that would be destroyed; non-git directories are purged unconditionally; non-existent paths are a silent no-op. `force=True` skips the dirty check and purges unconditionally — used by the `--cleansrc-force` CLI path. Used by `sysforge build --cleansrc[/-force]`, `sysforge update --cleansrc[/-force]`, `sysforge fetch --cleansrc[/-force]`, `sysforge run toolchain --cleansrc[/-force]`, and the source-sync recovery paths.
- `pkgctl_checkout(name, dest, *, timeout=60)` — `pkgctl repo clone --protocol=https <name>` run in `dest.parent`; fetches official Arch packaging repo. Output is streamed line-by-line to `_build_log.debug` so progress is visible at `-vvv` (cloning from gitlab.archlinux.org can take minutes on a fresh checkout). Raises `RuntimeError` on failure or timeout. `find_pkgbuild` passes `[git] clone_timeout` from `sysforge.toml`; `0` disables.
- `import_pgp_keys(pkgmeta, pkgbuild_path)` — ensures all `validpgpkeys` listed in the PKGBUILD are in the GPG keyring before `makepkg` runs. Strategy: (1) import any bundled `.asc` files from `keys/pgp/` alongside the PKGBUILD, (2) check which keys are still missing via `gpg --list-keys`, (3) fetch remaining via `gpg --recv-keys`. Import failures are logged as warnings — makepkg surfaces a clearer error if a key is still absent at verification time.
- `fetch_aur_name_cache(force=False)` — downloads `https://aur.archlinux.org/packages.gz` and extracts it to `$XDG_CACHE_HOME/sysforge/aur-packages.txt` (default `~/.cache/sysforge/aur-packages.txt`). Skips the download if the cache is less than 24 hours old unless `force=True`. Called as a side effect of `sysforge update`; read by `sysforge completions packages` to provide AUR package name completion.

`sysforge completions packages` — outputs local pkgbuild_src_dir packages + pacman sync DB names + AUR cache. Used by zsh completion for `build`, `packages add`. Caps output via `grep -m N "^$PREFIX"` in the completion script to avoid rendering thousands of entries; shows `zle -M` message when limit exceeded.

`sysforge completions local` — outputs only locally-cloned packages from `pkgbuild_src_dir` (no network). Used by zsh completion for `resolve` (only packages with a local PKGBUILD can be resolved without triggering a download).

`sysforge completions manifest` — outputs only names from the active `packages.toml`. Used by zsh completion for `packages remove` (only valid to remove what's already there).

### `rate_limit.py`

Shared token-bucket rate limiter for AUR RPC calls and git fetches. One `RateLimiter` instance lives inside the `SourceSyncScheduler` singleton so every AUR-facing primitive shares a single budget.

- `RateLimiter(min_git_interval_s=0.5, default_retry_after_s=60.0)` — tracks two monotonic clocks: `not_before` (global penalty window, set when the server issues `Retry-After`) and `last_git_fetch` (used to enforce `min_git_interval_s` between consecutive fetches). `wait_before_rpc()` / `wait_before_fetch()` block until both windows have elapsed; `apply_retry_after(seconds, source=…)` extends the penalty window; `remaining_penalty_s()` returns the penalty tail so callers can abort early if it exceeds `rate_limit_abort_s`.
- `parse_retry_after(header)` — parses RFC 7231 `Retry-After` in both delta-seconds and HTTP-date forms.
- `http_get_with_rate_limit(url, limiter, *, timeout=10)` — wraps `urllib.request.urlopen`, honours the limiter, and on HTTP 429/503 raises `RateLimited(seconds)` after calling `apply_retry_after`.
- `run_throttled_git(cmd, limiter, *, timeout=None)` — runs a git subprocess under `wait_before_fetch()`; scans stderr with `RATE_LIMIT_GIT_ERRORS` (`error: 429`, `Too Many Requests`, `error: 503`, `error: 502`) and raises `RateLimited` when the remote pushed back.
- `RateLimited(seconds)` — exception carrying the `Retry-After` value; unhandled instances propagate up to the scheduler which short-circuits the remaining batch.

### `source_meta.py`

Per-package AUR RPC + git snapshot cache. Backed by `<state_dir>/source_meta.toml` (atomic write-then-rename, same pattern as `build_state.py`).

- `SourceMetaCache(state_dir)` — loads the TOML on construction (silent fallback to empty cache if schema version mismatches).
- `get(pkgbase)` / `all()` / `delete(pkgbase)` — read/remove entries.
- `update(pkgbase, *, rpc_version=None, rpc_last_modified=None, rpc_package_base=None, head_commit=None, is_vcs=None, pkgbuild_sha256=None, last_fetch_at=None)` — merges keyword fields into the entry; `None` means "leave unchanged". Writes are buffered — `save()` flushes once per process via `atexit`.
- `mark_rpc_sync(timestamp=None)` / `last_rpc_at()` — tracks the last batched `aur_info` call so the scheduler can decide whether a fresh RPC is needed.

Schema (`SCHEMA_VERSION = 1`): `rpc_version`, `rpc_last_modified` (Unix timestamp from the AUR RPC), `rpc_package_base`, `head_commit`, `is_vcs`, `pkgbuild_sha256`, `last_fetch_at` (ISO 8601 UTC).

### `source_sync.py`

Process-wide scheduler that enforces the "one RPC call, zero git fetches on steady state" rule. Replaces the old four-sub-phase source-sync block in `sysforge update`.

Public types:
- `SyncRequest(pkgbase, pkgbuild_dir, source="aur", force_fetch=False)` — input. `source` is one of `"aur" | "repo" | "git" | "local"`. `"aur"` and `"git"` follow the same code path today (AUR RPC + `aur_clone` + `git_fetch_and_compare`); `"repo"` uses `pkgctl_checkout` against gitlab.archlinux.org; `"local"` short-circuits the scheduler entirely — no RPC, no clone, no fetch — for hand-maintained PKGBUILDs with no upstream remote (e.g. the kernel stage's `linux-custom`). A local-source request whose directory doesn't exist returns `STATUS_FAILED` (operator-fixable).
- `SyncResult(pkgbase, status, head_before=None, head_after=None, error=None)` — output. Status constants: `STATUS_UP_TO_DATE`, `STATUS_FETCHED`, `STATUS_CLONED`, `STATUS_DIVERGED`, `STATUS_RATE_LIMITED`, `STATUS_FAILED`, `STATUS_SKIPPED_OFFLINE`, `STATUS_SKIPPED_NO_TRACKING`, `STATUS_SKIPPED_LOCAL`, `STATUS_PURGE_REFUSED`.

Flow per request:
1. **RPC gate.** On the first AUR-source request of a batch, fire one `aur_info([…all AUR names…])` call and cache the results in `SourceMetaCache`.
2. **Short-circuit.** If the cached `rpc_version` / `rpc_last_modified` match the local HEAD's recorded values **and** the package is not a VCS `-git` / `-svn` / `-hg` / `-bzr` (forced-fetch) type **and** `force_fetch=False`, return `STATUS_UP_TO_DATE` without touching the network. This is the common-case path.
3. **Clone.** If the dir is missing or not a git repo, dispatch via the limiter — `pkgctl_checkout` for `source="repo"` (Arch packaging repo via `gitlab.archlinux.org`), `aur_clone` for `source="aur"`/`"git"`. The repo path is never translated to `STATUS_RATE_LIMITED` (gitlab.archlinux.org doesn't enforce AUR's 429/503 budget).
4. **Fetch.** Otherwise call `git_fetch_and_compare` — full-history fetch + HEAD compare + `--ff-only` merge, never resets/rebases. Works for both AUR and repo sources because pkgctl-cloned dirs are plain git repos with a tracking branch.
5. **Divergence.** A packaging repo's job is to mirror upstream, so `STATUS_DIVERGED` is auto-resolved **when the work-tree is clean** per the VCS-aware `git_is_dirty(is_vcs=…)` check: the scheduler hard-resets to `FETCH_HEAD` (`_reset_hard_fetch_head`) and the result becomes `STATUS_FETCHED`. This applies to **all non-local sources** (AUR and repo alike) — routine upstream force-pushes/amends no longer require `--cleansrc`. Only a **dirty** tree (real operator work — uncommitted non-pkgver edits, unpushed commits, or `diverged_user`) keeps `STATUS_DIVERGED`: the work-tree is untouched, the build continues against the local PKGBUILD, and the operator decides whether to `--cleansrc` next run.
6. **Rate limit.** `RateLimited` aborts the remaining batch via `_abort_remaining`, which populates pending results with `STATUS_RATE_LIMITED` so the UI can show per-package status instead of a single global error.

Singletons:
- `get_scheduler(*, state_dir=None, offline=False, cleansrc=False, cleansrc_force=False, force_devel=False, min_fetch_interval_ms=None, rate_limit_abort_s=None, fetch_timeout=None, clone_timeout=None)` — returns the process-wide scheduler, constructing it on first call. Subsequent calls with the same args are memoised; dedup keys: `(pkgbase)` — any given pkgbase is synced at most once per process. `cleansrc_force=True` implies `cleansrc=True` and propagates to `purge_src(force=True)` so `STATUS_PURGE_REFUSED` cannot occur — the operator has explicitly opted in to overwriting local work. `force_devel` only gates the forced-fetch behaviour for VCS pkgbases that *reach* the scheduler; the higher-level filter in `update.py:_sync_sources` is what keeps VCS pkgbases out of the request batch entirely when `--devel` is off (so `--cleansrc` never purges a `-git` tree the user hasn't opted in to rebuild).
- `reset_scheduler()` — test-only hook. Tests that need fresh state call this between runs.

### `build_state.py`

Build state persistence. `/var/lib/sysforge/build_state.toml` is a **superset of `pacman -Q`** — every installed package has an entry, regardless of whether sysforge built it. The `build_mode` field distinguishes them:

- `"profiled"` — built by sysforge; carries `pkgver`, `pkgrel`, `epoch`, `pkgbase`, `pkgbuild_dir`, `flags_string` (serialized resolved compiler flags, newline-separated `KEY=value` lines), `built_at` (ISO 8601 UTC timestamp), and optionally `built_upstream_commit` (40-char SHA of the just-built upstream tree, populated only for single-git-source VCS packages — read by `sysforge update --devel` to short-circuit `pkgver()` resolution via `git ls-remote`; absent for non-VCS, multi-git-source, or any PKGBUILD whose `source=()` has unresolved bash interpolation), `source` (`"aur"` / `"repo"` / `"git"` / `"local"` — the origin classification at build time, read by `sysforge update`'s source resolver so previously-built packages keep their origin across runs instead of being re-derived from live pacman + overrides every invocation; absent for back-compat entries written before the field existed; `"local"` means a hand-maintained PKGBUILD with no upstream remote, source-sync is skipped for it), `owner_stage` (e.g. `"kernel"` or `"toolchain"` — set by a pipeline stage that owns the package's lifecycle, so `sysforge update` skips it by default and points the user at the owning stage; `--include-stage-owned` overrides the skip; both the kernel and toolchain stages stamp it, each with a config-file bootstrap fallback in `primitives/stage_ownership.py` for the pre-first-build window), and `toolchain_variant` (`"gcc"` / `"stock_llvm"` / `"pgo_llvm"` — the active toolchain identity at build time, read by `sysforge update` to detect toolchain drift; absent for back-compat entries and for builds that ran with no toolchain stage configured). `source`, `owner_stage`, and `toolchain_variant` are *sticky* — `BuildState.record()` preserves the prior value when the caller doesn't pass one, so a rebuild through a code path that doesn't know about them won't erase the provenance. Split packages (multiple `pkgname` from one `pkgbase`) each get their own entry, all pointing at the same `pkgbuild_dir`.
- `"pacman"` — installed via pacman, not built through sysforge. Carries only `pkgver`, `pkgrel`, `epoch` parsed from `pacman -Q`; `pkgbase`, `pkgbuild_dir`, and `flags_string` are absent. Synthesised by `sync_with_installed()`.
- `"pgo_llvm_toolchain"` — LLVM toolchain packages built with profdata reuse: `makepkg_wrapper` injects `-fprofile-use=<saved-profdata>` when a compatible `clang.profdata` exists, otherwise prompts plain build / skip (default skip). See **PGO toolchain packages** below.

`BuildState.sync_with_installed(installed)` keeps the file in lockstep with `pacman -Q`: it adds a pacman-mode entry for every newly installed package and prunes entries for packages that are no longer installed. The prune pass also removes zombie entries left by pre-superset parser runs — e.g. legacy keys containing literal `$_pkgname` that can never match a `pacman -Q` name. `sysforge update` calls this at the start of every run and saves if anything changed.

Read by `sysforge update` for version drift detection (every installed AUR package is iterated regardless of `build_mode`; profiled entries carry the prior `pkgver` for change-detection, pacman-mode entries are checked against PKGBUILD freshness) and for flag drift detection in Phase 4.3 (profiled entries only — including, via the build-state-wide fold, profiled entries outside the run's package walk; pacman-mode entries are silently skipped). Follows the same atomic write-then-rename pattern as `pipeline/state.py`. Records must carry `build_mode`; the previous compatibility fallback that treated missing `build_mode` as profiled was removed.

On the write path, after a successful build `makepkg_wrapper.py` derives `pkgver`/`pkgrel`/`epoch` from the produced `.pkg.tar.*` filenames rather than the static PKGBUILD parse. The static parser intentionally leaves shell parameter-expansion forms (e.g. `${_ver/[a-z]/.${_ver//[0-9.]/}}`) untouched so it never produces a misleading partial substitution, but a built package's filename always carries the fully resolved version. Falling back to filenames prevents profiled entries from storing literal `$...` strings that would mismatch every subsequent vercmp and cause the package to be flagged for rebuild on every `sysforge update` run.

**Build failures** live in a reserved top-level `[failures]` table (keyed by pkgbase), held apart from the per-package install mirror so `all_packages()` / `sync_with_installed()` stay a clean superset of `pacman -Q` (the `failures` key is popped into a private dict on load and re-serialized separately; a package literally named `failures` would collide but none exists in practice). Each entry carries `failed_at` (ISO 8601 UTC), `error` (the failure message tail — last ~6 lines / 600 chars), and optionally `pkgver`, `signature`, and `fix_cmd` (the latter two from `build_diag` postflight diagnosis when a known pattern matched). API: `record_failure(pkgbase, *, error, pkgver=None, signature=None, fix_cmd=None, failed_at=None)`, `clear_failure(pkgbase) -> bool`, `all_failures() -> {pkgbase: record}`. A successful `record()` calls `clear_failure(pkgbase)` so the failure list self-heals on the next good build. `sysforge update`'s build fan-out records failures via `_record_build_failure` (opening a fresh `BuildState` so loop-time success writes aren't clobbered, and pulling `signature`/`fix_cmd` from the exception's `.diagnosis`, attached by `makepkg_wrapper`). Surfaced by `sysforge state failed`.

Public helpers: `parse_pacman_version(ver_str)` splits a `[epoch:]pkgver-pkgrel` string into a `(epoch, pkgver, pkgrel)` tuple; used by `sync_with_installed()`.

### `pkgbuild_review.py`

The PKGBUILD review gate. Before a package is built, compares the source clone's HEAD against the `reviewed_commit` recorded in `build_state.toml` (the clone HEAD at the last successful build — stamped sticky by `makepkg_wrapper`'s single `record()` site, so dep builds and pipeline stages are covered without caller threading) and, on a difference, shows the **full source-tree diff** — not just the PKGBUILD, so changes hiding in `.install` files, patches, or new sources are visible — and prompts: `[v]iew` (full patch through `pager.maybe_pager`) / `[a]ccept` / `[s]kip package` / `a[b]ort run`. The prompt reads a single keypress via `prompt.prompt_key` — no Enter needed. EOF/Ctrl-C at the prompt aborts (no answer is not consent). A package with no recorded `reviewed_commit`, or whose recorded sha vanished (purge + re-clone), is reviewed against git's empty tree — a full-content review. The comparison is commit-based (recorded → HEAD), deliberately not worktree-based: upstream changes arrive as commits via source sync, while uncommitted local edits are user-authored (the STATUS_DIVERGED case) and are not re-presented to their author. Auto-accept paths (logged, never prompt): non-interactive runs (stdin or stdout not a TTY), and callers passing `interactive=False` — `sysforge update`'s default mode. Owns the `[REVIEW]` tag. API: `head_commit(dir)`, `commit_exists(dir, sha)`, `review_target(pkgbase, dir, reviewed_commit, interactive=True) -> DECISION_*`, `review_deps(deps, interactive=True) -> DECISION_*`.

**Dependency gate (`review_deps`)** — the batched counterpart for AUR dependency PKGBUILDs built by `prepare_deps`. Takes `[(name, pkgbuild_dir, reviewed_commit), ...]`, runs the same HEAD-vs-reviewed-commit comparison per dep (empty-tree fallback included), and presents the changed ones as one summary block (short shas + `git diff --shortstat`) with a single prompt: `[v]iew diffs` (each dep's full patch through the pager) / `[a]ccept all` / `a[b]ort run`. Deliberately **no per-dep skip** — dropping a dependency breaks the package that needs it, so the decision is all-or-nothing. Auto paths mirror `review_target`: `interactive=False` emits one batched `auto-accepted N dependency change(s)` notice; non-TTY runs auto-accept with a warning. Returns `DECISION_CLEAN` when nothing changed.

**One home for the gate:** `build_core.build_and_install(review="prompt"|"auto"|"off")` runs it for every target *before* dep prep and the build loop — a skip never installs that package's makedeps; an abort returns `BuildOutcome(aborted=True)` with nothing built or installed (callers exit cleanly, no exception, so the verb sentinel clears normally). `build` defaults to `"prompt"` (the deliberate, targeted verb); `update` defaults to `"auto"` — changes are auto-accepted with a per-package `[REVIEW] auto-accepted` notice so a batch update stays unattended — and `--review` opts update back into prompting. For the `build` path (`sync_source=True`, where the wrapper's inline sync would otherwise run after the gate) the targets are pre-synced through `source_sync.get_scheduler()` first — the wrapper's later request dedups against the scheduler cache, and the gate sees the post-fetch HEAD. Disabled entirely (`"off"`) via `--no-review` (both verbs) or `[build] review = false` in packages.toml. Dependencies are covered too: `prepare_deps` receives the same `review` mode and runs `review_deps` over the resolved AUR deps (looking up each dep's `reviewed_commit` in build_state) between resolution and `build_resolved_deps`; an abort there returns `False` from `prepare_deps`, which `build_and_install` surfaces as the same clean `BuildOutcome(aborted=True)` return.

### `version.py`

Version comparison utilities. `vercmp(a, b)` wraps the system `vercmp` binary and returns -1/0/1 (negative/zero/positive output from vercmp is clamped). `format_version(globals_)` assembles an `[epoch:]pkgver-pkgrel` string from parsed PKGBUILD globals, omitting the epoch prefix when it is `"0"` or absent.

### `timing.py`

Wall-clock phase timing for long-running verbs (landed with the `--timings` / `--py-profile` global flags). `PhaseTimer` accumulates `PhaseRecord(name, duration_ms)` entries — `time.monotonic_ns()` deltas stored as milliseconds, the same lineage as `env_chain`'s `cost_ms` — via the `phase(name)` context manager (a raising body still records; the append is in a `finally`) or the explicit `start(name)`/`stop()` pair for long inline regions where a with-block re-indent is impractical (`stop()` without an open phase is a no-op so early-exit paths can call it unconditionally; `start()` over an open phase implicitly stops it). `render_report(timer, title=…) -> list[str]` builds aligned report lines — each phase carries a right-aligned duration plus a proportional bar (Unicode blocks with eighth-cell partials, scaled so the longest phase fills `_BAR_WIDTH`; any nonzero duration shows at least a sliver) — plus a bar-less total line, returning `[]` for an empty timer. **Pure primitive: stdlib only, never imports the pipeline layer, and never logs** — callers render the lines under their own tag (`update` under `[UPDATE]` via `_emit_timings`, `build` under `[BUILD]` via `_report_timings`; both emit at info level always and promote to `_log.ui` when `--timings` is set). `build_core.build_and_install(timer=None)` records `dep prep` / `build: <pkgbase>` / `install` onto the caller's timer or a self-made one — `BuildOutcome.phase_records` aliases the timer's records list so callers that didn't pass a timer can still report. Coarse wall-clock is deliberate: sysforge's runtime is dominated by subprocesses (makepkg/pacman/git) that a Python profiler only shows as wait; Python-level hotspots are `--py-profile`'s job (see §CLI Verb Framework → Global profiling flags).

### `vcs_pkgver.py`

`evaluate_vcs_pkgver(pkgbuild_dir, *, timeout=300) -> str | None` resolves a VCS PKGBUILD's effective `[epoch:]pkgver-pkgrel` by running `pkgver()` against the fetched upstream sources. Two-step makepkg invocation: (1) `makepkg -od --nobuild --noprepare --nodeps --skippgpcheck --noconfirm` updates VCS sources and runs `pkgver()`; (2) `makepkg --packagelist` prints the resolved filename, which is parsed via `_parse_built_pkg_filename` (the same helper `_find_existing_artifacts` uses) into `(epoch, pkgver, pkgrel)`. Returns `None` on any failure — non-zero exit, timeout, missing makepkg, unparseable output — with a WARN logged. Caller policy in `update.py`: `None` → `DEVEL_EVAL_FAILED` action, package skipped (not rebuilt). Used by `sysforge update --devel` to vercmp upstream-resolved against installed and only rebuild genuinely-stale VCS packages.

`peek_upstream_commit(pkgbuild_dir, *, timeout=30) -> str | None` and `read_built_upstream_commit(pkgbuild_dir, *, timeout=10) -> str | None` are the two halves of the `--devel` short-circuit cache. Both share a private `_single_git_source(globals_)` helper that parses `source=()` from `parse_pkgbuild`'s output, recognises `git+<url>`, `git://...`, and `<name>::<either>` forms (with `#commit=`/`#tag=`/`#branch=`/`#fragment=` fragments), and returns `(clone_name, url, fragment)` only when the PKGBUILD has exactly one git source and no remaining `${...}` interpolation. `peek_upstream_commit` runs `git ls-remote <url> <ref>` (or returns immediately for a `commit=<sha>` pin) to get the current upstream tip without fetching the working tree. `read_built_upstream_commit` runs `git -C <pkgbuild_dir>/src/<clone_name> rev-parse HEAD` to capture the SHA of the just-built tree, called from `makepkg_wrapper.py` immediately after a successful build so the SHA is persisted to `build_state.toml` as `built_upstream_commit`. Either helper returns `None` for multi-git-source / non-git / unresolved-variable / parse-failure / subprocess-failure cases, in which case the caller falls through to the canonical `evaluate_vcs_pkgver` slow path. The strict semantic — only-on-successful-build writes — means pre-existing build_state entries lacking the field stay slow until the package is naturally rebuilt; this is intentional, to keep the field's meaning unambiguous (it is the commit we built, not the commit we last observed).

### `diagnostics.py`

The unified diagnostic vocabulary: one `Finding` dataclass + the renderer, exit-code reducer, severity normaliser, adapters, and axis runner described in the `doctor.py` framework note above. Lives in `primitives/` so any layer can produce `Finding`s; it must never import the `pipeline` layer (pipeline-layer checks are adapted by their callers, not here). Public API: `Finding`; `SEV_ERROR`/`SEV_WARN`/`SEV_INFO`, `normalize_severity`, `severity_rank`; `adapt(category, obj)` / `adapt_many`; `from_toolchain_check(check, *, category)`; `from_fix_suggestion(suggestion, *, category)`; `error_count(findings)`; `Axis(name, label, run, clean_msg)`, `run_axes(axes)`; `render_axis(logger, label, findings, *, clean_msg, quiet)`. Log tag: `[DIAG]`. The `system_probe` / `state_probe` / `runtime_probe` axes return `Finding` objects directly (they import `diagnostics`); the older probes keep their own dataclasses and are adapted at the boundary.

### `system_probe.py`

Read-only pacman / system-integrity checks for `doctor --pacman`. Public API: `collect_system_findings() -> list[Finding]`. Internal checks: `_check_db_consistency` (`pacman -Dk`), `_check_stale_lock` (`/var/lib/pacman/db.lck`), `_check_pacfiles` (`*.pacnew`/`*.pacsave` under `_ETC`), `_check_orphans` (`pacman -Qtdq`). Strictly local-database — never issues a sync (`-Sy`), so a `doctor` run cannot change the installed package set. Module-level `_PACMAN_DB_LOCK` / `_ETC` are repointable for tests.

### `state_probe.py`

Read-only inspection of sysforge's own persisted state for `doctor --state`. Public API: `collect_state_findings(state_dir=None, installed=None) -> list[Finding]`. Surfaces `BuildState.all_failures()` (each a `warn` carrying the stored `build_diag` signature/`fix_cmd`), an interrupted stage sentinel via `StageSentinel.get_active()` (an `error` with the recorded `recovery_cmd`), and build-state drift computed from `BuildState.all_packages()` vs the live pacman db (an `info` for zombie entries). Never calls `BuildState.save()` or the recovering `stage_sentinel.check_and_recover_stale_sentinel` — drift is computed without mutating the in-memory state. Source-sync `STATUS_*` is omitted (the scheduler cache is per-process; a standalone `doctor` has none).

### `runtime_probe.py`

Read-only services / runtime-health checks for `doctor --services`. Public API: `collect_runtime_findings() -> list[Finding]`. `_check_failed_units` (`systemctl --failed` → one `error` per unit) and `_check_missing_firmware` (best-effort `journalctl -k -b` parse for "Direct firmware load … failed" → one deduped `warn`). DKMS health is checked in the `boot` axis (running kernel), not here, to avoid double-reporting. Every external command is guarded so an absent tool or permission error yields no findings (`run_axes` isolates exceptions as a backstop).

### `graphics_probe.py`

Read-only system-state checks for graphics stack health. Complements `doctor.py`'s package-walk: the package walk catches ABI/linkage drift; `graphics_probe` catches misconfiguration the ABI walk is structurally blind to (kernel-module parameters, compositor protocol advertisements, Steam client config, driver kmod/userspace version skew). Each check is a small pure probe that reads `/sys`, `/proc/cmdline`, `pacman -Q`, `lsmod`, `wayland-info`, or `~/.steam/root/config/config.vdf` — no writes, no side effects.

Public API: `check_system_graphics(config, *, gpu_vendors=None) -> list[GraphicsFinding]`. `GraphicsFinding` is a frozen dataclass with `severity` (`SEV_ERROR` | `SEV_WARN` | `SEV_INFO`), `check_id`, `message`, `remediation`. Vendor-gated checks run only when `gpu_vendors` includes the relevant vendor; caller may pass an explicit list or let the function auto-detect via hardware profile / `lspci` fallback.

Check inventory (v1):

| `check_id` | Probe | Gating | Severity when failing |
|---|---|---|---|
| `nvidia_modeset` | `/sys/module/nvidia_drm/parameters/modeset`, falls back to `/proc/cmdline` | `nvidia` vendor | error |
| `nvidia_fbdev` | `/sys/module/nvidia_drm/parameters/fbdev` — required when kernel ≥ 6.11 | `nvidia` vendor + kernel ≥ 6.11 | warn |
| `nvidia_driver_skew` | compare `nvidia-*-dkms` / `nvidia-utils` / `lib32-nvidia-utils` versions from `pacman -Q` | `nvidia` vendor | error |
| `nvidia_module_loaded` | `lsmod` for `nvidia` | `nvidia` vendor | error |
| `multilib_enabled` | grep `/etc/pacman.conf` for `[multilib]` | any GPU vendor | warn |
| `session_type` | `$XDG_SESSION_TYPE` + `$XDG_CURRENT_DESKTOP` | always | info (context only) |
| `xwayland_present` | `pacman -Q xorg-xwayland` when session is Wayland | Wayland session | error |
| `explicit_sync_protocol` | `wayland-info` — look for `wp_linux_drm_syncobj_manager_v1` (or legacy `zwp_linux_explicit_synchronization_v1`) in advertised globals | Wayland session + `nvidia` vendor | error |
| `steam_gpu_accel` | parse `~/.steam/root/config/config.vdf` for `GPUAccelerationEnabled "1"` | Steam installed | warn |
| `mesa_llvm_symbols` | `toolchain_safety.check_installed_consumer_symbols` — installed `libgallium`/DRI/Vulkan drivers must resolve every `LLVMInitialize*@LLVM_x.y` symbol against the installed `libLLVM` | any GPU vendor (mesa links libLLVM regardless) | error |

The `mesa_llvm_symbols` check is the one-line self-diagnosis for the "rebuilt the toolchain, now the desktop black-screens" failure mode: a self-built `libLLVM` with a reduced `LLVM_TARGETS_TO_BUILD` that dropped a backend mesa links unconditionally (AMDGPU/radeonsi, host-CPU/llvmpipe) leaves `libgallium` with dangling `undefined symbol: LLVMInitializeAMDGPU…`, killing every EGL/GL client. It reuses the toolchain stage's post-install symbol fact (one differ; remediation points at reinstalling official `llvm-libs` or rerunning `run toolchain` with the AMDGPU baseline). The toolchain stage's Gate 2/3 prevent this pre/post-install; this surfaces it for a system already in the state.

The explicit-sync check is the load-bearing one for NVIDIA-on-Wayland black-window breakage: when the compositor doesn't advertise `wp_linux_drm_syncobj_manager_v1`, XWayland games on NVIDIA fall back to implicit sync which is known-broken on the NVIDIA explicit-sync driver path. Note: the registry global is `wp_linux_drm_syncobj_manager_v1` — the protocol-document name `linux-drm-syncobj-v1` (i.e. the bare `wp_linux_drm_syncobj_v1` substring) never appears as an advertised global.

Log tag: `[GFX]`. No writes, no sudo, no network.

### `device_probe.py`

Full PCI/USB device inventory plus a driver-coverage check. Read-only; same `_run`/`_read_text`/frozen-finding idiom as `graphics_probe.py`. Walks `/sys/bus/{pci,usb}/devices`, reading each device's `modalias`, class, and the `driver` symlink (the bound-driver signal validates the *running* kernel). The device→module link is resolved against a complete reference kernel's `modules.alias` + `modules.builtin.modinfo` via `fnmatch` (exactly modprobe's matching), cached per reference dir; `find_reference_modules_dir()` picks the newest installed stock kernel (excludes any `custom` modules dir) so a custom kernel that omitted a driver doesn't hide its own gap. The module→`CONFIG_*` step layers two sources: the curated `_MODULE_TO_KCONFIG` table (vetted, always wins on overlap — common audio/NIC/NVMe/USB/GPU modules) and an optional caller-supplied `kconfig_map` (a loaded `kbuild_map` cache) that widens coverage to every module the parsed kernel tree knows; unknown modules degrade to "module name only".

Public API: `enumerate_devices(buses=("pci","usb"), kconfig_map=None) -> list[Device]`; `check_unsupported_devices(*, devices=None) -> list[DeviceFinding]` (flags functional — non-bridge/hub — devices with no driver and a known expected module); `find_reference_modules_dir() -> Path | None`. `Device` carries `bus`/`address`/`modalias`/`class_id`/`description`/`driver`/`expected_modules`/`suggested_kconfig`; `DeviceFinding` mirrors `GraphicsFinding`. Consumers: the hardware stage (`[[devices]]` inventory + `[kconfig_devices]` fold + WARNs; passes the cached `kbuild_map`), `doctor --hardware` (curated-only — stays read-only), and `kernel_safety.audit_resolved_config` (device-driver coverage; the kernel stage's Gate 2 passes the freshly parsed tree map). Filesystem roots (`_SYS_BUS`, `_MODULES_BASE`) are module-level for test repointing.

### `kbuild_map.py`

Module→`CONFIG_*` map derived from a kernel source tree's kbuild Makefiles (`obj-$(CONFIG_X) += driver.o` — the authoritative, version-exact mapping; the same data `make localmodconfig` parses). A kernel tree is only on disk transiently (the extracted makepkg srcdir), so the parsed map is persisted to a JSON cache (`KBUILD_MAP_FILENAME = "kbuild_module_map.json"`) in the state dir with provenance (`kernel_release`, `generated_at`, entry count).

Public API: `parse_kbuild_tree(tree_root) -> dict[module, CONFIG_X]` (walks `**/Makefile` + `**/Kbuild`, skips `Documentation/tools/scripts/samples/usr` at the top level; handles `+=`/`:=`/`=`, multi-object lines, backslash continuations; object stems normalized `-`→`_` to match `modules.alias`; first-wins on duplicates over a sorted walk); `save_map(path, mapping, kernel_release)` (atomic tmp+rename); `load_map(path) -> (mapping, release) | None` (None on missing/corrupt/wrong shape). Accepted gaps: directory-gated drivers (`obj-$(CONFIG_X) += dir/`) and composite-only module names aren't attributed — the curated `device_probe._MODULE_TO_KCONFIG` table covers the ones that matter and always wins on overlap.

Pure module: no logging (callers surface outcomes under their own tag) and no state-dir resolution (callers pass explicit paths, mirroring `BuildState(state_dir)`). Producer: the kernel stage's Gate 2 (the resolved `.config`'s parent *is* the just-built tree) parses and caches. Consumers: Gate 2's own device audit (fresh map, not the cache) and the hardware stage (loads the cache to widen `[kconfig_devices]`). First-ever kernel build runs curated-only; every run after has the cache — installed headers can't substitute (they don't ship the nested driver Makefiles).

### `kernel_safety.py`

Guardrails so the kernel stage can never leave the machine unbootable (see §Kernel stage boot-safety for the policy). Pure/read-only, fixture-testable via module-level path constants (`_BOOT_DIR`, `_PROC_MOUNTS`, `_CRYPTTAB`, `_MDSTAT`, `_MKINITCPIO_CONF`). `KernelFinding` adds `is_brick: bool` to the finding shape — True means "unbootable / dangerous to install"; the kernel stage hard-fails on those and warns on the rest.

Public API:
- `parse_kconfig(path)` / `parse_kconfig_text(text)` — the shared `.config` line parser (`CONFIG_X=y|m`, `# CONFIG_X is not set` → `n`); also reused by `dep_analysis._parse_kernel_config` for the running kernel.
- `detect_root_topology() -> RootTopology` — root FS, storage transport, and crypt/LVM/RAID stacking from `/proc/mounts` + `lsblk -s` (+ `/etc/crypttab`, `/proc/mdstat`).
- `audit_resolved_config(config, topology=None, devices=None) -> list[KernelFinding]` — the one validator: boot-critical symbols (root FS, root storage controller, core boot infra, systemd prereqs, console) keyed off topology, plus device-driver coverage from `device_probe` Devices. Accepts a `.config` path or a pre-parsed dict.
- `find_fallback_kernels(exclude_pkg=None)` / `verify_boot_artifacts(pkgname, bootloader)` / `check_dkms_for_kernel(kver)` / `list_dkms_modules()` / `check_mkinitcpio_hooks(topology)` / `check_boot_mount_space(min_mb=200)` — the Gate 1/Gate 3 fact-gatherers (fallback presence, post-install vmlinuz+initramfs+boot-entry, DKMS rebuild coverage, mkinitcpio HOOKS vs topology, `/boot` headroom).

The primitive must not import the pipeline layer; the kernel stage owns the abort/warn decisions.

---

## Flag Profile System

### Profile structure

```toml
[defaults]
profile = "standard"
toolchain = "gcc"        # global package-compiler default (see "Toolchain field")

[profiles.bare]
# Fallback profile, no flags

[profiles.standard]
extends = "bare"
# Compiler comes from the `toolchain` field (defaults to "gcc" via [defaults]).
# Override individual CC/CXX/AR/… keys to win over the bundle, or set
# `toolchain = "llvm"` here / in a rule to switch the whole bundle.
CFLAGS = "-march=native -O2 -pipe"
CXXFLAGS = "$CFLAGS"
LDFLAGS = "-Wl,-O1,--sort-common,--as-needed,-z,relro,-z,now"
makepkg_flags = ["--noconfirm", "--syncdeps"]

[profiles.optimized]
extends = "standard"
CFLAGS = "-march=native -O3 -pipe -fno-plt"
LDFLAGS = "-Wl,-O1,--sort-common,--as-needed,-z,relro,-z,now,--icf=all"

[profiles.pgo_llvm_toolchain]
extends = "optimized"
build_mode = "pgo_llvm_toolchain"

[profiles.patched]
extends = "optimized"
build_mode = "patched_pkgbuild"

[profiles.kernel]
extends = "bare"
build_mode = "kernel"
batch = true
makepkg_flags = ["--noconfirm", "--syncdeps", "-f", "-c"]
```

### Toolchain field

`toolchain = "gcc" | "llvm"` is a single knob that expands to the correct
compiler/binutils bundle, so a profile need not hand-set the six-plus correlated
keys (and risk a silently half-LLVM build). Valid in `[defaults]` (global
default) and any `[profiles.NAME]`.

| value  | expands to |
|--------|------------|
| `gcc`  | `CC=gcc`, `CXX=g++` (binutils from system base-devel) |
| `llvm` | `CC=clang`, `CXX=clang++`, `AR=llvm-ar`, `NM=llvm-nm`, `RANLIB=llvm-ranlib`, `STRIP=llvm-strip`, and `-fuse-ld=lld` injected into `LDFLAGS` when no `-fuse-ld=` is already declared |

Resolution (in `profile._expand_toolchain`, the one home, run **after**
`merge_extends` so the directive inherits/overrides like any key): an explicit
`CC`/`CXX`/`AR`/… in the resolved profile wins (`setdefault`); otherwise the
resolved profile's own `toolchain`; otherwise `[defaults] toolchain`. The
expansion is pure (no fs probing) — a missing `lld` is reconciled by the
emit-time linker guard, exactly as for a hand-written `-fuse-ld=lld`.

This field is the **package** compiler knob. It is distinct from two other axes
that also take `gcc`/`llvm`:

- `toolchain.toml`'s `compiler` — whether the toolchain *stage* builds/registers
  a system compiler. On a successful register/build the stage writes
  `[defaults] toolchain` to match it (via `config.set_default_toolchain`), so the
  package default tracks the registered compiler. See pipeline-layer → toolchain
  stage.
- `toolchain_variant` — which toolchain the stage *built* (`stock_llvm`/`pgo_llvm`/
  `gcc`), recorded in `build_state.toml` for drift detection. Not derived from
  this field.

### `extends` semantics

Full inheritance with explicit override. The child starts as a complete copy of the parent's resolved values, then applies its own keys on top.

**Direct keys** fully replace the parent's value.

**`[profiles.x.append]` subsection** — keys merged into the parent's value using token-level list merge rather than string concatenation.

#### Append merge algorithm

1. Tokenize parent and child values by whitespace
2. For each child token, resolve in this order:
   - **Explicit conflict group** — if the token belongs to a defined conflict group, remove all other group members from the accumulated list, insert the child token
   - **Prefix match** — extract the token's prefix (everything up to `=`, or up to trailing digits for flags like `-O2`); if a matching prefix exists, replace in-place
   - **Append** — no match, add to end
3. Reconstruct as space-joined string

**Worked example:**
```
parent CFLAGS:   "-march=native -O2 -pipe -fstack-protector"
append CFLAGS:   "-O3 -fno-stack-protector --icf=all"

-O3                   prefix "-O" matches "-O2"              → replace in-place
-fno-stack-protector  conflict group "stack"                 → removes "-fstack-protector", inserts
--icf=all             no match                               → append

result: "-march=native -O3 -pipe -fno-stack-protector --icf=all"
```

#### Conflict groups

Defined in the `[append_conflict_groups]` table of `/etc/sysforge/profiles.toml`:

```toml
[conflict_groups]
pic   = ["-fPIC", "-fPIE", "-fpic", "-fpie", "-fno-pic", "-fno-pie"]
lto   = ["-flto", "-flto=thin", "-flto=full", "-fno-lto"]
stack = ["-fstack-protector", "-fstack-protector-strong", "-fno-stack-protector"]
```

User-defined groups in `~/.config/sysforge/profiles.toml` (under `[append_conflict_groups]`) follow the same `extends_system` merge model. Explicit conflict groups take precedence over prefix matching.

### Rule match field semantics

All match fields optional. Omitting a field passes unconditionally.

| Field | Semantics |
|---|---|
| `pkgnames` | ANY match + glob |
| `not_pkgnames` | ALL absent + glob |
| `groups` | ALL match + glob |
| `not_groups` | ALL absent, exact |
| `depends_any` | ANY exact |
| `depends_all` | ALL exact |
| `not_depends` | ALL absent, exact |
| `makedepends_any` | ANY exact |
| `makedepends_all` | ALL exact |
| `not_makedepends` | ALL absent, exact |

`pkgnames`/`not_pkgnames` and `groups` support fnmatch glob patterns. `depends_*` and `makedepends_*` are always exact.

### Multi-rule merge and priority

Highest-priority matching rule wins outright — its profile is resolved in full via the `extends` chain. Equal priority: first occurrence wins. `priority` range: 0–99 (system), 100–199 (user, bumped on merge).

**`append_groups` is additive across all matched rules** regardless of priority. Every matched rule's `append_groups` is collected and appended to the package's final group list. This is asymmetric by design — flag resolution is winner-takes-all, groups are accumulative.

### `consumes` field

Declares which conf types a build requires (`makepkg`, `rust`, `cmake`, `meson`, `env`).

- **Default:** auto-inferred from `makedepends` via the `[consumes_inference]` table in `profiles.toml`
- **Override:** explicit `consumes` on a profile replaces the inferred value

```toml
# /etc/sysforge/profiles.toml
[consumes_inference]
cargo  = ["makepkg", "rust", "env"]
meson  = ["makepkg", "meson", "env"]
cmake  = ["makepkg", "cmake", "env"]
ninja  = ["makepkg", "env"]
```

### Conf key routing

Profile keys are routed to one of three delivery channels:

- **Toolchain env** (`toolchain` type: `CC`, `CXX`) — injected directly via subprocess env, always, regardless of `active_consumes`. makepkg does not export `CC`/`CXX` from `makepkg.conf` to child processes, so they must be present in the env that makepkg inherits at invocation time. SysForge handles this automatically — set them in a profile like any other key.
- **Conf file** (`makepkg`, `rust`, `cmake`, `meson` types) — written into the temp `makepkg.conf`. Only types in `active_consumes` are written.
- **Subprocess env** (`env` type, or any unclassified key) — injected via `subprocess.run(env=...)`. Used for `RUSTC_WRAPPER`, `CCACHE_DIR`, `SCCACHE_DIR`, `CC_LD`, `CXX_LD` (meson linker override), etc. Only delivered when `"env"` is in `active_consumes`. Keys that are present in the profile but not in `active_consumes` are logged as skipped (`[INFO][ENV]`).

Unclassified keys (not in any `CONF_KEY_MAP` type and not in `SYSFORGE_KEYS`) travel via env pass and are logged as `[WARN][ENV]`.

Toolchain keys (`CC`, `CXX`) from the **system** makepkg.conf are excluded from the emitted temp conf — makepkg sources the conf as a shell script, so any `CC`/`CXX` present would overwrite the env-injected values from the profile. Only env injection delivers toolchain keys.

### Flag guards

`emit_makepkg_conf` runs a series of guards after profile overrides are applied but before the conf is written. Each guard detects and reconciles toolchain incompatibilities, logging at `[WARN][CONF]` (the conf module narrates its own flag adjustments; the underlying transforms stay pure in `makepkg_flags`). Guards run in this order:

1. **Linker guard** — detects the effective linker from `-fuse-ld=X` in LDFLAGS (default: `ld`/bfd). Strips lld-only flags (`--icf=*`) when the effective linker is not lld.

2. **RUSTFLAGS linker reconciliation** — if RUSTFLAGS declares `-C link-arg=-fuse-ld=X` with a different linker than LDFLAGS, overrides it to match. Handles both spaced (`-C link-arg=...`) and compact (`-Clink-arg=...`) forms. Prevents LTO link failures from mismatched linkers (e.g. mold cannot process LLVM bitcode produced with lld).

3. **GCC thin-LTO rewrite** — `-flto=thin` is clang-only. When GCC is in effect, rewrites `-flto=thin` → `-flto` in LTOFLAGS, CFLAGS, CXXFLAGS, and LDFLAGS. Falls back to system conf values when the profile doesn't override a key.

4. **GCC + lld LTO disabling** — GCC LTO produces `.gnu.lto_*` bitcode that only GNU ld/gold can process; lld cannot read it. When GCC is in effect and the effective linker is lld, LTO is disabled entirely: LTOFLAGS cleared, `-flto*` stripped from flag keys, and `lto` flipped to `!lto` in OPTIONS (prevents makepkg's `${LTOFLAGS:--flto}` fallback).

5. **Full LTO stripping** (PGO only) — strips `-flto`/`-flto=full` from CFLAGS/CXXFLAGS/LDFLAGS and clears LTOFLAGS during PGO passes.

6. **lib32 march scrub** — when `invoke_makepkg` detects a `lib32-*` build (`pkgbuild_path.parent.name.startswith("lib32-")`), `emit_makepkg_conf` strips host-CPU-specific or 64-bit-only `-march=` tokens from CFLAGS and CXXFLAGS in both profile overrides and system-conf passthrough. Stripped values: `-march=native` (resolves to the host's amd64 microarch — `znver3` on Zen 3), `-march=x86-64`, `-march=x86-64-v2`, `-march=x86-64-v3`, `-march=x86-64-v4` (microarch levels defined only for 64-bit code). Other `-march=` values (e.g. `-march=i686`) and all non-`-march` flags are preserved. Without this guard a `[profiles.bare]` lib32-* build inherits the system conf's `-march=native` unchanged, and multilib GCC then refuses the compile with a confusing "unrecognized target arch" error rather than a clear "host flag stripped for lib32" log line.

7. **lib32 PGO scrub** — for a `lib32-*` build, `emit_makepkg_conf` strips PGO profile flags (`-fprofile-use`/`-fprofile-instr-use`/`-fprofile-generate`/`-fprofile-instr-generate`, via `makepkg_flags._strip_pgo_flags`) from CFLAGS/CXXFLAGS/LDFLAGS. This runs *after* the `compiler_flags_extra` injection, so it catches the toolchain stage's injected `-fprofile-use=<store>/clang.profdata` too. The profile is trained on the x86_64 clang self-build and is discarded by an i686 (`-m32`) build (clang emits `-Wbackend-plugin "count discarded"`), so it adds nothing and must not reach the lib32 build. See pipeline-layer → *lib32 is not toolchain-managed* for why lib32 isn't built by the toolchain stage at all by default.

Guards 3–4 fire when any of the following is true:
- **Profile CC is GCC** — `cc_override` (CLI `--cc`) > `resolved_profile["CC"]` resolves to a non-`clang` compiler.
- **PKGBUILD hardcodes GCC (proactive)** — `pkgbuild_meta.has_hardcoded_gcc()` statically scans every PKGBUILD(5) build-time function — `prepare()`, `build()`, `check()`, `package()`, and any `package_<pkgname>()` split-package variant — for direct `gcc`/`g++` invocations, `ccache gcc`, or `CC=gcc`/`CXX=g++` assignments. Quoted forms (`export CC='gcc -m32'`, `CXX="g++"`) are handled. `verify()` is excluded — it authenticates sources, never compiles. Conservative: ignores `$CC`/`${CXX}` references, `-lgcc` library references, and comments. False is not authoritative — a Makefile checked out in `src/` may still hardcode `g++`.
- **`lib32-*` package (proactive)** — Arch's multilib has no `lib32-clang`; every `lib32-*` package compiles with 32-bit GCC by construction. `invoke_makepkg` triggers the guard whenever `pkgbuild_path.parent.name` starts with `lib32-`, even when `has_hardcoded_gcc()` returns False. The directory name (rather than parsed `pkgname`) is used because real-world `lib32-*` PKGBUILDs interpolate (`pkgname=lib32-$_basename`), which the static parser does not expand.
- **Reactive GCC fallback (post-failure retry)** — set when the previous invocation of makepkg failed with a clang-flag-rejected-by-GCC error and `_run_build` is re-entering the conf emit path. See [Toolchain-mismatch auto-retry](#toolchain-mismatch-auto-retry).

The `[WARN][FLAG]` rewrite log records which trigger fired so the cause is visible in the per-package log. The effective linker is determined by guard 1 and shared with subsequent guards.

---

## Makepkg Wrapper

### Environment isolation

SysForge treats the calling shell environment as untrusted for build tool vars. All keys in the `makepkg` and `toolchain` conf types (`CC`, `CXX`, `CFLAGS`, `CXXFLAGS`, `LDFLAGS`, `MAKEFLAGS`, etc.) are stripped from the inherited shell env before makepkg is invoked. The temp conf is the sole authority — shell vars set by `.zshrc`, `.bashrc`, or upstream tooling cannot bleed through and override profile settings. Each stripped key is logged individually under `[INFO][ENV]` with its old shell value, so the full before/after state is visible in the log. If `extra_env` (the profile's env-type keys) would override a shell var that was *not* in the strip set, a `[WARN][ENV]` is emitted.

SysForge bootstrap vars (`SYSFORGE_STATE_DIR`, `SYSFORGE_CONFIG_DIR`) are explicitly exempt from this rule — they are SysForge's own interface, not build tool vars.

Any build tool override needed at invocation time should use the corresponding SysForge flag (`--cc`, `--cxx`, `--ld`), not a shell export. This applies to both `sysforge build` and `sysforge pipeline`.

> **Cancelled design:** an `[env_precedence]` TOML table with a configurable priority stack (profile = 100, makepkg.conf = 80, shell = 20, PKGBUILD export = 10) was previously planned. It is superseded by this model — shell bleed-through is not a tunable priority, it is prevented entirely.

### Failure handling

Each scenario has a configurable behaviour in `[failure_handling]`:

```toml
[failure_handling]
pkgbuild_unparseable  = "warn_and_fallback"
no_rule_matched       = "fallback"
profile_missing       = "abort"
profile_cycle         = "abort"
tempfile_write_failed = "abort"
env_conflict          = "warn_and_fallback"
abi_mismatch          = "warn_and_fallback"
dep_unsatisfied       = "warn_and_fallback"
```

**Behaviours:** `abort`, `warn_and_fallback`, `fallback`, `error`

`profile_missing` and `tempfile_write_failed` always abort regardless of config.

### Interactive mode

`--interactive` on `sysforge build` does two things: it strips `--noconfirm` from the profile's `makepkg_flags`, and it makes `invoke_makepkg` inherit the parent's stdout/stderr instead of piping them through the line-classification loop. Stdio passthrough is what keeps unbuffered prompts visible — pacman's conflict prompt (`Remove sysforge? [y/N]`) and similar `\r`/no-newline output reaches the terminal immediately rather than sitting in a pipe buffer until the user blindly presses Enter. The tradeoff is that line-based output classification (`failed_stage`, `missing_deps`, `toolchain_mismatch` auto-retry, stdout-match fallback for `AlreadyBuilt`, `captured_output` for `auto_repair`) is bypassed in this branch; exit-code-based detection (`returncode == 13` → `AlreadyBuilt`, `returncode == 8` → install failure) still fires. Useful during development to review makepkg prompts without editing the profile; not appropriate for `update` batch flows, which depend on the classification path and therefore default `interactive=False`.

### Toolchain-mismatch auto-retry

When a package's build system (e.g. a Makefile shipped in `src/`) hardcodes `g++` but the active makepkg.conf carries clang-only flags (typically `-flto=thin`), GCC aborts with `cc1plus: error: unrecognized argument to '-flto=' option: 'thin'`. Static PKGBUILD scanning cannot detect this case because the hardcoded compiler lives in a file that only appears after `makepkg` extracts sources. To handle it automatically, `invoke_makepkg` scans stdout/stderr for a narrow list of toolchain-mismatch patterns:

```python
TOOLCHAIN_MISMATCH_PATTERNS = (
    "unrecognized argument to '-flto=' option",
    "unrecognized command-line option '-flto=thin'",
)
```

When any pattern matches and the process exits non-zero, `invoke_makepkg` raises `ToolchainMismatchError` (a `subprocess.CalledProcessError` subclass) instead of the plain exception. `_invoke_with_retry` re-raises this type unchanged — bypassing the interactive "correct manually" prompt — and `_run_build` catches it, sets `reactive_gcc_fallback=True`, and re-enters `emit_makepkg_conf` exactly once. The second attempt fires guards 3–4 (thin-LTO rewrite, LTO-disable for lld) regardless of the profile's CC and typically succeeds. If the retry also fails, the error bubbles out as a normal build failure.

`AlreadyBuilt` (carries the offending pkgbuild path) is raised when the makepkg run exits 13 (`E_ALREADY_BUILT`) or its stdout contains `"A package has already been built"` — covers chroot wrappers that may rewrite the exit code. Distinct from `CalledProcessError` so callers (currently `update.py`'s build loop) can locate the existing `.pkg.tar` in PKGDEST and install it instead of marking the build failed. `PGOBuildSkipped` is the third wrapper-specific exception: raised from `_run_build` when a `pgo_llvm_toolchain` build needs profdata that's absent/incompatible and the user (or non-interactive default) chose to skip.

To make the pattern scan work for every build mode, `invoke_makepkg` uses a `Popen`-with-tee capture path for non-interactive builds: each line is matched against the patterns, then forwarded to stdout (or to `[DEBUG][MAKEPKG]` when verbosity ≥ 3). stdin remains inherited so sudo prompts still work. The capture path is **skipped entirely when `interactive=True`** (see §Interactive mode) — in that branch the child inherits stdout/stderr directly, so the toolchain-mismatch auto-retry is unavailable. Batch flows (`update`, pipeline stages other than `kernel`) leave `interactive=False`, so they retain the retry.

### Build-failure auto-repair

> **Status: implemented (all 4 scenarios).** Lives in `sysforge/primitives/auto_repair.py`. `_run_build`'s outer loop catches `CalledProcessError`, walks `auto_repair.REGISTRY`, and on the first match runs the corresponding repair before retrying.

`invoke_makepkg`'s line-tee captures every stdout line into `captured_lines` and attaches the list to the raised `CalledProcessError` (and `ToolchainMismatchError`) as `captured_output`. `_run_build` wraps that buffer in a `BuildOutputAccumulator` (lines + optional `srcdir` for on-disk inspection) and feeds it to `auto_repair.apply_first_match`. Each scenario's `detect(accum)` returns a `MatchInfo` (or `None`); on match the wrapper consults `[failure_handling]` for the per-scenario behaviour, runs `repair(pkgbuild_dir, info)`, and re-enters the build loop. The set of already-fired scenarios is tracked per build (`_repaired_scenarios`) so a misdetected error cannot loop — once a scenario fires it is excluded from subsequent matches in the same build.

**Interactive-failure diagnosis (BUILDDIR/LOGDEST-aware).** In the interactive branch makepkg inherits the TTY, so `captured_output` is empty and the auto-repair scan above is unavailable. `makepkg_invoke` instead recovers a best-effort `build_diag.diagnose` signature from on-disk artifacts: `makepkg_env._effective_build_dir(pkgbuild_path, resolved_profile)` locates the meson/cmake side-car logs (`meson-log.txt`, `CMakeError.log`) under `$BUILDDIR/<pkgbase>/src`, and `makepkg_env._logdest_tail(pkgbuild_path)` reads the tail of the newest `$LOGDEST/<pkgbase>-*.log` (makepkg's captured stdout when `OPTIONS+=log`). Both `BUILDDIR` and `LOGDEST` are resolved through `pacman.get_builddir()` / `pacman.get_logdest()` — env first, then the layered system `makepkg.conf` — so a user who configures these only in `/etc/makepkg.conf` still gets a diagnosis. Don't read `os.environ["BUILDDIR"]` or assume `~/builds` directly here.

Configuration extends `[failure_handling]` with four scenario keys:

```toml
vendored_deps_missing = "auto_repair"
pgp_key_missing       = "auto_repair"
srcinfo_drift         = "auto_repair_with_warning"
checksum_mismatch     = "prompt_user"
```

Three new behaviour values join the existing `abort` / `warn_and_fallback` / `fallback` / `error` set: `auto_repair` (repair silently, retry, log at info), `auto_repair_with_warning` (repair, retry, log at `[WARN]`), `prompt_user` (surface the diff and require explicit consent before repairing). In **batch mode** (`batch=true` on the resolved profile) the `prompt_user` behaviour is short-circuited to `aborted` — auto-repair never runs unattended for security-sensitive scenarios. This is the load-bearing rule for `checksum_mismatch`.

`.SRCINFO` drift is the one scenario that is **not** in `REGISTRY` — it has a different lifecycle. `_run_build` calls `auto_repair.preflight_srcinfo(pkgbuild_dir, behaviour)` before the build starts, regenerating `.SRCINFO` (with a `[WARN]` by default) when `makepkg --printsrcinfo` differs from the committed file. The other three scenarios fire on retry from a captured failure.

Repair modes:

- **Vendored deps missing** — meson wraps and git submodules collapse to one primitive (both are "PKGBUILD failed to fetch declared subprojects before configure"). Detection: `Automatic wrap-based subproject downloading is disabled` (meson) or `.gitmodules` present in `${srcdir}/<top>` with empty submodule paths. Repair: `meson subprojects download` for wraps, `git submodule update --init --recursive` for submodules, run from the project root. Safe — fetches only what the project itself declares; no PKGBUILD mutation, no `source=()` change.
- **PGP key missing** — complements the proactive `import_pgp_keys` invoked before makepkg. Catches the case where signature validation fails mid-build because upstream rotated keys after the proactive import or `prepare()` pulled in newly signed sources. Detection: `gpg: Can't check signature: No public key` with an extractable key ID. Repair: rerun `import_pgp_keys` targeting the surfaced ID, retry once. Same trust model as the proactive path.
- **`.SRCINFO` drift** — sysforge parses PKGBUILD directly, so its own pipeline never reads `.SRCINFO`, but drift breaks AUR push and clean-chroot consumers. Detection: `makepkg --printsrcinfo` output diffs against the committed `.SRCINFO`. Repair: regenerate, with a `[WARN]` log line. Default is `auto_repair_with_warning` rather than silent because drift usually signals upstream PKGBUILD churn the user should see.
- **Checksum mismatch** — **not silent.** Default `prompt_user`: sysforge surfaces old vs. new sums and the source URL, then requires explicit consent before invoking `updpkgsums`. Silent auto-fix would mask supply-chain compromise — an attacker who replaces an upstream tarball would have sysforge "fix" the checksum and proceed. The user-prompt requirement is the load-bearing security distinction between this mode and the others; do not relax it without an equivalent compensating control.

### Patched PKGBUILD preservation

On build failure, patched PKGBUILD files are left in place for diagnosis rather than deleted:

- `patched_pkgbuild` mode: `PKGBUILD.sysforge` and `pkgbuild_extracted_profile.toml` are preserved. A `[WARN][PATCH]` line is emitted noting their location.
- Groups-only mode (non-patch builds): `PKGBUILD.sysforge` is also preserved on failure with a `[WARN][BUILD]` message.

On success, all patch artifacts are cleaned up in both modes.

### Batch mode

`batch = true` on a profile switches to unattended mode — build failures abort immediately rather than prompting. Intended for pipeline use.

```toml
[profiles.batch]
extends = "standard"
batch = true
makepkg_flags = ["--noconfirm", "--syncdeps", "--rmdeps", "--install", "--noprogressbar", "--log", "--cleanbuild"]
clean_builddir = true
```

---

## Logging

All log output goes to stderr. Format: `[SYSFORGE][LEVEL][TAG] message`

Verbosity controlled by `-v`/`-vv`/`-vvv` on the CLI:
- Default: `[ERROR]` only
- `-v`: adds `[WARN]`
- `-vv`: adds `[INFO]`
- `-vvv`: adds `[DEBUG]` — full body dumps of every loaded config, resolved profile, conflict groups, inference map, and temp makepkg.conf

Set once at CLI entry via `log.set_verbosity(args.verbose)`. Tests run at verbosity 2 (all messages visible).

### Colour

`log.py` is the single colour authority for the whole codebase. `log.use_color()` is the one gate every output site consults, and `log.bold()` / `dim()` / `red()` / `green()` / `yellow()` / `cyan()` are the shared helpers that wrap text only when the gate is on — no site hand-writes escape codes. `ui/headers.py` and `ui/progress.py` import these rather than carrying their own ANSI constants.

Resolution precedence in `use_color()`:

1. Colour **mode** (`log.set_color_mode`, set once at CLI entry): `"never"` → off; `"always"` → on (beats the environment, so colour survives being piped into a pager or colour-aware tool).
2. Mode `"auto"` (default): `NO_COLOR` (any non-empty value) disables; then `FORCE_COLOR` (any non-empty value) forces on; otherwise colour follows whether the active stream is a TTY.

The mode is resolved at startup as **`--color=auto|always|never` flag > `[ui] color` config (`sysforge.toml`) > `"auto"`** (`cli._resolve_color_mode`); a junk value degrades to `"auto"`. File logs are always written plain regardless of the gate. Because the decision is per-call, output piped through the pager is coloured up front (the review diff passes `git diff --color=always` when the gate is on, then `less -R` carries the ANSI through).

### File logging

File logging runs at full verbosity regardless of the `-v` level — every `[INFO]`, `[WARN]`, and `[ERROR]` line is written to file even when the terminal shows only errors. Never let file I/O break a build: all file write errors are silently swallowed.

**Unified log** — one file for the entire run.

- Default path: `<state_dir>/sysforge.log` (i.e. `/var/lib/sysforge/sysforge.log`). For `sysforge update`: `<state_dir>/sysforge-update.log`.
- `sysforge update` always truncates at run start. `sysforge run pipeline` appends across runs and clears on success.
- A `# log cleared after successful run` marker is left in the file after truncation.
- `--log-dir <path>` overrides the directory.
- `--purge-log` (`run pipeline` only) truncates before the run starts.
- `--persist-log` suppresses truncation on success. Use when you want to keep the log for post-run analysis.
- `--no-unified-log` (`run pipeline` only) disables the unified log for this run.

**Per-package log** — one file per package build, written alongside the PKGBUILD.

- Path: `<pkgbuild_src_dir>/<pkgname>/sysforge_<pkgname>.log`
- Same lifecycle as the unified log: appends across builds, cleared on success unless `--persist-log`.
- Written by both `sysforge pipeline` (via the packages stage) and `sysforge build`.
- `--no-pkg-logs` disables per-package logs for `sysforge pipeline`.

**CLI flag summary:**

| Flag | Command | Effect |
|---|---|---|
| `--no-unified-log` | `run pipeline` | Disable unified log for this run |
| `--no-pkg-logs` | `run pipeline`, `run packages`, `run kernel` | Disable per-package logs for this run |
| `--no-pkg-log` | `build` | Disable the per-package log for this build |
| `--log-dir <path>` | `run pipeline`, `run packages`, `run kernel`, `build` | Override log file directory |
| `--purge-log` | `run pipeline` | Truncate unified log before run |
| `--persist-log` | `run pipeline`, `run toolchain`, `run packages`, `run kernel`, `build` | Keep log files after success |
| `--rebuild-profdata` | `run toolchain` | Force full 4-pass PGO build even if compatible profdata exists |
| `--auto-pgo` | `run toolchain` | Bypass all PGO confirmation prompts (required for non-interactive PGO runs) |
| `--cleansrc` | `build`, `update`, `fetch`, `run toolchain` | Purge each package's src dir and re-clone before fetching/building. Refuses (per package) on uncommitted changes, ahead-of-upstream commits, or `diverged_user` state |
| `--cleansrc-force` | `build`, `update`, `fetch`, `run toolchain` | Like `--cleansrc` but bypasses the dirty/diverged guard. Use when the upstream rewrote history (e.g. Arch packaging repos force-push every release) and the local commits have no value to preserve |

### Tags in use

**Core build subsystem** (`makepkg_wrapper.py` and related):

| Tag | Covers |
|---|---|
| `[ABI]` | ABI compatibility checks — soname comparison before and after build |
| `[BUILD]` | makepkg invocation, exit codes, patched PKGBUILD lifecycle |
| `[CACHE]` | ccache/sccache passive monitoring (per-build hit/miss delta, system probes) |
| `[CONF]` | Temp makepkg.conf generation, active consumes set |
| `[ENV]` | Env var routing; per-key shell strip (INFO); skipped keys not in active_consumes (INFO); unclassified profile key warnings (WARN) |
| `[FLAG]` | Flag-string transforms in `makepkg_flags` (linker detect/inject/replace, full-LTO & lld-flag strips, lib32 `-march` scrub). Conf-time flag adjustments (CLI `--cc`/`--cxx`/`--ld`, linker guard) log under `[CONF]` (P2b.6a); profile append-merge flag logging (conflict-group firing, token replacement) moved to `[PROFILE]` (P3.1) |
| `[GIT]` | Local git plumbing in `git_ops` — fetch/compare, dirty detection, safe purge (split out of `aur` in P2e). The build orchestrator's own source-sync result logs under `[BUILD]` (P2b.6c) |
| `[BUILD_PREP]` | Pre-build source acquisition — pkgctl checkout + validpgpkey import (`build_prep`; split from `[BUILD]` in P3.2) |
| `[KERNEL]` | Kernel stage: lsmod snapshot, kconfig fragment, build, post-install. The build orchestrator's kernel `LLVM=1` injection logs under `[BUILD]` (P2b.6c) |
| `[MAKEPKG]` | makepkg subprocess invocation + sudo-timeout retry: build status, inherited-shell-env scrub, toolchain-mismatch note, retry prompts (P2b.6b) |
| `[PATCH]` | PKGBUILD flag extraction, patching, artifact lifecycle; noninteractive kconfig target replacement |

**`[FLAG]` coverage (by design, partial).** Emitted for: CLI toolchain overrides (`--cc`, `--cxx`, `--ld`), linker token replacement and injection, linker guard stripping, RUSTFLAGS linker reconciliation, GCC thin-LTO rewrite, GCC+lld LTO disabling. Profile-side append-merge logging — conflict-group firing (group name, evicted tokens, inserted token) and prefix-match token replacement during `merge_extends` — moved to `[PROFILE]` in P3.1. Not emitted for: `apply_patch_pkgbuild` token changes (those use `[PATCH]`).

`[PGO]` was retired in P2b.6c: PGO build narration now logs under `[BUILD]` (build path) and `[TOOLCHAIN]` (toolchain stage), with "PGO"/"profdata" carried in the message text rather than a dedicated tag.

**Profile / config subsystem:**

| Tag | Covers |
|---|---|
| `[CONFIG]` | Config file loading (`profiles.toml`: flag profiles, conflict groups, consumes inference) |
| `[PROFILE]` | Profile resolution, rule matching, extends chain, group resolution, consumes inference, and append-merge — the per-facet `[CONF]`/`[FLAG]`/`[GROUPS]` loggers collapsed into one `[PROFILE]` in P3.1 |
| `[STATE]` | Pipeline state directory resolution |

**AUR / package management:**

| Tag | Covers |
|---|---|
| `[AUR]` | AUR name cache lifecycle, clone operations, and RPC queries (the separate `[MANIFEST]` tag collapsed into `[AUR]` in P3.5) |
| `[AUR_RESOLVE]` | Transitive AUR dependency-graph resolution / build order (`aur_resolve`; split from the `resolve` verb's `[RESOLVE]` in P3.3) |
| `[DEP]` | Soname dependency graph checks |
| `[DOCTOR]` | `sysforge doctor` — installed-package depends + linkage health check (was `[DOC]` before P3.4) |
| `[FAILURE]` | Failure scenario dispatch |
| `[GFX]` | `graphics_probe` — system-state graphics checks (kernel params, compositor protocols, driver skew) |
| `[PACMAN]` | pacman database and install operations |
| `[PROV]` | `provides_lookup` — reverse soname → package via `pacman -Fq` |
| `[VERSION]` | Package version comparison |

**Pipeline stages:**

| Tag | Covers |
|---|---|
| `[BASE_INSTALL]` | Stage 2: pacstrap, genfstab |
| `[CONFIGURE]` | Stage 4: hostname, locale, bootloader, user, services |
| `[HARDWARE]` | Stage 3: CPU/GPU/NVMe detection |
| `[PACKAGES]` | Stage 7: package build progress |
| `[PARTITION]` | Stage 1: GPT partitioning, mkfs, mount |
| `[PIPELINE]` | Stage sequencing, checkpoint events |
| `[RECONFIGURE]` | Stage 5: pre-build checkpoint and config review |
| `[TOOLCHAIN]` | Stage 6: LLVM/GCC build, PGO pass orchestration |

**Commands:**

| Tag | Covers |
|---|---|
| `[CLI]` | CLI entry point (invocation logging). The `build` verb logs under `[BUILD]` like the rest of the build subsystem (P3.3) |
| `[ENV_CHAIN]` | `sysforge env` — OS environment-inheritance chain snapshot (distinct from `[ENV]` build-env routing; P3.3) |
| `[FETCH]` | `sysforge fetch` — PKGBUILD download/update |
| `[REVIEW]` | PKGBUILD review gate (`primitives/pkgbuild_review.py`) — source-change diff prompt before building |
| `[UPDATE]` | `sysforge update` — version check, toolchain-variant + flag drift (Phase 4.3, via the `flag_drift` primitive), and rebuild |

---

## Man Pages

**Current — scdoc hybrid.** `man/sysforge.1` is rendered from a hand-written scdoc template plus auto-generated per-command sections:

```
tools/gen_options.py --template man/sysforge.1.scd.in --out man/sysforge.1.scd
scdoc < man/sysforge.1.scd > man/sysforge.1
```

- **`man/sysforge.1.scd.in`** (committed) — hand-written NAME / SYNOPSIS / DESCRIPTION / GLOBAL OPTIONS / FILES / ENVIRONMENT / EXIT STATUS / EXAMPLES / SEE ALSO / AUTHORS prose, with an `@OPTIONS@` marker where the generated COMMANDS sections splice in. scdoc syntax gotcha: indented continuation lines must not start with a table-control character (`[`, `|`, `]`) — scdoc errors with "Tables cannot be indented".
- **`tools/gen_options.py`** — walks `cli._build_parser()`'s subparser tree depth-first (so `packages add`, `run kernel`, etc. each get a `## <name>` section), emits a synopsis line plus one definition block per positional/option, escapes scdoc formatting characters in help text, and performs the splice itself (no sed). Subparsers registered without `help=` (the internal `completions` data sink) are excluded. Each command section also gets a `*Configuration:*` / `*Environment:*` trailer from the hand-maintained `_VERB_CONFIG` dict at the top of the script (qualified command name → config files / env vars consumed; commands without an entry get no trailer). The FILES and ENVIRONMENT sections of the template carry the inverse index ("Read by: …") — when a verb gains or loses a config source, update both `_VERB_CONFIG` and the template. The trailer is man-page-only by design: it is not wired into argparse epilogs, so `--help` output stays unchanged.
- **`man/sysforge.1.scd`** — intermediate, gitignored. **`man/sysforge.1`** — committed, so AUR-built tarballs ship the page without build-time tooling; the PKGBUILD `package()` installs the committed file directly and needs no man-page makedepend.
- Makefile target: `make man` (pins `COLUMNS=80` so any argparse-derived wrapping is deterministic). `scdoc` is a dev-machine dependency only (installed by `make dev`); `python-argparse-manpage` is no longer used anywhere.
- Release gate: the `manpage` group in `tools/check_shipped.py` reruns the exact same two-step pipeline into temp files and diffs against the committed page (`.TH` date header normalised), so option-help drift in `cli.py` without a `make man` commit blocks the release.

This gives hand-crafted prose with OPTIONS that stay automatically in sync with the CLI — editing flag help in `cli.py` and running `make man` is the whole workflow.

---
## Hardware Detection

Pipeline stage 3. Probes the running system via `/proc/cpuinfo` and `lspci`, emits `hardware_profile.toml` to `state_dir`. The file feeds kconfig automation (kernel stage) and is shown in the reconfigure config review.

**Detections and kconfig output:**

| Hardware | Detection | kconfig |
|---|---|---|
| AMD Zen 3 (family 25, model 33/80/68/24) | `/proc/cpuinfo` | `CONFIG_MZEN3 = y` |
| AMD Zen 4 (family 25, model 97/116/117) | `/proc/cpuinfo` | `CONFIG_MZEN4 = y` |
| AMD Zen 5 (family 26) | `/proc/cpuinfo` | `CONFIG_MZEN5 = y` |
| AMD CPU (family ≥ 25) | `/proc/cpuinfo` | `CONFIG_X86_AMD_PSTATE = y` |
| NVIDIA GPU | `lspci` | `CONFIG_DRM_NOUVEAU = n` |
| NVMe storage | `lspci` | `CONFIG_BLK_DEV_NVME = y` |

Unknown AMD CPU models get `CONFIG_X86_AMD_PSTATE` but no `CONFIG_MZEN*` entry — the kernel defaults to `CONFIG_GENERIC_CPU`.

**LLVM target derivation.** The hardware stage also writes `host_arch` (from `uname -m`) and an autodetected `llvm_targets` list — CPU backend from arch (`x86_64`→`X86`, `aarch64`→`AArch64`, `armv7l`→`ARM`, `riscv64`→`RISCV`, `ppc64le`→`PowerPC`) plus GPU backends from `gpu_vendors` (`amd`→`AMDGPU`, `nvidia`→`NVPTX`; `intel` contributes nothing because the Mesa Intel drivers don't depend on an LLVM backend). **Plus a mandatory `AMDGPU` baseline (`_SYSTEM_LIBLLVM_CONSUMER_TARGETS`) on every recognised arch — even nvidia/intel-only hosts:** the *system* `mesa` package links the `AMDGPU` (radeonsi) and host-CPU (llvmpipe) target-init symbols from `libgallium` **unconditionally**, so a rebuilt system `llvm-libs` that dropped `AMDGPU` leaves mesa with `undefined symbol: LLVMInitializeAMDGPU…` and bricks every EGL/GL consumer (the whole desktop). An unrecognised arch yields an empty list — "no filtering", i.e. upstream builds all targets, which is also safe for mesa. Consumed by `pkgbuild_patcher.patch_llvm_targets` when building any LLVM-toolchain package.

**The baseline is enforced at *resolution* time, not only at derivation.** `derive_llvm_targets` bakes `AMDGPU` into a freshly-derived list, but the actual build resolves `LLVM_TARGETS_TO_BUILD` from `hardware_profile.toml` (or an explicit `toolchain.toml [llvm] targets`) via `llvm_targets.resolve_or_detect_llvm_targets` — both of which *bypass* derivation. A profile cached before the baseline existed, or one a user hand-edited, would otherwise silently reintroduce a brick. So `resolve_llvm_targets`/`resolve_or_detect_llvm_targets` re-apply `_SYSTEM_LIBLLVM_CONSUMER_TARGETS` (via `_ensure_system_consumer_targets`) to **any** non-None, non-empty resolved set, from any source. The single opt-out is `[llvm] targets = []` ("build all", which already includes `AMDGPU`), which resolves to `None` and skips the enforcement. This is the layer the bricked-desktop regression slipped through: the fix had lived only in derivation while the build read the cached file.

**Mesa driver derivation (the meson analogue).** The hardware stage also writes `mesa_gallium_drivers` / `mesa_vulkan_drivers` from the same `gpu_vendors` (`derive_mesa_drivers`): `amd`→`radeonsi`/`amd`, `intel`→`iris,crocus`/`intel,intel_hasvk`, `nvidia`→`nouveau`/`nouveau`. These trim mesa's `-D gallium-drivers=all` / `-D vulkan-drivers=<every-driver>` (every ARM-SoC/mobile GPU mesa ships) down to what the host runs — a real build-time win when sysforge source-builds mesa. The invariant is the *inverse* of the LLVM `AMDGPU` one: where that guards against reducing *too little*, mesa's mandatory software baseline (`_MESA_MANDATORY_GALLIUM` = `llvmpipe`/`softpipe`/`zink`, `_MESA_MANDATORY_VULKAN` = `swrast`/lavapipe) guards against reducing *too much* — a build with no software renderer bricks headless/VM/GPU-reset-recovery sessions. The baseline rides every derived/resolved set, even a no-GPU host (which yields baseline-only). **Unlike LLVM filtering, mesa filtering is opt-in** (`[mesa] filter_drivers = true` in `sysforge.toml`, default off); resolution (`mesa_drivers.resolve_or_detect_mesa_drivers`) and baseline enforcement (`_ensure_mesa_software_baseline`) mirror the LLVM path, and a gallium reduction also intersects `gallium-rusticl-enable-drivers` with the built set (rusticl drivers must be a subset). Consumed by `pkgbuild_patcher.patch_mesa_drivers` (gated by `profile.is_mesa_pkgbase`) when building any mesa-family package; lib32-mesa **is** filtered (vendor- not arch-coupled, unlike lib32-llvm).

**`hardware_profile.toml` layout:**
```toml
[hardware]
cpu_vendor  = "AuthenticAMD"
cpu_family  = 25
cpu_model   = 33
host_arch   = "x86_64"
gpu_vendors = ["nvidia"]
llvm_targets = ["X86", "NVPTX", "AMDGPU"]  # AMDGPU always present (system mesa)
mesa_gallium_drivers = ["nouveau", "llvmpipe", "softpipe", "zink"]  # + software baseline
mesa_vulkan_drivers  = ["nouveau", "swrast"]                        # swrast = lavapipe baseline
nvme        = true

[kconfig]
CONFIG_MZEN3          = "y"
CONFIG_X86_AMD_PSTATE = "y"
CONFIG_DRM_NOUVEAU    = "n"
CONFIG_BLK_DEV_NVME   = "y"
# … plus arch-disable `=n` entries for every non-host kconfig domain
CONFIG_ARM64          = "n"
CONFIG_ARCH_QCOM      = "n"
# (and the rest of _ARCH_OWNED_KCONFIG minus the host's own domain)

[kconfig_devices]
# device-driven modular drivers for present devices, all "m" — see
# §Device-driven kconfig below
CONFIG_SND_HDA_INTEL  = "m"
CONFIG_IGC            = "m"
```

Written atomically (write-then-rename) to `<state_dir>/hardware_profile.toml`. The file has four readers:

- **`pipeline/stages/kernel.py`** — `_load_hardware_kconfig()` consumes `[kconfig]` and `[kconfig_devices]`; entries flow into the `sysforge.config` fragment merged into `.config` via `merge_config.sh` (precedence: manual `[[kconfig]]` > `[kconfig]` > `[kconfig_devices]`; the device table is gated by `kernel.toml device_kconfig`, default true). Absence is non-fatal (entries skipped with an INFO log).
- **`primitives/llvm_targets.py`** — `_read_hardware_targets()` consumes `[hardware] llvm_targets`; resolves the `LLVM_TARGETS_TO_BUILD` cmake arg injected by `pkgbuild_patcher.patch_llvm_targets`.
- **`primitives/mesa_drivers.py`** — `_read_hardware_drivers()` consumes `[hardware] mesa_gallium_drivers` / `mesa_vulkan_drivers`; resolves (opt-in, gated by `sysforge.toml [mesa] filter_drivers`) the `-D gallium-drivers=` / `-D vulkan-drivers=` meson options rewritten by `pkgbuild_patcher.patch_mesa_drivers`.
- **`pipeline/stages/reconfigure.py`** — surfaces the file in the pre-build config review so the user can hand-edit before kernel build.
- **`commands/doctor.py`** — consumes `[hardware] gpu_vendors` to scope the `doctor --graphics` health checks.

### Architecture-aware kconfig disable

In addition to the positive `=y` enables above, the hardware stage emits an `=n` line for every CONFIG_* key owned by a kernel architecture domain that is **not** the host's domain. The data lives in two module-level constants in `pipeline/stages/hardware.py`:

- `_ARCH_OWNED_KCONFIG: dict[str, frozenset[str]]` — `domain → set of CONFIG_* keys that only make sense when the kernel is targeting that domain`. Domains: `x86`, `arm` (32-bit), `arm64`, `riscv`, `powerpc`, `mips`, `sparc`, `loongarch`. Keys are **curated, not exhaustive** — top-level architecture umbrellas (`CONFIG_X86`, `CONFIG_ARM64`, …) plus the major SoC family umbrellas under `arm64` (`CONFIG_ARCH_QCOM`, `CONFIG_ARCH_TEGRA`, `CONFIG_ARCH_ROCKCHIP`, etc.). The Kconfig system itself gates most SoC drivers via `depends on ARCH_<vendor>`, so disabling the umbrella culls the subtree from `make nconfig` automatically.
- `_HOST_ARCH_TO_KCONFIG_DOMAIN: dict[str, str]` — `uname -m → domain`. Covers `x86_64`/`i686`/`i386` → `x86`, `aarch64` → `arm64`, `armv7l`/`armv6l` → `arm`, `riscv64`/`riscv32` → `riscv`, `ppc64le`/`ppc64`/`ppc` → `powerpc`, `mips`/`mips64` → `mips`, `sparc`/`sparc64` → `sparc`, `loongarch64` → `loongarch`.

`_arch_disable_kconfig(host_arch)` resolves the host domain, then iterates every *other* domain in the registry and emits `{CONFIG_X: "n"}`. Keys appearing in the host's own domain set are filtered out as a defensive guard (no clobber if a future kconfig key gains a presence in multiple domains). Unknown `host_arch` returns an empty dict and logs a WARN.

The `=n` entries land in the same `[kconfig]` table as the existing `=y` enables, so the kernel stage's existing merge path — `merged = {**device_kconfig, **hw_kconfig, **manual_kconfig}` — applies unchanged. A user cross-compiling or otherwise wanting an arch-disabled key re-enabled puts an explicit `[[kconfig]] option = "CONFIG_ARM64" value = "y"` in `kernel.toml`; the existing manual-override-wins-with-WARN behaviour in `_write_kconfig_fragment` extends to arch-disable entries.

### Device-driven kconfig (`[kconfig_devices]`)

The scalar `[kconfig]` heuristics cover CPU/GPU/NVMe; `[kconfig_devices]` covers everything else that is physically present. The stage takes the union of all enumerated devices' `suggested_kconfig` (see `device_probe.py` — modalias → expected module → `CONFIG_*`), subtracts any symbol the heuristic `[kconfig]` table already owns (so e.g. a nouveau-bound NVIDIA GPU can't re-enable the heuristic's `CONFIG_DRM_NOUVEAU = "n"`), and emits the rest as `"m"` — modular drivers don't load unless the hardware is present, so this is the safe default for device coverage.

The module→`CONFIG_*` resolution is two-layered: `device_probe`'s curated `_MODULE_TO_KCONFIG` table (vetted, always wins) plus the **kbuild map cache** (`<state_dir>/kbuild_module_map.json`, see §`kbuild_map.py`). The cache is harvested by the kernel stage's Gate 2 from the just-built source tree — the resolved `.config`'s parent is the version-exact tree, the only reliable place the kbuild Makefiles exist on disk (installed headers don't ship the nested driver Makefiles). The loop is self-improving: the first kernel build runs with curated-only coverage, Gate 2 caches the full tree-derived map, and every later hardware-stage run / fragment write resolves near-totally. The fold is consumed by the kernel stage's fragment merge at the lowest precedence (manual > hardware > device) and can be disabled wholesale with `kernel.toml device_kconfig = false`.

### Tested hardware scope

Design ambition is broad (every kconfig domain in the registry, every CPU/GPU brand the detection code recognises), but real-world validation is currently narrow. This section documents which paths have actually been exercised so users on untested hardware understand where they are taking implemented-but-unvalidated code paths.

**Tested on real iron** (the reference dev box):
- Host arch: `x86_64`
- CPU: AMD Ryzen 7 5800X3D — `AuthenticAMD` family 25 model 33 (Zen 3 / Vermeer)
- GPU: NVIDIA RTX 5070 (via `nvidia-open-dkms`)
- Storage: NVMe
- Distro: Arch Linux, kernel: custom `linux-custom` PKGBUILD

**Tested in VM** (`make vm-iso` → `make vm-install`):
- Host arch: `x86_64` (qemu/KVM guest)
- CPU: emulated/passthrough (typically host-passthrough)
- GPU: virtio (`gpu_vendors` likely empty or `["other"]`)
- Storage: virtio-blk or NVMe depending on VM config

**Implemented but never exercised against real hardware:**
- `host_arch ∈ {aarch64, armv7l, armv6l, riscv64, riscv32, ppc64le, ppc64, ppc, mips, mips64, sparc, sparc64, loongarch64}` — registry entries exist and are unit-tested, but no kernel has been built on any of these.
- Intel CPUs (`GenuineIntel`) — code path falls through to `CONFIG_GENERIC_CPU`, no Intel-specific CPU kconfig mapping exists.
- AMD CPUs older than Zen 3 — same fallback.
- AMD Zen 4 / Zen 5 — kconfig keys exist in `_AMD_CPU_KCONFIG` but the dev box predates them.
- Pure-AMD or pure-Intel GPU systems — Nvidia is the only GPU detection path exercised end-to-end.
- Non-NVMe storage (SATA, eMMC) — detection works (`_has_nvme` returns False), but no downstream `CONFIG_*` adjustment fires.

When a curated `=n` over-culls on an untested arch, the escape hatch is `kernel.toml [[kconfig]]` — adding the key back with `value = "y"` overrides the hardware-emitted disable per the existing merge semantics.

---

## Cache Management

### ccache and sccache

Configured via a `[cache]` table in `profiles.toml`:

```toml
[cache]
ccache  = "auto"   # auto | enabled | disabled
sccache = "auto"
```

`auto` — use if installed, skip silently if not.

Compiler invocation uses explicit absolute paths rather than ccache's symlink shim (incompatible with makepkg prepending `$srcdir` to `PATH`):

```
CC="ccache /usr/bin/clang"
CXX="ccache /usr/bin/clang++"
```

sccache wraps Rust via `RUSTC_WRAPPER=sccache` (env pass). `CARGO_INCREMENTAL=0` set globally for all Rust builds — incremental fingerprinting is unreliable with managed flags.

`cache = false` in `packages.toml` disables both caches for a specific package. Required for all PGO stages.

### Cache reporting

`[CACHE]` log tag emits passive monitoring lines at `[INFO]` level (visible at `-vv`):

- **Per-build:** ccache and sccache hit/miss deltas, bracketing each `makepkg` invocation. Hit rate % shown when compilations occur; "no compilations recorded" if delta is zero.
- **System probes (once per run):** ld.so cache mtime, pacman cache file count + size, ThinLTO cache dir size (extracted from `--thinlto-cache-dir=` in profile LDFLAGS).

`--cache-report` on both `build` and `pipeline` subcommands prints a structured per-package and totals summary to stderr at end of run, regardless of verbosity. This is the only user-visible output that ignores verbosity gating.

Cache probe uses `ccache --print-stats --format=tab` (ccache ≥ 4.0, standard on Arch) and `sccache --show-stats`. Both are skipped if the binary is absent.

### Toolchain fingerprint tracking

SysForge tracks a toolchain fingerprint (hash of compiler path + version) in its state dir. On mismatch (e.g. after LLVM version bump), `invalidate_on_toolchain_change` fires: `warn` (default) / `purge` / `ignore`.

---

## Graphics Stack Build Order

Build in this order to satisfy dependencies correctly:

1. **Stage 1 — LLVM**
   * PGO (64-bit): `llvm`, `llvm-libs`, `clang`, `lld`
   * Non-PGO (64-bit): `polly`, `compiler-rt`, `openmp`, `spirv-llvm-translator`
   * Non-PGO (lib32): `lib32-llvm`, `lib32-llvm-libs`, `lib32-clang`, `lib32-spirv-llvm-translator`
2. `vulkan-headers-git`
3. `vulkan-icd-loader-git`, `lib32-vulkan-icd-loader`
4. `mesa-git`, `lib32-mesa-git`
5. `egl-wayland`, `lib32-egl-wayland`, `xwayland-git`
6. `libinput-git`
7. COSMIC git packages
8. `xwayland-satellite`

**Reduced LLVM targets must keep `AMDGPU`.** When the toolchain stage rebuilds Stage-1 `llvm`/`llvm-libs` with a reduced `LLVM_TARGETS_TO_BUILD` (see §Hardware detection → *LLVM target derivation*), the set **must** include `AMDGPU` even on nvidia/intel-only hosts. Stage-4 `mesa` links the `AMDGPU` (radeonsi) and host-CPU (llvmpipe) target-init symbols from `libgallium` **unconditionally**; a system `libLLVM` missing them fails to load with `undefined symbol: LLVMInitializeAMDGPU…`, taking down every EGL/GL client (`cosmic-comp`, the greeter — the whole desktop), with healthy kernel/KMS still presenting as a black screen. `hardware.derive_llvm_targets` guarantees this via `_SYSTEM_LIBLLVM_CONSUMER_TARGETS`, and `llvm_targets.resolve_or_detect_llvm_targets` re-asserts it at resolution time so a stale/edited `hardware_profile.toml` can't drop it (see §Hardware detection → *LLVM target derivation*).

**Defense in depth (the desktop must never black-screen from a toolchain rebuild).** Three layers back the rule above: (1) **prevent** — the resolution-time AMDGPU baseline; (2) **catch pre-install** — toolchain **Gate 2** (`toolchain_safety.check_system_consumer_symbols`) `ldd -r`-diffs the freshly-built `libLLVM` against installed mesa consumers (`libgallium`/DRI/Vulkan) and aborts *before* `pacman -U` if any `LLVMInitialize*@LLVM_x.y` symbol they import would go missing — the live graphics stack is untouched; (3) **verify post-install** — **Gate 3** re-runs the diff against the now-installed libLLVM and triggers the snapshot auto-rollback on a miss (see §`pipeline-layer` → toolchain gates). Separately, `sysforge doctor --graphics` surfaces the same fact (`graphics_probe._check_mesa_llvm_symbols` → `check_installed_consumer_symbols`) so a system already in this state self-diagnoses in one line instead of presenting only as a black screen.

---

## Release Process

- **GitHub:** public from day one; source of truth for all code.
- **Per-release change history** lives in `docs/release-notes/vX.Y.Z.md` (see *Release notes* below). This section documents the *process* that cuts a release, not the history of past ones.

### AUR publishing process

Releases are driven by three Makefile targets — `make release-major`, `make release-minor`, `make release-patch` — each of which calls `tools/release.sh --bump=<level>`. The script handles the full flow end-to-end with a single up-front summary + approval prompt and one mid-run pause for the manual tag push. Phases:

1. **Bump, commit, tag.** Rewrites `pyproject.toml` (the single source of truth for version), `PKGBUILD` `pkgver=`, `PKGBUILD-git` `pkgver=` (leading `X.Y.Z` only — the `.r0.g0000000` suffix is preserved as the placeholder for the dynamic `pkgver()`), the `<!--version-->vX.Y.Z<!--/version-->` markers in `README.md` and `DESIGN.md`, regenerates `uv.lock` (via `uv lock`) and `man/sysforge.1` (via `make man`), then makes a single `release: vX.Y.Z` commit (which also includes `docs/release-notes/vX.Y.Z.md` — see *Release notes* below) and tags it.
2. **Push pause.** Prints `git push origin main && git push origin vX.Y.Z` and waits on ENTER. The user pushes manually (releases are deliberate, not background events). The script verifies the tag is on `origin` before continuing.
3. **Post-tag artifacts.** Fetches the GitHub tarball sha256, updates `sha256sums=` in `PKGBUILD`, **validates both `PKGBUILD` and `PKGBUILD-git` in a clean chroot**, regenerates `.SRCINFO` and `.SRCINFO-git` (both gitignored — local artifacts only), and makes a second `release: vX.Y.Z sha256` commit (the `.SRCINFO` files do not get committed).
4. **Final instructions.** Prints `git push origin main`, the `git clone`/`cp`/`commit`/`push` sequence for the `sysforge` and `sysforge-git` AUR repos, and the `gh release create vX.Y.Z --notes-file docs/release-notes/vX.Y.Z.md` command for the GitHub release. The user runs those manually.

**Release notes.** Every release ships curated notes at `docs/release-notes/vX.Y.Z.md` (sections: Highlights / Breaking changes / Fixes / Internal — see `docs/release-notes/v2.0.0.md` for the format). The pre-flight in `tools/release.sh` **hard-fails** when the notes file for the target version is missing, mirroring the check-shipped/check-personal/check-design gates; Phase 1 commits the file as part of the `release: vX.Y.Z` commit, and Phase 4 prints the `gh release create` command that publishes it. Notes are drafted from `git log <last-tag>..HEAD` plus this section's framing — inside a Claude Code session the `/release-notes` repo skill does this (a hookify rule reminds before any `make release-*` invocation); outside one, write the file by hand.

If interrupted between phases (Ctrl-C at the push pause, or a transient failure), re-running the same `make release-*` command resumes correctly: the script detects that the tag for the *current* `pyproject.toml` version already exists at HEAD and skips Phase 1.

The version markers in `README.md` and `DESIGN.md` wrap the single live version token (`<!--version-->vX.Y.Z<!--/version-->`); only it rotates per release. Each document must carry exactly one such marker. The `versions` check group below enforces lockstep across all marker locations; release pre-flight refuses to run if the markers (or the `pkgver` lines, or any other version-bearing field) are out of sync.

**Shipped-file pre-release checks.** Phase 1 of `tools/release.sh` (and the standalone `make check-shipped` / `make pre-release` targets) gate on `tools/check_shipped.py`, which validates every artifact the PKGBUILD ships:

- **`configs`** — every `etc/sysforge/*.toml` is parsed through its real runtime loader (`load_config`, `load_sysforge_toml`, `load_bootstrap`, the stage `_load_*` helpers); unknown top-level sections/keys against a per-file allowlist (`_KNOWN_SECTIONS` / `_KNOWN_TOP_KEYS`) are an error; missing `tests/data/etc/sysforge/` counterpart for every shipped TOML (except per-host `bootstrap.toml`) is an error. **Fixture↔shipped key-inventory lockstep** (`_check_fixture_lockstep`): for the flat stage/global configs (`_LOCKSTEP_FILES` = `kernel.toml`, `toolchain.toml`, `sysforge.toml`), the *set* of documented keys (active assignments + commented `# key =` examples + section headers) must match between the shipped file and its fixture — values may differ, the key set may not. This is the only cross-check tying the tracked fixtures to shipped reality now that the personal live config is fully decoupled; the rich-body configs (`packages.toml`, `profiles.toml`) are excluded because their fixtures legitimately carry test-specific `[[package]]` / profile bodies. The complementary **allowlist↔stage-code parity** guard lives in `tests/test_check_shipped.py` (`TestAllowlistCodeParity`): the allowlist must equal the keys each stage actually reads via `kernel_cfg.get(...)` / `tcfg.get(...)` (helper-resolved keys like `pgo_store` accounted for), and every read key must be documented in the shipped file — this is what catches a new config key that the code reads but nobody allowlisted or documented (the `base_config` class of regression).
- **`pkgbuild`** — every `install -Dm…` source in `PKGBUILD` must exist in the working tree; every `$pkgdir/etc/…` install target must be declared in `backup=()`, and vice versa (no stale `backup=` entries); `sha256sums` is not a placeholder (`SKIP`, all-zero, `DRYRUN…`).
- **`pkgbuild_parity`** — `PKGBUILD` and `PKGBUILD-git` parse to the same dict (via `pkgbuild_meta.parse_pkgbuild`) except for a tightly-scoped allowlist of keys that are *supposed* to differ (`pkgname`, `pkgver`, `pkgrel`, `pkgdesc`, `source`, `sha256sums`, `conflicts`, `provides`). `depends` / `makedepends` / `optdepends` / `backup` arrays must be byte-identical.
- **`hooks`** — every `etc/pacman.d/hooks/sysforge-*.hook` `Exec` line must invoke `tools/pacman-hook-helper.sh` and pass a subcommand the helper documents (`kernel`, `toolchain`, `buildstate`).
- **`completions`** — every verb and every long-flag in the argparse parser tree (reached via `sysforge.cli._build_parser`) must appear in both `completions/_sysforge` and `completions/sysforge.bash`; stale top-level verb entries in the zsh case statement (function-suffix matches case-word but parser doesn't know the verb) are an error. Mirrors the `completions-cli-parity` subagent's audit; this is the mechanical layer that runs every release.
- **`versions`** — `pyproject.toml` `[project] version` must equal `PKGBUILD` `pkgver=`, the leading `X.Y.Z` of `PKGBUILD-git` `pkgver=`, and every `<!--version-->vX.Y.Z<!--/version-->` marker in `README.md` and `DESIGN.md` (literal `vX.Y.Z` placeholder strings in prose are filtered out by the `\d+\.\d+\.\d+` constraint).
- **`manpage`** — regenerates `man/sysforge.1` via the scdoc-hybrid pipeline `make man` uses (`tools/gen_options.py` splices the argparse-derived COMMANDS sections into `man/sysforge.1.scd.in`, then `scdoc` renders) into temp files and diffs against the committed page; any difference is an error, with the fix `make man && git add man/sysforge.1`. The `.TH … "DATE"` header is normalised before diffing so the daily date change isn't a finding. Skipped with a `warn` if `scdoc` isn't on PATH. See the Man Pages section.

Findings default to hard fail (non-zero exit); pass `--warn` for a report-only mode. The `--check=<group>` flag scopes the run (repeatable). The script accepts `--repo=<path>` so the tests in `tests/test_check_shipped.py` can point it at synthetic trees in `tmp_path` to verify each drift case still fires.

**Clean chroot validation.** The release gate catches underspecified `depends`/`makedepends` that a host build would silently accept because the dep is already installed. It uses `makechrootpkg` from `devtools` and is a hard prerequisite for the release flow — not the everyday `sysforge build` path, which remains a direct host-side `makepkg` invocation for speed.

One-time setup on the release machine:

```bash
sudo pacman -S --needed devtools
sudo mkarchroot /var/lib/archbuild/extra-x86_64/root base-devel
```

Per-release, the script runs (for each of `PKGBUILD` and `PKGBUILD-git`, in a tmpdir so no build artifacts land in the working tree):

```bash
makechrootpkg -c -u -r "$SYSFORGE_CHROOT"
```

- `-c` snapshots a clean copy of the root chroot for every build, so state from a prior release cannot leak in.
- `-u` updates the root chroot against current `core`/`extra` before the snapshot, so validation runs against what an AUR user will actually hit.
- The build never installs to the host; the release machine stays clean.
- A missing or empty `*.pkg.tar.zst` in the build tmpdir is a hard failure.

Escape hatches:

- `--skip-chroot` — bypass the chroot gate when iterating on `release.sh` itself. Never use for a real publish.
- `SYSFORGE_CHROOT` — override the chroot root (default `/var/lib/archbuild/extra-x86_64`) for CI or VM release runs.
- `--dry-run` — walk through every step without writing files, committing, hitting the network, or running the chroot build. Implies `--skip-chroot`.

`makechrootpkg` bind-mounts require root; the script assumes passwordless sudo is configured for it and fails fast with a clear message otherwise.

---

## Drift detection

`sysforge update` is the primary drift surface — it detects **two** drift axes (version and flag), with `sysforge doctor` completing the picture for ABI drift:

**`sysforge update [PKG ...]`** (implemented) — handles **version drift** and **flag drift**.

*Version drift.* After the `source_sync` scheduler refreshes each PKGBUILD dir (one batched AUR RPC call followed by per-package clone or shallow fetch as needed), it compares the new `pkgver`/`pkgrel`/`epoch` against the installed version via `vercmp`. Packages where the PKGBUILD is newer are rebuilt with the current profile. VCS packages (`-git`/`-svn`/`-hg`/`-bzr`) are reported as `DEVEL` and skipped by default; with `--devel`, each VCS package's `pkgver()` is resolved up-front via `vcs_pkgver.evaluate_vcs_pkgver` (one `makepkg --nobuild` pass per VCS pkgbase) and the resulting version is vercmp'd against installed — only genuinely-stale packages are rebuilt; up-to-date packages are reported as `UP_TO_DATE` and skipped. Resolution failures (broken `pkgver()`, transient network) report `DEVEL_EVAL_FAILED` and are also skipped. One or more package names may be given as positional arguments to restrict the run to a subset of sysforge-managed packages; unrecognised names are warned and skipped.

*Flag drift.* Same package version but a different resolved compiler configuration — e.g. profile changed, new flag added, or build mode switched. At build time the resolved flags string is recorded per package in `build_state.toml`; Phase 4.3 re-resolves the current profile for each profiled package and diffs the result against the stored string (via `primitives/flag_drift.resolve_flag_drift`). Flag drift is **reported by default** and is network-free (`--offline --dry-run` gives a read-only report); `--rebuild-on-flag-drift` rebuilds the drifted packages, and `--rebuild-on-drift` is the umbrella over both the flag and toolchain-variant axes. See §`update.py` → *Phase 4.3 — Flag drift*.

**PGO toolchain packages** (`build_mode = "pgo_llvm_toolchain"`) are handled specially during update. `makepkg_wrapper.run()` reads `toolchain.toml → pgo_store`, checks for a saved `clang.profdata` and its `clang.profdata.version` sidecar, and compares the sidecar's LLVM major version against the PKGBUILD's `pkgver` major. If they match, `-fprofile-use=<profdata>` is injected and the build proceeds as a PGO-optimised build. If profdata is absent or version-mismatched (e.g. after a major LLVM bump), the user is prompted: **[p]lain build or [s]kip (default: skip)**. In non-interactive mode the build is skipped automatically. Skipped packages are counted separately in the update summary and do not count as failures. To rebuild profdata after a major version bump, run `sysforge run toolchain`. The toolchain stage itself also reuses compatible profdata — see the **Profdata reuse** section under stage 6.

**Stale-profraw post-build check.** After every non-PGO-managed build, `makepkg_wrapper.run()` globs `pgo_store` for `*.profraw` files. Any file with `mtime >= build_start - 1s` is treated as **fresh** — it was written by the build just completed, which means an instrumented LLVM is still installed on the system and the build was leaking profile data. The wrapper fatals, telling the user to reinstall `llvm`/`llvm-libs` or run `sysforge run toolchain`. Files strictly older than `build_start` are **orphans** left behind by a prior failed or partial toolchain run whose instrumented binaries the user has since cleaned up; these are unlinked in place and an info line is logged. The split makes the safety net self-healing: once the system is clean, the next build purges the residue automatically instead of requiring manual cleanup of `pgo_store`.

Build-state-wide flag-drift coverage (profiled entries outside the walk) is handled by Phase 4.3's fold — detect/report-only, with a `sysforge build` hint for rebuilds (see §`update.py` → *Phase 4.3 — Flag drift*).

`build_state.toml` is the shared source of truth for both drift axes. Written by `makepkg_wrapper.run()` after each successful build.

**`sysforge doctor`** completes the picture — it is read-only and catches the drift class the others don't: **ABI / linkage drift** on already-installed packages, e.g. a partial graphics-stack rebuild leaving `steam` linked against a `libfoo.so.N` that the system no longer exposes. See the `doctor.py` subsection for the full algorithm. Together: `update` → version + flag drift, `doctor` → ABI drift.

DAG stages are categorised as **bootstrap-only** (partition, base_install, configure) or **repeatable** (hardware, reconfigure, toolchain, packages, kernel). Only repeatable stages participate in drift-driven rebuild runs. `hardware` is repeatable because re-detecting after a hardware change (e.g. GPU swap) is safe and needs no root.

---

## Known Gaps

Implemented behaviour that is incomplete or has known limitations. These are not deferred features — they are holes in currently active code.

**`sysforge update` tracks every package sysforge source-built (build_state authority); `repo_mode = "profiled"` additionally surfaces drift for *unbuilt* repo packages.** `sysforge update` walks the union of: every installed AUR package (`pacman -Qm`); every package sysforge source-built (build_state `build_mode != "pacman"`, classified `repo_class = "source"` for repo origins) — so `sysforge build mesa` is durable, rebuilt from source on every update; any repo package whose override sets a behavior-changing field (`pkgbuild_patch`, `cache`, `reason`); and, with `repo_mode = "profiled"`, every remaining installed repo package. Source-built / overridden entries go through `pkgctl repo clone` (via `source_sync._sync_one` calling `pkgctl_checkout` on first visit and `git_fetch_and_compare` on subsequent runs, with a clean-tree hard-reset to upstream when the local clone diverges) and into the source-build loop. The remaining unbuilt, unmodified repo packages (`repo_class = "pacman"`, only present under `repo_mode = "profiled"`) take a fast path: one batched `checkupdates` call (`primitives.pacman.checkupdates_map`) resolves their pending-upgrade versions in a single subprocess; vercmp against the installed version emits `NEEDS_PACMAN_UPGRADE`; one terminal `sudo pacman -Syu` after Phase 6 (install) does the actual upgrade. This split is what makes "track every installed package" tolerable on a maintained workstation — without it, every repo package would mean an individual `git fetch` against the Arch packaging tree on every update run. The post-install ordering matters: source-built artifacts hit the system first so the `IgnoreGroup = sf-build` line added by `sysforge setup` protects them when `pacman -Syu` runs. If `checkupdates` is missing (no `pacman-contrib`), pacman-class packages report `SKIPPED_NO_CHECKUPDATES` and no `pacman -Syu` is dispatched. **Remaining limitation:** a repo package installed via plain `pacman -S` (never built by sysforge, no override) is not source-tracked unless you build it once (`sysforge build <pkg>`), add an override, or set `repo_mode = "profiled"`. `repo_mode` also governs the packages-stage bootstrap build path; one key, two surfaces.

**`sysforge build` already routes repo packages through `pkgctl_checkout` automatically.** `find_pkgbuild` (`primitives/config.py:91`) checks `is_repo_package()` before AUR-clone fallback, so `sysforge build firefox` Just Works for any repo package — no `repo_mode` plumbing required on the build side.

**`repo_mode = "profiled"` is the canonical repo-handling key.** The `[build] repo_mode = "pacman" | "profiled"` setting in `packages.toml` is parsed and honoured by `run packages` / `run pipeline` (repo packages with `repo_mode = "profiled"`, or per-package `pkgbuild_patch = true`, are built from source via `_build_aur()` using `find_pkgbuild` → `pkgctl_checkout`) and at steady-state by `sysforge update` (where `repo_mode = "profiled"` pulls every installed repo package into the bulk drift-surfacing walk — distinct from build_state, which independently tracks whatever sysforge has already source-built). `sysforge build` consults `find_pkgbuild` independently.

**`[env_precedence]` config table — design cancelled.** The original design proposed a priority stack (wrapper profile = 100, makepkg.conf = 80, shell passthrough = 20, PKGBUILD export = 10) and an `[env_precedence]` TOML table to configure it. This design is superseded. The current model is simpler and more predictable: build tool vars (`CC`, `CFLAGS`, `LDFLAGS`, etc.) are stripped from the inherited shell env in `invoke_makepkg` before makepkg runs — the temp conf is the sole authority for all makepkg-managed keys. Shell env bleed-through is not a configurable priority; it is prevented entirely. SysForge bootstrap vars (`SYSFORGE_STATE_DIR`, `SYSFORGE_CONFIG_DIR`) are exempt — they are SysForge's own interface, not build tool vars, and are not stripped. The `[env_precedence]` table will not be implemented.

---

## Roadmap

Forward-looking enhancements that build on existing infrastructure. Each is a candidate, not a commitment, and none is required for current functionality. Shipped work is recorded in `docs/release-notes/`, not here.

- **Rule priority auto-calculation** — auto-calculate a baseline specificity score from rule conditions (mirrors CSS specificity: more AND'd conditions = higher weight), with manual `priority` override for ties. Deferred until enough real rules exist to validate whether auto-priority causes ordering problems in practice.
- **Configure stage additions** — btrfs snapshot before build runs, ccache/sccache initialisation check, estimated build time heuristic.
- **Graphics runtime debugging refinement** — tighten the graphics/doctor diagnostics surface (exact scope TBD). A candidate when revisiting graphics-related code; not blocking.
- **System maintenance scope expansion** — grow sysforge beyond build/package management into a unified system-maintenance helper: track and manage user-owned system artifacts that currently live ad-hoc across `~/scripts`, `/etc/systemd/system/`, `/etc/pacman.d/hooks/`, etc. Candidate primitives: inventory of tracked files, source-of-truth dir under repo control, install/sync command, drift detection vs filesystem, integration with the existing config/profile/manifest layers.
## Standards & Specifications

SysForge commits to a set of external specifications so that its on-disk
footprint, CLI behaviour, data formats, and packaging are predictable,
portable, and interoperable with the wider Linux/Arch ecosystem. This section
is the **canonical list**. Every change that touches paths, the CLI surface,
output, versioning, encoding, or packaging is expected to cross-check the
relevant standard here; the release gate (`make check-standards` plus the
behavioural `tests/test_standards_compliance.py`) enforces the mechanically
checkable subset.

Status legend: **enforced** = a check/lint/test guards it · **followed** =
adhered to, partially or fully guarded · **target** = adopted, gap being closed.

### Committed standards

| # | Standard | Scope | Status | How it is enforced |
|---|----------|-------|--------|--------------------|
| 1 | [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/) | User dirs (`~/.config`, `~/.cache`, `~/.local/state`) | enforced | `primitives/paths.py` (`_xdg_base`); `check_standards` `paths` group; `tests/test_paths.py` |
| 2 | Filesystem Hierarchy Standard + systemd `file-hierarchy(7)` | System roots (`/etc`, `/var/lib`, `/var/cache`, `/run`) | enforced | `paths.py` (`CONFIG_BASE`), `pipeline/state.py`, `makepkg_pgo.py`; `check_standards` `paths` group |
| 3 | [Semantic Versioning 2.0.0](https://semver.org/) | Project version scheme | enforced | `tools/check_shipped.py` `versions` group (format + cross-file parity) |
| 4 | POSIX Utility Conventions + GNU long-options | CLI argument grammar (`-h/--help`, `-V/--version`, `--`) | followed | argparse in `cli.py`; `tests/test_standards_compliance.py` |
| 5 | [NO_COLOR](https://no-color.org/) + `FORCE_COLOR` | Terminal colour control | enforced | `log.use_color()` (single authority); `tests/test_standards_compliance.py` |
| 6 | stdout/stderr separation + exit-code contract | CLI behaviour (data→stdout, diagnostics→stderr; 0/1/2) | followed | `log._out()`, `verbs/runner.py`; `tests/test_standards_compliance.py` |
| 7 | [TOML 1.0.0](https://toml.io/en/v1.0.0) | Config + state file format | followed | `tomllib` everywhere; `check_shipped` `configs` group |
| 8 | RFC 3339 / ISO 8601 (UTC) | Timestamps in state files | followed | central `_now_iso()` helpers; `tests/test_standards_compliance.py` |
| 9 | UTF-8 | Text file encoding | enforced | explicit `encoding="utf-8"`; `check_standards` `encoding` group (ruff `PLW1514 --preview` is the one-shot fixer) |
| 10 | PEP 517 / 518 / 621 / 508 | Python packaging metadata | followed | `pyproject.toml` (hatchling backend, `[project]` table) |
| 11 | `PKGBUILD(5)` · `.SRCINFO` · `alpm-hooks(5)` · `makepkg.conf` | Arch packaging artefacts | enforced | `pkgbuild-spec-check`/`pkgbuild-edit` skills; `check_shipped` `pkgbuild`/`hooks` groups |
| 12 | `man-pages(7)` via scdoc | Manual page | enforced | `make man`; `check_shipped` `manpage` group |
| 13 | [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) | Release notes | enforced | `docs/release-notes/vX.Y.Z.md` category vocabulary; `check_standards` `changelog` group |
| 14 | [REUSE](https://reuse.software/) / SPDX (license: **MIT**) | Per-file licensing | enforced | SPDX headers + `LICENSES/MIT.txt` + `REUSE.toml`; `check_standards` `spdx` group (`reuse lint`) |
| 15 | Reproducible builds | Builds SysForge produces | followed | does not strip reproducibility OPTIONS / honours `SOURCE_DATE_EPOCH`; `tests/test_standards_compliance.py` |

### Notes on selected standards

**XDG / FHS (1, 2).** User-side roots resolve through `_xdg_base(env, default)`
in `paths.py` — config under `$XDG_CONFIG_HOME`, regenerable cache under
`$XDG_CACHE_HOME`, fallback runtime state under `$XDG_STATE_HOME`. System state
lives at `/var/lib/sysforge` (FHS application state) with the XDG state dir as a
non-root fallback; the regenerable PGO profdata cache lives at
`/var/cache/sysforge` (override: `SYSFORGE_PGO_STORE`). See **Config Layer** and
**Directory Structure**.

**SemVer (3).** Versions are strict `X.Y.Z`; the `-git` package carries the
`X.Y.Z.rN.gHASH` VCS suffix. `make release-{major,minor,patch}` is the only
bump path and keeps `pyproject.toml`, `PKGBUILD`, `PKGBUILD-git`, and the
`<!--version-->` doc markers in lockstep.

**Keep a Changelog (13).** `docs/release-notes/vX.Y.Z.md` *is* the changelog
(there is no separate top-level `CHANGELOG.md` to drift). Entries use the Keep a
Changelog category headings: `Added`, `Changed`, `Deprecated`, `Removed`,
`Fixed`, `Security`.

**REUSE / SPDX (14).** SysForge is MIT-licensed (`LICENSE`). First-party source
files carry per-file SPDX headers (a copyright tag plus the `MIT` license
identifier); generated/data files are covered in bulk by `REUSE.toml`; license
texts live under `LICENSES/`. `reuse lint` (when installed) is the authoritative
check, with a header-presence grep fallback.

**Reproducible builds (15).** SysForge must not *undermine* the reproducibility
of packages it builds: it does not inject non-deterministic data, preserves
reproducibility-relevant `OPTIONS`, and passes `SOURCE_DATE_EPOCH` through to the
build environment unmodified.

### Adding or changing a standard

This list has one home — this file. To add a standard: add a row (with its
enforcement mechanism), wire the mechanical check into `check_standards.py` or a
behavioural test, and update the `check-standards` coverage. Do not maintain a
parallel standards list elsewhere; CLAUDE.md points here.

---

