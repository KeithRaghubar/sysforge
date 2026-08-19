<!-- GENERATED FILE -- do not edit directly.
     Source of truth: the modular files under docs/design/ (see
     docs/design/index.md and docs/design/_manifest). Edit those, then run
     `make design`; `make check-design` guards against drift. -->

# SysForge Design Document

SysForge is an Arch Linux build and maintenance suite with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles, tracks build state for update detection, and automates the full build lifecycle — from fetching PKGBUILDs to installing profiled packages. Pacman owns the package database; SysForge owns the build configuration layer above it.

SysForge manages the profiled AUR-helper surface (install, update, and manage AUR and custom packages with system-tuned profiled builds) and a full bootstrap pipeline (stages 1–3: install via archinstall, hardware detection, configure) that automates a fresh Arch install from the ISO. Current release is **<!--version-->v3.1.0<!--/version-->**; per-release changes are recorded in `docs/release-notes/`.

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
21. [Scope & Non-Goals](#scope-non-goals)
22. [Standards & Specifications](#standards-specifications)
23. [Privilege-Escalation Seam](#privilege-escalation-seam)

---

## Philosophy

SysForge was motivated by source-based distros' compile-time control and performance tuning, without their fragility and maintenance overhead. The core insight is that source-based systems conflate several concerns that are better separated:

- **Hardware profiling** — what the machine has
- **Compiler flags** — how to build for it
- **Feature selection** — what to enable

SysForge separates these into distinct config layers and produces a standard mutable Arch system as output. It is not a distro. There is no ISO, no divergence from upstream Arch, no custom package ecosystem.

This document describes only **implemented** design. Planned features, candidate enhancements, and the rationale for purposely-excluded or abandoned ideas live in `/ROADMAP.md`. Roadmap items carry version-prefixed IDs (`<version>-<TYPE><n>`, counter reset only on a major/minor bump, not patch) that appear only in the roadmap and release notes — never here.

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

### Module & function decomposition

SysForge has no line-count lint for functions or modules; decomposition is
driven by **ownership and reuse**, not size. The governing rule is *one home per
concern*: a given decision (a path, a detection, an injection, a gate) is
computed in exactly one place, and every caller routes through it. The standing
list of these single-home invariants lives in `sysforge/CLAUDE.md` (the
lazily-loaded code-seam fragment: one-home invariants + the toolchain/kernel
deep invariants; the root `CLAUDE.md` carries only always-on process
conventions); this section is the *rubric* behind them — when to extract, and where the extracted code belongs.

**Promote logic to a `primitives/` function when** any of:

- **A second caller appears.** The moment two command modules (or a command
  module and a stage) need the same decision, it moves to `primitives/` — never
  copied. Two command modules must not import each other (above), so a shared
  primitive is the *only* way for them to share logic.
- **It is a policy-free fact.** Pure derivations — "which LLVM targets does this
  GPU need", "is this a musl-static build", "what is `PKGDEST`" — belong in a
  primitive that does guarded reads and degrades to a safe default rather than
  raising. The *stage/verb* owns the abort/warn/prompt policy; the primitive
  owns the facts. (`toolchain_safety.py`, `kernel_safety.py`, `flag_drift.py`,
  and the `pacman.get_*` path resolvers are the model — pure, never log.)
- **It guards an invariant a test must pin independently.** If a regression test
  needs to assert the behaviour in isolation (e.g. the lib32 flag scrub, the
  cmake-anchor finder), it wants a named, importable seam.

**Keep logic inline in the command module / stage when** it is policy
(sequencing gates, deciding to prompt vs abort), it has exactly one caller and
no test needs it in isolation, or extracting it would only relocate a single
straight-line block without removing duplication. Premature extraction that adds
an indirection with one caller is churn, not decomposition.

**Splitting an existing function** is warranted when a distinct, separately
*testable* responsibility is buried inside it (the classic "this 200-line
function has a 30-line pure sub-computation a test keeps reaching into via
monkeypatch"), or when two callers want different *prefixes/suffixes* around a
shared middle. Splitting purely to hit a line target is not — a long but linear,
single-responsibility function (a stage's gate sequence, a PKGBUILD render) is
more readable whole than fragmented across helpers that are each called once.

**Where extracted code lands:** a cross-cutting fact or operation → `primitives/`
(its own module if it owns a subsystem — `mesa_pgo.py`, `bolt.py`, `kernel_fdo.py`
— else an existing cohesive one); verb-specific orchestration → the command
module or `pipeline/stages/`; never a "utils" grab-bag. When adding a new
single-home concern, record it in `sysforge/CLAUDE.md` so the invariant is
discoverable, and cross-reference the owning DESIGN.md section. Cited
paths/symbols are kept fresh by the `check_standards` `claude_md` group.

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
│   ├── help_cmd.py                    # sysforge help subcommand (read-only alias for --help)
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
│       ├── archinstall_config.py      # pure BootstrapConfig → archinstall JSON (schema pin, _BASE_PACKAGES home)
│       ├── archinstall_invoke.py      # sole archinstall shell-out (which() gate, 0600 tmp config, --silent)
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
│       ├── audio_probe.py             # doctor audio axis: failed pipewire user units, vanished sink
│       ├── network_probe.py           # doctor network axis: no default route, manager/DNS conflicts
│       ├── failure.py                 # failure scenario handling (shared)
│       ├── resource_guard.py          # controller RLIMIT_AS cap + lift_for_child() for subprocesses
│       ├── cache_probe.py             # passive ccache/sccache monitoring ([CACHE] tag)
│       ├── aur.py                     # AUR RPC v5, git clone, pkgctl checkout, GPG key import
│       ├── rate_limit.py              # shared RPC + git fetch rate limiter (RateLimiter, RateLimited)
│       ├── source_meta.py             # per-package AUR RPC + git HEAD cache (source_meta.toml)
│       ├── source_sync.py             # process-wide SourceSyncScheduler (RPC-first, sequential)
│       ├── env_persist.py             # write EDITOR/VISUAL to /etc/environment or ~/.zshenv
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
│           ├── _partition_plan.py     # shared destructive-op confirmation (plan table, glyph downgrade, _confirm)
│           ├── install.py             # stage 1: disk + base install + identity via archinstall
│           ├── hardware.py            # stage 2: CPU/GPU/NVMe detection + PCI/USB inventory → hardware_profile.toml
│           ├── configure.py           # stage 3: sysforge-specific tuning (makepkg.conf, mirrors, desktop, self-install) (arch-chroot)
│           ├── reconfigure.py         # stage 4: pre-build checkpoint
│           ├── toolchain.py           # stage 5: LLVM/GCC toolchain build (optional 4-pass PGO)
│           ├── packages.py            # stage 6: package builds
│           └── kernel.py              # stage 7: kernel build
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
├── Makefile                           # dev interface; `make help` lists every target
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
  (`enable_build_from_source` / `cache` / `reason`) or globally via
  `repo_mode = "build_from_source"`.

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
- `source` — `repo` (pacman) vs `aur`/`git`/`local`. A **routing hint**: it tells the bootstrap/build paths how to obtain the package, falling through to pacman / AUR RPC inference if omitted. It does **not** by itself put a package under steady-state tracking — tracking comes from sysforge having built the package (build_state), not from a manifest entry. So an entry with only `name` + `source` has no override effect at steady-state, and `sysforge packages add` rejects it (pair it with a behavior-changing field — `enable_build_from_source`, `cache`, `reason` — for the entry to override anything). When sysforge builds a package, it records the resolved `source` in build_state, so the registry is self-describing without re-inferring origin later.
- `enable_build_from_source` *(bool)* — if `true`, this **repo** package is built from source (via `pkgctl repo clone` + makepkg with sysforge flag profiles) instead of installed as the binary via pacman. It also opts the package into `sysforge build` without a confirmation prompt (see §`build`). The boolean replaced the misleadingly-named `pkgbuild_patch` (which never patched anything — real PKGBUILD patching is gated by separate predicates); as of 3.0.0 the legacy `pkgbuild_patch` key is no longer honored on read (`config.normalize_package_entry` is now an identity function) — a manifest that still spells it no longer opts the package into a source build. `packages_cmd` still rewrites the key to the current name on the next `packages.toml` write (`add`/`remove`), so a file touched since the rename self-cleans; a never-rewritten file keeps the stale key inert.
- `cache` *(bool)* — `false` disables ccache/sccache for this package (required for PGO stages).

```toml
[build]
pkgbuild_src_dir = "~/src"        # PKGBUILD source tree; auto-cloned if absent
repo_mode = "build_from_source"   # default for repo packages: "pacman" | "build_from_source"

[[package]]
name = "mesa-git"
enable_build_from_source = true   # override: build this repo package from source

[[package]]
name = "llvm"
cache = false                     # override: never cache instrumented PGO objects
```

An entry with only `name` and no override fields has no effect on the build. `sysforge packages add` rejects such calls.

### `[build]` global section

- `pkgbuild_src_dir` — directory holding pre-cloned PKGBUILDs (`<pkgbuild_src_dir>/<name>/PKGBUILD`). Missing AUR clones are auto-fetched here on demand.
- `repo_mode` — controls how the **bootstrap** (`run packages`) builds repo-source entries: `"pacman"` (install via `pacman -S --needed`) or `"build_from_source"` (build from PKGBUILD with sysforge flag profiles); per-package `enable_build_from_source = true` forces source-build regardless. At **steady-state** `repo_mode = "build_from_source"` is a *bulk drift-surfacing* switch: it pulls **every** installed repo package into `sysforge update`'s walk so repo-side version drift is reported alongside AUR drift. It is **not** how you get a repo package source-built going forward — that happens automatically once sysforge has built it (build_state authority; `sysforge build mesa` is the natural entry). Of the bulk set, only the overridden / already-source-built subset is rebuilt from source; the remainder takes a fast pacman path (`checkupdates` for upgrade detection, one terminal `sudo pacman -Syu` after the source-build loop). This avoids a per-package `pkgctl repo clone` for every installed repo package and is what makes the "track everything" mode tolerable on a maintained workstation. The legacy value `"profiled"` was removed in 3.0.0 and is now rejected (single resolver: `config.resolve_repo_mode`).
- `system_upgrade` *(bool, default `false`)* — finish every `sysforge update` run with a single `sudo pacman -Syu` (Phase 6.5), independent of `repo_mode`. This is the standalone form of the trailing upgrade: no packages are added to the version-check walk and no `checkupdates` probe runs, because pacman resolves the transaction itself — one subprocess. The `repo_mode = "build_from_source"` route above still triggers the same Phase 6.5 transaction off its classified `repo_class = "pacman"` set; the two are independent inputs to one gate. The flag pair `--sysupgrade` / `--no-sysupgrade` overrides per run through the `config.resolve_flag_default` precedence seam (`--no-sysupgrade` is the explicit-off leg, checked first). `--offline` suppresses it entirely. Ordering is the same either way: source artifacts install in Phase 6 first, protected from the transaction by the `IgnoreGroup = sf-build` line `sysforge setup` adds.

### Package groups

`[group.<name>]` tables declare named sets that expand into `[[package]]`-equivalent entries at load time, so a desktop stack (e.g. 20+ git packages) can be tracked without enumerating every member as its own block:

```toml
[group.cosmic]
packages = ["cosmic-session-git", "cosmic-comp-git", "cosmic-settings-git"]
# Optional defaults inherited by every member:
# enable_build_from_source = true
```

Expansion semantics (single expansion point: `primitives/config.expand_package_groups` — every manifest consumer routes through it; do not re-expand `[group.*]` elsewhere):

- Each member becomes a synthetic entry carrying its group defaults plus `group = "<name>"` marking its origin.
- An explicit `[[package]]` entry for the same name wins **outright** over the group entry — no field merge — so a member can be individually overridden.
- The first group to claim a name wins over later groups.
- Bootstrap (`run packages`): members are installed like any entry. Steady-state (`sysforge update`): members participate as overrides; a member with no group defaults is legitimately inert (its meaning is the bootstrap set) and is exempt from the inert-override warning that hand-written entries get.
- `packages list` shows groups as written in the file (name, member count, defaults, members), after the explicit-entry table. Groups are hand-edited TOML, **or** written by the guided desktop selection below; `packages add`/`remove` manage explicit `[[package]]` entries only.

#### Curated desktop catalog

`primitives/pkg_catalog.py` ships a curated catalog of desktop-environment groups (`gnome`, `kde`, `xfce`, `mate`, `cinnamon`, `lxqt`, `budgie`, `cosmic`) and is the single home for the catalog, the guided selection prompt (`select_desktop`), the per-entry display-manager pairing (`display_manager_for`), and the `[group.*]` writer (`write_desktop_group`). It lives in the primitives layer because three surfaces consume it:

- **`sysforge packages add-group <de>`** — writes the chosen catalog group into `packages.toml` (idempotent: re-running replaces the same-named group block; other `[[package]]` blocks and tables are preserved byte-for-byte). Creates the file with the standard header if absent.
- **Configure stage (bootstrap, stage 3)** — after copying config into the target, `select_desktop` resolves the choice: `bootstrap.toml [desktop] environment` wins non-interactively (unattended installs); otherwise a TTY run prompts ("Install a graphical desktop? → numbered menu"); a non-TTY run with no preselection skips. The group is written into the *target's* `packages.toml` so the later packages stage installs it.
- **Reconfigure step `desktop`** — offers the same guided selection on a live system, writing to the live manifest.

The writer only adds `[group.*]` text; expansion stays in `config.expand_package_groups`. Each entry is intentionally minimal — a core session plus its display manager (and, for lightdm-based desktops, a greeter); users extend their own group afterward. Every entry declares a `display_manager` package that is also a member of its `packages` tuple, so the package installs *and* its unit can be enabled.

**Display-manager enablement is the single fix that makes the selection actually boot into a GUI.** Installing the DM package on Arch does not enable its systemd unit, so the packages stage (the one home — both fresh-install and `sysforge run packages` flow through it) enables `<display_manager>.service` for every desktop group whose DM package built, via `_enable_display_managers` → `pkg_catalog.display_manager_for`. It runs once per distinct DM, outside the sentinel scope (cosmetic), and never `--now`-starts the unit (the install may be headless over SSH) — it takes effect on the next boot. This is **not** done in the configure stage: configure runs in the chroot *before* any packages are installed, so the unit doesn't yet exist there.

### Manifest lifecycle commands

`sysforge packages` is a small namespace for managing override entries:

- **`packages list`** (default when no subcommand) — tabulates entries: name and any override fields set. `--orphans` lists entries whose package is not currently installed (informational only; entries are still valid rules).
- **`packages add <pkg> [--source ...] [--enable-build-from-source] [--no-cache] [--reason TEXT]`** — adds or updates an override entry. Requires at least one of `--enable-build-from-source`, `--no-cache`, `--reason` (the *behavior-changing* override fields); calls with only `<pkg>` or `<pkg> --source` are rejected. `--source` is metadata that pins routing (`repo` vs `aur`) — it doesn't satisfy validation on its own, since classification arrives at the same value automatically. Entries with no behavior-changing override are auto-pruned on the next `packages.toml` write-back (`add` or `remove`); the auto-prune is legacy-aware (a pre-rename `pkgbuild_patch` entry counts as non-inert so it is never silently dropped — it is migrated to the current key name instead, see below).
- **`packages add-group <de>`** — writes a curated desktop-environment group (see *Curated desktop catalog* above) into `packages.toml`. Idempotent; the group installs (and its display manager is enabled) via `sysforge run packages`.
- **`packages remove <pkg>`** — removes the `[[package]]` block for the named entry using line-level manipulation; preserves all surrounding comments and section headers.

All subcommands accept `--packages FILE` to target a specific file (default: `/etc/sysforge/packages.toml`).

`build_state.toml` inspection and repair has its own namespace — see `sysforge state` (`state list`, `state repair`).

Valid per-entry fields: `name`, `source`, `enable_build_from_source`, `cache`, `reason`. The legacy `pkgbuild_patch` key is no longer read as of 3.0.0 — any file write (`add`/`remove`) still rewrites it to `enable_build_from_source` in place. Unknown fields are ignored.

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

`sync-config` is an **add-only**, comment-preserving merge from `etc/sysforge/*.toml` into the live config dir (`$SYSFORGE_CONFIG_DIR` itself, else `/etc/sysforge`, or `--target DIR`): it injects keys, tables, and their leading comment blocks the live file is missing, and never overwrites a value the live file already sets (even if the shipped default changed). Arrays-of-tables (`[[package]]`) are user content and are left untouched. Bare keys are spliced before the first table header (TOML adjacency rule); new tables are appended. The merge is **key-anchored**, so it can only carry comments that lead an active key it injects — pure documentation comments and *commented-out example* settings (`# interactive = true`) have no key to anchor to. When a shipped file gains such content the live file lacks, the shipped file is written verbatim beside the target as `<name>.sfnew` (pacnew-style) for the operator to diff and adopt; a stale `.sfnew` is removed once the drift is resolved. Drift detection is line-level and value-aware: a commented example the live file has **already adopted** by uncommenting it into an *identical* active line (`# interactive = true` shipped vs `interactive = true` live) is not drift and does not spill a `.sfnew`, but a differing value or a trailing inline note still does. One drift class is **structural** rather than textual and blocks the merge outright: a section header that is active in the shipped file but only present *commented out* in the live file (a pre-`3.0.0-STD1` vintage still carrying `# [build]`). A commented header does not disable its section — it reassigns every key beneath it to the preceding table or to the top level, so the settings stay syntactically valid while being read from the wrong place. The add-only merge cannot flip an existing line from commented to active, and worse, it would see the table as absent and append a *second* copy holding the shipped defaults, superseding the operator's orphaned value on reparse. Header liveness is therefore compared in **both** directions on the pre-merge text (the comment-signature subtraction cannot see this, being one-directional — activating a header removes its commented form from shipped); on a hit the file reports status `needs merge`, the write is **skipped entirely** rather than merged into a structure the tool has misread, and the `.sfnew` companion is spilled pointing at `sysforge config merge`. A header commented out in the *shipped* file is an example block (`#[group.cosmic]`), not a live section, and is excluded. `tomlkit` is a **dev-only** dependency (ephemeral `uv run --no-sync --with tomlkit`), never added to `pyproject.toml`. `--dry-run` reports without writing. `bootstrap.toml` is excluded (per-host, no live counterpart).

**Adopting the `.sfnew` residue — `sysforge config merge`.** Hand-merging the `.sfnew` companions is the only remaining manual step, so `sysforge config merge` (verb `config-merge`, `config_cmd.py`) is a pacdiff-style interactive driver over them. It scans the resolved config dir (`--config-dir` override, else `paths.CONFIG_DIR`) for `*.sfnew` — plus pacman's own `*.pacnew`/`*.pacsave` for sysforge config files on a packaged install — and for each presents: `[v]iew` (a `difflib` unified diff through `$PAGER` via `maybe_pager`), `[m]erge` (launch the resolved diff/merge tool with `live new`, then re-loop), `[s]kip`, `[r]emove` (drop the companion once the live file is satisfactory), `[o]verwrite` (copy the companion over the live file verbatim — guarded by a confirm and a "discards your local values" warning, never the default, intended for the `.pacnew` accept-maintainer case), and a[b]ort. Because a `.sfnew` is the *verbatim shipped file*, there is **no blind "accept theirs"** as a primary action — the safe path is merge-then-remove. The verb edits config files in place but never builds/installs, so it carries **no sentinel**. The diff/merge tool resolves through one home shared with the reconfigure editor chain — `primitives/editor.resolve_merge_tool` (`SYSFORGE_MERGE` > `sysforge.toml [ui].merge` > `$DIFFPROG` > `vimdiff`) launched via `run_tty_argv` (the `/dev/tty` passthrough, also used by `resolve_editor`'s callers). `--list`/`--dry-run` reports the companion→target pairs without prompting (scripting/CI).

### Dev fixtures vs. personal config

`tests/data/etc/sysforge/` is the **git-tracked test fixture set** wired in `tests/conftest.py` (which *forces* `SYSFORGE_CONFIG_DIR` to that dir directly, so a developer shell exporting its own value cannot leak into the suite). It is kept in shipped↔fixture parity by `make check-shipped`. A developer's **personal live config** is a separate, untracked dir (e.g. `~/sf-config`, holding the TOML files directly) that the shell's `SYSFORGE_CONFIG_DIR` points at and that `make sync-config` services — keeping personal config out of the tracked tree while leaving the fixtures deterministic.

#### Shipped-config comment style

Every key in `etc/sysforge/*.toml` is documented by the contiguous run of `#`
lines **immediately above** the line that assigns it — an active assignment or a
commented-out example, both anchored by the same `key =` shape:

    # key — one-line summary, then wrapped prose naming every accepted value
    #   form with an example for each.
    # key = "example"

A multi-line prose block is never placed *after* a key's line. `tools/sync_config.py`
is **key-anchored** — it can only carry a comment block that *leads* an active key it
injects — so trailing prose would be invisible to config adoption. The leading block
also gives `check_shipped`'s `config_comments` group an unambiguous, machine-readable
boundary: it walks upward from the key's own line (which it always includes) and stops
at the first non-comment line or at the previous key's own anchor line, whichever comes
first — so one key's block can never absorb a neighbour's paragraph.

Carve-outs: section-divider banners (`# ==== … ====`, `# ── [paths] ──`) lead a section
rather than a key, and a short unit/enum hint *may* trail a value on that same anchor
line, counting as part of that key's documentation because the anchor line is always
included (`nice = 19  # 0..19`, `ionice = "idle"  # IO class: "idle" | "best-effort"`).
Anything needing more than one line still gets a leading block.

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
| `[ui]` | `editor` | — | Editor for reconfigure stage (overridden by `SYSFORGE_EDITOR` env; one of three persistence targets offered by the reconfigure editor step — see §Pipeline Layer) |
| `[ui]` | `merge` | — | Diff/merge tool for `sysforge config merge` (`.sfnew` adoption). Resolved `SYSFORGE_MERGE` env > this > `$DIFFPROG` > `vimdiff`; accepts args (`"nvim -d"`, `"meld"`). Shares one home with the editor chain in `primitives/editor.py` (`resolve_merge_tool`/`resolve_editor`/`run_tty_argv`) |
| `[git]` | `fetch_timeout` | `30` | Seconds before a `git fetch` times out during source sync (0 = no limit). The `pull_timeout` alias was removed in 3.0.0; a stale key warns once and is ignored |
| `[git]` | `clone_timeout` | `60` | Seconds before `git clone` / `pkgctl repo clone` times out (0 = no limit) |
| `[build]` | `python` | `system` | Python interpreter for PKGBUILD `build()` steps, pinned ahead of any pyenv/asdf/conda shim on `PATH` so a bare `python` resolves to the interpreter its `python-*` makedepends were installed against. `system` / unset → `/usr/bin/python`; a bare version like `3.12` → `/usr/bin/python3.12`; or an absolute path. Resolved choice logged at DEBUG; an unusable value warns and falls back to the system python |
| `[build]` | `nice` | — | Build CPU-throttle: scheduling niceness `0..19` (out-of-range clamped). Applied as a `nice -n` front-end on the makepkg invocation — soft yield, full speed when idle. See §Flag/Profile System (build throttling) |
| `[build]` | `ionice` | — | Build IO-throttle class: `"idle"` or `"best-effort"`. Applied as `ionice -c {3,2}` |
| `[build]` | `cpu_quota` | — | Hard CPU ceiling, either `"N%"` (100% = one core) or a decimal fraction of total cores (`0.5` → half the host, translated against `os.cpu_count()` for portability). Enforced by wrapping makepkg in a transient `systemd-run --scope --user -p CPUQuota=N%` (folds in `Nice=`/`IOSchedulingClass=`); degrades to nice/ionice with a warning when `systemd-run` is absent or the user slice lacks CPU-controller delegation |
| `[build]` | `jobs` | — | Parallel build jobs; rewrites the `-jN` token in the emitted `MAKEFLAGS` (appends `-jN` if absent — make honours the last `-j`, so a `-j$(nproc)` baseline is still capped) |
| `[build]` | `mem_limit` | — | Per-build memory ceiling so a runaway build (OOM-prone link, sanitizer, LTO) can't take down the workstation. A byte count or binary-suffixed size (`"24G"`, `"512M"`). Off the `cpu_quota` path it's applied as an `RLIMIT_AS` clamp in the makepkg child's preexec; on the `cpu_quota` (`systemd-run --scope`) path it's delivered as a tighter cgroup `-p MemoryMax=` instead, so the two mechanisms never double-apply. Junk/zero/negative dropped with a warning. See §Flag/Profile System (build throttling) |
| `[build]` | `repo_track` | `"stable"` | Which sync-DB release a `source = "repo"` checkout tracks: `"stable"` pins to the release tag matching pacman's currently-resolvable sync-DB version (`get_pacman_sync_version`) so a source build matches what pacman would install; `"main"` leaves the checkout on the packaging repo's default branch (testing-track). Single read chokepoint `config.resolve_repo_track`; unrecognised values **warn and** fall back to `"stable"` via the shared `config.resolve_enum(raw, known, default, *, key)` seam (the one home for known-vocabulary string options — validate, warn on mismatch, fall back to the documented default). `resolve_repo_mode` routes through the same seam (falling back to `"pacman"`) — this lenient path serves the several defensive readers that only compare the resolved value. Its **authoritative** load point, `packages._load_packages`, is instead strict: it validates the raw `repo_mode` token against `config.REPO_MODE_ACCEPTED_INPUTS` and **hard-fails** on a typo, because silently falling back to `"pacman"` at that boundary would drop the source builds the user configured (strict-at-the-boundary, lenient-in-the-interior). The legacy `"profiled"` token was removed in 3.0.0 and is rejected by both. Distinct from packages.toml's `repo_mode` (source-vs-pacman install strategy) — this key only affects *which commit* a repo checkout sits on |
| `[aur]` | `min_fetch_interval_ms` | `500` | Minimum gap between consecutive git fetches against aur.archlinux.org (millisecond resolution) |
| `[aur]` | `rate_limit_abort_s` | `120` | If the accumulated AUR `Retry-After` penalty would exceed this many seconds, the remaining sync batch is aborted rather than waited out |
| `[mesa]` | `filter_drivers` | `false` | Opt-in master switch for hardware-filtering mesa's gallium/vulkan drivers (the meson analogue of LLVM target filtering). Off → mesa builds every upstream driver. On → `mesa_drivers.resolve_or_detect_mesa_drivers` trims `-D gallium-drivers=` / `-D vulkan-drivers=` to the detected GPU vendors, always keeping the mandatory software baseline (gallium `llvmpipe`/`softpipe`/`zink`, vulkan `swrast`/lavapipe) |
| `[mesa]` | `gallium` | — | Optional explicit gallium-driver override list (non-empty pins the axis, still baseline-enforced; absent → autodetect). Tokens must be valid meson gallium drivers |
| `[mesa]` | `vulkan` | — | Optional explicit vulkan-driver override list (same semantics) |
| `[security]` | `freeze_sources` | `false` | Master switch for the source freeze: when `true`, all new source egress (AUR clones, `pkgctl repo clone` checkouts of official-repo packages, source-sync fetches, both `vcs_pkgver` seams) is refused for the run — existing checkouts still build. Resolved by `net_policy.resolve_net_policy` via the shared `config.resolve_flag_default(args, "frozen", cfg, "freeze_sources")` seam, with precedence `--no-frozen` > `--frozen` > `[security] freeze_sources` > `false` (`--no-frozen` is an explicit-off wrapper on top of the shared seam, which has no "explicit false" concept). `--thaw PKG[,PKG...]` is a per-run *lift*, never a switch — it narrows an already-active freeze rather than enabling one. A refused package surfaces as the `STATUS_FROZEN` scheduler blocker (see §Primitives Layer → `source_sync.py`) |

### Toolchain drift detection (`toolchain.toml`)

`[toolchain] drift_detect` selects how `update` fingerprints the active toolchain to catch a **same-variant** rebuild (Phase 4.25; see §`update`). Resolved by `config.resolve_drift_detect()` — a missing file/key or an unrecognised value all fall back to the default. The value is the sole input to `build_fingerprint.toolchain_fingerprint(method, cc)`, whose opaque output is both stamped into `build_state.toml`'s `toolchain_fingerprint` at build time and recomputed for the active toolchain at update time; comparison is equality-only, so a method flip self-heals (old stamps stop matching → one fail-safe rebuild re-stamps).

| Key | Default | Description |
|-----|---------|-------------|
| `drift_detect` | `"fingerprint"` | `"fingerprint"` — `clang_identity`: path + size + nanosecond mtime + `--version` line. Fast, no hashing; a byte-identical reinstall flips mtime → one spurious (fail-safe) advisory. `"content_hash"` — sha256 of the resolved `libLLVM.so` (the real codegen carrier — the driver links it dynamically, so hashing the driver would miss a libLLVM-only rebuild) mixed with the `--version` line; precise but hashes a ~100 MB+ object each check. Falls back to `clang_identity` when no libLLVM resolves (e.g. a gcc variant) |

### Hardware overlays

The hardware detection stage emits `hardware_profile.toml` which feeds kconfig automation and gates hardware-specific packages in `packages.toml`. Key machine-specific caveats (Ryzen 7 5800X3D + RTX 5070):

- Explicit disable of `nouveau`
- CPU-specific flags: `CONFIG_MZEN3`, `CONFIG_X86_AMD_PSTATE`

---

## Pipeline Layer

Python DAG orchestrator with checkpoint/resume. Stages run in order:

1. **install** — fully implemented (disk + base install via archinstall: partition, format, mount, pacstrap, genfstab, bootloader, users, services, and system identity)
2. **hardware** — fully implemented (CPU/GPU/NVMe detection → hardware_profile.toml)
3. **configure** — fully implemented (sysforge-specific tuning: makepkg.conf, pacman ParallelDownloads, reflector mirrorlist + db sync, shell dotfiles, desktop group, sysforge self-install via arch-chroot)
4. **reconfigure** — fully implemented (pre-build checkpoint: config review, disk/network/gpg checks, build preview). The preview footer includes a median-based build-time estimate (see *Build-time estimate* below) when history exists.
5. **toolchain** — fully implemented (LLVM/GCC, optional 4-pass PGO bootstrap, compiler propagation to packages/kernel)
6. **packages** — fully implemented
7. **kernel** — fully implemented

Stages 1–3 are **bootstrap-only** — they run once from a live install environment. Stages 4–7 are **repeatable** and run on the installed system. Use `sysforge run pipeline --start-from reconfigure` to run the pre-build checkpoint on a live system; use `--start-from packages` to skip straight to builds. Stages 4–7 are also available as standalone `sysforge run <stage>` commands for repeated, out-of-pipeline use (e.g. `sysforge run packages`). The toolchain (5) and kernel (7) stages default to `enabled = false` because building a custom toolchain or kernel is an opt-in decision; users who want the stock system compiler and pacman kernel leave them disabled.

The **install** stage replaces the earlier hand-rolled `partition` + `base_install` stages (and the identity half of the old `configure`): disk layout, formatting, mounting, pacstrap, genfstab, bootloader, user/service creation, and system identity (hostname, locale, timezone, keymap, sshd) are all delegated to **archinstall**, driven from a generated headless JSON config (`archinstall --config <file> --silent`). See *Install stage* below.

### Build-time estimate and pre-build snapshot

**Build-time estimate.** Every build_state record carries a `build_seconds` ring — the last 5 whole-second durations of a successful build (see §Primitives Layer → `build_state.py`). `primitives/build_estimate.py` sums the **median** of each target's ring per distinct pkgbase (a split package is counted once, not once per pkgname); a target with no build history contributes nothing and is called out as unknown rather than silently zeroed. The reconfigure stage's build preview (`_step_preview`) and the `build`/`update` verbs print this as a one-line estimate before the build loop starts (`build_estimate.format_estimate`), and `build_core.build_and_install` prints a second, post-build **estimated-vs-actual** line (`format_estimate_vs_actual`) once the real wall-clock duration is known — the estimate is self-calibrating: each build feeds its own ring for the next one.

**Pre-build snapshot.** `[build] pre_build_snapshot` (ships commented-out, off by default) opts into a pre-build btrfs snapshot taken by `primitives/snapshot.py`'s `ensure_pre_build_snapshot(config, *, dry_run, interactive)`. It fires at the three build-orchestrator seams — `build_core.build_and_install` (the `build`/`update` engine), the packages stage, and the kernel stage — each call site guarded by the same module-level once-guard so a single process (even one that runs several of these seams, e.g. a full pipeline run) takes **at most one** snapshot. Resolution: delegate to snapper when a config already covers `/` (snapper owns retention for that subvolume); otherwise take a raw read-only `btrfs subvolume snapshot`. Either way **sysforge takes but does not reap snapshots** — cleanup is the operator's job, mirroring the boundary the toolchain/kernel rollback snapshots already establish. The primitive is a no-op on a non-btrfs root and under `--dry-run`, and is deliberately non-fatal: a snapshot failure never blocks a build.

### Reconfigure editor gate (stage 4)

The reconfigure stage opens config files in `$EDITOR`, and so do the build stages that follow it — `makepkg_invoke`'s build-failure recovery menu is the point where an operator fixes a broken recipe, and it has nowhere to go without one. The stage therefore treats a usable editor as a **precondition it must establish**, not a nicety.

**Persisting the pick.** A usable editor being a precondition of the *system*, not only of
sysforge, the step's save prompt offers three targets rather than one: `sysforge.toml [ui] editor`,
`/etc/environment` (bare `KEY=value`, read by PAM at login — reaches graphical apps and every
shell), and `~/.zshenv` (`export KEY=value`, sourced by every zsh invocation). Selection is
multi-select in the same input style as the step menu. `EDITOR` and `VISUAL` are written together
and confirmed once per target: a declined confirm leaves the file untouched, so a mismatched pair
is unrepresentable rather than merely avoided. Targets are independent — a write failure on one
warns and continues to the next, never aborting the stage.

Before prompting, the step renders the whole resolution order via `editor.describe_editor_chain()`
(the single home for editor precedence — `resolve_editor` is a thin reader over it, so the display
cannot disagree with the editor that actually launches), annotated with the winning rung, any rung
that holds a value but lost (`shadowed by N`), and any rung set to a command not on PATH. Rung
indices and the winner are derived from the rung list, never written as literals, so adding a rung
cannot mis-number the display or (since `resolve_editor` reads the winner) point at the wrong editor.
`env_chain.sources_defining()` supplies the files each value came from — each shown *with the value
it contributes*, since two files setting `EDITOR` differently is the ambiguity the display exists to
resolve — including sources sysforge will not write (PAM, login-shell-only inits) with the reason
shown inline. Both `$EDITOR` and `$VISUAL` carry this sub-listing, because the persistence step
writes both. One `collect_env_chain()` snapshot is taken per render and threaded into every lookup:
`sources_defining()` collects its own when passed none, and that reads ~14 init files and spawns a
`systemctl` probe.

Persisting to a file target *without* also selecting `sysforge.toml` while `[ui] editor` holds a
different value is warned about before the write. `[ui] editor` is rung 2 and `$EDITOR` is rung 3,
so that combination produces a system-wide `EDITOR` sysforge itself ignores — the warning names both
values and which one will actually launch, rather than letting the write silently contradict the
chain display above it.

Writes go through `primitives/env_persist.py`, which splits pure planning (`plan_write` — returns
the per-variable before/after and one of `create`/`append`/`replace`/`nochange`) from application
(`apply_write` — a direct in-place `write_text` on the happy path; on `PermissionError` it stages
the content to a chmod'd temp file and copies it into place through the §22 privilege seam for
`/etc/environment`. Neither path is a rename-based atomic replace). The
syntax difference between targets is load-bearing: `env_persist` must write what
`env_chain._parse_shell_init_file` accepts for that file, guarded by a round-trip test.

Two consequences of that round-trip contract are enforced in the primitive rather than assumed of
its callers. On the **write** side, a value with no encoding every reader of the file agrees on —
quotes, newlines, carriage returns, NULs, surrounding whitespace, the empty string — is rejected at
plan time instead of escaped harder: these files are read by pam_env, by `env_chain` and by the
user's shell, and they disagree, so there is no correct encoding to pick. Ordinary quoting
(`code -w` → `'code -w'`) is the one form all three accept and stays legal. On the **read** side,
`plan_write` recognises every assignment form `env_chain` does — bare, `export KEY=value`, and the
split `KEY=value; export KEY` — in the reader's own precedence order, so the `current` value it
reports and the one `env_chain` reports cannot diverge. Matching a strict subset of the reader is
what makes the prompt say `currently unset` beneath a chain display showing the real value, then
append a duplicate rather than replace.

Resolution is `primitives/editor.py`'s single home (`resolve_editor` → `SYSFORGE_EDITOR` > `sysforge.toml [ui].editor` > `$EDITOR` > `$VISUAL` > detected `vim`/`nano`/`vi`; `editor_usable` requires it on PATH). Three enforcement points sit on top of it:

- **Per-step gate.** `_EDITOR_NEEDING_STEPS` (`config`, `makepkg`) — the steps that open files *inside* the stage. `_run_selected_steps` calls `_require_usable_editor` before each, which runs the picker (with pacman-backed install of the chosen binary) and raises `RuntimeError` to abort the stage if the user cancels, rather than letting every subsequent edit prompt silently no-op.
- **Pipeline-handoff gate.** `_gate_editor_for_pipeline`, immediately before the "Ready to proceed to toolchain → packages → kernel?" confirm. The per-step gate covers only two steps, so a step subset skipping both (or a skipped `editor` step) would otherwise hand the build stages no editor at all. Skipped under `--standalone` (nothing runs after reconfigure); with no TTY or under `--dry-run` it warns instead of blocking on a prompt nobody can answer.
- **Adoption.** `_adopt_editor` exports the accepted editor as `SYSFORGE_EDITOR`. Downstream consumers call `resolve_editor()` fresh rather than receiving the stage's threaded local, so without this the pick would evaporate at the stage boundary. Adoption is unconditional; the `Save as sysforge default? [y/N]` prompt (`_offer_save_editor`) is a *separate* choice about persisting to `sysforge.toml` for future invocations, and declining it no longer costs the current run its editor.

### Bootstrap workflow (stages 1–3)

Stages 1–3 run from a live Arch install environment (booted from the install ISO). The state dir must be set to the target system so pipeline state persists across the reboot:

```bash
# From the live environment — iso-install.sh sets this up automatically
sysforge run pipeline --state-dir /mnt/var/lib/sysforge
```

When stage 3 (configure) completes, the reconfigure stage detects it is running on the live ISO (via `/run/archiso`) and raises `BootstrapRebootRequired`. The runner catches this as a clean stop (exit 0), saves state, and prints the resume command. After rebooting into the installed system:

```bash
sysforge run pipeline --resume
```

**`iso-install.sh`** (`tools/iso-install.sh`) automates the live-ISO setup steps: checks connectivity, installs sysforge from the AUR (`sysforge` by default; pass `--git` to install `sysforge-git` instead), and prompts for all required bootstrap values with validation (timezone checked against `/usr/share/zoneinfo/`, passwords entered silently with confirmation). Writes a complete `bootstrap.toml` and prints the pipeline command when done. Builds the AUR package as a temporary unprivileged user (`aurbuild`) since `makepkg` refuses to run as root; the user and its sudoers drop-in are removed on exit.

**`bootstrap.toml`** (`/etc/sysforge/bootstrap.toml`) configures stages 1–3. The package does not install this file directly — it ships a starter template at `/usr/share/sysforge/bootstrap.toml.example`. `iso-install.sh` writes the live file from interactive prompts; for hand-edit setups, copy the example to `/etc/sysforge/` first.

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
countries = ["Canada"]  # full country NAMES, not ISO codes (optional)
protocol  = "https"
age       = 12                 # reflector --latest N hours

[desktop]
environment = "gnome"   # optional — gnome|kde|xfce|mate|cinnamon|lxqt|budgie|
                        # cosmic; installs a curated desktop package group and
                        # enables its display manager. Unset + a TTY → the
                        # configure stage prompts; unset + no TTY → no desktop.
```

**Install stage (stage 1)** delegates disk + base install + system identity to **archinstall**, driven by a generated headless JSON config. Three pieces, each with one home:

- `primitives/archinstall_config.py` — **pure** `build_archinstall_config(cfg: BootstrapConfig, disk_size_bytes: int) -> dict`. No archinstall import, no I/O: it maps the bootstrap config onto the archinstall JSON schema (pinned `ARCHINSTALL_SCHEMA_VERSION = "3.0.15"`). `[partition] device/esp_size_mib/root_fs` → the disk layout (ESP `fat32` at 1 MiB + a root partition of `cfg.root_fs` sized to fill the rest of the disk, `wipe: true`). **The 3.0.15 headless schema has no "fill remaining" sentinel** — every partition size is a concrete value archinstall converts straight to a sector length, and a size of `0` becomes a zero-length partition that parted rejects (`start … length=0`). So the root size is computed as a concrete MiB value from the real disk: `_root_size_mib(cfg, disk_size_bytes) = disk_MiB − (1 MiB head + esp_size_mib) − 1 MiB GPT tail` (the tail leaves room for the secondary GPT + parted's MiB end-alignment; too-small disks raise `ValueError`). The caller supplies `disk_size_bytes` — the builder stays pure, so the disk *probe* lives in the stage (`_partition_plan.probe_disk_size_bytes`, an `lsblk --nodeps --bytes` read). `[system]` hostname/locale/keymap/timezone → the matching top-level keys; `root_password`/`username`/`user_password` → `!root-password` + a sudo `users[]` entry; `[mirror] countries` → `mirror_config.mirror_regions` (a baseline the configure stage's reflector step later refines). These must be **full country names** ("Canada"), not ISO codes ("CA"): archinstall keys its mirror-region map on the full names scraped from archlinux.org and raises `KeyError` on a bare code. `iso-install.sh`'s `_prompt_country` accepts either form for convenience but normalizes to the canonical name (via `reflector --list-countries`) before writing `bootstrap.toml`, so the config it produces always satisfies both consumers. The base package set — `_BASE_PACKAGES`, whose **sole home** is this module — is emitted as `packages[]` (+ `zsh`/`zsh-completions` and a `chsh` custom command when `shell == "zsh"`). systemd-boot, `services: ["sshd", "NetworkManager"]`, and sshd/serial-console hardening ride as `bootloader_config` + `services` + `custom_commands`, matching the VM fixture (`tools/vm/archinstall-config.json`), which is the golden source of truth for the 3.0.15 schema.
- `primitives/archinstall_invoke.py` — the **only** side-effecting unit: `run_archinstall(cfg_dict, *, dry_run)`. A `shutil.which("archinstall")` preflight is the sole coupling to archinstall (only present on the live ISO, never imported — keeping the live-ISO-only dependency out of the package-build path); absence raises with live-ISO guidance. It writes the config to a `0600` `mkstemp` temp file, runs `archinstall --config <file> --silent` via `run_or_raise`, and unlinks the temp file in a `finally`. A soft version-drift check (`archinstall --version` vs the pinned major/minor) **warns only**, never hard-fails, so routine archinstall updates aren't blocked. `--dry-run` prints the generated JSON with password fields redacted (`!root-password`, `users[].!password` → `***`) and the command, performing no side effects.
- `stages/install.py` — orchestration: `load_bootstrap()` → `probe_disk_size_bytes(cfg.device)` → `build_archinstall_config(cfg, disk_size_bytes=…)` → `run_archinstall()`. A real run that can't probe the device hard-fails (never partitions with an unknown size); `--dry-run` falls back to a nominal 40 GiB so the preview still renders a concrete layout. Because `archinstall --silent` never prompts, the stage **preserves sysforge's own destructive-operation confirmation** before handing off: `_confirm(cfg)` (moved with the partition-plan table + glyph-downgrade + existing-partition-table detection to the shared `stages/_partition_plan.py` helper) prints the plan box and requires explicit confirmation — a device that already carries a partition table gets an overwrite-specific prompt that **defaults to no**, so a stray or non-interactive run never silently wipes a populated disk. `--dry-run` skips the prompt and prints the plan + redacted JSON.

**Configure stage (stage 3)** no longer sets system identity — archinstall did that in the install stage. It runs only sysforge-specific tuning inside `arch-chroot` (and the sysforge bootstrap machinery archinstall can't provide):
- `ParallelDownloads` in `pacman.conf`; optional `[makepkg]` PACKAGER/MAKEFLAGS in the target `makepkg.conf`
- Reflector mirrorlist (skipped gracefully if `reflector` absent in chroot) — a fine-tuning pass over the baseline `mirror_regions` archinstall set
- Pacman db refresh against the fresh mirrorlist: `pacman -Sy` then `pacman -Fy` (`_sync_pacman_dbs`, best-effort — a transient mirror failure warns but never aborts configure). Seeding the **files** db here lets the reconfigure editor picker map an editor binary to its package on first boot without a separate sync.
- The sysforge `root:sysforge` group + `/var/lib/sysforge` state dir (setgid, so post-configure stages inherit the group), the shell dotfiles (root red / user green prompt), the resume-reminder profile.d drop-in, and copying `/etc/sysforge/` into the target
- sysforge install in target via `makepkg` from the source tree's PKGBUILD, run as the build user with a temporary `NOPASSWD` sudoers drop-in (removed after install). The configure stage stages the source as `sysforge-$pkgver.tar.gz` so makepkg uses the local copy instead of fetching, runs with `--skipchecksums --skipinteg` since the tarball is locally produced, and ends with sysforge owned by pacman (`pacman -Q sysforge`).
- Desktop environment (optional): **after** the sysforge install (whose `pacman -U --overwrite='/etc/sysforge/*'` restores the shipped `packages.toml`), `pkg_catalog.select_desktop` resolves `[desktop] environment` (non-interactive) or prompts on a TTY, then writes the chosen `[group.*]` into the target's `packages.toml` so the packages stage installs it. The only interactive point in an otherwise non-interactive stage; non-TTY runs with no preselection skip silently. See Package Manifest → *Curated desktop catalog*.

The hardware stage (stage 2) needs no config — it auto-detects and writes `hardware_profile.toml` to `state_dir`. After reboot the file is at its natural path (`/var/lib/sysforge/hardware_profile.toml`) and the kernel stage picks it up automatically.

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

**Post-build change summary.** A `Stage` carries `reports_changes: bool` (default `False`) and
`change_root: str | None` alongside the existing `makepkg_bearing`, for the same reason:
whether a stage's work changes installed packages is a property of the stage, not of the pipeline
verb, so standalone `sysforge run <stage>` gets the same treatment as a full pipeline run.
`_run_stage_with_change_report()` wraps both call sites that invoke `stage.run()`
(`run_stage_standalone` and `run_pipeline`) — when `reports_changes` is set it takes a
`primitives/change_report.snapshot()` before and after the stage, diffs them, classifies the
outcome, and renders the summary via `log.ui`. A stage may contribute its own extra blocks below
the version rows by overriding `Stage.change_extras(config, state, options)`, which returns
`list[change_report.ExtraBlock]`; the default implementation returns an empty list, so the runner
never needs stage-specific knowledge and no stage is required to override it. Every part of the
reporting path is guarded independently — a snapshot, classify,
extras, or render failure degrades to a warning; the stage's own exception, if any, is re-raised
unchanged so reporting can never turn a successful build into a failure or mask a real one.
`packages`, `kernel`, and `toolchain` opt in; `install` does not.

**Kernel kconfig blocks.** The kernel stage's `change_extras()` override contributes two blocks
answering "what did I actually change about this kernel", which the subpackage and hotplug toggles
otherwise make invisible. The version rows already carry the size delta those toggles move, so no
separate size block is needed.

*Kconfig vs previous build* diffs the resolved `.config` against the one the last build produced.
`primitives/kconfig_history.py` owns that history — nothing else in sysforge keeps a resolved
`.config` after the build tree is cleaned — archiving each successful build's config (located by
the existing `_resolve_built_config`, tagged with `_built_kernel_release`) gzipped to
`<state_dir>/kconfig-history/<pkgname>-<release>.config.gz`, newest `KEEP` (5) per pkgname, pruned
on write and bounded at a few hundred KiB. The comparison runs through the new
`kernel_safety.diff_kconfig(old, new)`, a symmetric sibling to `diff_requested_kconfig` (which is
hardcoded to the requested-vs-resolved axis and iterates only sysforge's own intent); both parse via
the shared `parse_kconfig()`. `diff_kconfig` deliberately does *not* normalise an absent option to
`n` the way `diff_requested_kconfig` does — on the build-to-build axis, "the symbol did not exist in
that kernel" and "the symbol existed and was off" are different facts, and collapsing them would
fabricate churn on every major bump. Output caps at `_KCONFIG_DIFF_CAP` (40) symbols with a
`… and N more` pointer; the full list goes to the unified log, so a major bump cannot bury the
version rows.

*Kconfig merge drift* relocates `_gate2_kconfig_drift`'s existing result into the summary by having
it *return* its drift list; the mid-run warnings are unchanged. `None` (as distinct from `[]`)
means the check never ran, which the block states explicitly, reusing the "did NOT run" wording
established by 2.6.1-B6 — the AlreadyBuilt path is exactly where a stale build makes the check most
relevant, so silence there would be the wrong answer.

Both capture points are best-effort and cannot raise into a build. The runner's re-raise of
`stage_error` moved into a `finally` in the same change: the reporting path guards `Exception`
while `stage.run()` is caught with `BaseException`, so a `KeyboardInterrupt` landing in the
snapshot/render window would otherwise replace the stage's own error and skip the caller's
`state.mark_failed()`. Raising from `finally` gives the precedence wanted — a stage failure always
beats a reporting failure.

**Toolchain identity block.** The toolchain stage's `change_extras()` override contributes one
`Toolchain:` block answering "what will my next builds be built with". `probe_toolchain_identity(state, options)`
snapshots a `ToolchainIdentity` — the `cc`/`cxx` `--version` first lines (via the existing
`build_fingerprint.compiler_version_line()`), the linker name, the `toolchain_variant` and
`toolchain_fingerprint` from the pipeline state, and the `flags_string` recorded in
`build_state.toml` for every entry stamped `owner_stage = "toolchain"`. `run()` takes the "before"
snapshot as its first act; `change_extras()` takes the "after" one and hands both to the pure
`toolchain_identity_lines(before, after)`, which renders a field that moved as `old → new` and one
that did not as a bare value, omitting fields empty on both sides. Flag deltas route through
`flag_drift.diff_flags()` for the shared `+added` / `-removed` vocabulary. Every field degrades
independently to its empty default, and an entirely empty identity yields no block rather than a
bare header. The block is deliberately **not** a benchmark: an uncontrolled timing figure printed
as a summary row would read as signal when it is noise, which is exactly why `kernel-bench.sh`
clears ccache/sccache and drops caches before measuring anything.

**User-facing output.** The runner emits a welcome banner (sysforge version + ordered stage chain) and a status snapshot (`✓ done`, `▸ running`, `· pending`, `↳ skipped_to`) before the loop, a stage banner before each stage (`[N/M] name` between two `═` rules), a `✓ name complete` line after each stage, and a closing rule on success. All of this routes through `log.ui` so it reaches both stderr and the unified log regardless of `-v` level. Visual primitives live in `sysforge/ui/headers.py` and share the `═` rule + bold-cyan style with `tools/iso-install.sh` (parallel `_double_rule` / `_step` / `_field` helpers in shell). Step counters are 1-based against the full stage list, so `--start-from configure` shows `[3/7]`, not `[1/…]`.

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

### Directory provisioning

sysforge writes into FHS-rooted directories the unprivileged build user does not own out of the box — `/var/lib/sysforge` (state, logs, sentinels) and `/var/cache/sysforge` (the regenerable PGO profdata store). **Provisioning these has one home: `primitives/fs_provision.py`.** Do not add a parallel `mkdir`/`chown`/`sudo` path.

The ownership model is a single one for every writable sysforge runtime dir: **`root:sysforge`, setgid mode `2775`**. The `sysforge` group lets group members read/write state and the PGO cache across runs (and across users on a shared host); the setgid bit makes every subdirectory created underneath inherit the group automatically. `/etc/sysforge` is deliberately *not* in this set — config stays root-owned and read-only to sysforge.

`fs_provision.ensure_writable_dir(path)`:

- **Fast path** — a plain `mkdir` landing a writable directory (already provisioned by the shipped `tmpfiles.d`, or a user-owned location like an XDG dir) returns immediately and never touches sudo. Before returning, if the process **owns** the landed dir and **holds** `SYSFORGE_GROUP` among its groups, the dir's group + setgid mode are best-effort self-healed with a sudo-free `chgrp`/`chmod` — so a dir first created `user:user 0755` by the unprivileged build user (no setgid bit, children can't inherit the group) is corrected in place without falling to the sudo slow path. Any heal failure is swallowed: the dir is already writable.
- **Slow path** (root-owned ancestor / not writable) — provisions via sudo: create the group if missing (`groupadd -f`), add the build user durably (`usermod -aG`, effective next login), then `install -d -m 2775 -g sysforge` and `chgrp`/`chmod` to **repair** an already-existing dir whose ownership predates this policy. The durable group add plus the per-run `chgrp`/`chmod` together mean the dir is usable *this* run even before the user's new group membership takes effect.
- **Fail-safe** — when sudo is unavailable it raises `FsProvisionError`; callers fall back to a user-writable location (the state-dir resolver drops to the XDG state dir; logging drops to a plain `mkdir`).

The build user is resolved once, in `fs_provision.build_user()` (`SUDO_USER` > `USER` > `getpass.getuser()`). The same `SYSFORGE_GROUP`/`SYSFORGE_DIR_MODE` constants are reused by the VM-bootstrap `configure.py` state-dir setup, so bootstrapped and package-installed systems agree. Install-time, the shipped `tmpfiles.d` provisions all four dirs `root:sysforge 2775` and a shipped `sysusers.d` declares the group (so `systemd-sysusers` creates it before `systemd-tmpfiles`) — making the runtime sudo path a no-op on a normally-installed host. `fs_provision.empty_dir_contents(path)` clears a directory's contents while leaving the node intact, used for the PGO purge so a root-owned parent never blocks the cleanup.

### Kernel stage (stage 7)

Builds a custom kernel from a PKGBUILD. The stage is a clean no-op if `/etc/sysforge/kernel.toml` is absent or has `enabled = false`, so systems using a stock pacman kernel skip it without needing `--start-from`. Opt-in by design — users who want a stock kernel leave the stage disabled. Also fires the opt-in pre-build snapshot (see *Build-time estimate and pre-build snapshot* above) before the build, sharing the same process-wide once-guard as the packages stage and `build_and_install`.

**`kernel.toml` structure:**

```toml
pkgname          = "linux-sysforge"  # LOCAL name to build/install as (defaults to upstream_pkgname)
upstream_pkgname = "linux-zen"   # optional: upstream package to pull/track (clone dir + sync target)
pkgbuild_src_dir = "~/src"       # parent dir; PKGBUILD is at <pkgbuild_src_dir>/<srcdir>/PKGBUILD
srcdir           = "linux"       # source dir override (default: upstream_pkgname, else pkgname)
bootloader       = "systemd-boot"    # systemd-boot | grub | none  (default: systemd-boot)
interactive      = true              # default: true — interactive kconfig (make nconfig)
compiler         = "llvm"            # "gcc" | "llvm" — kernel-stage compiler (optional)
base_config      = "pkgbuild"        # "pkgbuild" (default) | "running" | <path> — base .config source
build_headers    = true              # default: true — build the -headers subpackage (DKMS needs it)
build_docs       = false             # default: false — drop the -docs subpackage from the build
source           = "local"           # "local" | "repo" | "aur"; omitted → auto-resolve
                                     # "local" = hand-maintained PKGBUILD, no remote sync.
                                     # "repo"/"aur" = clone/fetch through the source-sync
                                     # scheduler. ("git" was a phantom value — no URL field
                                     # ever existed — and now errors clearly.)

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

`srcdir` is needed when the PKGBUILD directory name differs from the tracked name (e.g. `pkgname = "linux-sysforge"` but the repo is cloned as `~/builds/linux`). Resolution: `srcdir` → `upstream_pkgname` → `pkgname`, first set wins (`_srcdir_path`).

**Source tracking & local-rename (F40).** `upstream_pkgname` decouples *what to pull* from *what to build/install as*. Two modes fall out of the schema:

- **Pure-local** (default): `upstream_pkgname` unset, `source = "local"`, `pkgname` names the hand-maintained tree — no sync, no rename; byte-identical to the pre-F40 behavior.
- **Track-upstream:** `upstream_pkgname = "linux-zen"` (+ optional distinct `pkgname = "linux-mine"`) — the stage clones/fetches `linux-zen` into `<pkgbuild_src_dir>/linux-zen`, and when `pkgname` differs it threads `BuildOptions.rename_pkgbase_to = pkgname` into the build; `makepkg_wrapper._run_build` applies `pkgbuild_patcher.patch_pkgbase_rename(mode="coexist")` to the patched PKGBUILD so the build installs *alongside* the official package. The local rename is applied **before** the optional FDO `-sysforge` suffix, so the layers stack orthogonally (`linux-zen` → `linux-mine` → `linux-mine-sysforge`); the rename dict that rides to `build_state` keeps `origin_pkgbase = <upstream_pkgname>` so `sysforge update` still source-syncs the upstream tree. With `pkgname` omitted (defaults to `upstream_pkgname`) there is no rename and the repo-collision check is the safety net against shadowing the official package.

**Name resolution** is `_resolve_names` (one home): `upstream_pkgname` or `None`, `pkgname` defaulting to `upstream_pkgname`; neither set is an error. **Source resolution** is `_resolve_source(kernel_cfg, srcdir_path)`: an explicit `source` is honored (`git` errors — it was a phantom value with no URL to clone from); when omitted it auto-resolves `local → repo → aur` — an existing non-git tree is `local` (never clobbered by a re-clone), an existing git clone is `repo` (the scheduler's generic fetch rebases via the tree's own origin, skipping the AUR RPC), and a missing tree picks its clone remote via `aur.is_repo_package` on the tracked name (sync DB hit → `repo`/pkgctl, else `aur`). The build entry runs the source sync **before** requiring the PKGBUILD path, so a missing tree is bootstrapped by the scheduler's clone-if-missing path instead of aborting with "clone it first".

**The `-sysforge` collapse (intentional):** `patch_package_suffix`'s idempotency guard means a `pkgname` already ending in `-sysforge` gets no second suffix from an optimized (AutoFDO/Propeller `use`) build — the optimized build then *replaces* the prior build under the same name rather than coexisting. To keep an optimized and a stock build installed side-by-side, choose a `pkgname` that does not end in `-sysforge`; documented inline in the shipped `kernel.toml`.

**Kernel-stage compiler override:** `compiler = "gcc" | "llvm"` is independent of the toolchain stage. A system that keeps gcc system-wide can still build the kernel with LLVM (or vice versa). Resolution order: `--compiler` CLI flag > `kernel.toml compiler` > toolchain-stage pipeline state (cc/cxx set by stage 5) > profile defaults. When set to LLVM, the standard `LLVM=1 LLVM_IAS=1` env vars are injected by `makepkg_wrapper` automatically — no extra PKGBUILD changes needed. Note: `compiler = "llvm"` builds the kernel *with* clang but does **not** apply the toolchain's PGO profdata — that profdata trains the clang binary, not the linux target. The kernel's own profile-guided path is sample-based AutoFDO/Propeller (`--autofdo`, LLVM-only), described under *Sample-based kernel FDO* below — a distinct mechanism from the toolchain PGO.

**Headers/docs subpackages (`build_headers` / `build_docs`):** the kernel image is always built; the optional `-headers` and `-docs` subpackages are toggled independently. `build_headers` defaults **on** (DKMS and any out-of-tree module build need the matching kernel headers to compile); `build_docs` defaults **off** (the docs subpackage is slow to package and rarely needed) — note this drops the `-docs` subpackage that a stock kernel PKGBUILD would otherwise build. Resolution per toggle: CLI flag (`--headers`/`--no-headers`, `--docs`/`--no-docs`, `argparse.BooleanOptionalAction`, default unset → `None`) > `kernel.toml` (`build_headers`/`build_docs`) > hard default. The single home for the resolve is `_resolve_subpackages`. Disabling a subpackage works by **dropping its entry from the PKGBUILD's `pkgname` array** — `pkgbuild_patcher.patch_kernel_subpackages` (called from `makepkg_wrapper` for every kernel build, after `patch_kernel_btf_guard`) removes any array token whose dequoted value ends with `-headers`/`-docs`. It rewrites **both** the initial `pkgname=(...)` literal **and** every `pkgname+=(...)` append — modern Arch kernel PKGBUILDs add the optional subpackages via `pkgname+=("$pkgbase-docs")`, which a single-array walk would miss (leaving `-docs` to build and install; B7); an append that existed solely to add now-dropped subpackage(s) has its whole statement line removed rather than leaving an empty `pkgname+=()`. Standard Arch kernels synthesize each `package_$_p()` via `for _p in "${pkgname[@]}"; do eval …`, so an entry absent from the array is never packaged; the `_package-headers()`/`_package-docs()` helper bodies are left untouched. **Disabling docs additionally neutralizes the doc *build*:** stock Arch `linux` builds the documentation in `build()` (`make htmldocs SPHINXOPTS=-QT`), not only in `_package-docs()`, so dropping the `-docs` pkgname token stops packaging but not the (often multi-minute Sphinx) compile — which would make the summary's `docs=off` a lie. When `build_docs` is off the same patcher neutralizes any doc-target make line (`_neutralize_kernel_doc_build`, two passes over the goals — htmldocs/pdfdocs/infodocs/mandocs/…): a **mixed** line such as `make all htmldocs` loses *only* its `*docs` goals (→ `make all`) so the real build survives, while a line whose goals are **exclusively** `*docs` (with optional `VAR=val`/flag tokens) is commented out whole with a `# sysforge(docs off):` prefix. Both passes treat shell control operators and redirections (`&`, `;`, `|`, `&&`, `>…`) as punctuation the shell owns, never as make goals — stock Arch `linux` *backgrounds* its doc build (`make htmldocs SPHINXOPTS=-QT &`), and counting the trailing `&` as a surviving real goal made an exclusive-doc line look mixed: only `htmldocs` was stripped, leaving a goalless `make &` that resolves to make's **default** goal (`all`) and runs a second kernel build concurrently with the real one, killing `build()` with `E_USER_FUNCTION_FAILED` (B15). A backgrounded exclusive-doc line is additionally rewritten to `true &` rather than merely commented: the following line is invariably `local pid_docs=$!` and the function later `wait`s on that pid, so removing the only background job would leave the capture reading an unset or stale `$!` — and a `wait` on a non-child exits non-zero, fatal under makepkg's errexit. A trivial background job keeps `$!` valid and `wait` at 0, with the original command preserved in the trailing `# sysforge(docs off):` comment. The patcher needs no sentinel (every edit is self-idempotent — a commented line no longer starts with `make`, a stripped/removed token no longer matches), is a no-op when both subpackages are kept or the targeted subpackage is absent, and preserves both the single-line and one-token-per-line array layouts. When `build_headers` is disabled, Gate 1 escalates the standing DKMS reminder into a hard warning: no `<pkg>-headers` will be installed, so out-of-tree and DKMS modules (nvidia-open-dkms → black screen) cannot rebuild and won't load on reboot; any present DKMS modules are named.

**Resolution summary.** After resolving compiler (+ its origin), variant, bootloader (+ whether the chosen one is detected installed), source, interactive mode, kconfig counts, the headers/docs subpackage toggles (`subpkgs:` line), and the boot-safety gate settings, the stage emits a single labelled "Kernel build plan:" block (`_log_resolution_summary`). It prints on every run (useful before a multi-hour build) and is the readable core of `--dry-run`, replacing decisions previously scattered across the log. The standalone interactive default also emits a one-line nudge pointing at `--non-interactive` for unattended runs.

**Variant-inheritance nudge.** When `compiler` is unset (neither CLI nor `kernel.toml`) and the toolchain-stage variant is `pgo_llvm`, the stage emits a WARN naming the inherited variant and recommending that the operator persist `compiler = "llvm"` in `kernel.toml` so the choice survives a future toolchain-stage disable (which clears `[stages.toolchain.result]`). `stock_llvm` gets the same nudge at INFO level. `gcc` and `system` variants are silent — gcc is the safe default and `system` means there's no opinion to project.

**Configured-vs-installed toolchain mismatch.** The variant nudge above reflects what the toolchain *stage* registered in pipeline state; this check reflects on-disk reality. When `toolchain.toml` requests a custom LLVM toolchain (`enabled = true`, `compiler = "llvm"`) but stock repo LLVM is installed or its PGO profdata is version-skewed, the stage emits a WARN before the build. Detecting "stock vs custom" is subtle: the toolchain stage replaces LLVM **in place with stock pkgnames** (`llvm`, `clang`, … — it is the system compiler, no `-sysforge` suffix), so a custom/PGO build's packages keep names that exist in the `extra` sync DB and pacman therefore classifies them as repo (`install_origin == "repo"`), *indistinguishable from a genuine stock install by pacman alone*. The authoritative "sysforge built this toolchain" signal is the sticky `owner_stage == "toolchain"` marker the install-bearing pass stamps into `build_state.toml` (never auto-demoted — `BuildState.reconcile_external_installs` exempts owner-stage entries), so any `install_origin == "repo"` package that build_state records as toolchain-owned is subtracted from the stock set (`_toolchain_built_packages`). When build_state is empty (the stage was never run) nothing is subtracted and a genuine stock install still fires. The check is also fully suppressed in two intentional-stock cases: `skip_build = true` (which registers the installed compiler as-is — stock-vs-custom is then a deliberate choice, mirroring the `compiler = "gcc"` register-only short-circuit), and `pgo = false` + packages.toml `repo_mode = "pacman"` (the stage installs the stock LLVM suite from the repos on purpose via `install_repo_pkgs`, so a stock install is the chosen path — `_packages_repo_mode_is_pacman`; PGO always builds from source regardless of `repo_mode`, so this branch is pgo-off only). It uses `llvm_state.detect_toolchain_config_mismatch`, which is built strictly on `collect_llvm_state` (the sanctioned LLVM-inspection entry point) — this is **provenance reporting**, deliberately *not* a third toolchain *health* probe (those remain `_verify_llvm_install` and `toolchain_preflight._probe_cc`). The same detector backs `sysforge doctor --toolchain`. There is intentionally no persisted "toolchain is correct" flag: the live install state plus the sticky build_state record are the source of truth, computed on read.

**Per-kernel toolchain-drift check.** Stage entry compares the installed kernel's recorded `toolchain_variant` (from `build_state.toml`) against the active variant. On mismatch (e.g. installed kernel was built under `stock_llvm`, active is `pgo_llvm`), the stage emits a WARN before the build runs. This mirrors `sysforge update`'s drift sweep but covers the kernel package, which `update` excludes via the stage-ownership skip. Back-compat: no recorded variant → silent (older builds preceded the field).

**Bootloader-installed preflight.** Stage entry probes for systemd-boot (`/boot/loader/loader.conf`) and grub (`/boot/grub/grub.cfg`); falls back to `pacman -Qq systemd grub` when neither marker is present. When the resolved `bootloader` (≠ `none`) isn't in the detected set, a single non-fatal WARN surfaces the mismatch *before* the build runs — so a user on a grub-only system who left the default `systemd-boot` configured gets an early signal instead of a post-install `bootctl update` failure. False negatives on exotic setups (UKI, custom loaders) don't block the build; the post-install branch still tolerates the bootloader-update failure.

**Pkgname/pkgbase consistency check.** After the source sync, the stage static-parses the PKGBUILD via `parse_pkgbuild` and confirms the parsed `pkgbase` (or `pkgname` for non-split packages) matches the *pre-rename* name of the tree — `upstream_pkgname` when tracking an upstream, else `pkgname` (the local rename is a patch applied later in `makepkg_wrapper`, so the on-disk PKGBUILD always carries the upstream name). A typo or a cloned PKGBUILD whose `pkgbase` has drifted from the directory name raises a clear `RuntimeError` at stage entry instead of failing late at `makepkg --install` after a multi-hour build.

**Pkgname repo-collision check.** Immediately after the consistency check, the stage tests `kernel.toml pkgname` against the pacman sync DBs via `aur.is_repo_package` (one `pacman -Si`). A custom kernel should carry a unique name; if the name matches an official package (e.g. `linux`, `linux-lts`), building and installing it would overwrite the stock package on `pacman -U`. Interactive runs prompt for confirmation (`prompt_choice`, default no); unattended runs (`--non-interactive` or no TTY) abort; `--dry-run` warns without prompting.

**kconfig fragment:**

Hardware-driven kconfig entries come from `hardware_profile.toml [kconfig]` (emitted by the hardware stage). The kernel stage locates that file via `config["hardware_profile"]` when present, else falls back to `state_dir / "hardware_profile.toml"` — the path the hardware stage actually writes — so a standalone `run kernel` after a standalone `run hardware` picks up the profile even though no in-process pipeline state carried the path (mirrors the resolution in the reconfigure hardware-profile review). These include both positive `=y` enables (CPU/GPU/NVMe-driven) and architecture-disable `=n` umbrellas — when the host is x86_64, the hardware stage writes `# CONFIG_ARM64 is not set`, the same for RISC-V/PowerPC/MIPS top-level keys and a curated set of ARM64 SoC families, culling unreachable subtrees from `make nconfig`. See §Hardware Detection → *Architecture-aware kconfig disable* for the registry. Device-driven entries come from `hardware_profile.toml [kconfig_devices]` (modular drivers, `=m`, for devices present on the machine — see §Hardware Detection → *Device-driven kconfig*), gated by `kernel.toml device_kconfig` (default true). Manual overrides from `kernel.toml [[kconfig]]` are merged on top — precedence is manual > hardware > device; manual wins on conflict with a `[WARN]` (including arch-disable entries — a cross-compile use case can re-enable `CONFIG_ARM64=y` per the override path), while device entries are machine-derived advisories that hardware/manual override silently. The combined result is written to `<pkgbuild_src_dir>/<srcdir>/sysforge.config` before `makepkg` runs, each entry annotated with a `# source: manual|hardware|device` line. sysforge **patches the merge into the PKGBUILD** so a stock kernel PKGBUILD applies the fragment without cooperation: the kernel build contributes a `kconfig_plan.BASE_SEED` and `FRAGMENT_MERGE` step to the ordered kconfig plan (see §Primitives Layer → `kconfig_plan.py`), rendered into `prepare()` right after the kconfig-setup anchor (a `make olddefconfig`/`oldconfig`/`defconfig` line, else the `.config` seed) — a file-guarded base-config seed (`cp "$startdir/sysforge.base.config" .config` + `make olddefconfig`, see *Base config* below) followed by a guarded `scripts/kconfig/merge_config.sh -m .config "$startdir/sysforge.config"` + `make olddefconfig` (a bool-only symbol receiving `=m` is normalized by `merge_config.sh`/`olddefconfig`). Both blocks are wrapped in `if [ -f … ]`, so when a source produced no file the step is a runtime no-op. A PKGBUILD that already calls `merge_config.sh` (or references the fragment) is detected and left untouched — no double-injection; a PKGBUILD with no anchor at all is warned about (the fragment can't be placed). The whole merge is gated by `kernel.toml kconfig_merge` (default true) — set it false to skip the fragment entirely; when disabled, any stale `sysforge.config` from a prior run is removed so the PKGBUILD doesn't merge a leftover (the `device_kconfig` toggle, by contrast, gates only the device-driven sub-source).

**Requested symbols are verified against the resolved `.config`.** Writing a fragment line is not the same as getting the symbol: an illegal value for the symbol's type, a symbol upstream renamed or removed, or an unmet host-tooling dependency each void the request with no fragment-level signal, and the next `make olddefconfig` erases the evidence. The kernel build therefore contributes a final `kconfig_plan.VERIFY` slot (2.6.1-F23) that re-reads the resolved `.config` inside `prepare()` — after every merge *and* after any operator review — and warns per symbol whose requested value did not land. It warns rather than fails; `kernel_safety.py` remains the only hard gate. See §Primitives Layer → `kconfig_plan.py` for the mechanism.

Manual override validation: `option` must match `CONFIG_[A-Z0-9_]+`; `value` must be non-empty (`n` to disable); duplicates within `kernel.toml` are an error.

If no source provides any kconfig entries, no fragment is written. The fragment is written *after* the source sync (so a `--cleansrc` re-clone doesn't wipe it) and *after* compiler resolution, so its banner carries a toolchain-provenance line (`# toolchain variant: <variant>  cc: <path>`) giving a `.config` diff between two builds a trail of which toolchain produced it.

**Base config (`base_config`):**

The fragment is an *overlay* — it does not define the build's starting `.config`. `base_config` selects that base: `"pkgbuild"` (default, no-op — the PKGBUILD provides its own base), `"running"` (the running kernel's config, read via `dep_analysis.read_running_kconfig_text` from `/proc/config.gz` then `/boot/config-$(uname -r)`), or a path to a `.config` file. Resolution order: the `--base-config` CLI flag > `kernel.toml base_config` > the `"pkgbuild"` default (mirroring `--compiler`/`--bootloader`). For `"running"`/`<path>`, sysforge writes the resolved config to `<pkgbuild_src_dir>/<srcdir>/sysforge.base.config` before the build (dry-run aware) and the `BASE_SEED` slot — ordered before `FRAGMENT_MERGE` by `SLOT_ORDER` — injects the `cp sysforge.base.config .config` + `make olddefconfig` seed at the same injection point as the fragment merge — so a stock PKGBUILD honours `base_config` without cooperating. sysforge never mutates tracked source files. The seed is file-existence guarded, so the `"pkgbuild"` default (which writes no `sysforge.base.config`) is a runtime no-op. A `"running"` source that resolves to nothing (no `/proc/config.gz`, no `/boot/config-*`) warns and falls back to the PKGBUILD base; an unknown non-path value raises. The resolved source appears in the "Kernel build plan:" summary (`base cfg:` line).

**lsmod snapshot:**

Before the build, `lsmod` output is captured to `<state_dir>/lsmod.snapshot` (unless `capture_lsmod_snapshot = false`). This lets the PKGBUILD run `make localmodconfig` reproducibly using a fixed module set from the running system rather than whatever is loaded at build time. `localmodconfig` strips drivers for hardware *not loaded at snapshot time* — Gate 1 warns about this and Gate 2 (below) is the backstop that catches a dropped root-path driver before install.

**Keeping hotplug drivers through minimization (`keep_hotplug_drivers`):** `localmodconfig`/`localyesconfig` minimize to hardware present *at build time*, so a device attached later (a USB drive, an SD card, a hot-plugged PCI/CardBus card) can end up with no built driver. `kernel.toml keep_hotplug_drivers` (default off; also `--keep-hotplug-drivers`/`--no-keep-hotplug-drivers`, CLI wins over the config key) re-enables a curated set of hotplug driver classes — USB host/gadget, USB4/Thunderbolt, MMC/SD, hot-plug PCI/CardBus, hot-plug HID — regardless of what was attached at build time. The set is additive (it only ever enables; no `is not set` lines), and each symbol carries the value its kconfig type permits — `=m` for tristate symbols, `=y` for the `bool` ones (`CONFIG_HOTPLUG_PCI`, `CONFIG_HOTPLUG_PCI_PCIE`, `CONFIG_CARDBUS`). Writing `=m` for a bool makes kconfig discard the assignment entirely with `symbol value 'm' invalid for X`, so the symbol silently falls back to its tree default and the re-enable is lost; `tests/test_stage_kernel.py::test_hotplug_kconfig_values_are_legal` pins the per-symbol values, and adding a symbol to the curated set means checking its type in the kernel tree first. Resolution is `_resolve_keep_hotplug_drivers`; when it's on, the stage writes the curated set to a dedicated `sysforge.hotplug.config` fragment (distinct from the main `sysforge.config` fragment above) next to the PKGBUILD. The merge is contributed as the `kconfig_plan.HOTPLUG_MERGE` slot, ordered by `SLOT_ORDER` *after* `GENERATE` (so any `localmodconfig`/`localyesconfig` minimization has already run and can't strip the re-enabled modules) and *before* `REVIEW` (the configured tail or the injected `make nconfig`), so the operator still reviews the final, hotplug-safe config. File-existence guarded and idempotent, like the other fragment merges. When the key resolves off, any stale `sysforge.hotplug.config` from a prior run is removed. Boot safety remains authoritative — this mechanism only *adds* modules; it doesn't touch Gate 1/Gate 2.

The snapshot **accumulates**: `_capture_lsmod_snapshot` union-merges each new `lsmod` capture with the prior snapshot by module name (`_merge_lsmod`), with the current capture's row winning on overlap (fresher `Size`/`Used` data) and prior-only rows retained. This means a module that was loaded once (USB device, VPN, container netfilter) but isn't loaded during a later capture is never dropped just because that particular run didn't have it active — the module set grows monotonically across builds instead of resetting each time. There is no reset flag: to start over, delete `<state_dir>/lsmod.snapshot` manually.

**Configurable kconfig target sequence (`kconfig_targets`):**

`kernel.toml [kernel] kconfig_targets` (default: unset — feature off, zero behavior change) names an explicit sequence of `make <target>` kconfig-generation steps to run, replacing the PKGBUILD's own kconfig invocation. `resolve_kconfig_targets(kernel_cfg, interactive=…)` (in `pipeline/stages/kernel.py`) validates and reorders the configured list before it reaches the patcher:

- **Silent targets** (`olddefconfig`, `defconfig`, `allmodconfig`, `alldefconfig`, `savedefconfig`, `listnewconfig`) always run, in any interactive mode.
- **Prompting targets** (`oldconfig`, `localmodconfig`, `localyesconfig`, `mod2yesconfig`) require an interactive stage run; requesting one under `interactive=False` raises, naming the fix — `oldconfig` names its silent equivalent `olddefconfig`, the local*/mod2yes targets say to run the stage interactively (no silent equivalent exists for those).
- **UI targets** (`config`, `nconfig`, `menuconfig`, `xconfig`, `gconfig`) are capped at one per run — a second UI target raises — and any UI target is always reordered to run **last**, after every non-interactive target has shaped the `.config`, regardless of its position in the configured list.
- **`randconfig` is rejected outright** (not merely under non-interactive) — a randomized kernel config is a boot-safety hazard with no legitimate build-path use.
- **`localmodconfig`/`localyesconfig`** additionally emit a `[WARN]`: they over-minimize the config to modules *currently loaded* on the build machine — high risk (a driver needed later but not loaded now gets stripped), low reward — and are what causes the lsmod snapshot (above) to accumulate at `<state_dir>/lsmod.snapshot`.

The resolved sequence is threaded as `BuildOptions.kconfig_targets` (kernel.py → `makepkg_wrapper.py`), which contributes it as the `kconfig_plan.GENERATE` slot plus a UI tail split into `REVIEW` (see §Primitives Layer → `kconfig_plan.py`) — the caller fills these slots by key alongside `BASE_SEED`/`FRAGMENT_MERGE`/`HOTPLUG_MERGE`, so their fill order is irrelevant; `SLOT_ORDER` places `GENERATE` after the seed/merge slots and before `HOTPLUG_MERGE`/`REVIEW`/`VERIFY`, and a UI target left in the configured sequence for a run that turns out non-interactive is still stripped by `install`'s non-interactive rewrite pass. The configured sequence composes with the fragment merge rather than replacing it: `GENERATE` and `HOTPLUG_MERGE` both render into the `POST` region after the seed/merge blocks, so the sequence — any UI target last — shapes the fully seeded+merged `.config`. When a configured sequence is set, the caller fills `REVIEW` from the configured UI tail (or leaves it empty) instead of contributing an injected review — the two are mutually exclusive because they're the same slot. When set, the "Kernel build plan:" resolution summary shows the ordered sequence as `t1 → t2 → t3 (configured)` in place of the default kconfig-target line.

**Review-menu gate is review-only, not resolve-only (`_REVIEW_KCONFIG_RE`).** The injected `REVIEW` contribution (below) is dropped by `KconfigPlan.install`'s drop rules only when the PKGBUILD already has a genuine operator-review target — an ncurses/X menu (`nconfig`, `menuconfig`, `xconfig`, `gconfig`, or the line-prompt `config`). `oldconfig` is deliberately excluded from that check: it is a non-interactive *resolve* step (applies defaults for any symbol the fragment merge didn't already set) rather than a review, so a PKGBUILD that only runs `make oldconfig` still gets the injected `REVIEW` slot rendered. This is distinct from `_INTERACTIVE_KCONFIG_RE`, the wider match `install`'s non-interactive rewrite pass uses to rewrite surviving `oldconfig` → `olddefconfig` on non-interactive runs (that pass needs `oldconfig` included). Both regexes live in `kconfig_plan.py` alongside the code that uses them.

**Interactive kconfig (kernel-stage default):**

`sysforge run kernel` is interactive by default — the kernel stage passes `interactive=True` into `BuildOptions`. On the interactive path the caller contributes `kconfig_plan.review_step()` to the `REVIEW` slot when no configured review target already fills it, rendering a TTY-guarded pause followed by `make nconfig` into the PKGBUILD's `prepare()` (in the `POST` region, after the seed/merge/generate/hotplug slots) so the user reviews and edits the resolved config before the build proceeds — sysforge supplies the interactive target itself rather than depending on the PKGBUILD having one (a stock PKGBUILD that only runs `make olddefconfig` would otherwise show no menu). `KconfigPlan.install`'s drop rules suppress the injected `REVIEW` when the PKGBUILD already has an interactive target of its own — no second `nconfig`. The makepkg subprocess inherits the parent TTY in interactive mode (`makepkg_invoke`), so the ncurses UI renders on the controlling terminal. The pause is part of `review_step`'s rendered lines, immediately before `make nconfig` — the only point that is genuinely *after all merges, before the editor* (B6). A stage-level pause before `makepkg` necessarily fired before those in-`prepare()` merges ran, so the operator confirmed before the config was even assembled; the injected pause (`if [ -t 0 ]; then read -rp … || true; fi`) instead lets them read the merged result just before `nconfig` opens. It is TTY-guarded (inert on `--non-interactive`/no-TTY/captured-stdin/`--dry-run` paths) and errexit-safe under makepkg's `set -e` (`read` returning non-zero on EOF is swallowed by `|| true`; the `if` yields 0), and `Ctrl-C` still aborts. It is rendered by **both** review-slot builders: the injected `review_step()` and — as of F22 — a *configured* `kconfig_targets` UI tail (`ui_target_step`), whose prompt names the configured target rather than a hardcoded `nconfig`. The pause originally shipped only with the injected `nconfig`, reasoning that a target the operator named in `kernel.toml` needed no confirmation; that was wrong, because the pause is not a confirmation of the *target* but the operator's checkpoint on the assembled `.config`, equally wanted however the review target got there — a `kconfig_targets = ["olddefconfig", "nconfig"]` config otherwise dropped straight into the menu with no chance to inspect the merge result. A PKGBUILD supplying its own interactive target still keeps it with no pause: no plan step renders that line, so there is nothing to attach a pause to. The default can be flipped via `kernel.toml interactive = false` or the `--non-interactive` CLI flag; `install(noninteractive=True)` then drops the injected `REVIEW` slot outright (or rewrites a *configured* UI tail to `olddefconfig` via its `noninteractive_rewrite`, per `ui_target_step`), and separately rewrites any surviving PKGBUILD-owned interactive target (`oldconfig`, `nconfig`, `menuconfig`, `xconfig`, `gconfig`) to `make olddefconfig` for unattended runs. `olddefconfig` applies defaults for all new symbols without terminal interaction; VAR=val arguments before the target (e.g. `ARCH=x86_64`) and trailing comments are preserved. `--noconfirm` only controls makepkg's own prompts and has no effect on interactive make targets inside the PKGBUILD.

Note: when other verbs (`sysforge build`, `sysforge update`) build a kernel PKGBUILD with `build_mode = "kernel"` on the resolved profile, those paths still default to *non-interactive* — interactive-by-default is a kernel-stage-only contract because the stage is the user-driven kernel build entry point.

**Post-build kconfig drift (advisory):** after the build, beside Gate 2 (pre-install, outside the sentinel), `_gate2_kconfig_drift` compares the options sysforge merged into `sysforge.config` against the resolved `.config` and warns on any that didn't survive — disabled (`y/m`→`n`/absent), changed (built-in↔module, or a string/int value change), or re-enabled (`n`→`y/m`). On the interactive path a drop usually means the operator toggled it off in `make nconfig`; otherwise it's typically `make olddefconfig` dropping a request whose dependencies are unmet. sysforge can't distinguish the two without a full dependency solve, so this **never blocks** the build — unlike the boot-critical Gate 2 audit it sits next to. The diff itself is a pure fact in `kernel_safety.diff_requested_kconfig` (returning `KconfigDrift` records over the same `parse_kconfig_text` representation both files share — a missing resolved option normalises to `n`, so a requested `n` that ends up absent is correctly not flagged); the stage owns only the logging. The check keys off the fragment's existence, so it is on exactly when `kconfig_merge` produced a fragment (both on or both off).

**Source sync via the scheduler:**

The kernel stage routes its source refresh through `source_sync.get_scheduler().request(SyncRequest(..., source=<resolved source>))` ahead of the build, the same path as the toolchain stage — and (F40) *before* `_pkgbuild_path` requires the PKGBUILD, so a missing tree is bootstrapped by the scheduler's clone-if-missing path. With `source = "local"` (explicit or auto-resolved), the scheduler short-circuits (no RPC, no clone, no fetch) — only `--cleansrc` / `--cleansrc-force` would attempt a purge, but a hand-maintained tree has no remote to re-clone from, so users on the `local` path leave cleansrc unset. For `source = "repo"` / `"aur"`, the normal sync runs: `--cleansrc` purges and re-clones (refusing on dirty/ahead/no-upstream clones); `--cleansrc-force` overrides that guard; cleansrc forces a sync even when `--no-update` is also set. `STATUS_FAILED` / `STATUS_RATE_LIMITED` / `STATUS_PURGE_REFUSED` raise.

`STATUS_DIVERGED` (upstream advanced but the local tree can't fast-forward — local commits or a dirty tree) gets stronger handling in the *kernel* stage than the plain warning the other verbs use, because building a kernel off stale or hand-edited source is exactly the easy-to-miss footgun. `_warn_and_confirm_diverged` enriches the WARN with ahead/behind counts (`classify_head_vs_upstream`) so the "upstream has new commits but the local repo is dirty" case is spelled out, then **gates the build**: an interactive run must confirm (`prompt_choice`, default no), and an unattended run (`--non-interactive` or no TTY) aborts. Either decline raises, leaving nothing built (the sync runs before the sentinel). `--cleansrc` to discard local edits is the suggested escape hatch.

The source sync (including the `--cleansrc` purge, which `purge_src` does as a non-atomic `shutil.rmtree`) runs **outside** the boot sentinel by design: it mutates only the src tree, nothing boot-critical, so wrapping it in the sentinel — whose `recovery_cmd` is `sudo mkinitcpio -P` — would be semantically wrong. The atomicity contract is "purge, then clone"; an interrupted purge leaves a missing/partial PKGBUILD that fails **loudly** at `_pkgbuild_path` on the next run (with a hint to re-run `--cleansrc` to re-clone), not a silent brick. No sentinel is needed because the running kernel was never touched.

**Stage-ownership stamp:**

After a successful build, the kernel stage stamps `owner_stage = "kernel"` and `source = "local"` (or the configured value) into `build_state.toml` via `BuildOptions`. `sysforge update` honours that marker and skips the kernel package by default — the canonical update path is `sysforge run kernel`, not a sweep through `update`. Before the first kernel-stage build has written that stamp, the config bootstrap fallback in `primitives/stage_ownership.py` (consulted by `update.py`) reads `kernel.toml`'s `pkgname` and applies the same skip; split sub-packages collapse to the kernel `pkgbase` via the same `get_pkgbase()` lookup that handles other custom-built split packages. `--include-stage-owned` overrides the skip; naming the package explicitly on the `sysforge update` command line is treated as an opt-in for that run.

**Sample-based kernel FDO (AutoFDO / Propeller).**

`sysforge run kernel --autofdo=record|capture|use` (with optional `--propeller`) is the kernel's sample-based optimization path — the third method on the shared profile-store rails (after the LLVM toolchain PGO and mesa PGO), and the first that is **sample**-based rather than *instrumentation*-based. Where PGO instruments the binary (`-fprofile-generate`), AutoFDO never instruments: you build a kernel with `CONFIG_AUTOFDO_CLANG=y` (which only adds profiling debug info), boot it, sample its branches with `perf record -b`, and convert the samples to a profile with `create_llvm_prof`; the optimized rebuild consumes the profile through the kernel build's `CLANG_AUTOFDO_PROFILE` make-variable (`-fprofile-sample-use`). Because collection runs on the *booted* profiling kernel, it necessarily spans a reboot — so the flow is three steps, each a separate `run kernel` invocation. The one home for all of it is `primitives/kernel_fdo.py` (pure facts + command assembly); the stage only orchestrates.

- **`--autofdo=record`** — merges `CONFIG_AUTOFDO_CLANG=y` (+ `CONFIG_PROPELLER_CLANG=y` under `--propeller`) into the kconfig fragment and builds+installs an *instrumented* profiling kernel. It installs under a **distinct sysforge-owned coexist name** — `kernel_fdo.record_pkgname(pkgname)` → `<pkgname>-sysforge-fdo` — applied via the same `rename_pkgbase_to` → `patch_pkgbase_rename(mode="coexist")` seam as the use-build and the F40 upstream rename (F26). This is a safety property: the instrumented (unoptimized, profiling-debug-info) kernel must **not** overwrite the production kernel — neither the stock `<pkgname>` nor the optimized `<pkgname>-sysforge` use-build — so it earns its own `/boot` entry and bootloader fallback, and a re-`record` only ever replaces a prior sysforge profiling kernel (the coexist name is itself the ownership gate, mirroring the `owner_stage` + coexist-rename discipline). The stage tracks this effective name for the repo-collision check, the sentinel metadata, and post-install Gate 3 (`/boot/vmlinuz-<pkgname>-sysforge-fdo`); the subsequent `--autofdo=capture` step resolves its `vmlinux` from the same-named build tree (`resolve_vmlinux(record_pkgname(pkgname))`) and tells the operator to reboot into it. The store stays keyed on the stock `pkgname` so record/capture/use share one profile location. The fragment merge gains a fourth source, `fdo`, at precedence device < hardware < **fdo** < manual (so a manual `[[kconfig]]` can still override it, with a WARN that this may disable the optimization); `kernel_fdo.fdo_kconfig` is the single producer. A contradictory `kconfig_merge = false` (which would suppress the required config) is refused.
- **`--autofdo=capture`** — a **read-only, non-mutating** step (no build, no install, no sentinel). It resolves this host's branch-sampling `perf` event, the matching `vmlinux`, and the store output paths, then **prints** the exact `perf record -b … && create_llvm_prof …` commands for the operator to run while exercising the booted profiling kernel. sysforge does not run `perf` itself — the guided design keeps it out of the BRS-fragile perf-orchestration business and honours that the workload is the operator's to drive. It provisions the store (`fs_provision.ensure_writable_dir`) so the printed `create_llvm_prof --out=<store>/…` can write.
- **`--autofdo=use`** — requires the collected profile (`kernel_fdo.require_profile`, a clean pre-build abort if the record→capture step was skipped), injects the profile-path make-variables (`CLANG_AUTOFDO_PROFILE`, + `CLANG_PROPELLER_PROFILE_PREFIX`) through the build's **`extra_env`** seam — the same channel that already injects `LLVM=1`, since `make` imports the environment as make-variables — and stamps the optimization `build_mode` (`autofdo_kernel` / `propeller_kernel`). That earns the `-sysforge` **coexist** rename (`patch_package_suffix(mode="coexist")`, applied in `makepkg_wrapper`; mode chosen by `profile.rename_mode_for_build_mode`), so the optimized kernel installs *alongside* the stock kernel for bootloader fallback — no `conflicts`/`replaces`. Because the install is renamed, the stage tracks an effective `<pkgname>-sysforge` for the post-install Gate 3 (`/boot/vmlinuz-<that>`), the sentinel metadata, and the repo-collision check. `build_state` records the renamed name with sticky `origin_pkgbase` so `sysforge update` still source-syncs the upstream tree.

**LLVM-only, with an `os.environ["CC"]` fallback.** AutoFDO/Propeller have no GCC path in sysforge, and `CONFIG_AUTOFDO_CLANG`/`CONFIG_PROPELLER_CLANG` have no GCC equivalent at all. The stage hard-aborts (clean `RuntimeError`, single home `_gate_fdo_llvm` over `_fdo_is_llvm`) unless the resolved kernel compiler is clang — `compiler = "llvm"`, or a resolved/`$CC` basename of `clang*`. `--propeller` is a modifier that requires `--autofdo` (`_resolve_fdo` validates the combination). Recommended over BOLT for the kernel — the two are not stacked.

**Branch-sampling preflight.** `kernel_fdo.detect_branch_sampling` resolves the `perf -b` event from `/proc/cpuinfo`: Intel uses LBR (`BR_INST_RETIRED.NEAR_TAKEN`), AMD uses BRS — available only on **Zen 3+** (family ≥ 0x19; `--pfm-events RETIRED_TAKEN_BRANCH_INSTRUCTIONS`), and **experimental** for AutoFDO. Pre-Zen3 AMD has no usable branch-sampling path and is reported unsupported. A `--autofdo=record` run surfaces this capability (and the experimental caveat) as a WARN before the operator commits to building and booting a profiling kernel, so the feasibility risk is visible up front.

**Kernel stage boot-safety.**

The kernel stage must never leave the machine unbootable. Three gates wrap the build/install, backed by `primitives/kernel_safety.py` (the policy — what aborts vs warns — lives in the stage; the facts live in the primitive). Brick-class findings (`is_brick=True`) hard-fail; everything else warns.

To make a *pre-install* hard-fail possible, the build is **split from the install**: the stage builds with `BuildOptions.no_install=True` (the profile's `-i`/`--install` flags are stripped via `INSTALL_FLAGS`), audits the resolved `.config`, then installs the produced artifact via `makepkg_wrapper.install_built_packages()` (a `sudo pacman -U` of the built `.pkg.tar*`). The artifact is located via `_find_artifacts`, which searches the union of `pacman.get_pkgdest()` (makepkg's configured `PKGDEST`, env- or conf-resolved) and the PKGBUILD dir — so a non-default `PKGDEST` doesn't strand the install looking in the wrong directory (the same resolver path the ABI report and `build_core` use). Because `PKGDEST` is shared across every build, `install_built_packages` scopes that union down to *this* build's artifacts (`_artifacts_for_pkgbuild`) — otherwise a populated `PKGDEST` would drag every previously-built package into the kernel's `pacman -U`. The authoritative scope is a **build-time manifest**: `_capture_built_manifest` records the exact basenames `makepkg --packagelist` prints against the *patched* `PKGBUILD.sysforge` (rename + dropped subpackages applied) on build success, and the install matches that set exactly. This is essential for a renamed kernel — by install time the patched PKGBUILD is cleaned up, leaving only the upstream PKGBUILD whose pkgname (`linux`) prefix-matches unrelated (`linux-custom`, `linux-steam-integration`) and stale (`linux-sysforge-<oldver>`) artifacts, which the pre-B9 pkgname scope swept into `pacman -U` and could use to downgrade the running kernel. Absent a manifest (or when none of its entries are present), it falls back to pkgname scoping (`_parse_built_pkg_filename`-matched declared pkgnames), then to the full union when the PKGBUILD can't be parsed — so a static-parse limitation degrades to locate-everything rather than installing nothing. The manifest is removed after a successful install. Separately, `pkgbuild_patcher.patch_kernel_config_install` injects a `/boot/config-<release>` install into the kernel's `package()` (preferring the split `package_<pkgbase>()`, falling back to a bare `package()`, then to the unsuffixed `_package()` helper used by the standard Arch kernel PKGBUILD's `eval`-loop split-package idiom — which synthesizes the real package functions at runtime and is therefore invisible to a static parser) when the PKGBUILD doesn't already ship one, so the resolved `.config` is pacman-tracked under `/boot`; it locates the build tree via `include/config/kernel.release` and is skipped idempotently when `/boot/config` is already installed. A third sibling patcher, `pkgbuild_patcher.patch_kernel_btf_guard` (also called from `makepkg_wrapper` for every kernel build), gates the stock PKGBUILD's bpftool `vmlinux.h` step: `make -C tools/bpf/bpftool vmlinux.h` and the `package()` install of the produced `vmlinux.h` both hard-require a `.BTF` section in `vmlinux`, which only exists when `CONFIG_DEBUG_INFO_BTF=y`. A BTF-off resolved `.config` — e.g. `base_config="running"` seeded from a lean, debug-info-free kernel — would otherwise fail the build at `failed to find '.BTF' ELF section`. The patcher wraps the build step in, and reduces the `vmlinux.h` install to, a runtime `if [[ $(scripts/config -s CONFIG_DEBUG_INFO_BTF) = y ]]` guard (the same idiom the PKGBUILD already uses for `CONFIG_DEBUG_INFO_BTF_MODULES`), evaluated against the real resolved config — so a BTF-on build keeps both steps and a BTF-off build skips them, with no sysforge-side BTF prediction. It is idempotent (a `# sysforge: BTF guard` sentinel) and a no-op when the step is absent (commented out / already removed). Without the split, Arch's pacman hooks (`kernel-install`/mkinitcpio) would build the initramfs and boot entry *at install time* — before any audit could run. The build mutates nothing and runs **outside** the sentinel, so a Gate 2 abort leaves the system completely untouched (nothing installed, no sentinel set).

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

**Concurrency lock.** The build → Gate 2 → install window is additionally wrapped in `primitives.build_lock.build_lock(state_dir / "kernel-build.lock", label="kernel", noun="build")` so two concurrent `sysforge run kernel` runs sharing a state dir can't clobber `~/builds/<pkgbase>` (the second `nconfig`/makepkg would step on the first's `.config`). This is the **same shared primitive** the toolchain stage's PGO lock (`_pgo_lock`) and the stage sentinel's liveness lock both delegate to. It remains distinct from the sentinel *record*: the *lock* is transient mutual exclusion held only for the run, while the *sentinel* persists an interrupted boot-critical mutation across runs — the sentinel now pairs the two, using a lock of its own to tell a live owner from a dead one (see *Interrupted-install protection* below). The kernel lock lives under `state_dir` (not `/var/tmp` like the PGO lock, whose staging dirs are genuinely global), so per-state-dir test runs stay isolated. Skipped in `--dry-run` (nothing is built).

### Packages stage (stage 6)

Fires the opt-in pre-build snapshot (see *Build-time estimate and pre-build snapshot* above) once, before the build loop starts.

Walks `packages.toml` in order:
- `source = "repo"` → `sudo pacman -S --needed --noconfirm`
- `source = "aur"` / `"git"` → `_resolve_pkgbuild()` → `makepkg_wrapper.run()`. PKGBUILD lookup order: `packages.toml [build] pkgbuild_src_dir` → `profiles.toml [paths] pkgbuild_src_dir` → AUR clone.
- Non-fatal per-package failures: build continues, failures recorded in state
- Summary at end: `Total | Built | Failed | Skipped`

The AUR-dep build and per-package install loop are wrapped in `sentinel_scope(state_name="packages", …)` (no `recovery_cmd` — there's no single shell command that restores a partially-installed package set; the operator verifies with `pacman -Dk` and re-runs `sysforge run packages`). Per-package `RuntimeError` is caught and reported via the state machine; only an interruption or unexpected exception inside the scope preserves the sentinel.

### Toolchain stage (stage 5)

**Opt-in:** stage is a clean no-op if `/etc/sysforge/toolchain.toml` is absent or has `enabled = false`. Systems that skip this stage use whatever compiler is already installed; packages and kernel stages proceed normally.

**`toolchain.toml` structure:**

```toml
enabled     = true   # must be true to activate the stage
compiler    = "gcc"  # "gcc" (default when key absent) or "llvm"; LLVM is opt-in
pgo         = true   # only meaningful when compiler = "llvm"; ignored for gcc
skip_build  = false  # skip build; just register compiler paths in pipeline state

# Staging prefixes. Pass 1 outputs land in stage1 (system /usr never touched);
# Pass 3 outputs land in stage2 and are used as CC/CXX in Pass 4; Pass 4 stages
# the freshly-built OPTIMIZED libLLVM into stage3 so the non-pgo suite links
# against the libLLVM that ships (ABI coherence — see Pass 4 below).
pgo_staging1 = "/var/tmp/sysforge-llvm-stage1"
pgo_staging  = "/var/tmp/sysforge-llvm-stage2"
pgo_staging3 = "/var/tmp/sysforge-llvm-stage3"

# PGO data dir: profraw files written here during Pass 3, merged to clang.profdata
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

**Repo-install path (`compiler = "llvm"`, `pgo = false`, `packages.toml [build] repo_mode = "pacman"`).** When the operator has opted LLVM in but left PGO off *and* `packages.toml` is in the default `pacman` repo mode, the stage honours that package-sourcing preference and installs the stock LLVM suite from the Arch repos (`pacman -S --needed --noconfirm` over the resolved `[packages]` lists) rather than compiling it. It then registers the `/usr/bin/clang`/`clang++`/lld paths with `variant = "stock_llvm"` and returns — no PKGBUILD resolution, no makepkg. The install is the system-mutation window, so it runs inside the shared `stage_sentinel.sentinel_scope` (recovery = `sudo pacman -S <suite>`); `--dry-run` logs the intended install and skips it. Decision precedence for `compiler = "llvm"`: **PGO on → always build from source** (a profiled toolchain is the whole point of enabling PGO and has no repo artifact, so `repo_mode` is irrelevant); **PGO off + `repo_mode = "build_from_source"` → single-pass build**; **PGO off + `repo_mode = "pacman"` → this repo install**. `repo_mode` is read through the single chokepoint `config.resolve_repo_mode`; a missing/unreadable `packages.toml` defaults to `pacman`. The install helper has one home — `pacman.install_repo_pkgs`.

**Profile-default propagation.** On a *successful* register/build — the GCC register-only short-circuit, the `skip_build` path, the LLVM repo-install path, or the LLVM final pass after Gate 3 — the stage writes `profiles.toml [defaults] toolchain` to match `toolchain.toml`'s `compiler` via `config.set_default_toolchain` (`_propagate_default_toolchain`, the single home). This makes `toolchain.toml` the upstream source of truth: the package-compiler default (`flag-profile-system` → *Toolchain field*) always tracks the compiler the stage just registered, so a system that enables the stage with `compiler = "gcc"` builds packages with gcc, and flipping to `"llvm"` propagates to every package without a second edit. It writes only the **user live** `profiles.toml` (the file the resolver reads), only on success (a failed LLVM build returns before this point, so the default never flips to an uninstalled clang), and never on `--dry-run`; an unwritable config is a WARN, not a stage failure. This is *not* `toolchain_variant` (which records what was *built*, in `build_state.toml`).

**Build-safety gates + build/install split (kernel-parity).** The LLVM path mirrors the kernel stage's three-gate / build-install-split structure so a broken or doomed build can never leave the live `/usr` toolchain inconsistent. The pure, unit-testable facts live in `primitives/toolchain_safety.py` (`ToolchainFinding(severity, check_id, message, remediation, is_brick)`); the toolchain stage owns the abort/warn *policy*. `toolchain_safety` imports `LLVM_LOCKSTEP_SUITE` from `toolchain_preflight` (both primitives — no layering issue) and is **not** a third health-check entry point: `_verify_llvm_install` (pipeline, post-install) and `toolchain_preflight._probe_cc` (primitives, update path) remain the only two, and `_verify_llvm_install`'s skew arm now draws from `toolchain_safety.detect_suite_skew`.

- **Gate 1 — pre-build preflight** (`_gate1_preflight`, outside the sentinel, runs for both PGO and non-PGO). Brick-class aborts *before any build time is spent*: PKGBUILD pkgver skew across the lockstep suite (`check_pkgver_lockstep`; `spirv-llvm-translator` + `lib32-*` are excluded so their legitimately-different versions don't false-positive — the bug the old whole-set `_check_pkgver_consistency` had); a non-functional clang (`smoke_test_compilers`, now run on the non-PGO path too); insufficient build-filesystem headroom (`check_build_space`, deduped by `st_dev`); `[multilib]` disabled while a `lib32-*` is in scope (`check_multilib_enabled`). Each brick is overridable (`--allow-version-skew`, `--skip-build-space-check`, `require_multilib = false`) and **downgraded to a warning in `--dry-run`**. Advisory (warn-only): residual `-fprofile-generate` instrumentation; an incomplete rollback snapshot.
- **Bootstrap-compiler self-heal (inside Gate 1, `_install_bootstrap_compilers`).** The LLVM path is a 4-pass bootstrap: Pass 1 must be compiled by an *already-installed* `/usr/bin/clang`, and `lld` links every pass. Both are needed **earlier than any other build prerequisite** — before `build_core.batch_install_makedeps` runs — and neither appears in the llvm PKGBUILD's `makedepends` (upstream builds with gcc), so nothing else in the pipeline would ever install them. On a clean machine (`base-devel` ships gcc, not clang) the stage therefore bricked on `smoke:clang_missing` with a manual `pacman -S clang` hint, making the LLVM toolchain unreachable without an out-of-band step. Gate 1 now maps the *missing*-class smoke findings to their providing package (`_BOOTSTRAP_PKG_FOR`: `smoke:clang_missing`→`clang`, `smoke:lld_missing`→`lld`), installs them via `install_repo_pkgs` inside a `sentinel_scope` (a mutation window, sequential with — never nested in — the build sentinel), then **re-runs `smoke_test_compilers` and acts on the result**, so a freshly-installed-but-broken clang still aborts. `smoke:clang_broken` is deliberately *not* auto-installable: it signals a mismatched lockstep suite from an aborted run, whose remediation is reinstalling the whole suite under the operator's eye, not a blind `-S clang`. In `--dry-run` the install is previewed and the original brick is preserved (downgraded to a warning by the enclosing gate).
- **Soname gate — pre-build, between Gate 1 and the build** (`_gate_soname_consumers`, calls `toolchain_safety.assess_libllvm_soname_impact`). PGO/optimization never changes `libLLVM`'s `SOVERSION` or exported symbols, so a same-version rebuild is ABI-identical and silent; but a libLLVM **version bump** changes the soname (`libLLVM.so.22.1` → `libLLVM.so.23.0`), and every installed package that links the old soname (mesa's radeonsi/radv/llvmpipe, and others) is left with a dangling `NEEDED` until rebuilt. The gate derives the target soname from the resolved llvm PKGBUILD pkgver, reads the installed soname authoritatively from `llvm-libs`'s on-disk file list, and — when they differ — enumerates the installed consumers (every package whose recorded `%DEPENDS%` carries a `libLLVM.so=<old major.minor>` soname dep, via `dep_analysis._SONAME_RE`), collapsed to pkgbase and **excluding** the LLVM lockstep suite + the packages being built this run (they're rebuilt here already). The `rebuild_soname_consumers` mode (CLI `--rebuild-soname-consumers` > `toolchain.toml` > `"prompt"`) decides: `prompt` (default) warns + lists + asks, approving captures the consumers for rebuild after Gate 3 and declining is a clean abort (non-TTY aborts — never silently break the system, pointing at `=auto`/`=off`); `auto` approves without prompting; `off` builds the toolchain but rebuilds nothing, printing the manual `sysforge build <consumers>` command. `--dry-run` previews the impact without prompting or rebuilding. The facts function is pure (guarded reads degrade to "no impact"); the stage owns the prompt/abort policy.
- **Build (outside the sentinel).** All passes build with `install=False`; the build functions return the built package map. A build-pass failure therefore mutates nothing and leaves **no sentinel** (matches kernel).
- **Gate 2 — pre-install ABI audit** (`_gate2_audit`, outside the sentinel, both paths). Scans the built `.pkg.tar*` for the `_ZNSt*@LLVM_*` hazard via `toolchain_safety.scan_abi_hazards`, **and** for a **graphics-consumer symbol shortfall** via `toolchain_safety.check_system_consumer_symbols`: it extracts the about-to-be-installed `libLLVM.so.*` from the built packages (lib32 excluded) and, for each installed external consumer that links that exact soname (mesa's `libgallium`/DRI/Vulkan drivers, curated in `_SYSTEM_LIBLLVM_CONSUMER_GLOBS`), checks every `LLVM_x.y`-versioned symbol the consumer imports is exported by the new libLLVM. `_diff_consumers_against_libllvm` splits a miss into two classes (see §`graphics-stack` → *Two stranding classes*): an **unhealable** target-init drop (`LLVMInitialize*` — a reduced `LLVM_TARGETS_TO_BUILD` dropped a backend the consumer needs, e.g. AMDGPU; the symbol exists nowhere) **aborts before any `pacman -U`** — nothing installed, no sentinel; a **healable** `std::` re-export drop (only non-target-init `LLVM_*` symbols — the `-fprofile-use` libLLVM inlined away the weak libstdc++ copies the stock build re-exported at the *same* soname) does **not** abort. For the healable case Gate 2 enumerates the installed libLLVM consumers via `toolchain_safety.libllvm_abi_consumers` (the reverse-dep `%DEPENDS%` walk shared with the soname gate, here un-gated on a soname change), applies the `rebuild_soname_consumers` mode (prompt/auto/off, mirroring the soname gate), and **returns** them for rebuild after Gate 3. This is the gate that `scan_abi_hazards` (intra-build only) never covered: it checks *already-installed* external consumers, the gap that let a target-reduced libLLVM ship.
- **Snapshot.** Right before the install, `cached_pkg_files_for(lockstep suite ∪ built names)` (in `primitives/pacman.py`) locates each currently-installed member's `.pkg.tar*` in the pacman cache. This is the offline-undo source. Gate 1 warns up front when any member's archive is missing (auto-undo will fall back to `pacman -S`).
- **Gate 3 — post-install verify, inside the sentinel** (`_verify_llvm_install`). Its `expected_targets` (for the `built ⊇ expected` `llvm-config --targets-built` check) is the **actually resolved** target set — `resolve_or_detect_llvm_targets(TOOLCHAIN_PATH, <state_dir>/hardware_profile.toml)`, the same value the build patched in — **not** just `toolchain.toml [llvm] targets`. On an autodetect host that key is unset, so the old form resolved to `None` and skipped the check entirely; sourcing the resolved set makes it actually assert (after the Part-1 baseline, that includes AMDGPU). A **post-install graphics arm** then runs `toolchain_safety.check_installed_consumer_symbols()` (the same symbol diff as Gate 2, but vs the *now-installed* `/usr/lib/libLLVM.so.*`). Only **unhealable** (target-init) findings fold into the verify issues; **healable** `std::` re-export misses are *expected* here — mesa is not rebuilt until *after* Gate 3, so folding them in would auto-rollback the very libLLVM the queued consumer rebuild is about to make coherent — and are logged, not failed. On any (unhealable) failure, the stage **auto-restores** the prior-good toolchain from the snapshot in one `pacman -U` transaction (`_rollback_to_snapshot` → `batch_install_pkgs`): if restore succeeds the live `/usr` is whole again, so the sentinel is **cleared** and a `RuntimeError` is raised telling the user to investigate; if restore fails or the snapshot was incomplete, the sentinel is **kept** with `recovery_cmd` set to the snapshot restore (offline `pacman -U <cached>`, falling back to `pacman -S <suite>`). So a target-reduced libLLVM that somehow reached install is reverted automatically while rollback is still armed, rather than surfacing as a black screen at next login.

- **Consumer rebuild — after Gate 3, outside the sentinel** (`_rebuild_soname_consumers`). The rebuild set is the **union** of the pre-build soname gate's consumers (a soname *bump*, mode `prompt`-approved or `auto`) and Gate 2's healable `std::` re-export drift consumers (a same-soname ABI regression) — deduped. The now-installed-and-verified toolchain is followed by a rebuild of those consumers through the shared `build_core.build_and_install` engine (resolved via `find_pkgbuild` — auto-cloning repo packages like mesa — and the user's normal profile, so they re-link the new libLLVM: a soname-bump consumer picks up the new `NEEDED`, a std::-drift consumer re-links the dropped symbols to libstdc++ `@GLIBCXX_*`). It runs **outside** the toolchain sentinel deliberately: a consumer rebuild failure must **not** roll back the intended toolchain bump — it surfaces as an actionable `RuntimeError` naming the failed packages and the manual `sysforge build` command, with the new toolchain left healthy. The system self-stabilises — once a std::-drift consumer is rebuilt it no longer imports the `…@LLVM_<ver>` `std::` symbols, so a later `run toolchain` sees no stranding and queues no rebuild.

The mutation window is therefore exactly install → Gate 3 → (rollback), followed by the out-of-band consumer rebuild. The concurrent-run lock (`_pgo_lock`) wraps the whole build → audit → snapshot → install window, like the kernel stage's `kernel-build.lock`. A consolidated resolution summary (`_log_toolchain_resolution_summary`) prints the compiler/pgo/variant, package counts, staging paths, gate settings, and snapshot availability — the readable core of `--dry-run`.

**PKGBUILD resolution:** follows `find_pkgbuild` lookup order (local `pkgbuild_src_dir` → `pkgctl repo clone`) for every package. After path resolution, the stage routes each unique resolved `pkgbuild_dir` through `SourceSyncScheduler.sync_many()` so missing trees are cloned and pre-existing trees are refreshed against AUR/repo upstream — same RPC short-circuit, rate-limit, and dirty-tree handling as `sysforge update`. Pass `--no-update` to skip the sync step (use whatever is on disk verbatim). Blocker statuses (`STATUS_FAILED`, `STATUS_RATE_LIMITED`, `STATUS_PURGE_REFUSED`) abort the stage; `STATUS_DIVERGED` is a warning. Resolved paths are then displayed in a table and the user is prompted to confirm or abort. On abort, the resume command is printed (`sysforge pipeline --resume --state-dir <dir>`) so they can make manual modifications and return.

**LLVM PGO bootstrap (only when `pgo = true`):**

Every PGO pass runs makepkg with `--cleanbuild --force` so the prior pass's `.pkg.tar.zst` in PKGDEST never short-circuits the next build (each pass produces a different artifact at the same pkgver). `makepkg` runs without `--install` — sysforge controls when (and where) Pass outputs land. **Only the final Pass 4 install touches `/usr`.** Earlier passes go to staging prefixes; the live system is never made ABI-incoherent mid-run. A sudo keepalive thread refreshes credentials every 60 seconds throughout the sequence (the final `pacman -U` still needs root). `llvm-profdata` is invoked with `RLIMIT_AS` lifted (`resource_guard.lift_for_child`) so it is not constrained by the sysforge controller's 2 GiB virtual address space cap.

The sequence is **four builds** (Pass 1, Pass 2, Pass 3, Pass 4) across three on-disk staging prefixes (`pgo_staging1` → `pgo_staging` → `pgo_staging3`) before the final install. Pass 4 is itself split into coherent sub-passes (4a → 4b → 4c):

1. **Pass 1** — instrumented build of the pgo packages (`llvm`, `llvm-libs`) with the system compiler + `-fprofile-generate=<pgo_store>/`. Every output is **extracted to `pgo_staging1`** (no `pacman -U`, no live-root mutation), including the cmake-config / static-lib `llvm` package — Pass 2's `find_package(LLVM)` needs those configs. The instrumented `.a` archives that land alongside surface `__llvm_profile_*` link errors for anything that consumes LLVM component targets; Pass 2 and Pass 3 work around that by **force-loading** the clang profile runtime into LDFLAGS **and selecting lld** (see below). Spurious profraw from CMake feature probes is purged before later passes begin.

2. **Pass 2** — **non-instrumented** build of the non_pgo packages (`clang`, `lld`, `compiler-rt`, `polly`, `openmp`, `spirv-llvm-translator`) against stage1. The Pass 2 environment sets `CMAKE_PREFIX_PATH=<staging1>/usr` **and** injects `-DLLVM_DIR="<staging1>/usr/lib/cmake/llvm"` (same `cmake_llvm_dir` mechanism as 4b — see Pass 4 for why the env var alone is insufficient) so `find_package(LLVM)` resolves stage1's headers and configs; the resulting binaries link against stage1's `libLLVM.so` and are ABI-coherent with it (which keeps the Pass-3 training compiler coherent too). **LD_LIBRARY_PATH is deliberately NOT set** for Pass 2 — that would force the host `/usr/bin/clang` to load stage1's libLLVM and recreate the version-skew failure mode this design exists to prevent. `linker_flags_extra = _profile_runtime_ldflag()` force-loads the runtime via `-Wl,--push-state,--whole-archive <runtime_dir>/libclang_rt.profile-x86_64.a -Wl,--pop-state` so the instrumented static archives' `__llvm_profile_*` references resolve at link time. Crucially, Pass 2 passes `toolchain_variant="pgo_llvm"` so the variant-driven linker default (below) injects `-fuse-ld=lld`: the PGO bootstrap runs under a `CC=gcc` profile whose makepkg.conf defaults to **bfd**, and bfd's strict left-to-right archive resolution would otherwise drop a non-force-loaded runtime *before* the instrumented archives reference it — the historical Pass 2 `undefined reference to __llvm_profile_*` failure. lld is order-independent, and the `--whole-archive` form is order-proof under either linker (belt-and-suspenders). Pass 2 outputs are extracted into the same `pgo_staging1`, making it **self-sufficient**: stage1 now has a working clang and a working libLLVM, both built from the in-tree LLVM source, both ABI-coherent.

3. **Pass 3** — training run. CC is `<staging1>/usr/bin/clang` (built in Pass 2), and the Pass-3 environment redirects dyld / cmake at stage1 via `LD_LIBRARY_PATH=<staging1>/usr/lib:…`, `CMAKE_PREFIX_PATH=<staging1>/usr:…`, `PATH=<staging1>/usr/bin:…`. The running clang and the libLLVM it loads are guaranteed coherent because they were built together — no possibility of version drift against `/usr`. Pass 3 builds pgo + non_pgo packages; the act of running stage1's clang against stage1's instrumented libLLVM generates profraw as a side effect. `LLVM_PROFILE_FILE` uses `%m_%p` (per-module-hash + per-PID) so parallel `make -j` clang processes each write their own `.profraw` file rather than contending on one; `CCACHE_DISABLE` and `SCCACHE_DISABLE` are set so neither cache tool bypasses the instrumented compiler. `linker_flags_extra` carries the same force-loaded profile-runtime LDFLAGS, and Pass 3 likewise sets `toolchain_variant="pgo_llvm"` (lld), so its non_pgo `find_package(LLVM)` builds against stage1's instrumented `.a` archives still link cleanly. A background daemon merges profraw into `clang.profdata` every 15 seconds using adaptive batch sizing (starts at 128 files; halves on OOM; minimum batch 8). No install. After the build, Pass 3 binaries are extracted to `pgo_staging` (stage2). The stage2 outputs are **non-instrumented** since Pass 3 doesn't apply `-fprofile-generate`.

   **Training-corpus enrichment (`[packages] training_corpus`, opt-in).** Pass 3's profile only reflects the code clang was made to compile, and LLVM's own C++ self-build under-exercises graphics/C-heavy codegen. `training_corpus` (default `["llvm"]`; `"llvm"` is the implicit base) lists *extra* packages — currently `"mesa"` — that Pass 3 additionally compiles **with the same instrumented stage1 clang and the same `LLVM_PROFILE_FILE`**, so their profraw lands in the same `pgo_store` and merges into the one `clang.profdata`. The resolution/build seam has one home: `_resolve_training_corpus(tcfg)` returns the non-llvm extras (unknown members warned + dropped, "llvm" stripped, deduped); the call site resolves them through the same scheduler-synced `_resolve_all_pkgbuilds` path as the toolchain packages (best-effort — a resolution miss degrades to an LLVM-only corpus with a warning, never blocks the build) and threads a `corpus_map` into `_build_llvm_pgo_inner`, which runs a second `_build_pass` right after the main training build (daemon still merging). Three invariants make this safe and correct: corpus members are **never installed** and **never `-fprofile-use` targets** (the profile stays clang-keyed — applying clang's profile *to* mesa would be a category error; that is Phase-3 mesa-PGO, a separate cycle); the corpus build uses `staged_deps=True` (`--nodeps`, no `--syncdeps`) so it keeps the **no-pacman-mutation** invariant and the extras' makedepends must already be installed; and the build is **best-effort** — a failure (missing makedep, mesa configure quirk) is logged and the run proceeds with whatever LLVM-only profraw was collected, so enrichment can never brick the toolchain build. LLVM/PGO path only (a non-PGO single-pass build generates no profraw).

4. **Pass 4** — final optimized build of pgo + non_pgo (+ lib32 only if explicitly opted in; empty by default — see *lib32 is not toolchain-managed*) with `-fprofile-use=<clang.profdata>`. It is split into coherent sub-passes so the non-pgo suite links against **the exact libLLVM that ships**, not the live `/usr` one:

   - **4a — pgo** (`llvm`, `llvm-libs`): the optimized PGO build. CC selection is conditional on whether `<pgo_staging>/usr/bin/clang` exists: when the PGO set includes `clang` (so Pass 3 produced a staged clang), 4a uses `CC=<pgo_staging>/usr/bin/clang` and redirects dyld / cmake at stage2 (`LD_LIBRARY_PATH`/`CMAKE_PREFIX_PATH`/`PATH`) — the staged clang's NEEDED libLLVM is stage2's, so the redirect is ABI-coherent. The shipped default only PGO-builds `llvm`/`llvm-libs` (clang is `non_pgo`, lives in stage1, never stage2), so the **system-clang fallback** applies: `CC=/usr/bin/clang` with the stage2 redirect **suppressed** (pointing stock clang at stage2's `LLVM_TARGETS_TO_BUILD`-restricted libLLVM would recreate the Pass 2 version-skew failure — missing target-init symbols like `LLVMInitializeBPFTarget`).
   - **stage3 extract** — the just-built optimized `llvm`/`llvm-libs` `.pkg.tar*` are extracted into `pgo_staging3` (via `_extract_pass2_to_staging`), so `<pgo_staging3>/usr` holds the libLLVM + headers + cmake configs that the install will ship. **Both split members must be staged**: `llvm-libs` carries `libLLVM.so*` while the `llvm` (dev) package carries `usr/lib/cmake/llvm/LLVMConfig.cmake` and the headers — without the latter, 4b's `find_package(LLVM)` finds nothing under `CMAKE_PREFIX_PATH` and silently falls back to the system `/usr` libLLVM (defeating the whole split → Gate-3 brick). Because the artifact lookup matches per-pkgname, the glob is **version-anchored** (`{name}-[0-9]*-*.pkg.tar*`, same idiom as `pacman.cached_pkg_files_for`) so the shorter `llvm` pkgname does not swallow the `llvm-libs-…` sibling artifact. As a belt-and-suspenders guard, `_assert_staging_has_llvm_cmake(pgo_staging3)` runs immediately after extraction and **aborts before install** if `LLVMConfig.cmake` is absent — converting a multi-hour build + bricked install into an immediate, actionable error.
   - **4b — non_pgo** (`clang`, `lld`, …): steered at the just-built optimized libLLVM via **both** `CMAKE_PREFIX_PATH=<pgo_staging3>/usr` (build env) **and** a `-DLLVM_DIR="<pgo_staging3>/usr/lib/cmake/llvm"` injected into the PKGBUILD `cmake` line (`pkgbuild_patcher.patch_llvm_dir`, threaded as `cmake_llvm_dir` through `_build_pass` → `make_build_options` → `_run_build`). The env var alone proved **insufficient**: `find_package(LLVM CONFIG)` silently resolved the live `/usr` libLLVM despite `CMAKE_PREFIX_PATH` (env-var search precedence lost to the system prefix), so clang/lld linked the wrong libLLVM and bricked at Gate 3. `LLVM_DIR` is the highest-precedence config-mode override — a cmake **cache** variable, checked before any prefix search and persisted in `CMakeCache.txt` across the PKGBUILD's repeated `cmake ..` calls (so injecting after the first invocation is sufficient, mirroring `patch_llvm_targets`). Both cmake-arg injectors append the new `-D` arg at the **true end of the (possibly `\`-continued) cmake statement** (`_cmake_statement_end`), not the first newline — so `-DLLVM_TARGETS_TO_BUILD` (injected first, for `clang`/`lld`/`compiler-rt`) and `-DLLVM_DIR` compose on one command. Inserting at the first newline instead splices the second arg into the first's continuation chain and orphans an arg, which bash then runs as a command (`-DLLVM_TARGETS_TO_BUILD…: command not found`, `build()` exit 4). Both injectors also anchor on the cmake **configure** invocation via `_find_cmake_configure_anchor`, **not** a bare `^cmake\b` line match: the anchor requires a same-line argument (so a lone `cmake` element in a multi-line `makedepends=(...)` array — e.g. `spirv-llvm-translator` — is never chosen; the loose match spliced `-DLLVM_DIR` into the dependency array and makepkg rejected it, exit 12) and skips action-mode invocations (`cmake --build`/`--install`/`-E`/…) which ignore `-D` cache args. **Post-patch validation gate:** after all injectors run and before makepkg, `pkgbuild_patcher.validate_patched_pkgbuild` (called from `makepkg_wrapper._run_build`, gated to the injection path) re-parses the patched PKGBUILD and fails fast on the two corruption classes that previously surfaced only hours into a build — **G1** requires the parsed dependency/identity globals (`depends`/`makedepends`/`checkdepends`/`provides`/… via `pkgbuild_meta.parse_pkgbuild`) to be unchanged from upstream (catches a `-D` arg landing in a dep array), and **G2** requires each managed injected token (`-DLLVM_TARGETS_TO_BUILD=`/`-DLLVM_DIR=`) to ride a `cmake` command's continuation-joined logical line (catches an orphaned arg, while ignoring legitimate `-D …` *array elements* like `cmake_options=(-D FOO=ON)` since only the two managed tokens are checked). A violation raises `PkgbuildPatchError` and aborts before anything is built. **Why this matters:** the std::-symbol re-export profile flips between build modes — stock/instrumented libLLVM emits an out-of-line weak copy of e.g. `std::string::_M_assign`, which the `LLVM_X.Y { global: *; }` version script globs into the LLVM namespace (so libLLVM *exports* `_M_assign@@LLVM_X.Y`), while the `-fprofile-use` build inlines it away and imports it from libstdc++ (`@GLIBCXX_*`). If clang were built against an *exporting* libLLVM (stock `/usr`, stage1, or stage2) but the run ships the *non-exporting* PGO libLLVM, `libclang-cpp` would dangle `_ZNSt*@LLVM_X.Y` and the live clang would brick at the first symbol lookup. Building 4b against stage3 makes clang record its true ABI (`@GLIBCXX_*`), coherent with the shipped libLLVM. As in Pass 2, **only `CMAKE_PREFIX_PATH` is set in the env, never `LD_LIBRARY_PATH`** (the host clang compiles the source; it must not be forced to *load* the staged libLLVM — `-DLLVM_DIR` steers only the cmake config lookup, not the runtime loader). **Post-build link assertion:** immediately after 4b (still pre-install), `_assert_pass_links_shipped_libllvm` reruns `toolchain_safety.scan_abi_hazards` on the just-built `clang`/`lld` `.pkg.tar*`; a correctly-steered build yields **zero** `_ZNSt*@LLVM_*` undefined refs, so a non-empty result means the steering was *still* defeated and the stage aborts **before install** (no sentinel, no rollback), naming the offending pass — the earliest, most specific catch.
   - **4c — lib32** (only when explicitly opted in): same `CMAKE_PREFIX_PATH=<pgo_staging3>/usr` + `-DLLVM_DIR` steering and the same post-build link assertion.

   Across all sub-passes `LLVM_PROFILE_FILE` is cleared so any inherited Pass-3 training env can't leak, and `linker_flags_extra` is left unset (stage3's LLVM is non-instrumented — no `__llvm_profile_*` references, and no Pass 2/3 residual flag leaks into the final binaries). The install — the **only** `sudo pacman -U` against `/usr` — runs after the Gate-2 audit on the built packages. Staging prefixes (stage2 *and* stage3) are removed only **after** the post-install verify passes (see below), so a failed verify keeps the stage prefixes on disk for diagnostic inspection. Profdata is **preserved** at `<pgo_store>/clang.profdata`; a version sidecar `clang.profdata.version` (LLVM major integer, e.g. `22`) is written alongside it so `sysforge update` can check compatibility before reusing the profdata.

**Pre-install ABI hazard check (Gate 2).** Between the Pass-4 build and the final `sudo pacman -U`, `_gate2_audit` extracts each built `.pkg.tar*`'s shared libraries and scans their `nm -D` output via `toolchain_safety.scan_abi_hazards` (which uses `abi_check._undefined_versioned`). Any UND versioned symbol whose mangled name is in the C++ stdlib namespace (`_ZNSt*`) and whose version starts with `LLVM_` is a hard block: it means a built binary (typically `libclang-cpp.so`) recorded a `std::string` (or similar) requirement against the `LLVM_X.Y` version namespace — i.e. clang was linked against a libLLVM that *re-exports* the C++ stdlib while the run ships one that does not. Installing those binaries would leave the live toolchain unable to resolve `std::string` methods at runtime (`symbol lookup error: libclang-cpp.so: undefined symbol _ZNSt..., version LLVM_22.1`). The Pass-4 coherent sub-pass split (4b builds the non-pgo suite against the shipped libLLVM in stage3) is the **primary** prevention; the 4b/4c post-build link assertion (above) is the earliest catch; Gate 2 is the final **backstop** that refuses to install if a hazard slips through anyway. Gate 2 runs *outside* the sentinel, so a hazard aborts with the live `/usr` intact and **no sentinel**; the user is told to restart with `--rebuild-profdata`. (This scan moved out of `_pgo_install` — which now only installs — and is shared with the non-PGO path.) Gate 2 (and the 4b/4c assertion) depend on `abi_check._extract_sos` actually extracting: it now `mkdir`s its destination before `bsdtar -x -C` (`scan_abi_hazards` extracts each package into a per-package subdir to avoid same-named `.so` collisions, and that subdir does not pre-exist — without the `mkdir`, `bsdtar` failed with *"could not chdir to …"*, the audit saw zero shared objects, and the gate passed **vacuously**, which is exactly how the brick reached install).

**ABI-safety invariant (Path B).** The live `/usr` is observably coherent before and after every step except the single final `pacman -U`, and even that is now reversible: Gate 3 verifies the result and auto-rolls-back to the snapshot on failure. A run that aborts before install (build failure, Gate 1, or Gate 2) leaves the system exactly as `sysforge run toolchain` found it — nothing installed, no sentinel. A run whose install verifies-bad restores the prior-good suite from the pacman cache. No half-installed instrumented `libLLVM.so`, no orphaned `/usr/bin/clang` that can't resolve `LLVMInitializeBPFTarget@LLVM_22.1`. The only role `/usr/bin/clang` plays in the run is **as a bootstrap host compiler in Pass 2** (compiling source into objects, never loading a different-version libLLVM); version drift between the in-tree LLVM source and the installed system packages is therefore no longer a failure mode.

**Stage ownership (`sysforge update` skip).** The install-bearing final pass (Pass 4, or the single pass when `pgo = false`) stamps `owner_stage = "toolchain"` into `build_state.toml` via `BuildOptions` — mirroring how the kernel stage stamps `owner_stage = "kernel"`. `sysforge update` honours that marker and skips the LLVM suite by default, pointing the user at `sysforge run toolchain` instead of rebuilding `llvm`/`clang`/`lld`/`compiler-rt` mid-sweep. Intermediate PGO passes (1/2/3) leave the marker unset so their transient, soon-overwritten staging writes don't claim ownership. Before the first toolchain-stage build has written that stamp — and for build_state entries written by older sysforge versions that predate the field — the config bootstrap fallback in `primitives/stage_ownership.py` (consulted by `update.py` via `load_stage_ownership()`) reads `toolchain.toml` and applies the same skip, but **only when** the stage is `enabled` *and* `compiler = "llvm"` (the default/unset `gcc` path is register-only and owns no LLVM, so stock pacman LLVM stays pacman-class and is left alone). Ownership is the **union** of `is_llvm_pkgbase` (prefix match: `llvm`/`clang`/`compiler-rt`/`lld`) **excluding `lib32-*`** and the explicit `toolchain.toml [packages]` lists captured in the same snapshot. The `lib32-*` exclusion is deliberate: the toolchain stage doesn't build lib32 by default (see *lib32 is not toolchain-managed*), so claiming implicit ownership would make `update` skip them with nothing building them; they're toolchain-owned only when explicitly listed in `[packages] lib32`. The configured set is what catches members `is_llvm_pkgbase` doesn't match by prefix — notably `spirv-llvm-translator` (and any custom-listed package) — so they're skipped too, not just the prefix set. `--include-stage-owned` overrides the skip; naming an LLVM package explicitly on the `sysforge update` command line is an opt-in for that run. This is the exact analogue of the `kernel.toml` bootstrap fallback (see the kernel stage's stage-ownership note).

**lib32 is not toolchain-managed.** `[packages] lib32` defaults to **empty** (`_DEFAULT_LLVM_LIB32 = []`); the toolchain stage builds no lib32 packages unless a user explicitly opts them back in. The reason is a target-set asymmetry: lib32 LLVM packages ship no headers of their own and compile against the all-target 64-bit `/usr/include/llvm` headers, but the toolchain stage's host-driven `LLVM_TARGETS_TO_BUILD` filter (e.g. `X86;NVPTX`) would reduce lib32-llvm's target set. lib32-clang's GPU-offload tools (`clang-nvlink-wrapper`, `clang-sycl-linker`) call `InitializeAllTargets()` — resolved from those all-target headers — and then fail to link against the reduced lib32 libLLVM (`ld.lld: undefined symbol: LLVMInitializeAArch64AsmParser`, etc.). PGO adds nothing either: the profile is trained on the x86_64 clang self-build and is discarded by the i686 (`-m32`) build. lib32 packages instead come from `sysforge update` (repo, full targets, no PGO). Two guards keep an explicit opt-in correct anyway: `makepkg_wrapper._maybe_patch_llvm_targets` never injects a reduced target set for `lib32-*` (always all targets, matching the shared headers), and `makepkg_conf`'s lib32 scrub strips `-fprofile-use` (via `makepkg_flags._strip_pgo_flags`) so no foreign-arch profile reaches the i686 build. `build_diag` recognises the reduced-target link failure (`toolchain:lib32-reduced-target`) and points at the real fix rather than a spurious "version skew".

**Pass 2 skipped when `non_pgo` is empty.** Minimal configs (tests, intentionally-narrow rebuilds) can set `[packages] non_pgo = []`. In that case stage1 has no clang, and Pass 3 falls back to `/usr/bin/clang` — recreating the bootstrap-host-clang behaviour, where the user is responsible for keeping system clang ABI-coherent with the in-tree LLVM source. The non-empty default (clang/lld/compiler-rt/...) is the supported path.

**BOLT post-link optimization (Pass 5, opt-in, EXPERIMENTAL — currently BLOCKED).** With `toolchain.toml [bolt] enabled = true`, a fourth pass layers BOLT (Binary Optimization and Layout Tool) on top of the PGO toolchain — the canonical **PGO→BOLT "fast clang"** stack. BOLT is the fourth optimization method on the shared profile-store rails (after the toolchain PGO, mesa PGO, kernel AutoFDO) and the only **post-link** one: unlike PGO/AutoFDO there is no compiler flag, so the one home is `primitives/bolt.py` (store, command builders, `collect_profile`/`bolt_binary`, tool checks, **and the generated `llvm-bolt` PKGBUILD**) and the orchestration is `_run_bolt_pass4`.

> **BLOCKED — the standalone tool build does not link against a dylib-only LLVM.** Every BOLT tool (`llvm-bolt`, `perf2bolt`, `bat-dump`, `heatmap`, …) is built with `DISABLE_LLVM_LINK_LLVM_DYLIB` in `bolt/tools/*/CMakeLists.txt`, so it links the *per-component* static LLVM archives (`libLLVMObject.a`, `libLLVMMC.a`, the X86 target libs, `libLLVMTransformUtils.a`, …) rather than `libLLVM.so`. The PGO toolchain — like the stock Arch `llvm` package — is **dylib-only**: it ships `libLLVM.so` plus only the handful of static libs that can't live in the dylib (`Support`/`Demangle`/`TableGen`/…), so those component archives are **absent** and a standalone `llvm-bolt` link fails with `ld.lld: error: unable to find library -lLLVMObject`. (`merge-fdata` is the lone tool that links — it needs only `Support`.) The fix is to build BOLT **in-tree** with the toolchain's LLVM, where the build tree still holds the archives; that is not yet implemented. Until then `_build_bolt_tools` guards on `bolt.standalone_build_viable()` (probes for `libLLVMObject.a`/`libLLVMMC.a`) and, on a dylib-only host, skips Pass 5 with a clear WARN before downloading the tarball — so `[bolt] enabled = true` is a clean no-op rather than a late link failure. Keep `enabled = false`.

*Why it is experimental — and where the tools come from.* BOLT (`llvm-bolt`/`perf2bolt`/`merge-fdata`) is **not in the official Arch repos**, and the stock `llvm` package does **not** build it. But the `bolt/` subtree ships *inside* the `llvm-project-$pkgver.src.tar.xz` monorepo tarball the toolchain's `llvm` PKGBUILD already downloads, and BOLT supports a standalone build (`bolt/CMakeLists.txt` sets `BOLT_BUILT_STANDALONE` and runs `find_package(LLVM)`) — exactly how Arch builds `clang`/`lld` as separate packages from the same tarball. So sysforge **builds the BOLT tools itself** rather than depend on an out-of-repo package: `bolt.materialize_pkgbuild` generates an `llvm-bolt` PKGBUILD (modeled on the `clang` component, version-locked to the installed llvm — BOLT links libLLVM internals, so a mismatch won't even configure) into `pkgbuild_src_dir/llvm-bolt/`, and `_build_bolt_tools` builds+installs it standalone against the just-installed PGO libLLVM (`sha256sums=SKIP`: the tarball is byte-identical to, and shares makepkg's SRCDEST cache with, the one the `llvm` build PGP-verified moments earlier). The only thing the user must supply is `perf` (linux-tools), needed for profile collection.

*Mechanism.* (1) When BOLT is enabled, Pass 4a (libLLVM) and 4b (clang) link with `-Wl,--emit-relocs` (`bolt.emit_relocs_ldflag`, threaded as `linker_flags_extra` through `_build_llvm_pgo_inner` → `_build_pass`) so the shipped binaries retain the relocations BOLT needs — lib32 (4c) is never BOLTed and is untouched. (2) After the PGO toolchain installs *and* **passes Gate 3**, `_run_bolt_pass4` runs inside the sentinel (so a mishap stays covered by the snapshot rollback) in two steps: **4a** builds+installs the BOLT tools (above), then **4b** profiles the installed `/usr/bin/clang` on a representative compile job (a generated header-heavy C++ TU, or `[bolt] training_workload`) via `perf record` → `perf2bolt`, rewrites it with `llvm-bolt` (ext-tsp block layout, hfsort+ function reordering, function/cold/eh splitting, icf), **smoke-tests** the result, and only then atomically replaces `/usr/bin/clang`. It is **best-effort and never regresses the working toolchain**: a failed tool build (4a), a missing `perf`, a `perf`/`llvm-bolt` failure, or a failed smoke test WARNs and leaves the verified PGO clang in place. BOLT optimizes the installed binary **in place under its stock name** (not a `-sysforge` rename — the toolchain stage *is* the in-place system-toolchain replacement, guarded by Gate 3 + snapshot rollback; a renamed system compiler would bypass that machinery and rename what every `depends=llvm` resolves against). One consequence: because it is a post-link rewrite of an installed file, `pacman -Qkk clang` reports it modified — inherent to post-link optimization, not corruption. The `llvm-bolt` package is stamped `owner_stage = "toolchain"` so `sysforge update` skips it like the rest of the suite. `[bolt] libllvm` (default off) extends BOLT to the more-fragile `libLLVM.so`; the clang executable is the default target. LLVM/PGO path only; off by default.

**Dep resolution for staged passes.** Pass 1 builds against the live `/usr` and keeps the profile-supplied `--syncdeps`, so missing build tools (cmake, ninja, python, z3, libffi, …) are pacman-installed normally. Pass 2, Pass 3, and Pass 4 build against a stage prefix; `CMAKE_PREFIX_PATH=<staging>/usr` makes `find_package(LLVM)` see the staged headers and cmake configs, but pacman has no knowledge of those staged packages. `_build_pass(staged_deps=True)` therefore strips `--syncdeps`/`-s` (via the shared `SYNC_FLAGS` constant from `makepkg_wrapper.py`, the same set `pacman.BATCH_STRIP_FLAGS` removes for batch builds) from the resolved profile's makepkg flags and appends `--nodeps` for those three passes. Without that, makepkg's pre-build dep check would invoke `sudo pacman -S llvm=<pkgver>` and fail with "target not found" (the just-built version is not in any repo). The non-llvm build deps stay required — they're expected to already be on the system from Pass 1's `--syncdeps` install.

**Concurrent-run lock.** `ToolchainStage.run` acquires an advisory `flock(2)` (`_pgo_lock`, the shared `build_lock` primitive) on `_pgo_lock_path(staging1)` = `<pgo_staging1>.parent/sysforge-pgo.lock` (typically `/var/tmp/sysforge-pgo.lock`) around the whole build → audit → snapshot → install window — not just the PGO passes, so the non-PGO path is guarded too (mirroring the kernel stage's `kernel-build.lock`). The sentinel scope guards re-entry on the state-dir but not the `/var/tmp` staging dirs or `~/pgo`, both of which two concurrent runs would corrupt. The lock file holds the owner's PID, so the loser surfaces "another sysforge PGO build is running (pid N)" rather than a confusing mid-flow failure. The path is in `staging1.parent` rather than inside `pgo_store` so the Pass-1 purge cannot delete it. Skipped in `--dry-run` (the lock file would be a side effect).

**Post-install libLLVM resolution check.** `_verify_llvm_install` runs `ldd /usr/bin/clang` and `ldd /usr/bin/lld` and asserts that any `libLLVM*.so` lines resolve under `/usr/lib`. A `/var/tmp/sysforge-llvm-stage*` path appearing in `ldd` of an installed binary means Pass 4 packaged a bad RPATH or the install is incomplete — `/usr` looks consistent until `/var/tmp` gets cleaned, at which point the live toolchain silently breaks. The verify-stage check catches that before the sentinel clears.

**Verify-failure diagnostic dump.** On a `_verify_llvm_install` failure, `ToolchainStage.run` calls `_dump_stage_dynsym_evidence(staging3, state_dir)` before the recovery prompt. The brick is a C++ stdlib symbol bound to libLLVM's `LLVM_<ver>` version node, so the actionable evidence is the **set difference** between what the *installed* consumers (`libclang-cpp` / `liblldCommon`) demand under `@LLVM_*` and what the *installed* `libLLVM` provides — read straight from the live `/usr` files that bricked. The dump lists those missing symbols (the brick cause) and, when the kept `stage3` libLLVM *does* export them, notes that the shipped libLLVM diverged from what Pass 4b linked against — pointing directly at an incomplete/incoherent stage3. It is written to `<state_dir>/llvm_abi_hazard.log`. (This replaced an earlier dump of `stage2`'s `libLLVM` defined exports, which on the profdata-reuse fast path captured a stale/empty prefix that never contained the brick symbols.) The installed libLLVM is selected to **match the soname the consumers actually link** (`_newest_so` keyed on the consumer's `lib*.so.<major>.<minor>` version), not a lexical-first glob — otherwise a compat package's older runtime (e.g. `llvm21-libs`'s `libLLVM.so.21.1` sitting beside `llvm-libs`'s `libLLVM.so.22.1`) is picked, its `@LLVM_21.1` exports satisfy nothing the `@LLVM_22.1` consumers demand, and the diff reports a false *"0 NOT provided"* all-clear that hides the brick. Staging removal is deferred until verify passes, so the `stage3` prefix survives the failure path for this contrast. The log path is surfaced in the WARN block alongside the suggested recovery command.

**Profdata reuse:** before purging `pgo_store`, the stage checks for an existing `clang.profdata` + version sidecar. The sidecar's LLVM major version is compared against the `pkgver` in the pgo PKGBUILDs (not the installed version — the toolchain stage builds a *new* version). If compatible (same major), passes 1–3 are skipped entirely and only the optimized build (Pass 4) runs, using system clang as CC (which, after a prior successful run, is already PGO-optimized). Stage1/stage2 are not needed in this path, but **stage3 still is**: Pass 4 stages the freshly-built optimized libLLVM there and builds the non-pgo suite (4b) against it for ABI coherence — exactly as on the full path. `--rebuild-profdata` forces a full 4-pass build regardless, e.g. after upstream codegen changes within the same major version.

**Pass-4 input-fingerprint reuse (opt-in).** Profdata reuse skips the *training* (passes 1–3) but still rebuilds every Pass-4 package. When a *late* Pass-4 package fails (e.g. `spirv-llvm-translator` in 4b), a rerun would needlessly re-optimise the identical `llvm`/`llvm-libs` (4a, the heaviest target) and other unchanged packages. The opt-in input-fingerprint cache (`primitives/build_fingerprint.py`, CLI `--reuse-built` > `toolchain.toml reuse_unchanged` > off) skips a Pass-4 package whose **inputs are unchanged and whose built artifact is still on disk**. The fingerprint folds everything that determines the output: the upstream PKGBUILD content hash, the source-tree git HEAD, the compiler `--version` line (`compiler_version_line` — **not** the full `clang_identity` path+size+mtime: Pass 4 runs with the staged stage-2 clang on a profgen run but `/usr/bin/clang` on a resume, so keying on the binary's path/bytes would guarantee a miss on the exact rerun-after-failure case reuse exists for; the actual trained codegen is already pinned by the profdata hash below, and a genuine version bump still invalidates — 2.1.0-B14), the injected/profile flags (`compiler_flags_extra`/`linker_flags_extra`/`cmake_llvm_dir`/`extra_flags`), a `config_digest` (`hash_obj` of the flag-relevant config — `profiles`/`rules` + `toolchain.toml`, the latter because `[llvm] targets` drives the `LLVM_TARGETS_TO_BUILD` patch the upstream-PKGBUILD hash can't see), the `clang.profdata` content hash (hashed once per run), and the installed versions of the **external** build-deps only. The toolchain build-set members (`llvm`, `llvm-libs`, `clang`, …) are **excluded** from that dep-version fold: Pass 4 satisfies them from the staging prefix (`--nodeps`), so their live-`/usr` version is not a build input, and the staged libLLVM they actually link is already pinned via `staged_dep_fps`. Folding it in would spuriously invalidate every Pass-4 fingerprint the instant the suite is self-installed — defeating cache reuse on a post-install sanity re-run, since e.g. `clang`'s `depends=('llvm-libs')` would flip the moment the freshly-built `llvm-libs` is installed (`_dep_versions_from_globals(exclude=…)`, 2.5.1-B2). Sub-passes are **Merkle-chained**: 4b/4c fold in 4a's current fingerprints (`staged_dep_fps`), so if a rebuilt libLLVM's fingerprint shifts, every consumer's does too and they rebuild — no stale libLLVM can ride through a cache hit. The cache is keyed by `(pass_id, pkgbase)` and stored at `<pgo_store>.parent/build-cache/build_cache.json` (`_reuse_cache_path`) — a **sibling** of `pgo_store`, deliberately outside it so a fresh 4-pass start's `empty_dir_contents(pgo_store)` purge cannot wipe a prior run's records before the resume consults them (each entry binds its own `profdata_sha`, so a surviving-but-stale entry from a different profdata fail-safe-misses rather than mis-hitting — 2.1.0-B14). It is **always written** (so a first, non-opted-in run populates it) but only **consulted** when opted in. The whole mechanism is fail-safe — a missing/changed artifact (size or nanosecond mtime), a fingerprint mismatch, a config edit, a schema bump, dry-run, or the GCC register-only path all force a normal rebuild; it never reuses a stale build. `--rebuild-profdata` (full 4-pass + `pgo_store` purge) also bypasses it. The logic lives in `_build_pass` (per-package skip/record, returning `{pkgbase: fingerprint}` for the chain) with the cache primitives in `build_fingerprint`; do not add a second cache or skip path.

**Sidecar write timing.** The version sidecar is written **right after Pass 3 completes** (after the final profraw merge produces `clang.profdata`, before Pass 4 starts) — not after a successful Pass 4 install. The sidecar's only invariant is "this profdata is for LLVM major N", which is determined entirely by what Pass 3 instrumented; Pass 4 success has no bearing on it. Writing it post-Pass-3 means a Pass-4 failure (e.g. a transient toolchain bug, an aborted run) still leaves recoverable profdata that the next invocation can reuse via `_check_existing_profdata` rather than being forced into a full 4-pass rebuild. The major itself is derived from the in-tree PGO PKGBUILD `pkgver` (`_pgo_target_major`), matching the value `_check_existing_profdata` will later compare against — symmetric with the reuse check, and correct across major bumps where `pacman -Q llvm` would report a stale value.

**Confirmation gating (PGO).** Unlike the rest of sysforge (which is automation-focused), the LLVM PGO sub-flow is fragile enough that wrong profdata silently mis-optimises the resulting compiler. Four decision points in `_build_llvm_pgo` therefore prompt the user before destructive or long-running work, all sharing a single `_pgo_confirm` helper:

1. **Reuse vs rebuild** — when compatible profdata is found, prompt `[Y/n]` to reuse; declining triggers a full 4-pass rebuild (and continues into prompts 2–3).
2. **Purge `staging/` and `pgo_store/`** — prompt `[y/N]` before clearing; declining aborts PGO. The `staging1/2/3` dirs (under world-writable `/var/tmp`) are `rmtree`'d, but `pgo_store` is cleared with `fs_provision.empty_dir_contents` — it lives under the root-owned FHS parent `/var/cache/sysforge`, so `rmtree`'s final `rmdir` would need write on that parent and fail with `EACCES`; emptying the contents (all unprivileged-written) keeps the node and needs no parent write. After the purge, `pgo_store` is (re)provisioned via `fs_provision.ensure_writable_dir` (`root:sysforge 2775` — see *Directory provisioning* above). A user-writable / env-override `pgo_store` takes the direct path and never touches sudo.
3. **4-pass start** — prompt `[y/N]` before launching the ~2–3 hour 4-pass sequence; declining aborts PGO.
4. **Suspicious Pass-3 profdata size** (`< 10 MiB`) — prompt `[y/N]` to continue into Pass 4; declining aborts before Pass 4 so the user can investigate instrumentation.

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

**Variant-driven linker soft default.** `emit_makepkg_conf` injects `-fuse-ld=lld` into `LDFLAGS` when (a) `toolchain_variant in {"stock_llvm", "pgo_llvm"}`, (b) no explicit `ld_override` was passed, (c) the resolved `LDFLAGS` (profile first, then system conf) declares no `-fuse-ld=` linker, (d) `lld` is on `PATH`, and (e) this is not a kernel build. The defaults-not-overrides rule is the key invariant: a profile that already declares `LDFLAGS="… -fuse-ld=mold"` keeps mold, and an explicit `BuildOptions.ld_override` still wins (hard override beats soft default). Effect: any build path that flows through `BuildOptions.toolchain_variant` — `packages` stage, `sysforge update`, `sysforge build`, and the **PGO bootstrap's own Pass 1/2/3/4** — picks up the toolchain's linker without each caller having to repeat the propagation. The PGO consumer passes (2/3) depend on this specifically: they run under the `CC=gcc` profile (default bfd), and the `pgo_llvm` variant is what flips them to lld so the force-loaded profile runtime resolves regardless of archive order. The kernel build path is opt-out because kernel linker selection is controlled by `LLVM=1`, not LDFLAGS. `gcc` and `system` variants skip the injection so sysforge doesn't override the user's makepkg.conf when it has no LLVM opinion.

**Stale-state wipe (disabled / absent stage).** When `toolchain.toml` is absent or has `enabled = false`, the stage clears any prior `[stages.toolchain.result]` from pipeline state before returning. This prevents the failure mode where a user runs `compiler = "llvm"`, disables the stage, and subsequent `packages`/`kernel` stages keep using the stale `cc=/usr/bin/clang`/`ld=lld` overrides — the disable opts out of all downstream LLVM propagation, not just the build.

**Interrupted-install protection.** Three layers wired into the LLVM build path (the GCC path is register-only and skips all three). Note the sentinel now wraps only the install → Gate-3 → rollback window (the build runs before it, outside the sentinel — see *Build-safety gates* above):

1. **Stage sentinel** (`primitives/stage_sentinel.py`) — writes `<state_dir>/stage_in_progress.toml` just before the `sudo pacman -U` of the run and clears it after Gate-3 verification passes (or after a successful auto-rollback, since the system is then whole again). Schema records `stage`, `started_at`, `compiler`, `pgo`, and a `recovery_cmd` string. The recovery command is **snapshot-aware** (`_snapshot_recovery_cmd`): when every suite member's prior `.pkg.tar*` is cached it's an offline `sudo pacman -U <cached files>`; otherwise it falls back to the online `sudo pacman -S <suite>`. On every subsequent sysforge invocation, `cli.main()` calls `check_and_recover_stale_sentinel()` before dispatching install-bearing commands (`build`, `update`, `run *`, `setup`) — the gate is centralised in `cli._gate_sentinel_check(args)`, which also skips read-only invocations (`--dry-run`) so users can inspect the system without first running recovery. If a sentinel is found, the operator is prompted to auto-run the recovery command (`[y/N]`). **TTY-only prompt:** when stdin is not a TTY (background sessions, scripts, IDE wrappers), the prompt would silently auto-decline; `check_and_recover_stale_sentinel` instead emits an explicit error naming the sentinel file path and the recovery command, then returns False. **Verify-after-clear:** after `sentinel.clear()` runs, the recovery path checks that `sentinel.path.exists()` is False before printing "Recovery completed". A still-present file means the recovery cleared a different path (state-dir mismatch, namespace/chroot surprise) — the path is logged loudly so the operator can investigate instead of trusting a false-positive cleared message. Refusing recovery exits with status 2 and leaves the sentinel in place; success clears the sentinel and proceeds.

    **Liveness guard.** Presence of the sentinel cannot distinguish "a previous run died mid-mutation" from "a run is alive right now" — and the recovery prompts above are unanswerable, and destructive, against a live run: clearing a live owner's sentinel lets the second run's `mark_started` overwrite the record wholesale, after which whichever run finishes first `clear()`s the other's (silently — `clear()` suppresses `FileNotFoundError`), leaving two concurrent install-bearing runs with no interruption record for either. So `sentinel_scope` also holds an exclusive `flock` on `<state_dir>/stage_in_progress.lock` for the stage's lifetime, via the shared `primitives/build_lock.py` primitive (its `noun` parameter carries the "install stage" wording; the build stages pass `noun="build"` and keep their existing messages). Liveness becomes "is the lock takeable?", which the kernel answers correctly even after `SIGKILL` or power loss — a PID recorded in the sentinel could not, since PID recycling would report a dead owner as alive forever, unrecoverable given the no-override decision below. Two layers use it: `check_and_recover_stale_sentinel` probes the lock at CLI entry and returns False **without prompting** when an owner is alive (naming the holder PID and the recorded stage), making the clear prompt unreachable; and `sentinel_scope` acquires it for real — before `mark_started`, so a live owner's record is never overwritten — catching the probe/acquire race plus any mutating verb reached outside `cli._gate_sentinel_check`'s allowlist. Contention there raises `RuntimeError`, which `verbs/runner.py` already converts to exit 1. **No override:** a live owner is unambiguous, so a prompt would only invite the mistake the guard prevents; escaping requires killing the owning process. The posture splits by error type — contention is strict (hard refusal), while `OSError`/`PermissionError` stay lenient (warn + proceed, matching the sentinel write itself) so a read-only state dir cannot lock the user out of every mutating verb. Per-state-dir scoping is automatic since the lock lives in the state dir, so an isolated `SYSFORGE_STATE_DIR` (test fixtures, VM) never contends with a live run. Scopes are sequential, never nested — the `run` verbs set `requires_sentinel = False` precisely so a verb-level scope cannot wrap a stage's own.

2. **Post-install verification (Gate 3)** — after the `pacman -U` of the LLVM run, `_verify_llvm_install()` checks: (a) `pacman -Q` versions across `_LLVM_VERSION_MATCH_SET` (which *is* `LLVM_LOCKSTEP_SUITE` from `toolchain_preflight` — `llvm`/`llvm-libs`/`clang`/`lld`/`compiler-rt`/`polly`/`openmp`) agree, via `toolchain_safety.detect_suite_skew` (the canonical interrupted-install symptom — a mismatched `llvm-libs` is the exact failure mode that produces a broken GUI). The suite spans several independent pkgbases, so lockstep is on **pkgver** (the upstream release keying the `LLVM_<ver>` ABI namespace); a pkgrel-only divergence across pkgbases (e.g. an `llvm` -2 rebuild beside `clang` -1) is a legitimate state and is *not* a skew — only the shared-pkgbase pair `llvm`/`llvm-libs` is held to full `pkgver-pkgrel` lockstep. (b) `clang --version` and `ld.lld --version` invoke cleanly without missing-symbol errors (probing `ld.lld`, the GNU-compatible flavor `-fuse-ld=lld` resolves to — bare `lld` is the generic multiplexer driver and always exits 1 without a flavor), (c) `ldd` of installed clang/lld resolves libLLVM under `/usr/lib` (`_check_llvm_link_resolution`), (d) when `[llvm] targets` is configured, `llvm-config --targets-built` is a superset. On failure the stage **auto-rolls-back** to the pre-install snapshot rather than prompting (the kernel-parity overhaul replaced the old interactive `_prompt_llvm_recovery`): a successful restore clears the sentinel and raises "prior toolchain was restored"; a failed/incomplete restore keeps the sentinel with the snapshot recovery command. This verification is comprehensive and fatal, but only runs inside `run toolchain`; if a toolchain run is interrupted before it (or a later partial pacman transaction reintroduces a skew), the broken state can still reach an everyday `sysforge update`. That gap is closed by the `cc:<name>` compiler-health probe in `toolchain_preflight` (see §`toolchain_preflight.py`), which re-detects the suite-wide pkgver skew / non-runnable clang before any package builds — deliberately a lightweight independent check sharing the `LLVM_LOCKSTEP_SUITE` constant rather than importing this pipeline-layer verifier into the primitives layer.

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

Every top-level CLI verb (`build`, `update`, `fetch`, `doctor`, `resolve`, `env`, `help`, `setup`, `log`, `completions`, `packages …`, `state …`, `config …`, `run …`) is a `Verb` subclass — the `Verb` ABC and the `PreCheckResult`/`ExecResult` result types live in `sysforge/verbs/base.py`, while each concrete verb lives in its own per-command module (`build_cmd.py`, `run_cmd.py`, `env_cmd.py`, `help_cmd.py`, `completions_cmd.py`, `update.py`, `packages_cmd.py`, …). Verbs are dispatched through `run_verb()` in `sysforge/verbs/runner.py`. The framework is intentionally thin: three phases, two result types, one runner, one shared sentinel primitive. Argparse wiring in `cli.py` attaches the verb class via `parser.set_defaults(verb_cls=XVerb)` (never a `func=` callback), and `main()` resolves it via `sys.exit(_dispatch(args.verb_cls, args))` — a thin wrapper around `run_verb` that adds the optional cProfile harness (see *Global profiling flags* below).

**Parent-verb subcommand default (invariant).** A verb namespace declares a
default subverb via `set_defaults(verb_cls=…, <dest>=…)` on the *parent* parser
**iff** it has a single obvious read-only "show me" view; otherwise its
subparsers set `required = True`. Today: `doctor` → `system`, `packages` →
`list`, `artifact` → `list`, `state` → `list` carry defaults; `config` and `run`
require a subcommand, because their subverbs mutate or diverge with no natural
landing point. A new namespace picks a side by this test, not by precedent from
whichever namespace was copied. Set the subparser `dest` alongside `verb_cls` so
downstream code sees a consistent subcommand name either way.

**Three-phase contract.** Each verb implements:

- `pre_check(args) -> PreCheckResult` — validate args, load config, run preflights (LLVM safety, dirty-state guards, sudo checks). No state mutation. Returns one of three terminal shapes:
  - **proceed**: `skip_reason=None, blocker=None`, optional `ctx` dict carried into later phases.
  - **skip** (success short-circuit): `skip_reason="…"` — verb exits 0 with the reason logged.
  - **block** (failure short-circuit): `blocker="…", exit_code=N` — verb exits non-zero with the message logged.
- `execute(args, pre) -> ExecResult` — do the work. May mutate state. `ExecResult.exit_code` propagates to the process; `ExecResult.artifacts` is a free-form dict for `post_validate` to read.
- `post_validate(args, pre, result) -> None` — verify post-conditions, write final state, raise `RuntimeError` on failure. Default is a no-op.

**Result types** (`PreCheckResult` and `ExecResult`) are plain dataclasses with `ctx` / `artifacts` dicts; the runner does not inspect their contents. This keeps phase boundaries loose enough for ad-hoc data flow within a verb without inventing a per-verb context class.

**Sentinel handling.** Verbs whose `execute` mutates the live system set `requires_sentinel = True`. The runner wraps `execute + post_validate` in `sentinel_scope(state_dir, verb.name, recovery_cmd=…, retry_cmd=…, **metadata)` from `primitives/stage_sentinel.py`. On entry, the sentinel writes `stage_in_progress.toml`; on normal completion (both phases pass), it clears. On `RuntimeError` or `CleanExitRequested`, the sentinel is left in place so the next sysforge invocation blocks at the CLI-entry recovery prompt. `sentinel_scope` also installs an `InterruptScope`, so verbs participate in the same first-Ctrl-C-defers-to-safe-boundary behaviour as the toolchain stage. It additionally holds the `stage_in_progress.lock` liveness lock for the scope: if another run already holds it, entering the scope raises `RuntimeError` **before** `mark_started`, which the runner's existing handler converts to exit 1 without clobbering the live run's sentinel (§Pipeline layer, *Liveness guard*). The toolchain pipeline stage uses the same primitive — there is one implementation, shared.

**Read-only verbs** (`env`, `help`, `resolve`, `log`, `state list`, `state orphans` without `--prune`, `state failed` without `--clear`/`--clear-all`, `packages list`, `doctor` without `--apply`) implement `execute` (the work is printing) and return `ExecResult()`; `post_validate` defaults to no-op and `requires_sentinel = False`. They use the same dispatch path as mutating verbs — no second code path.

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
| `help` | null | walk the subparser chain for `[COMMAND …]`; `print_help()` | null | no |
| `setup` | read pacman.conf | check + patch IgnoreGroup | re-read confirms write | no |
| `log` | null | resolve unified/per-pkg log path; page through `$PAGER` | null | no |
| `packages {list,add,remove}` | load packages.toml + validate override fields | rewrite TOML | null | no |
| `state {list,repair,orphans}` | load state dir | inspect / repair / prune | null | `repair` only |
| `config merge` | null | scan config dir for `.sfnew`/`.pacnew`; pacdiff-style view/merge/remove loop | null | no |
| `revert-to-stock` | resolve state dir + `plan_revert` over `BuildState` (pure, no mutation) | prompt/`--force`/`--dry-run` gate, then per-target reinstall / atomic replace / derename-then-reinstall via `pacman` | (state written inline) | yes |
| `search` | null | print installed → repo → AUR sections (fixed order, empty omitted) for one term | null | no |
| `uninstall` | resolve state dir + `plan_uninstall` over `BuildState` (pure) | `pacman -Rnsu`, then demote any tracked target via `state forget` + reconcile | (state written inline) | yes |
| `run …` namespace | build `RunOptions` | delegate to `pipeline.run_pipeline` / `run_stage_standalone` | pipeline framework | (pipeline owns it) |
| `artifact list` | null | `primitives/artifacts.unified_rows()` + PATH check + optional `scan()` for `--unmanaged` | null | no |
| `artifact review` | null | interactively offer discovered candidates for adoption via `primitives/artifacts.iter_offerable()`; off-TTY lists candidates + adopt hint | null | no |
| `artifact adopt <path>` | null | `primitives/artifacts.adopt()` — copy live → managed, seed registry entry | null | no |
| `artifact edit <name>` | null | launch editor on managed copy, then `primitives/artifacts.rehash()` | null | no |
| `artifact deploy <name>\|--all` | null | `primitives/artifacts.deploy()` — per-class live write + post-deploy action; refuses on `drifted`/`conflict` without `--force`/`--adopt-live` | null | yes |
| `artifact remove <name>` | null | `primitives/artifacts.remove()` — per-class pre-remove action + live unlink; refuses on `drifted`/`conflict` without `--force`; `--purge` also drops the managed copy | null | yes |

### `revert-to-stock`

Undoes a source-built or optimized package back to its official repo version. `pre_check` resolves the state dir and calls `plan_revert(bs, targets)` (pure — no mutation) to classify each target against `BuildState`, including a reverse lookup via the shared `install_reconcile.resolve_installed_name(bs, name)` helper (iterated in sorted key order so a stock base always resolves to the same pkgname deterministically) so naming the *stock* base of a renamed build (e.g. `mesa` when the tracked entry is `mesa-sysforge`) still resolves. That helper is the single home for the reverse lookup — `uninstall` uses it too. Four actions, distinguished by `profile.is_optimized_build_mode(mode)` and — for optimized builds — `profile.rename_mode_for_build_mode(mode)`:

- **`skip`** — untracked or already `build_mode = "pacman"`; nothing to do.
- **`reinstall`** — plain `source_built`, installed under the stock name: just reinstall the repo package (`pacman -S <name>`).
- **`replace`** — optimized, `conflict` rename (mesa, `pgo`, and every optimization except kernel FDO): the `-sysforge` build declares `provides`/`conflicts` for the stock name, so reverse deps depend on the *stock* name (satisfied by the provides). A `pacman -R <renamed>` would therefore fail ("breaks dependency"). Instead `execute` runs `pacman -S <origin_pkgbase>` **alone** — pacman detects the conflict with the installed `-sysforge` build and removes-plus-installs stock atomically in one transaction, keeping reverse deps satisfied throughout. **No explicit remove.**
- **`derename`** — optimized, `coexist` rename (kernel FDO only): the renamed build genuinely coexists with stock (parallel-installed, version-suffixed /boot files), so reverting needs `pacman -R <renamed>` **then** `pacman -S <origin_pkgbase>`.

This reuses the same `is_optimized_build_mode`/`rename_mode_for_build_mode` predicates the rename/coexist machinery uses elsewhere — no second "is this optimized?" or mode test. `execute` narrates every plan (including skips), then gates on `--dry-run` (report only) and `--force` (skip the confirmation prompt; non-interactive without `--force` refuses with a hint). Actionable targets are processed in order via `pacman.remove_pkgs`/`pacman.reinstall_repo_pkgs`; a `CalledProcessError` **stops processing remaining targets** and returns exit 1 (partial batches never silently continue), with a message naming the step that actually failed: a `derename` remove-step failure reports "nothing changed" (system intact), a `derename` reinstall-step failure warns the system is left without the package (with a recovery command), and a `replace`/`reinstall` single-call failure reports the atomic reinstall failed (system intact). Each successfully-reverted target calls `cmd_state_forget` (the same `state forget` verb code, not a duplicate) so `update` stops rebuilding it, then a final `BuildState.reconcile_external_installs(install_reconcile.external_install_targets())` pass demotes anything pacman now owns as a belt-and-suspenders check — reusing the one-home demotion path rather than adding a parallel one. `requires_sentinel = True` since `execute` mutates installed packages and state.

### `search`

Read-only lifecycle verb (`requires_sentinel = False`). One term is queried against three sources in fixed order — installed (`pacman -Qs`), repo sync DBs (`pacman -Ss`), and the AUR (RPC v5 `search/<term>?by=name-desc`) — and each non-empty section is printed under a header; an empty section is omitted. Local/repo are pacman passthroughs captured with `--color always` (native rendering preserved, emptiness detectable via `pacman.search_local`/`search_repo`); the AUR section is sysforge-rendered from `aur.aur_search` (`aur/<name> <version>` + indented description, colour-matched to the pacman blocks — coloured source prefix, bold name, green version, via `log.use_color()`). Consecutive non-empty sections are separated by a blank line. AUR is the one optional source: `aur_search` swallows network/timeout/JSON errors and returns `[]`, so a search never hard-fails on the third source.

### `uninstall`

Mutating lifecycle verb (`requires_sentinel = True`). `pre_check` resolves each name through the shared `resolve_installed_name` (so naming a stock base reaches its `-sysforge` build) and builds a pure `plan_uninstall` classifying each target's installed name + whether it's tracked. `execute` narrates the plan, removes via `pacman.uninstall_pkgs` (`pacman -Rnsu`, interactive — pacman prints its own confirmation; a `CalledProcessError` returns exit 1 and demotes nothing), then for any tracked target demotes it out of the build-state authority via `cmd_state_forget` (the same `state forget` code) followed by a `BuildState.reconcile_external_installs(install_reconcile.external_install_targets())` pass. This is the exact demotion composition `revert-to-stock` uses — no parallel path — so `update` stops rebuilding an uninstalled package.

### `artifact list`

Read-only lifecycle verb (`requires_sentinel = False`). All logic lives in
`primitives/artifacts.py` (see §Primitives Layer → `artifacts.py`); the verb is a thin shell that
renders `unified_rows()` (managed registry entries joined with sysforge's own hooks via
`pacman_hooks.diff_status()`) as a `STATUS OWNER CLASS NAME` table, warns when
`script_root_on_path()` confirms `False` (never on `None` — an escalated `sudo` invocation
abstains rather than false-warn), and with `--unmanaged` additionally lists `scan()` discovery
candidates not already in the registry and not sysforge-owned.

### `artifact review`

Read-only lifecycle verb (`requires_sentinel = False`) — adoption it triggers is copy-only, never a
live-system write, so it needs no sentinel. Interactively offers discovered candidates for adoption:
`primitives/artifacts.iter_offerable(registry, ignore)` composes discovery
(`scan()`) with the existing managed/sysforge-owned exclusions and a third — declined candidates
recorded in a persistent ignore-list (`<state_dir>/artifacts-ignored.toml`, `path → content-hash`).
The ignore-list is a sibling of the registry, deliberately kept in its own file: the registry is
documented as regenerable (rebuildable from managed content), so folding declines into it would let
a registry rebuild silently forget every "no". It is keyed by both path and the content-hash seen at
decline time, and self-prunes entries whose file no longer exists on `load()`, so a candidate
re-surfaces once its content changes or the ignore entry goes stale. For each remaining candidate the
verb prompts `[a]dopt / [s]kip / [i]gnore / [q]uit` — `a` calls `artifacts.adopt()` as `artifact
adopt` does, `i` records the path+hash into the ignore-list, `s` leaves it to be re-offered next run,
`q` stops the walk early. Off a TTY it never prompts: it lists the reviewable candidates and prints a
`sysforge artifact adopt <path>` hint, exiting 0.

### `artifact adopt` / `artifact edit`

Both read-only-of-the-live-system lifecycle verbs (`requires_sentinel = False`) — they mutate only
`USER_DATA_DIR/artifacts/` and `artifacts.toml`, never a live file, so neither needs the sentinel
protection that guards actual system mutation. All logic lives in `primitives/artifacts.py` (see
§Primitives Layer → `artifacts.py`); the verbs are thin shells.

`artifact adopt <path> [--class C]` copies the file at `<path>` into the managed set via
`artifacts.adopt()` and prints the resulting name/class/dest. `--class` overrides the
root-inferred class; an unknown class, an unreadable source, an already-managed name, or an
attempt to adopt a sysforge-owned hook by name all fail with a clear message and exit 1.

`artifact edit <name>` resolves `name` against the registry (exit 1 if unmanaged), opens the
managed copy in the configured editor (`primitives/editor.py`), and on a clean editor exit calls
`artifacts.rehash()` to recompute `auth_hash` from the saved content. It then prints
`status_of()`'s result for the artifact — `ok`/`pending`/`drifted`/`conflict`/`missing`, computed
fresh from the three-way `auth_hash`/`deployed_hash`/live-file-hash comparison described in
§Primitives Layer → `artifacts.py` — with a `sysforge artifact deploy <name>` hint when the result
is `pending` (a plain edit's expected outcome, since the edit alone can't touch `deployed_hash` or
the live file). A nonzero editor exit status skips the re-hash so a botched edit session doesn't
falsely promote a corrupted save to "current."

### `artifact deploy` / `artifact remove`

Mutating lifecycle verbs (`requires_sentinel = True`) — the only two `artifact` subcommands that
touch the live filesystem, so they carry the sentinel protection and the journal mirror
(`journal_target` returns the artifact name, or `"all"` for a batched deploy — §Standards row 20)
that `list`/`adopt`/`edit` don't need. All contract logic lives in `primitives/artifacts.py` (see
§Primitives Layer → `artifacts.py` → *Per-class deploy/remove contracts*); the verbs are thin shells
over `deploy()`/`remove()`.

`artifact deploy <name>` pushes one managed artifact's authoritative content to its live
destination; `--all` deploys every registered artifact in one run, tallying failures rather than
aborting the batch on the first refusal. A `drifted`/`conflict` artifact (the live file changed
outside sysforge, or already held unrelated content) makes `deploy` **refuse** rather than silently
pick a side — resolve it with one of two mutually exclusive escape hatches: **`--force`** (the
managed copy wins, discarding the live edit) or **`--adopt-live`** (the live file wins: its content
is pulled back into the managed copy and re-hashed, then written back out — the artifact ends up
`ok` with the live file's content now authoritative). There is no default resolution — silently
choosing a side is data loss in one direction or the other. `--adopt-live` is fenced to the drift
states plus `ok`: it refuses on `pending` (it would discard an undeployed managed edit) and on
`missing` (no live file to adopt), so neither an unguarded read nor a silent overwrite of
irreplaceable managed content is possible. An already-`ok` artifact is reported as unchanged rather
than as if it were rewritten. A unit deploy runs `systemctl
daemon-reload` afterward so systemd sees the change immediately. After the run, `deploy` prints the
same PATH warning `artifact list` does, but narrower: once per run (not per artifact) and only when
a `script`-class artifact actually landed *and* `script_root_on_path()` confirms `False` — never on
the `None` abstain (an escalated `sudo` invocation, where `PATH` is `secure_path` and unrepresentative
of the user's own shell).

`artifact remove <name>` removes one managed artifact from the live system. Symmetric with `deploy`,
it **refuses** on a `drifted`/`conflict` artifact unless **`--force`**: the live file holds edits
made outside sysforge that exist nowhere else, so unlinking it would destroy them silently
(`deploy --adopt-live` first is the way to keep them). An enabled systemd unit
is disabled (`systemctl disable --now`) before its file is unlinked, so nothing is left
running-but-file-less. Removing a `pacman-hook`-class artifact prints a warning first — there is no
systemd-equivalent quiesce step for a hook, so the verb itself flags that removal changes what
happens on the next pacman transaction. **`--purge`** additionally drops the managed copy and its
registry row; without it, the managed copy survives (`deployed_hash`/`deployed_at` cleared) so the
same artifact can be `deploy`ed again later without re-adopting it — removing from the live system
and discarding the content are different decisions.

### Top-level help tiers

`sysforge --help` groups the top-level `COMMAND` list into three usage tiers — **Everyday** (`build`, `update`, `fetch`, `search`, `help`), **Inspect** (`doctor`, `resolve`, `env`, `log`, `state`, `artifact`), and **Maintain** (`setup`, `config`, `packages`, `run`, `revert-to-stock`, `uninstall`) — instead of one flat, registration-ordered block, so a new user can tell routine drivers from ad-hoc introspection. The grouping is presentation-only (no behavioural change, no config flag). argparse folds every subparser into a single `_SubParsersAction` pseudo-group with no per-command category hook, so the tiering lives in `cli._TieredHelpFormatter`, which intercepts that one action and re-emits its choices under the tier headers; every other action (options, the `COMMAND` metavar line, per-verb and sub-verb `--help`) formats via the base `HelpFormatter` untouched. The tier map `cli._COMMAND_TIERS` is the single source of truth: `cli.tiered_command_order()` flattens it, and `tools/gen_options.py` orders the man-page COMMANDS sections by that list so the page stays in lockstep with the help (both completions mirror the order too). A `check_completions`-style parity test asserts the map partitions the user-facing verbs exactly (none missing, none duplicated); the internal `completions` verb is registered without help text and stays out of both the map and the listing.

### The `help` verb

`sysforge help [COMMAND [SUBCOMMAND]]` is a read-only alias for `--help`, for users who reach for a
help *verb* before a help *flag*. `HelpVerb` (`help_cmd.py`, `requires_sentinel = False`) re-enters
`cli._build_parser()` — a function-local import, since `cli` imports `HelpVerb` at module scope —
walks the `_SubParsersAction` chain word by word, and calls `print_help()` on the parser it lands on.
It is an alias rather than a re-implementation: the output is the same parser object's help, so
`sysforge help state failed` and `sysforge state failed --help` are byte-identical. An unrecognised
topic is a usage error (exit 2) naming the offending word and listing the valid topics at that level,
not a traceback. Help text goes to stdout via `print_help()` rather than `log.ui`, so it stays
identical to the flag and never accumulates in the log files.

`-h/--help` itself is argparse-supplied at every level and always worked; what was missing was
discoverability in the hand-written completions. Both files now advertise it from a **single**
dispatch point — zsh appends it with `_describe -o` after the per-verb handler runs
(`_sysforge_help_flag`), bash with `COMPREPLY+=(…)` after its `case` — rather than repeating the flag
in all 42 `_arguments` specs and every bash flag list.

### Global profiling flags

Three top-level flags expose sysforge's own runtime performance (stdlib only, no new dependencies). All are position-independent: `_hoist_global_flags` in `cli.py` (a sibling of `_hoist_verbosity_flags`, run in the same argv-preprocessing pipeline) moves them — including `--py-profile-out`'s value token and its `=FILE` form — before the subcommand so argparse accepts them anywhere.

- **`--py-profile`** — `_dispatch` wraps `run_verb` in `cProfile.Profile()` and prints the top 25 functions by cumulative time to stderr at exit (stderr so piped stdout stays clean; the progress bottom-row is cleared first). The profiler stop/report sits in a `finally`, so verbs that `sys.exit()` from inside `execute` still emit stats. Only `run_verb` is wrapped — argv preprocessing and parser construction stay out of the profile. Known limitation: cProfile is main-thread-only, so `update`'s threaded version check shows up as join-wait, and subprocess work (makepkg/pacman/git) appears as wait time — wall-clock phase costs are `--timings`' job.
- **`--py-profile-out FILE`** — additionally `dump_stats(FILE)` for pstats/snakeviz; implies `--py-profile`. A separate flag (rather than an optional argument) so `sysforge --py-profile update` can't swallow the subcommand as a filename.
- **`--timings`** — promotes the wall-clock phase report to UI output after `build`/`update` runs. The phases are recorded unconditionally via `primitives/timing.PhaseTimer` (see §Primitives Layer → `timing.py`) and always written to the log at info level; the flag only changes where the report surfaces. `update` times source sync, version check, drift detection, and `pacman -Syu` around the engine; `build_core.build_and_install` records `dep prep`, per-package `build: <pkgbase>`, just-in-time `install deps: <pkgbase>` (when an intra-batch dep is installed ahead of a dependent), and `install` onto the caller's timer (or its own, exposed as `BuildOutcome.phase_records`).

A fourth global flag, **`--color=auto|always|never`**, is hoisted the same way (it carries a value token). It feeds the colour authority described in §Logging → Colour: `cli._resolve_color_mode` resolves `--color` flag > `[ui] color` config > `"auto"` and calls `log.set_color_mode()` once at startup. `auto` honours `NO_COLOR`/`FORCE_COLOR` and TTY detection; `always`/`never` force the decision (so colour survives a pager pipe, e.g. the coloured PKGBUILD review diff).

Two further global flags, **`--no-throttle`** and **`--turbo`**, are hoisted the same way (both valueless). They set a run-scoped build-throttle override once at startup: `cli._resolve_throttle_override(args)` maps them to `"bypass"` / `"boost"` (`--turbo` wins when both are given) and calls `build_throttle.set_run_override()`, mirroring the colour authority. `resolve_throttle` reads that process-global when no explicit override is passed, so the flags reach the deep `makepkg_invoke` throttle site without a threaded parameter (see §Flag/Profile System → Build throttling).

Three more global flags implement the **source freeze**: **`--frozen`** (valueless) and **`--no-frozen`** (valueless) override `[security] freeze_sources`, and **`--thaw PKG[,PKG...]`** (repeatable, appends) exempts named pkgbases from an active freeze. `net_policy.resolve_net_policy(args, cfg)` resolves the precedence `--no-frozen` > `--frozen` > config > `false` via the shared `config.resolve_flag_default` seam and is called once at CLI entry, installing the result with `net_policy.set_policy()` — mirroring the colour/throttle authorities, a consulted module-global rather than a threaded parameter. See §Config Layer (`[security] freeze_sources`) and §Primitives Layer → `source_sync.py` (`STATUS_FROZEN`).

**Why not unify with the pipeline `Stage` contract?** Stages already presume multi-stage DAG semantics, per-stage checkpoints, and an opinionated `PipelineState`. Most CLI verbs are single-shot and don't want a pipeline state file. The verb framework reuses `sentinel_scope` for install-bearing protection but otherwise stays independent, so `sysforge env` is not paying for pipeline machinery it doesn't need. The `run` namespace verbs are exactly the thin shim from CLI → pipeline.

### Shared build engine (`build_core.py`)

`build` is a strict subset of `update`: both route their actual building through one engine in `sysforge/build_core.py`, so the two paths cannot drift the way they once did (a `build` that left makepkg's `-s`/`--syncdeps` in place would have makepkg run `pacman -S` on an AUR-only dependency and fail, while `update` stripped those flags and pre-resolved every dep itself). `update` extends the shared core with the things that are genuinely its own — version checking, source-sync scheduling, `--install-only`, toolchain pre-flight, the bulk `pacman -Syu`, and the run summary — but the dependency prep, the per-package makepkg invocation, and the install are identical code. Multi-package `build` runs end with their own `Build complete:` totals block (`build_cmd._print_build_summary`, mirroring `update`'s built/failed/skipped/pgo-skipped lines from the `BuildOutcome`); single-package runs skip it since the per-package narration already tells the whole story.

**Repo-package opt-in gate.** `build` source-builds AUR/git/local targets unconditionally — that is their only path — but a **repo** package is normally a pacman binary, so source-building one is opt-in. Before handing a `source = "repo"` target to the engine, `build_cmd` checks whether it is already opted in (global `repo_mode = "build_from_source"` **or** per-package `enable_build_from_source = true`, resolved through `config.resolve_repo_mode` / `expand_package_groups`). If opted in, it builds silently. Otherwise, on a TTY it prompts (`build from source? [y/N]`); a `yes` builds the package **and** records `enable_build_from_source = true` in `packages.toml` (reusing the `packages_cmd` writers — no parallel mutator), a `no` skips just that target and continues the batch. On a non-TTY it skips the target with a hint (set the key, or pass `--force`). The **`--force`** flag bypasses the gate entirely: it source-builds every argument for that run only and never prompts for or modifies `packages.toml` opt-in keys. `--force` is *only* the opt-in waiver — it never reaches makepkg; **`--rebuild`** is the separate flag that forces the build itself (per-target `-f`, above). The gate lives in `build_cmd` (helpers `_load_repo_optin` / `_repo_pkg_opted_in` / `_write_repo_optin`); the source-origin stamping that classifies a target `repo` happens first, so the stamp is unaffected by the gate decision.

**Instrumentation PGO (`build --pgo=record|use`).** Instrumentation PGO is "just a build flag" — it rides the `compiler_flags_extra` seam (`emit_makepkg_conf` appends it to CFLAGS/CXXFLAGS/LDFLAGS) with no second injector and no meson `-Db_pgo` surgery — so it works on **any** package, not only mesa (F5). mesa remains the seeded/default target and the only one with bespoke graphics handling; it is also the canonical example of a *runtime-exercised library*, where an instrumented build only emits profile data when applications later call into it, so the store path is baked into the build rather than discovered at runtime. Every `mesa_pgo` function takes a `pkgbase`:

- **`--pgo=record`** injects `-fprofile-generate=<store>` (store from `mesa_pgo.resolve_store(pkgbase)`: mesa-family keeps the back-compat `makepkg_pgo.resolve_method_store(method="pgo-mesa")` location so already-collected mesa profiles are never orphaned, while any other target gets its own `resolve_method_store(method="pgo", target=pkgbase)` → `<root>/pgo/<pkgbase>`; provisioned `root:sysforge` setgid via `fs_provision.ensure_writable_dir`). The instrumented build installs over the stock package; *any* process that loads it appends `.profraw` to the store with no per-session env setup. The instrumented build is not optimized, so it keeps its stock package name (`build_mode = source_built`).
**Per-target force rebuild (`force_rebuild`).** `resolve_cleanbuild_flags` computes one `batch_flags` list for the whole batch, because cleanbuild is a batch-wide policy. Forcing makepkg (`-f`) is not: it belongs to the individual target. Both `BuildTarget` and `update._UpdateResult` therefore carry a `force_rebuild` flag, and the build loop appends `-f` to *that target's* `extra_flags` alone. It is set by `update`'s drift promotion (toolchain or flag drift → `NEEDS_REBUILD`) and by `build --rebuild`. Both rebuild at an **unchanged** `pkgver`, so the matching artifact is still in `PKGDEST` and makepkg would exit 13 without it — the batch would report success while reinstalling the very artifacts the drift detector objected to (3.0.0-B9). The flag stays per-target because `AlreadyBuilt → REUSE` is load-bearing for the resume case (a run interrupted between build and install must not rebuild what it already has). On a *forced* target `AlreadyBuilt` is unreachable, so the loop treats it as a hard build failure rather than routing it to the reuse posture — that assertion is what keeps the defect from regressing silently.

Either `--pgo` mode forces a **full clean build** (`makepkg -C -c`) regardless of `--no-cleanbuild` / `[build] cleanbuild`: a `record` or `use` pass must never reuse object files left by a *differently*-instrumented prior run (stale objects silently corrupt the profile). Policy lives in one place — `build_core.resolve_cleanbuild_flags(no_cleanbuild=, extra_flags=, pgo_mode=)` returns the `(batch_flags, strip_flags)` pair; when `pgo_mode` is set it returns `-C -c` (never stripped) and the cleanbuild opt-out is ignored (1.2.0-F24).

- **`--pgo=use`** merges the collected `.profraw` into one `<pkgbase>.profdata` (`mesa_pgo.merge_profraw` → `llvm-profdata`; a clean `MesaPgoError` abort if nothing was collected or the tool is missing). The merge folds any prior `<pkgbase>.profdata` back in as an input (cumulative signal, like the toolchain stage) and then **prunes the consumed `.profraw`** — the merged profdata is the durable store, the raw is transient, so the per-package store stays bounded instead of leaking a fresh raw every `record→use` cycle. It then injects `-fprofile-use=<profdata>` (plus `-Wno-profile-instr-out-of-date`/`-unprofiled` so a `-Werror` build tolerates expected profile skew), and earns the `-sysforge` rename. The recorded `build_mode` is `mesa_pgo.build_mode_for(pkgbase)` — `pgo_mesa` for mesa (back-compat), the generic `pgo` for everything else; both are in `_OPTIMIZED_BUILD_MODES`. The rename is applied in `makepkg_wrapper._run_build` gated on `profile.is_optimized_build_mode` (the one home for "does this build earn the suffix?") and is `conflict` mode: `patch_package_suffix` rewrites every split member's pkgname **and its `package_<name>()` function** (or makepkg aborts at packaging time) and injects `provides`/`conflicts`/`replaces` covering the stock names. Attribution is **per member**: makepkg evaluates these arrays per `package_<name>()`, where an in-body reassignment shadows any top-level (global) array, so a member that owns a literal package function (mesa's `package_mesa()` reassigns `provides`/`conflicts`/`replaces`) gets its *own* stock name injected **inside that body** — surviving the reassignment — while only members with no literal function (a single bare `package()`, or `$pkgbase`-cascaded members) fall back to the global injection. A single global covering every stock name was the B1 regression: it was dropped for members that reassign (so `mesa-sysforge` no longer replaced stock `mesa` and failed to install) and over-attributed every sibling's name to members that don't. `_validate_rename` (G3) checks the *effective* (body-overrides-global) array per member, not just the globals, so the broken attribution can no longer pass validation. `build_state` records the renamed names with `origin_pkgbase = <pkgbase>` so `update`'s source-sync still correlates back to the upstream tree.

**Profile reuse is durable across rebuilds.** A source-tracked package is rebuilt every `update` (and any plain `sysforge build <pkg>`) — and without re-applying the profile the user collected, that rebuild would silently regress to a stock, unprofiled build, contradicting the one-shot `--pgo=use`. So when a rebuild runs with **no** explicit `--pgo` mode, `makepkg_wrapper._run_build` calls `mesa_pgo.reuse_profdata(pkgbase)`: if a merged `<pkgbase>.profdata` already exists in the package's store (the durable signal that this host opted into PGO for it), it re-injects `use_flags` through the same `compiler_flags_extra` seam and re-stamps `build_mode_for(pkgbase)`, so the `-sysforge` rename persists too. No re-merge happens — once `use` swaps the instrumented build for the optimized one, no new `.profraw` accrues, so the existing profile is current; and `use_flags` already demotes the skew warnings, so a slightly-stale profile never `-Werror`-fails the rebuild. Bare `.profraw` with no merged profdata (record-only, never `use`d) is *not* reused — there is nothing consumable yet, so the build falls back to normal. Because this path bypasses `BuildVerb.pre_check`'s LLVM gate, the reuse is itself guarded on `profile.is_llvm_toolchain` (resolved compiler: explicit override > resolved profile > env `CC`) so a clang `.profdata` is never fed to a gcc build. To opt back out, remove the store's `<pkgbase>.profdata` (or `sysforge state forget <pkg>`).

The whole feature is LLVM-only — it instruments with clang and merges with `llvm-profdata` — and `BuildVerb.pre_check` blocks cleanly under `toolchain = gcc` via `profile.is_llvm_toolchain` + `LLVM_REQUIRED_HINT` before any build work. PGO is rarely worth the doubled build + manual record/use workload outside a hot, long-lived library, so `pre_check` emits a **"not recommended"** warning (one home: `config.pgo_warns_for`, reading `sysforge.toml [pgo] allow`) for any target that is neither mesa-family nor allow-listed — a warning only, the build proceeds. lib32 mesa is excluded from the flag injection (the lib32 PGO flag-scrub at conf emit strips `-fprofile-*`). See *Flag/Profile System → Flag guards* and §`primitives-layer` (`mesa_pgo.py`).

- **`build_and_install(targets, *, sync_source, …) -> BuildOutcome`** — the engine. Runs `prepare_deps`, then a per-package build loop, then `install_built`, returning the built/failed/pgo-skipped lists and the install-failed flag. Each makepkg call uses `strip_flags = BATCH_STRIP_FLAGS` (`{-s, --syncdeps, -i, --install}`) and `force_batch` when non-interactive, so makepkg never resolves deps via pacman and never installs inline — sysforge owns both. `targets` is any object exposing `pkgbase`/`pkgnames`/`pkgbuild_path`/`source` (`update._UpdateResult` qualifies directly; `build` builds a `BuildTarget` from the parsed PKGBUILD via `target_from_pkgbuild`). When the caller doesn't pass `pkgdest`, the engine resolves it from the system makepkg.conf (`pacman.get_pkgdest`) — artifacts land in `PKGDEST` when one is set, so the post-build snapshot, the `AlreadyBuilt` artifact scan, and the just-in-time install must all search there, not the PKGBUILD dir (the `build` verb relied on the caller default and silently installed nothing on PKGDEST systems; 2026-06-12 fix).
- **Intra-batch dependency ordering + just-in-time install** — before the build loop, `_order_targets_by_intra_deps` topo-sorts the batch (stdlib `graphlib`) by edges from each target's `depends` + `makedepends` + `checkdepends` matched against the other members' `pkgname`s **and `provides`** (version constraints stripped; soname provides like `libvulkan.so` participate; the parse is purely intra-batch — nothing external is queried; a dependency cycle warns and keeps the original order). The build loop then installs a freshly built member's artifacts (via `install_built`) *before* a dependent member's makepkg call, so the dependent configures against the new version instead of the stale installed one; the final bulk install skips files the just-in-time path already handled. Rationale: `prepare_deps`' AUR resolver only orders *missing* deps — a batch sibling already installed at a stale version never creates an edge there, so an alphabetical batch could build a loader against old headers whose new version sat unbuilt later in the same batch (the Vulkan 1.4.354 failure, 2026-06-12). A failed intra-batch dep only warns: the dependent still builds against the installed version and records its own failure normally.
- **`prepare_deps(pkgbuild_paths, config, *, building_names, …)`** — pre-installs missing repo *build deps* in one `pacman -S` transaction (`batch_install_makedeps`) and builds AUR/local deps in topo order (`resolve_aur_deps_batch` + `build_resolved_deps`), excluding the packages about to be built themselves. The repo arm collects `depends` + `makedepends` + `checkdepends` (`pacman.collect_builddeps`), **not just makedepends**: the per-package makepkg call runs with `-s` stripped, and makepkg checks runtime `depends` before building too, so a missing repo runtime dep would abort the build with exit 8 ("Could not resolve all dependencies"). It **filters the missing set to sync-repo packages first** (`aur.repo_packages`, the same classifier the AUR resolver uses) — an AUR-only dep mixed into the `pacman -S` transaction makes pacman abort with "target not found" and install *none* of the repo deps either, so AUR deps are excluded here and left to the AUR arm (which resolves `depends + makedepends`). Both arms are best-effort — a failure warns and lets the build proceed, surfacing a genuinely-missing dep as a per-package build failure with a diagnosis rather than aborting the whole batch up front.
- **`install_built(built_pkg_files, *, always_install=frozenset(), interactive=False) -> (files, install_failed)`** — dedupe, re-fetch the installed set (makedep/AUR pre-install may have expanded it), `filter_pkgs_to_installed` for split-pkgbase safety, then one `pacman -U`. The keep-set is the currently-installed pkgnames **union `always_install`** — the pkgnames the caller explicitly asked to build. `build_and_install` passes the build targets' pkgnames, so a fresh `sysforge build <new-pkg>` installs the package the user asked for instead of dropping it for not being installed yet; for `update` the union is a no-op (its targets are already installed). A conflict-mode `-sysforge` rebuild (e.g. `mesa --pgo=use`) emits artifacts renamed off every stock pkgname (`mesa-sysforge`), so `filter_pkgs_to_installed` keeps them via their `replaces` rather than their absent pkgname — otherwise the optimized build would complete and then be silently dropped at install. Reused by `update`'s `--install-only` artifact-scan branch (which keeps the default empty set). `interactive` threads down to `pacman.batch_install_pkgs(..., interactive=…)`: when set it drops `--noconfirm` and inherits pacman's TTY streams so a package-conflict question (`X and Y are in conflict. Remove Y? [y/N]`) is put to the operator instead of auto-answered `N` and aborting the transaction. `build_and_install` passes its own `interactive` from both the just-in-time and final-install call sites; `update`'s non-interactive call keeps the default (`--noconfirm`). The non-interactive path still has to install the conflict-mode rename without an operator at the prompt: pacman only auto-processes `replaces` on a sync upgrade, so on a local `-U` the renamed drop-in (`mesa-sysforge` declaring `replaces = mesa`) still raises the conflict prompt, which `--noconfirm` declines (default `N`) and aborts. `batch_install_pkgs` therefore adds `--ask=4` (`ALPM_QUESTION_CONFLICT_PKG`) **only when** a built package's `replaces` names a currently-installed package — auto-confirming exactly the intended drop-in removal; absent a real replaces-installed relationship the prompt keeps its safe default so an unexpected conflict still aborts.
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

Implements `sysforge update` — the update manager. The iteration scope is the **live install set**: every installed AUR package (`pacman -Qm`), plus every package sysforge source-built (`build_state.toml` is the tracking authority — `build_mode != "pacman"`), plus any repo packages selected by overrides or by `repo_mode = "build_from_source"`. `packages.toml` entries apply as overrides where present (see §Package Manifest). Organized into 7 phases:

**Phase 0 — Init.** Load BuildState, config, `packages.toml` overrides. Open unified log (always truncated). Refresh AUR name cache (skipped with `--offline` or `--install-only`). Before the superset sync, `_reconcile_external_demotions` consults the pacman-hook sentinels (`primitives/install_reconcile`): any `source_built` package reinstalled from the repo via an external `pacman -S` (buildstate-hook targets minus sysforge's own `pacman -U` self-install targets) is demoted to a plain `pacman` marker so this run no longer rebuilds it from source — the automatic equivalent of `sysforge state forget`. Stage-owned packages are exempt.

**Phase 1 — Package set assembly** (`_assemble_package_set`). Build a unified `{pkgname: entry}` dict by walking the live install set: AUR (`pacman -Qm`) is always walked; every package sysforge source-built is always walked (`build_state.toml` `build_mode != "pacman"` — the authority that makes `sysforge build mesa` durable); repo packages are additionally walked when their `[[package]]` entry sets a behavior-changing field (`enable_build_from_source`, `cache`, or `reason`), or when `[build] repo_mode = "build_from_source"` is set in `packages.toml` (the latter pulls every installed repo package into scope). Each repo entry is sub-classified into `repo_class = "source"` (has a behavior-changing override **or** was already source-built → goes through pkgctl-clone + makepkg) or `repo_class = "pacman"` (no override, never source-built → fast pacman path via `checkupdates` + a single terminal `pacman -Syu`). A source-built repo package must take the `"source"` path: a deferred `pacman -Syu` would be a no-op anyway (`IgnoreGroup = sf-build` shields the installed artifact), so rebuilding from source is the only way to refresh it. A bare `source = "repo"` entry is inert metadata (matches the `sysforge packages add` validator) and is *not* a trigger; the loader emits a warn line so it gets cleaned up. `packages.toml` entries are applied as override overlays (`source`, `enable_build_from_source`, `cache`, `reason`); installed packages with no entry use defaults. Source classification is read from `build_state.toml`'s `source` field when present (set at build time) so a previously-built package keeps its origin across runs; falls through to override → pacman-foreign-inference for unrecorded packages. For AUR packages without a build_state record: bulk `aur_info` resolves the real `pkgbase` (split-package fix, e.g. `ob-xd-common` → pkgbase `ob-xd`). Apply positional PKG filter. Group by `pkgbase` to deduplicate split packages. Manifest entries whose package is not installed (e.g. a stored rule for `mesa-git` while repo `mesa` is installed) are not iterated — they are inert rules under the rules-not-install model.

**Phase 2 — Source sync** (`_sync_sources`). Ensures every iterated package has an up-to-date local PKGBUILD. Skipped entirely with `--offline` (and with `--install-only`, which forces offline since no rebuild will happen). VCS pkgbases (`-git`/`-svn`/`-hg`/`-bzr`) are filtered out of the request batch when `--devel` is **not** in effect — Phase 3 returns `DEVEL` for those packages without rebuilding, so the purge / clone / fetch / pkgver-resolve work is wasted; `--cleansrc` therefore never touches VCS checkouts unless `--devel` is also passed. Explicit positional pkgnames do not override this — the skip is uniform, mirroring the existing Phase 3 build-step skip. Pacman-class repo packages are likewise excluded (their upgrade detection runs through `checkupdates_map` in Phase 3 and the upgrade itself is dispatched as one terminal `sudo pacman -Syu`). The remaining requests delegate to the `source_sync.SourceSyncScheduler` singleton (`get_scheduler(...)`), issuing one `SyncRequest` per package — AUR sources go through the RPC short-circuit and `aur_clone`/`git_fetch_and_compare`, repo sources skip the RPC short-circuit (no AUR-RPC equivalent for the Arch packaging repo) and clone via `pkgctl_checkout` then refresh via `git_fetch_and_compare`; for repo sources only, a `STATUS_DIVERGED` outcome on a clean working tree triggers a hard-reset to `FETCH_HEAD` because pkgctl checkouts carry no user commits worth preserving (dirty trees are still respected and stay diverged):

1. **RPC-first.** The scheduler batches one `aur_info` call for all AUR packages in the run. For every package whose cached `rpc_version` / `rpc_last_modified` still matches the local HEAD metadata, the request short-circuits to `STATUS_UP_TO_DATE` — no `git fetch` executes at all.
2. **Clone on miss.** Missing / empty / non-git dirs hit `aur_clone` through the shared rate limiter.
3. **Shallow fetch + compare.** Everything else runs `git_fetch_and_compare` (depth-1 fetch, non-destructive HEAD compare). VCS packages (`-git`/`-svn`/`-hg`/`-bzr`) reaching the scheduler are always force-fetched regardless of RPC metadata — but this only happens under `--devel`, since `_sync_sources` filters them out otherwise (see above).
4. **Divergence is surfaced, not fixed.** A local-plus-upstream divergence (e.g. force-push, local commits) yields `STATUS_DIVERGED`; the build proceeds against the local PKGBUILD and an operator can opt into `--cleansrc` on the next run.
5. **Rate-limit aware.** AUR `Retry-After` is honoured; runs where the remaining penalty exceeds `[aur] rate_limit_abort_s` mark all pending packages `STATUS_RATE_LIMITED` rather than hanging for minutes.

Statuses treated as sync failures for the downstream buildability filter: `STATUS_FAILED`, `STATUS_RATE_LIMITED`, `STATUS_PURGE_REFUSED` (collected in `_SYNC_BLOCKING_STATUSES`, dispatched per-package via `_SYNC_STATUS_TO_ACTION`). `STATUS_DIVERGED` is a warning, not a blocker. `sync_failures` carries `(status, error_message)` so `_check_one_pkgbase` can map each blocking status to a distinct user-facing action: `STATUS_FAILED` → `PULL_FAILED`, `STATUS_RATE_LIMITED` → `RATE_LIMITED`, `STATUS_PURGE_REFUSED` → `PURGE_REFUSED` (cleansrc path).

**Phase 3 — Version check.** Parse PKGBUILD, look up installed version from `pacman -Q`, compare with `vercmp`. Produces a unified `_UpdateResult` per iterated package. Actions: `NEEDS_REBUILD`, `UP_TO_DATE`, `DEVEL`, `DEVEL_EVAL_FAILED`, `DOWNGRADE`, `PULL_FAILED`, `RATE_LIMITED`, `PURGE_REFUSED`. (`NOT_INSTALLED` is no longer emitted: under the live-install-set iteration model, only installed packages reach Phase 3.) For VCS packages (`-git`/`-svn`/`-hg`/`-bzr`) the static PKGBUILD `pkgver` is just a seed; without `--devel` the worker short-circuits *before* the pkgbuild_dir probe and PKGBUILD parse, returning `DEVEL` directly from the installed version (no `pkgbuild_ver`, no `pkgbuild_path`) — this matches the Phase 2 source-sync filter so both edges of the pipeline are silent on VCS pkgbases under the default mode. With `--devel`, the worker first attempts a cheap short-circuit: if `build_state.toml` carries a `built_upstream_commit` for the pkgbase, `vcs_pkgver.peek_upstream_commit` runs `git ls-remote` against the PKGBUILD's `source=()` URL and on a SHA match the package is reported `UP_TO_DATE` without ever touching makepkg. On a cache miss (no stored SHA, multi-git-source, ls-remote failure, or differing SHA) the canonical resolver `vcs_pkgver.evaluate_vcs_pkgver` runs `pkgver()` against the fetched upstream sources and the resulting `[epoch:]pkgver-pkgrel` is vercmp'd against installed (`NEEDS_REBUILD` / `UP_TO_DATE` / `DOWNGRADE`). Resolution failures are reported as `DEVEL_EVAL_FAILED` and skipped — the package is left untouched on the assumption that a missed update is preferable to wasted rebuilds when `pkgver()` is transiently broken (broken bash, dropped network mid-fetch, missing makedeps for `prepare()`). **Coexist-renamed packages** (an optimization rename of a repo or AUR package: the installed name carries the `-sysforge` suffix while the synced PKGBUILD still ships the stock `pkgname`) resolve correctly across the rename without any special lookup: the `build_state.toml` `pkgbuild_dir` already points at the *stock* source, so Phase 2 source sync and this phase's PKGBUILD parse both read the stock upstream version, and the installed version is looked up by the renamed installed name (`vercmp` is name-agnostic, so drift is detected regardless of the suffix). The one place the rename matters is the AUR RPC version rescue for PKGBUILDs whose `pkgver` uses bash expansion the static parser can't evaluate: the RPC cache is keyed by the stock upstream base, so when the renamed base misses the rescue falls back to the `origin_pkgbase` recorded in `build_state.toml` — this is the sole reader of `origin_pkgbase` in the update path. A coexist-renamed repo package (the common case, e.g. optimized `mesa`/`llvm`) never needs the rescue since repo PKGBUILDs resolve statically; the fallback only matters for a renamed *AUR* package with an unresolvable `pkgver`.

**Phase 4 — Summary + dry-run gate.** Print per-package status. Exit if `--dry-run`. The summary header always lists per-action counts (`5 need rebuild, 30 up to date, 7 skipped (3 rate-limited, 2 devel, 1 pull failed, 1 purge refused)`); per-package detail is suppressed by default for non-actionable statuses (`UP_TO_DATE` / `DEVEL` / `DEVEL_EVAL_FAILED` / `RATE_LIMITED` / `PURGE_REFUSED` / `PULL_FAILED`) and surfaced with `-v`. `NEEDS_REBUILD` and `DOWNGRADE` always render per-package because they are actionable. The `_consume_pacman_hook_sentinels()` pass at `cmd_update` entry surfaces kernel/toolchain reminders dropped by the libalpm PostTransaction hooks (see §`pacman.py`); the buildstate sentinel is consumed silently. A fourth hook, `artifacts` (`Target = *`, no `NeedsTargets` — it must fire on every transaction, not just package-name matches), drops a sentinel consumed by `_consume_artifact_sentinel()`: it reruns `iter_drifted` + `iter_offerable` against the artifact registry/ignore-list and warns only when either returns non-empty (silent-on-clean, since most transactions touch nothing in the inventory); best-effort — a corrupt registry or IO error is swallowed and the sentinel is unlinked regardless. Called once per-run at entry (scan-and-warn) and again at end-of-run (`silent=True`, unlink-only, clearing sentinels sysforge's own transactions just dropped).

**Phase 4.25 — Toolchain drift.** Drift identity is the pair `(toolchain_variant, toolchain_fingerprint)`, both recorded per package in `build_state.toml`, compared against the active toolchain resolved once via `pipeline.state.get_toolchain_variant` / `get_toolchain_fingerprint(PipelineState(state_dir))`. A package drifts when **either** component differs — folded into one `drifted` list with a per-package reason: (a) *different variant* — the recorded `toolchain_variant` differs from active (e.g. you swapped `compiler = "gcc"` → `compiler = "llvm", pgo = true` and previously-built packages still carry `toolchain_variant = "gcc"`); (b) *same variant, different fingerprint* — the variant name matches but the toolchain was rebuilt since the package was built (fresh PGO codegen, unchanged libLLVM soname) — the case the variant string alone misses. The fingerprint rule fires only when **both** the recorded and active fingerprints are present, so entries stamped before the field existed are never flagged (self-heals: any rebuild re-stamps, including after a `drift_detect` method flip). The active fingerprint is computed once by `build_fingerprint.toolchain_fingerprint(config.resolve_drift_detect(), cc)` — the same call used to stamp builds, threaded `build_core.build_and_install` → `BuildOptions` → `BuildState.record` — so stamping and comparison never diverge. The `[toolchain] drift_detect` method (`"fingerprint"` stat vs `"content_hash"` libLLVM hash; see §Config Layer) selects the fingerprint content. Pure pacman-mode entries (`build_mode = "pacman"`) have no variant and are never candidates. Skipped entirely when the active variant is `"system"` (no toolchain stage has run). Always prints a one-line summary if any drift is detected. `--explain-drift` prints the full list (with each package's reason) and exits before Phase 5. `--rebuild-on-toolchain-drift` (or the umbrella `--rebuild-on-drift`) promotes drifted `UP_TO_DATE` results to `NEEDS_REBUILD` (off by default — drift is informational because most C/C++ packages don't measurably benefit from a re-stamp; the leverage is in libLLVM consumers, not blanket rebuilds). Drifted results that don't have a resolvable `pkgbuild_path` (typically pacman-class) are warned and skipped rather than crashing the build loop. Promotion also sets `force_rebuild` on the result, which Phase 5 spends as a per-target makepkg `-f`: drift rebuilds run at an unchanged `pkgver`, so without it makepkg finds the matching artifact in `PKGDEST` and skips the build entirely (3.0.0-B9; see §CLI Verb Framework → Shared build engine).

**Phase 4.3 — Flag drift.** Re-resolve the current profile for each iterated source-built package (`build_mode = "source_built"`) and diff the freshly serialized flags against the `flags_string` recorded in `build_state.toml` at build time; packages whose flags now resolve differently are *flag-drifted*. Detection is delegated to `primitives/flag_drift.resolve_flag_drift` — the single flag-drift engine. Patched-PKGBUILD and kernel builds carry their flags embedded in the PKGBUILD, so the embedded profile is extracted before resolution (matching the build-time path); a PKGBUILD that fails to parse is warned and skipped, never fatal. Flag-drift detection is **network-free** — it only re-resolves the local PKGBUILD + stored flags — so `sysforge update --offline --dry-run` is the read-only, no-network flag-drift report. Always prints a one-line summary if any drift is detected; `--explain-drift` lists every drifted package with a per-key flag diff (alongside toolchain drift) and exits before Phase 5. `--rebuild-on-flag-drift` promotes flag-drifted `UP_TO_DATE` results to `NEEDS_REBUILD` (off by default — one edit to a shared profile can drift *every* source-built package, so an unattended `update` must not silently trigger a full-system rebuild); `--rebuild-on-drift` is the umbrella that opts into both drift axes. Promotion on this axis likewise sets `force_rebuild` (per-target `-f`). Drifted results without a resolvable `pkgbuild_path` are warned and skipped. Promoted packages flow through the normal Phase 5/6 build+install, so the `filter_pkgs_to_installed` split-package guard applies unchanged. **Scope — build-state-wide fold (absorbed from the removed `converge` verb):** after the walk-driven pass, a second pass covers every source-built `build_state.toml` entry *outside* the run's package walk — now that a plain source-built package is *in* the walk (and directly promotable), the remaining out-of-walk cases are stage-owned (kernel/toolchain) entries filtered from the walk, or entries excluded by a positional `PKG` name filter. Fold entries respect the positional `PKG` filter, are reported and listed under `--explain-drift` like any other drift, but cannot be promoted to `NEEDS_REBUILD` (there is no walk entry to promote) — under a rebuild flag they get a warning pointing at `sysforge build <pkg>` (or the owning pipeline stage) instead. When the walk itself is empty but source-built build_state entries exist, `update` skips the "no packages in scope" early-exit and proceeds straight to the drift phases, so a drift-only run works on a system with no AUR packages.

**Phase 5 — Build.** Filter to buildable packages: `NEEDS_REBUILD`. Inside `build_core.build_and_install`, the **PKGBUILD review gate** (`primitives/pkgbuild_review.py`) runs first. `update` defaults to the gate's **auto mode**: each package whose clone HEAD differs from its recorded `reviewed_commit` is auto-accepted with a per-package `[REVIEW] auto-accepted` notice, so a plain `update` stays unattended. `--review` opts into the interactive mode (`build`'s default): a full source-tree diff prompt (view / accept / skip / abort, single keypress) before dep prep — skip drops the package (counted as skipped in the summary), abort ends the run with nothing built or installed. `--no-review` or `[build] review = false` skips the gate entirely (no notices). (Phase 3 already converted VCS packages to `NEEDS_REBUILD` / `UP_TO_DATE` / `DEVEL_EVAL_FAILED` under `--devel`, so no separate `DEVEL` union is needed at this stage.) Batch makedeps pre-install (single `sudo pacman -S`). AUR dep resolution + build. Single build loop for all packages. `--cleanbuild` (`-C`) prepended by default (suppressed by `--no-cleanbuild`). `--syncdeps`/`-s` and `--install`/`-i` stripped; packages installed in phase 6. `AlreadyBuilt` raised by `makepkg_wrapper.run` (PKGDEST already holds the matching `.pkg.tar`) is treated as a successful build: `_find_existing_artifacts` locates the matching files in pkgdest and queues them for install, instead of marking the pkgbase failed. The exception is a **forced** target (`force_rebuild`, i.e. drift-promoted or `build --rebuild`): `-f` was passed, so makepkg cannot legitimately have skipped the build, and reusing the stale artifact is treated as a hard failure.

With `--install-only` the build loop is replaced wholesale: no makedep batching, no AUR dep resolution, no `makepkg` invocation. For each buildable result, `_find_existing_artifacts(pkgdest_or_pkgbuild_dir, pkgnames, pkgbuild_ver, installed_ver=...)` is called directly. Hits are queued for phase 6; misses log a `[SKIP]` line and are counted alongside the existing `skipped` total.

`_find_existing_artifacts` is a two-stage lookup. First it tries the strict glob `{pkgname}-{pkgbuild_ver}-*.pkg.tar.*` — the common path for non-VCS packages where the static PKGBUILD parse equals the filename version. If that returns nothing it falls back to a pkgname-only glob `{pkgname}-*-*-*.pkg.tar.*`, parses each filename's `(epoch, pkgver, pkgrel)`, and picks the newest by `vercmp`. The fallback is required for VCS (`-git`/`-svn`/...) packages, where `pkgver()` bumps the version dynamically at build time (PKGBUILD `pkgver=0.1.0` → artifact `0.1.0.r45.g1234567`) so the static `pkgbuild_ver` never matches the filename. When `installed_ver` is supplied (always under `--install-only`), the fallback further excludes any artifact not strictly newer than installed, preventing redundant reinstalls or downgrades. The `AlreadyBuilt` call site omits `installed_ver`: makepkg has already proved the artifact exists in PKGDEST, so the lookup just needs to find it.

**Phase 6 — Install + finalize.** Filter the built `.pkg.tar.*` files with `filter_pkgs_to_installed` so only files whose `pkgname` is already present in `pacman -Q` reach `pacman -U` — split `-git` pkgbases emit one file per split pkgname, and rebuilding must not silently add sub-packages the user never installed (e.g. `pipewire-full-git` emits 16 files; only the 2 installed ones get installed). New dependencies built via `build_resolved_deps` are handled on a separate path and are unaffected by this filter. Single `sudo pacman -U` for the kept set. Cache report, final summary, close unified log.

**Phase 6.5 — Trailing system upgrade.** One `sudo pacman -Syu` (`--noconfirm` appended when the run carries it), dispatched after Phase 6 so source-built artifacts are already installed and shielded by the `IgnoreGroup = sf-build` line `sysforge setup` adds. Two independent inputs open the gate: the walk classified at least one `repo_class = "pacman"` package as `NEEDS_PACMAN_UPGRADE` (only reachable under `repo_mode = "build_from_source"`), **or** the run requested a system upgrade — `--sysupgrade`, or `[build] system_upgrade = true` in `packages.toml`, resolved through `config.resolve_flag_default` with `--no-sysupgrade` checked first as the explicit-off leg. `--offline` suppresses the phase outright under either input. The request route adds no packages to the walk and issues no `checkupdates` probe: pacman resolves the transaction itself, so the cost is one subprocess. Because a requested upgrade is work in its own right, it also suppresses the earlier "Nothing to rebuild." early exit — otherwise a flag-only upgrade would no-op on an idle system. A non-zero return sets `pacman_upgrade_failed`, which feeds `_failure_exit_code` and the unified log's success flag.

**Final summary (end-of-run).** The built/failed/pacman result summary is rendered by a single presentation-only renderer, `update_summary._print_result_summary(ResultSummary)` — distinct from the Phase-4 version-check preview (`_print_summary`, over `_UpdateResult`). `update.py` assembles the `ResultSummary` via `_build_result_summary` (which reads `results`, keeping the renderer pure) and prints grouped, aligned sections honoring the Unicode/`use_color` gates. Built and failed packages render `pkgbase: installed_ver → pkgbuild_ver` (arrow degrades to `->` under the Unicode gate), looked up from a `pkgbase → (installed_ver, pkgbuild_ver)` map built from `results`; a package with no version pair falls back to a bare name. Dependencies installed as a build prerequisite by `prepare_deps` are surfaced as their own `Dependencies:` category (threaded out via `BuildOutcome.installed_deps` — reporting only, no second dep loop). Stage-owned (kernel/toolchain) packages — partitioned out of the walk by `_assemble_package_set` into its third return value `stage_owned_packages` (entries stamped `owner_stage`, built via the same resolvers rather than subtracted before entry-building) — are version-checked advisory-only by `_detect_stage_owned_updates`, which reuses `_check_one_pkgbase` (no parallel checker) under the same `offline` gate as the main walk (offline ⇒ no advisory) and, for any that are behind, emits a `Stage-owned updates available:` line pointing at the owning stage (`run kernel` / `run toolchain`). Detection only — stage-owned packages are never promoted to `NEEDS_REBUILD` or rebuilt from `update`. The `Pacman-Syu:` block has two presentations, because Phase 6.5 has two inputs: with a classified package list it renders one `pkgbase: old → new` line per entry; for a requested system upgrade (`ResultSummary.system_upgrade_ran`, no classified list — pacman owned the resolution) it renders the single line `system upgrade (pacman resolved the transaction)` and the header counts it as `system upgraded`. The `(transaction FAILED)` label is shared by both.

**Exit code.** `_cmd_update_body` returns the run's exit code, `cmd_update` passes it through, and `UpdateVerb.execute` wraps it as `ExecResult(exit_code=…)`. There are two failure seams, deliberately distinct. A **source-freeze denial** raises `RuntimeError` via `_raise_if_frozen` — the runner's exit-1 path, which *preserves* the stage sentinel so the next invocation stops at the recovery prompt. Every **other** failure — a package that failed to build, a cleansrc `STATUS_PURGE_REFUSED` denial (both land in `failed_pkgs`), a failed `pacman -U` install, a failed `pacman -Syu` — goes through `_failure_exit_code`, which returns 1 without arming the sentinel: the failure is already reported and needs no recovery prompt. A **partial** failure is still a failure (one package out of twenty exits 1); a wrapper that wants to tolerate that reads the summary counts. The read-only routes — `--check-drift`/`--explain-drift`, `--dry-run`, "no packages in scope", "Nothing to rebuild" — return before any failure tally exists and always exit 0. `doctor --apply`'s delegated rebuild propagates the same code.

**Phase timing.** The body records wall-clock phase durations on a `primitives/timing.PhaseTimer` — `source sync`, `version check`, `drift detection`, and `pacman -Syu` around the engine, plus the `dep prep` / `build: <pkgbase>` / `install` records `build_core.build_and_install` appends to the same timer. The report (`render_report`, rendered by `_emit_timings` under `[UPDATE]`) is always written at info level (so it lands in the unified log) and is promoted to UI output with the global `--timings` flag. It is emitted at the final summary, at the "Nothing to rebuild" early exit, and on the `--explain-drift` / `--dry-run` exits, so `sysforge --timings update --dry-run` is the cheap way to see where a check-only run spends time.

Positional: `[PKG ...]` — optional package names to restrict the run to a subset of packages.

Flags: `--interactive`, `--packages`, `--dry-run`, `--devel`, `--offline`, `--install-only`, `--no-cleanbuild`, `--cleansrc`, `--state-dir`, `--profile-conf`, `--cache-report`, `--no-pkg-log`, `--persist-log`, `--log-dir`, `--makepkg`, `--review`, `--no-review`, `--explain-drift`, `--rebuild-on-toolchain-drift`, `--rebuild-on-flag-drift`, `--rebuild-on-drift`.

`--install-only` is mutually exclusive with the build-tuning flags `--makepkg`, `--no-cleanbuild`, `--cleansrc`, `--interactive`, and `--cache-report`; argparse rejects the combination. It implies `--offline`. Use it to install artifacts left in PKGDEST by a previous interrupted run, or by a manual `makepkg` invocation, without re-entering the build loop.

**Unattended full update.** `sysforge update` (no positional args) is the supported recipe for a hands-off "rebuild everything outdated" run: walks every installed AUR package, every package sysforge previously source-built (build_state), and any repo packages with source-build overrides, rebuilds those flagged `NEEDS_REBUILD`, and automatically clones any missing src dirs. Add `--cleansrc` to also discard divergent upstreams — this is destructive but per-package safe, since `purge_src` refuses any clone that holds uncommitted changes. `--cleansrc` also bypasses the RPC short-circuit so every AUR package in the run is re-cloned from scratch rather than trusting the cached metadata. VCS pkgbases are exempt from `--cleansrc` unless `--devel` is also passed (their checkouts are never touched in the default mode, since the build step skips them too). A refused package is counted as failed and skipped.

### `doctor.py`

Implements `sysforge doctor` — the unified diagnostic front-end for sysforge-managed system health. Its original axis is package depends + linkage drift (the class of breakage where a partial rebuild leaves an installed package referencing ABIs that no longer exist — e.g. graphics-stack drift: mesa, vulkan, libglvnd, GPU driver), but it now also runs a set of read-only **system-state axes** (toolchain provenance, compile-cache readiness, hardware/boot readiness, graphics misconfiguration, pacman/system integrity, sysforge state integrity, boot/kernel runtime, storage/filesystem, services/runtime health, audio/sound stack, network/connectivity). Read-only — never rebuilds or installs (except the `--apply` rebuild bridge), and the new axes never mutate state (no `pacman -Sy`, no `BuildState.save()`, no sentinel recovery). Bare `sysforge doctor` runs every system axis — the intended one-stop debug source for "is anything wrong with my sysforge-managed system".

**Two scopes (`system` / `pkg`).** The surface is split into `sysforge doctor system [AXIS…]` and `sysforge doctor pkg [TARGETS] [AXIS…]`. Bare `sysforge doctor` is `doctor system` — unchanged as the everyday full sweep. The split exists because a single `PKG` positional previously meant three different things (a walk target, a scope qualifier for `rust`/`integrity`, and — via `--graphics` — a target *injector*), none of which `--help` distinguished. Two rules apply identically at both scopes:

1. **No axis flag → that scope's defaults.** `doctor system` runs the 13 non-opt-in axes; `doctor pkg mesa` runs `abi + rust + integrity` scoped to mesa. The opt-in axes are included at package scope because their cost is a whole-system scan, not a per-package one.
2. **A broad target selector suppresses the opt-ins.** `doctor pkg --all` / `--repo` runs `--abi` only; name an opt-in axis explicitly to override. This reuses the existing `_OPT_IN_AXES` frozenset rather than introducing a second policy. `--graphics` is *not* a broad selector for this purpose — it expands to ~15 curated packages, not the whole system.

So `doctor pkg cosmic-comp-git --rust` means exactly one thing: the rust axis, that package, no linkage walk.

`_resolve_axis_names` selects at system scope, `_resolve_pkg_axis_names` at package scope (over `_PKG_AXES = ("abi", "rust", "integrity")`). `abi` is registered in `_SYSTEM_AXIS_ORDER` so selection/ordering stay uniform, but is excluded from every system sweep and kept **last** in that tuple so `distro` keeps leading the system report.

**Migration.** The flat flags were removed in 3.0.0, and the migration hint that translated them (`_DOCTOR_MIGRATION` + the pre-argparse hook in `cli.py::_main`) was retired in 3.1.0 on the schedule its deprecation record declared (`doctor.flat_flags`, standards row 24). A stale flat invocation now gets argparse's own "unrecognized arguments". One structural test survives the deletion — `doctor --boot` must not parse — so the removal of the flat surface itself stays pinned.

**Unified `Finding` framework (`primitives/diagnostics.py`).** Every axis is a *producer* that returns `list[diagnostics.Finding]` — one finding shape (`category`, `severity`, `check_id`, `message`, `remediation`, optional `fix_cmd`/`auto_remediable`/`is_brick`) that subsumes the per-probe dataclasses that grew up independently (`GraphicsFinding`, `DeviceFinding`, `KernelFinding`, `ToolchainMismatchFinding`) plus the two outliers `ToolchainCheck` (`toolchain_preflight`) and `FixSuggestion` (`build_diag`). The probes keep their own dataclasses; `diagnostics.adapt` / `from_toolchain_check` / `from_fix_suggestion` convert at the boundary, so no probe is rewritten and the layering rule holds (the framework lives in `primitives` and never imports the `pipeline` layer — pipeline-layer checks are adapted by their callers). Rendering (`render_axis`), exit-code reduction (`error_count` — error-severity or `is_brick` ⇒ non-zero), severity normalisation (`normalize_severity` folds `"warning"` → `warn`), and exception-isolated axis running (`run_axes` — a raising probe degrades to one `*:probe_error` warning, never aborting the sweep) are centralised there. This is the backbone intended to become the single source for sysforge's internal error-checking/recovery; internal callers are migrated onto it incrementally (they currently keep their own entry points).

**Internal-caller adaptation status.** The two outlier internal shapes have correct, tested adapters into `Finding`: `from_toolchain_check` (`toolchain_preflight.ToolchainCheck`, used by `update`'s batch preflight + the toolchain stage) and `from_fix_suggestion` (`build_diag.FixSuggestion`, the build-failure diagnoser). Two live consumers already route internal state through the framework via `doctor`: the `state` axis surfaces persisted `build_diag` failures (`build_state.toml` `[failures]`) and the `toolchain` axis surfaces `llvm_state` provenance. The remaining migration — re-routing the *live* `update`-preflight and toolchain/kernel-stage render helpers through `render_axis` (which would change their on-screen output text) — is intentionally deferred: the adapters make it a mechanical follow-up, but it touches well-tested core install-path output and is out of scope for the doctor-focused work, so those callers keep their existing renderers for now.

For each target package, reads `/var/lib/pacman/local/<pkg>-<ver>/` directly: `files` for package-owned paths (filtered to `.so`/`.so.*`), `desc` for the `%DEPENDS%` array. Then runs two checks per package:

- **Depends check.** For each depends entry: versioned package deps verified via `pacman -T` + `vercmp`; `libfoo.so` and `libfoo.so=N` entries verified via `dep_analysis.soname_satisfied` against the `ldconfig -p` set.
- **ABI/linkage check.** Calls `abi_check.check_so_files` on the installed `.so` files — same symbol cross-check logic that `sysforge build --abi-check` runs, pointed at `/usr/lib/...` instead of a fresh archive.

Closure walk: by default, BFS over the target's `%DEPENDS%` transitively so one command covers the full dependency neighbourhood (the typical Steam-black-window pattern is a breakage one or two levels down from the root the user names). `--shallow` restricts to direct depends only. BFS dedupes on the resolved real pkgname from `pacman -Q` to collapse `provides`/virtual-package cycles. Output groups issues by the package the issue was found in, not by the root that triggered the walk, so overlapping closures from multiple roots produce one report per affected package.

**`--abi` is the walk's axis.** The depends + linkage walk was previously the only implicit check and the only one that was not a registered axis — a hand-rolled loop with its own printer, summary line, and exit-code contribution. It is now the explicit `--abi` axis, produced by the `AbiWalk` class. `--abi` covers depends-satisfaction *and* soname/ABI linkage together, not two flags: `_check_one` returns both from one traversal, sharing `_walk_closure` and the parsed `ldconfig` set. Splitting them would mean splitting the producer, which buys nothing.

`AbiWalk.run()` returns `list[Finding]` for rendering **and** retains structured `WalkResult` records on `self.results`, plus classified `--suggest` candidate buckets, for the `--apply` bridge. This side-channel is deliberate: `Finding` carries no structured payload, and `--apply` needs classified install/rebuild candidates rather than rendered text. It is also the design's known weak point — an ordering dependency the type system cannot enforce, since `--apply` produces nothing if the axis never ran. `_require_completed_walk(walk)` guards it, raising `RuntimeError` when `results is None`. Note that `results == []` is a *valid completed walk* (scanned, found nothing); only `None` means "never ran", so the guard tests `is None` rather than falsiness. `_abi_walk_for(args, config)` caches one instance on `args` so the axis producer and the apply bridge share the same object.

**Grouped rendering.** `render_axis` re-sorts findings globally by severity, which would interleave findings from different packages — for `doctor pkg --all` across hundreds of packages that destroys per-package adjacency. So `Finding` gained an optional `subject` field (the group key) and `render_axis` a `grouped=True` mode: groups ordered by their worst severity then by name, findings ordered by severity within each group. The `abi` axis renders grouped; every other axis renders flat, unchanged.

*Behaviour change:* grouped rendering groups **findings**, so a clean package no longer emits a per-package block the way `_print_report` did. The axis's clean message plus the `Scanned N package(s)` summary cover it. Tests that assert *which* packages a walk covered therefore assert against `walk.results` rather than printed headers.

Per-package group headers and the final summary both tag each package with its installation origin — `[aur]` for foreign packages (`pacman -Qm`) and `[repo]` for non-foreign. Example: a `steam 1.0.0.79-1 [aur]` group header and `Affected: steam [aur] (62), mesa [repo] (3)`. The tag reflects where the *currently installed* copy came from, not where updates might be available; an AUR package that's also shipped by a repo still reads `[aur]`. This directly distinguishes the rebuild surface: `[aur]` findings are fixed by a rebuild through sysforge's own build path; `[repo]` findings require a `-Syu` that includes a maintainer rebuild. Not-installed roots read `(not installed)` without an origin tag.

`--graphics` expands to a curated stack: always `mesa[-git]`, `lib32-mesa[-git]`, `vulkan-icd-loader`, `lib32-vulkan-icd-loader`, `libglvnd`, `lib32-libglvnd`, `egl-wayland[-git]`, `xwayland[-git]` / `xorg-xwayland[-git]`, `wayland` + `lib32-wayland`, `libdrm` + `lib32-libdrm`, `libva` + `lib32-libva`, `libvdpau` + `lib32-libvdpau`, `gamescope`; plus per-vendor additions driven by the hardware overlay's `gpu_vendors` list (`nvidia` → active `nvidia-*` / `nvidia-open*-dkms` driver + `lib32-nvidia-utils` + `nvidia-settings`; `amd` → `vulkan-radeon`, `lib32-vulkan-radeon`, `libva-mesa-driver`; `intel` → `vulkan-intel`, `lib32-vulkan-intel`, `intel-media-driver`). The list is filtered against `pacman -Q` so only installed variants are actually verified — avoids false negatives on boxes that don't have lib32 counterparts. The expansion table lives in `doctor.py::GRAPHICS_BASE` / `GRAPHICS_BY_VENDOR` as reference data, not config.

**`--graphics` splits by scope.** The flag previously did two unrelated jobs in one invocation; the subcommand boundary separates them with no new name. `doctor system --graphics` runs the system-state probes from `primitives/graphics_probe.py` only — it no longer injects any package targets. `doctor pkg --graphics` selects the curated graphics-stack target set described above, a peer of `--all`/`--repo`. Running both reproduces the old combined behaviour. The system-state probes These catch classes of graphics breakage that ABI/linkage walks cannot see: kernel-module parameters, NVIDIA driver version skew, session-type / compositor misconfiguration, missing Wayland explicit-sync protocol, Steam client config regressions. See `graphics_probe.py` below for the check inventory. Findings with severity `error` contribute to the exit code; `warn` and `info` do not.

`--hardware` runs a hardware/boot-readiness axis: it inventories all PCI/USB devices and flags any present device with no driver bound (`device_probe.check_unsupported_devices`), then audits the **running** kernel's `.config` (from `/proc/config.gz` or `/boot/config-$(uname -r)`) against the detected devices and root topology (`kernel_safety.audit_resolved_config`) — the on-the-spot diagnostic for "device X has no driver" / the `CONFIG_SND_PCI`-class trap. Unlike `--graphics`, `--hardware` needs no package targets and can be run on its own (`sysforge doctor --hardware`); it renders findings through `diagnostics.render_axis` in the `[SEV] check_id: message → remediation` format. `error`-severity findings (brick-class boot-config drops, carried via `is_brick`) contribute to the exit code; device-driver and degraded findings warn. Because the whole axis reads the **running** kernel, every finding is tagged with a reboot caveat (`_with_reboot_hint` in `doctor.py`): doctor re-probes live each run, but a kernel rebuilt/installed but not yet booted won't change these lines until reboot — so a freshly-applied fix correctly persists until the new kernel is the running one.

`--toolchain` runs a configured-vs-installed toolchain axis via `llvm_state.detect_toolchain_config_mismatch` (which wraps the sanctioned `collect_llvm_state` entry point — provenance reporting, not a third toolchain *health* probe). When `toolchain.toml` requests a custom LLVM toolchain (`enabled = true`, `compiler = "llvm"`) but stock repo LLVM is installed, or the PGO profdata is version-skewed, it reports the mismatch in the same `[SEV] check_id: message → remediation` format. Because the toolchain stage replaces LLVM in place with **stock pkgnames**, a successful custom/PGO build still reads as `install_origin == "repo"` to pacman; the axis subtracts any such package that `build_state.toml` records as toolchain-owned (`owner_stage == "toolchain"`) so a correctly-built toolchain does *not* spuriously warn — a genuine never-built stock install (empty build_state) still fires. The whole axis is suppressed in two intentional-stock cases: `skip_build = true` (registers the installed compiler as-is — stock-vs-custom is a deliberate choice, like the `compiler = "gcc"` register-only path), and `pgo = false` + packages.toml `repo_mode = "pacman"` (the stage installs the stock suite from the repos on purpose). It contributes `error`-severity findings to the exit code. Like `--hardware`, it needs no package targets and runs standalone (`sysforge doctor --toolchain`); the two can be combined (their exit codes OR together). This is the standalone surface of the same check the kernel stage emits before a build.

`--rust` runs a Rust-toolchain **provenance** axis (`doctor._collect_rust_findings` over `primitives/rust_probe.py::collect_rust_findings`) — the read-only "which Rust toolchain will a build actually use?" lens, the Rust analog of `--toolchain`'s C/LLVM provenance check. It reports the effective `cargo`/`rustc` owner (a `rustup`-managed channel via `rustup show active-toolchain`, or the distro `rust` package; no toolchain at all is a clean `info`), `warn`s when the active `rustup` default is non-`stable` (nightly/beta/pinned — every Rust build then silently uses it), and — at package scope — resolves each target's `rust-toolchain.toml` pin and `warn`s when the pinned channel is not installed (rustup would fetch it mid-build, turning an apparent local build into a network fetch). **Negative results are now explicit** (they were silent `continue`s, which is why `doctor --rust PKG` read as though it ignored its argument): severity depends on how a target entered the set. An **explicitly named** target that cannot be resolved to a PKGBUILD is a `warn` (`rust-pin-unresolved`), and one that resolves but has no `rust-toolchain.toml` is an `info` (`rust-no-pin`). Targets pulled in by a **broad selector** collapse into a single `rust-pin-survey` `info` carrying the counts, so `doctor pkg --all --rust` reports a readable summary instead of several hundred near-identical lines. `collect_pin_findings(config, packages, *, derived=())` carries the provenance split; `_pin_findings_for` returns exactly one finding per target, which is what lets the survey classify by `check_id`. **Advisory only: emits `info`/`warn`, never `error`** — it can never fail a `doctor` run or gate a build, and never rewrites a pin or mutates rustup state. Opt-in (`rust ∈ _OPT_IN_AXES`, excluded from the bare/`--all` sweep, reachable only via `--rust`): the pin checks are meaningful only when the user names targets, and provenance is not a system-health defect. Sits next to `toolchain` in axis order. Standalone (`sysforge doctor --rust [PKG…]`). See `rust_probe.py` in the primitives layer for the finding inventory.

`--cache` runs a compile-cache readiness axis (`doctor._collect_cache_findings` over `primitives/cache_probe.check_cache_readiness`) — the point-in-time "is the compile cache set up correctly *before* a build relies on it?" lens, distinct from the build verbs' `--cache-report`, which measures per-package hit/miss *effectiveness after* a build. Same underlying probes (`probe_ccache`/`probe_sccache`), two lenses. For each of ccache/sccache it reports whether the tool is installed, its cache dir is writable, and a non-zero size cap is configured. Absence is optional, not a defect: when **neither** tool is installed it emits a single `info` ("no compile cache configured; builds won't benefit from caching") — matching the boot axis's missing-but-optional convention — never contributing to the exit code. A tool that is installed but misconfigured (unwritable dir / unset-or-zero size cap) is a `warn` carrying its remediation (`ccache -M <size>` / set `SCCACHE_CACHE_SIZE`); a ready tool contributes to the clean message. All reader logic stays in `cache_probe` (the one home for cache knowledge); no cache subprocess logic lives in `doctor.py`. Cheap and read-only, so it runs in the default/`--all` sweep (not opt-in). Sits next to `toolchain` in axis order as a build-toolchain health check. Standalone (`sysforge doctor --cache`).

`--pacman` runs a pacman / system-integrity axis (`primitives/system_probe.py::collect_system_findings`) — all read-only against the *local* database (never `-Sy`): `pacman -Dk` local-db dependency consistency (missing deps → `error`), a lingering `/var/lib/pacman/db.lck` from an interrupted transaction (`warn`), unmerged `*.pacnew`/`*.pacsave` config files under `/etc` (`warn`), and `pacman -Qtdq` true orphans (`info`). Two size/freshness checks join the same axis: **package-cache size** (`_check_pkg_cache`, F15) sums the `.pkg.tar*` archives in the pacman cache dirs resolved via `pacman.get_pacman_cache_dirs()` (never hardcoding `/var/cache/pacman/pkg`) and `warn`s over `[doctor] pkg_cache_warn_gb` (default 5 GB) with `paccache` remediation — distinct from `cache_probe`, which reports *build* caches (ccache/sccache); and **mirrorlist freshness** (`_check_mirror_freshness`, F16) `warn`s when `/etc/pacman.d/mirrorlist`'s mtime age exceeds `[doctor] mirrorlist_stale_days` (default 14) — file-age only, never a live latency probe. It also folds in **sysforge libalpm-hook drift** (`doctor._collect_hook_findings` over `primitives/pacman_hooks.diff_status`): each shipped hook/helper that is missing from or stale against the canonical source emits a `warn` with remediation `run \`sysforge setup\``. Read-only — `diff_status` only reads. Standalone (`sysforge doctor --pacman`).

`--state` runs a sysforge state-integrity axis (`primitives/state_probe.py::collect_state_findings`) — read-only inspection of sysforge's *own* persisted state: recorded build failures from `build_state.toml`'s `[failures]` table (each `warn`, surfacing the `build_diag` signature + `fix_cmd` when present), an interrupted stage sentinel via `StageSentinel.get_active()` (`error`, carrying the recorded `recovery_cmd` — it does **not** call the recovering `check_and_recover_stale_sentinel`), and build-state drift vs the live pacman db (`info`, zombie entries for uninstalled packages). It also flags **live record-stage PGO builds** (`_check_instrumented_builds`, F14): for each tracked package it resolves the PGO store (`mesa_pgo.resolve_store`) and `warn`s when a bare `.profraw` is present with **no** merged `.profdata` — an instrumented, unoptimized build left live — pointing at `sysforge build <pkg> --pgo=use` or a repo rollback. Detection is provenance-only (never inspects a binary); a package that never used `--pgo` has no store and is silently skipped. The last source-sync `STATUS_*` is intentionally *not* surfaced: the source-sync scheduler cache is per-process, so a standalone `doctor` run has no sync results. Standalone (`sysforge doctor --state`).

`--boot` runs a boot/kernel-runtime axis (`doctor._collect_boot_findings`, reusing `primitives/kernel_safety`) — the running-system analog of the kernel stage's gates 1/3: per-bootable-kernel boot-artifact verification (`verify_boot_artifacts`: vmlinuz + initramfs + a boot entry; gaps are brick-class `is_brick` → exit code), a recovery-fallback check (`find_fallback_kernels`; only one bootable kernel → `info`), `/boot` free space (`check_boot_mount_space`), and DKMS modules for the running kernel (`check_dkms_for_kernel(running_kernel_release())`; these `dkms:*` findings carry the running-kernel reboot caveat, while the filesystem-live boot-artifact / `/boot`-space findings do not). The running-kernel `.config` device audit stays in `--hardware` (it is device-driver coverage, distinct from boot-artifact readiness), so there is no double-report. Standalone (`sysforge doctor --boot`).

`--distro` runs a distribution-identity axis (`primitives/os_release.py::collect_distro_findings`), read from `os-release(5)` — `/etc/os-release` then `/usr/lib/os-release`, shell-style `KEY=value` with quote stripping, `ID` defaulting to `linux`, `ID_LIKE` as the space-separated parent list. It reports the **support tier**, not a health check: Arch itself (`ID=arch`) is the primary base and stays silent in the bare sweep (the axis renders its clean line — a plain Arch host learns nothing from being told it is Arch), while `--distro` explicitly always prints the identity, which is the point of asking. An Arch derivative (`ID_LIKE` contains `arch`) gets an `info` naming what *is* validated there (packaging, dependency resolution, `makepkg.conf` merge) and what is not (bootstrap, kernel staging, graphics/DKMS). A distro that declares no Arch base, and an unreadable `os-release`, are both `warn`. **No finding on this axis is ever an `error`** — a support tier must not change doctor's exit code. A file that exists but parses to nothing counts as unreadable, so a truncated `os-release` cannot masquerade as `ID=linux` via the spec default. This module is the **only** permitted home for distro identity (**Standards** row 23, `check_standards` `distro_portability` group): never `pacman.conf` section names, `/etc/arch-release`, or a hostname. Standalone (`sysforge doctor system --distro`).

`--storage` runs a storage/filesystem axis (`primitives/storage_probe.py::collect_storage_findings`), read-only, never mounts. Two checks: **build-dir free space** (`_check_disk_space`, F17) resolves the configured `pkgbuild_src_dir` and `warn`s when free space is under `[doctor] disk_low_gb` (default 10 GB) — the standalone-`doctor` analog of the reconfigure stage's disk step, now sharing the one `probe_free_space` home (`shutil.disk_usage` lives only there; reconfigure consumes it, keeping its own AUR-count build-size estimate locally); and **`/etc/fstab` integrity** (`_check_fstab`, F18) `warn`s on any real entry whose `UUID=`/`LABEL=`/`PARTUUID=`/`PARTLABEL=` or bare device path no longer resolves under `/dev/disk/by-*`, skipping pseudo filesystems, network fs types, and any entry carrying `nofail`. Standalone (`sysforge doctor --storage`).

`--services` runs a services/runtime-health axis (`primitives/runtime_probe.py::collect_runtime_findings`): failed systemd units (`systemctl --failed` → each `error`), firmware a driver requested but could not load this boot (best-effort parse of `journalctl -k -b` for "Direct firmware load … failed" → `warn`; degrades silently when the journal is unreadable; the `missing_firmware` finding carries the current-boot reboot caveat, while live `failed_unit:*` findings do not), and **error-priority journal lines this boot** (`_check_boot_errors`, F19: one deduped, capped `warn` over `journalctl -b -p err` matching failed-start / core-dump / segfault / kernel-panic / filesystem / OOM patterns — current-boot scoped, read-only). Standalone (`sysforge doctor --services`).

`--audio` runs an audio/sound-stack axis (`primitives/audio_probe.py::collect_audio_findings`): failed PipeWire/WirePlumber **user** services (`systemctl --user --failed`, filtered to the pipewire/wireplumber unit family → each `error`) and a vanished output sink (`pactl list short sinks` showing only the dummy `auto_null` device → `warn`, the "audio device disappeared" symptom). The sound stack is user-scoped, so under `sudo` (no reachable session bus) both commands exit nonzero and the axis degrades to clean rather than false-positive a phantom "no audio device" — a missed finding is recoverable, a false alarm is not. Input sources are deliberately not flagged (a machine with no microphone is normal). Read-only — never restarts a unit. Standalone (`sysforge doctor --audio`).

`--network` runs a network/connectivity axis (`primitives/network_probe.py::collect_network_findings`). **No active network calls** — it inspects local routing/manager/DNS *configuration* only (a live DNS lookup or ping would hang or flap with real connectivity). Three checks, all `warn`: no default route (`ip route show default` empty → `no_default_route`); a connection-manager ownership conflict (more than one of NetworkManager/systemd-networkd/dhcpcd/connman/netctl reports `enabled` via `systemctl is-enabled` → `network_manager_conflict`); and a DNS provisioner conflict (`systemd-resolved` active but `/etc/resolv.conf` is a static file rather than the resolved stub symlink → `dns_resolved_unmanaged`). The manager-conflict check is the axis's unique value: the `services` axis only reports units already in the `failed` state, but a manager conflict typically presents as flapping, not a hard failure. False-positive-averse: an absent tool / nonzero exit / managed symlink / missing resolv.conf all degrade to clean, and "no manager enabled at all" is deliberately not flagged (a host may use a manager we don't enumerate). Read-only — never enables/restarts a unit or rewrites resolv.conf. Standalone (`sysforge doctor --network`).

Vendor detection for `--graphics` prefers the hardware profile (`/var/lib/sysforge/hardware_profile.toml` → `[gpu] vendors`); when that file is absent, falls back to `lspci -nnk` scraping, extracting `nvidia`/`amd`/`intel`/`radeon` from VGA-class device strings. The `lspci` fallback is used for both the package-expansion vendor list and the graphics-probe vendor-gating.

**Invocation modes (which axes + which package walk).** Resolved by `_resolve_axis_names`:

- **Bare `sysforge doctor`** (no packages, no `--repo`, no axis flag) → runs **every system axis** (distro, toolchain, cache, hardware, graphics, pacman, state, boot, storage, services, audio, network) in `_SYSTEM_AXIS_ORDER` and **no** package walk. The fast "is anything wrong" default. (Previously bare was a usage error.)
- **`--all`** → every system axis **plus** the full per-package walk over `pacman -Q` (foreign and non-foreign). The exhaustive "is anything broken anywhere" sweep.
- **Explicit axis flags** (`--distro` / `--toolchain` / `--cache` / `--hardware` / `--graphics` / `--pacman` / `--state` / `--boot` / `--storage` / `--services` / `--audio` / `--network`) → exactly those axes, in canonical order. `--graphics` additionally adds the graphics-stack closure to the package walk (it is both an axis flag and a package-walk trigger).
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

**`--apply` bridge.** `--apply` (implies `--suggest`) hands the REBUILD-classified candidates to `sysforge update` for actual rebuild. Drift-rebuild only: install candidates (not yet installed) are surfaced as `→ run: sysforge build <pkg>` informational lines but never invoked. Repo packages outside `sysforge update`'s scope (no behavior-changing override, no `repo_mode = "build_from_source"`) are surfaced as `→ run: sudo pacman -S <pkg>` and skipped. Foreign packages — and repo packages eligible under `repo_mode = "build_from_source"` — are gathered into a single eligible list, the user is prompted (`--no-confirm` skips), and `cmd_update` is invoked with that list as the positional pkgname filter. `--dry-run` reports the rebuild list without invoking the build. `--apply`'s exit code dominates the doctor exit — a successful rebuild produces exit 0 even if doctor surfaced issues. The bridge is intentionally thin: rather than extracting `update.py`'s build loop into a separate primitive, doctor synthesizes a `cmd_update` args namespace and reuses the existing path verbatim.

> **Real-world status (2026-05-02): unit-tested only.** The unit tests
> (`tests/test_doctor.py::test_apply_*`) mock `cmd_update` entirely, so the
> end-to-end "doctor finds drift → update rebuilds → install succeeds" path
> has not been exercised against a live system yet. Treat `--apply` as
> shipping behind tested-by-mock semantics; full integration verification
> is pending.

Public API: `cmd_doctor(args)`. Positional `[PKG ...]` and flags `--distro`, `--graphics`, `--hardware`, `--toolchain`, `--rust`, `--cache`, `--pacman`, `--state`, `--boot`, `--storage`, `--services`, `--audio`, `--network`, `--all`, `--repo`, `--shallow`, `--quiet` (suppress clean lines, show only issues), `--suggest` / `-s` (inline + end-of-run candidate lookup via files db), `--apply` (drift-rebuild bridge), `--no-confirm`, `--dry-run`. New axes register in `_SYSTEM_AXIS_ORDER` / `_AXIS_FLAGS` / `_system_axes` with a `_collect_<axis>_findings` producer (looked up through module globals so tests can monkeypatch them).

**Progress phase.** The verb runner paints a generic `progress.phase("doctor: starting…")` on dispatch; `cmd_doctor` overwrites it with phase-accurate labels as it runs — `f"doctor: {axis.label}"` per system axis (in `_run_system_axes`) and `f"doctor: auditing {pkgname}"` per package during the walk — so the bottom-anchored indicator advances instead of sitting on "starting…" for the whole sweep. Progress messaging only; the axes/walk stay read-only.

**`[doctor]` config.** Thresholds for the size/space checks live in `sysforge.toml`'s `[doctor]` section — `pkg_cache_warn_gb` (5.0), `mirrorlist_stale_days` (14), `disk_low_gb` (10.0). Each check reads its value with the documented default as fallback, so a config without `[doctor]` behaves as the baked defaults.

Log tag: `[DOCTOR]` (was `[DOC]` before P3.4). Primitive lookup helper lives in `sysforge/primitives/provides_lookup.py` — see the `provides_lookup.py` subsection for the public API. NEEDED-soname extraction reuses `abi_check.needed_sonames` (public since doctor calls it directly for ABI-issue suggestions). System-state probes live in `sysforge/primitives/graphics_probe.py` — log tag `[GFX]`, public API `check_system_graphics(config, *, gpu_vendors=None)`; invoked from `cmd_doctor` when `--graphics` is set.

### `setup_cmd.py`

Implements `sysforge setup` — one-shot pre-flight that stops `pacman -Syu` from silently clobbering sysforge-built packages with upstream repo binaries. It inspects `/etc/pacman.conf` for `IgnoreGroup = sf-build` and, if missing, offers to add it (interactive prompt). Packages built by sysforge carry the `sf-build` group, so the IgnoreGroup line gates the whole rebuild surface behind a single policy knob rather than requiring a per-package `IgnorePkg`.

Public API: `cmd_setup(args)`. Flag: `--pacman-conf PATH` (default `/etc/pacman.conf`) for VM or chroot runs where the file lives elsewhere. No effect if the line is already present. Intended to be run once after first installing sysforge; safe to re-run.

`setup` also installs/refreshes sysforge's **libalpm hooks** after the IgnoreGroup step. The three shipped hooks (`sysforge-kernel.hook`, `sysforge-toolchain.hook`, `sysforge-buildstate.hook`) plus `pacman-hook-helper.sh` are the input side of `update`'s reminder/auto-demote logic; the PKGBUILD installs them at package time, but a **source-checkout dev workflow** (or an edited hook) leaves them missing or stale, silently disabling that logic. Hook provisioning has one home — `primitives/pacman_hooks.py` — which resolves the canonical source (repo checkout preferred, else the wheel's bundled `sysforge/_data/` copy shipped via a `force-include` in `pyproject.toml`), byte-compares it against the installed copy (`diff_status` → `ok`/`missing`/`stale`), and writes the missing/stale files through `fs_provision._run_priv` (the existing sudo-or-fail path; no second sudo path). A privileged failure prints a "re-run with `sudo sysforge setup`" hint rather than crashing. The same `diff_status` facts are surfaced read-only by `doctor --pacman` (`doctor._collect_hook_findings`, a warning per drifted hook). `tools/check_shipped.py::check_hooks` asserts `pacman_hooks.HOOK_NAMES` matches the shipped `.hook` files and that the `force-include` ships the hooks dir + helper.

### `state_cmd.py`

Implements `sysforge state` — a small read/repair namespace for `build_state.toml` (the live install-state mirror and the `[failures]` table). Separate from `sysforge packages` (which manages override rules in `packages.toml`); the split mirrors the rules-vs-state separation described in §Package Manifest.

- **`state list`** — tabulates `build_state.toml` entries: pkgbase, build_mode, last build, profile/flags. Read-only. Also appends an *Untracked foreign packages* section listing any installed `pacman -Qm` package that has no `build_state.toml` entry — those slipped past sysforge (installed manually) and won't be rebuilt by `sysforge update` from a known PKGBUILD without a fresh fetch.
- **`state repair`** — two independent passes over `build_state.toml`. (1) Re-parses PKGBUILDs for entries whose stored fields contain unexpanded shell variables (e.g. `${pkgver}` literals from a buggy parse) and rewrites those rows. (2) Rewrites entries carrying a *known legacy* `build_mode` token to its current spelling — `profiled` → `source_built` (2.6.1-F5, which removed the read-side alias). Pass 2 needs no PKGBUILD and does not skip an entry whose `pkgbuild_dir` is missing, so it reaches exactly the stale rows pass 1 cannot. An unrecognized-but-not-legacy `build_mode` is left alone rather than coerced, since guessing would invent build history. `--dry-run` previews fixes without writing.
- **`state orphans`** — scans `PKGDEST` for `.pkg.tar*` artifacts whose pkgname is installed AND whose version is strictly older than the installed version (the **superseded** category). Read-only by default; `--prune` deletes after a y/N prompt (`--no-confirm` skips). The detection primitive `pacman.detect_orphan_artifacts(pkgdest, installed)` returns `{"superseded": [...]}`.
  - **Files whose pkgname is *not* installed are intentionally NOT surfaced.** They could be a build kept on purpose (a kernel artifact whose source has local commits the user wants to keep available for later install, a test build, etc.) and we can't safely tell the difference. Per the load-bearing rule: if `--prune` wouldn't safely delete a file, don't list it. Users who want a broader view can use `paccache`/manual inspection.
  - Files whose `.PKGINFO` can't be read or whose filename doesn't parse are silently skipped — never deleted.
- **`state failed`** — lists the `[failures]` table of `build_state.toml`: pkgbase, failure timestamp, diagnosis signature, the diagnosed fix command (when `build_diag` matched a known signature), and the truncated error tail. Read-only by default. `--clear PKGBASE` removes one entry and `--clear-all` removes all (both rewrite `build_state.toml`, so they take the sentinel like `state repair`). Failures are recorded by `sysforge update`'s build fan-out (`_record_build_failure`) and auto-clear on the next successful build of the same pkgbase (`BuildState.record` pops the matching failure), so the list stays a live view of *currently-broken* packages.
- **`state forget PKG…`** — deletes the named packages' `build_state.toml` records so `sysforge update` stops maintaining them (the "hand it back to pacman" escape hatch for the durable-by-default tracking model — see §Package Manifest). A name matching a pkgbase forgets every split-package member sharing it (`BuildState.delete` per pkgname). The *installed* artifact is untouched and still carries the `sf-build` pacman group, so `pacman -Syu` won't replace it; reverting fully to the stock repo binary is a separate `pacman -S <pkg>`. Rewrites `build_state.toml`, so it takes the sentinel like `state repair`. (Uninstalling a package already auto-stops tracking via `sync_with_installed`'s prune; `forget` covers the keep-installed-but-unmanage case.)
- **Pagination.** `state list`, `state orphans`, and `state failed` pipe their output through `$PAGER` (or `less -RF` / `more` as fallbacks) when stdout is a TTY. `--no-pager` disables. The pager wrapper `_maybe_pager(use_pager)` lives in `state_cmd.py` and degrades gracefully when no pager binary is available.

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

**Viewing logs.** `sysforge log` (no args) pages the unified log at `<state_dir>/sysforge.log`; `sysforge log <pkg>` pages the per-package log at `<pkgbuild_src_dir>/<pkg>/sysforge_<pkg>.log`. Both pipe through `$PAGER` (parsed as a shell word list via `shlex.split`, so `PAGER="less -RF"` works; default fallback `less -RF`, then `more`) via the shared `primitives/pager.py:maybe_pager` context manager (also used by `state list` and `state orphans`). The fallback deliberately omits `-X` — that flag suppresses less's alternate-screen switch, so it paints inline into scrollback and its redraws desync on modern terminals. `maybe_pager` also runs the pager subprocess inside `ui/progress.py:suspended()`, releasing any active DECSTBM scroll region for the pager's lifetime — otherwise a verb that painted a progress phase before paging (e.g. `state orphans`' pre-scan status) would clamp less into the reserved band and desync its redraws. Missing files surface as a non-zero exit with the searched path — no AUR clone fallback, so a typo never causes a network operation. Tab-completion uses `sysforge completions local` (dirs under `pkgbuild_src_dir` containing a PKGBUILD).

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

Pure constants module — the canonical directory of every config file sysforge reads. `CONFIG_BASE` is derived from `$SYSFORGE_CONFIG_DIR` (default `/`, so system config resolves under `<base>/etc/sysforge/`, including `BOOTSTRAP_PATH`). User-side roots follow the XDG Base Directory Specification via the `_xdg_base(env, default)` helper: `USER_CONFIG_DIR` (`$XDG_CONFIG_HOME/sysforge`), `USER_CACHE_DIR` (`$XDG_CACHE_HOME/sysforge`), `USER_STATE_DIR` (`$XDG_STATE_HOME/sysforge`). The resolved path lists (`CONFIG_PATHS`, `CONFLICT_GROUP_PATHS`, `CONSUMES_INFERENCE_PATHS`) layer the user file (`$XDG_CONFIG_HOME/sysforge/…`) over the system file in `extends_system` order. Helpers: `resolve_packages_path(config)` returns the `packages.toml` path the rest of the codebase should use (honouring `--packages` overrides). No I/O — just path strings.

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
- `get_pkgdest()` / `get_builddir()` / `get_srcdest()` / `get_logdest()` — resolve the corresponding makepkg path variable via the shared `_resolve_makepkg_path(key)` helper, which mirrors makepkg's own precedence: **environment first** (`os.environ`), then the layered `parse_system_makepkg_conf()` (`/etc/makepkg.conf` → user conf), quotes stripped and `~`/`$VARS` expanded, else `None`. These are the single home for "where did makepkg put / read this?" — any new code that needs to *locate* a built artifact, build tree, downloaded source, or build log must call them rather than assuming `~/builds` / the PKGBUILD dir / a hardcoded default (a recurring bug class). `BUILDDIR` resolution in particular feeds `makepkg_env._effective_build_dir` (side-car log diagnosis) and `kernel._resolve_built_config` (resolved `.config` discovery), both of which must honour a `BUILDDIR` set only in `/etc/makepkg.conf`. `PKGDEST` resolution feeds `makepkg_wrapper._find_artifacts` (the union-of-`PKGDEST`-and-PKGBUILD-dir locator behind `install_built_packages` + the post-build ABI report) and `build_core`'s artifact snapshot — so a non-default `PKGDEST` doesn't strand the install/ABI scan looking in the PKGBUILD dir. `install_built_packages` wraps that locator in `_artifacts_for_pkgbuild`, which scopes the union in two tiers. **Preferred:** a build-time manifest (`_BUILT_MANIFEST_NAME`, `.sysforge-built.list`) — the exact package basenames `makepkg --packagelist` emitted, recorded by `_capture_built_manifest` on build success *while the patched `PKGBUILD.sysforge` is still present* — is matched exactly. Recorded for any **name-affecting** build: one that applies an extracted profile **or** a rename (kernel local-rename / `-sysforge` suffix); a rename is exactly the case pkgname scoping can't recover (2.5.1-B3 — a rename-only kernel previously recorded no manifest because capture was gated on the extracted profile alone). This is the only reliable scope for a **renamed** build (e.g. a `-sysforge` kernel): by install time the patched PKGBUILD is cleaned up, leaving only the upstream PKGBUILD whose pkgname (`linux`) prefix-matches unrelated (`linux-custom`, `linux-steam-integration`) and stale-version (`linux-sysforge-<oldver>`) artifacts — the pre-B9 bug that swept those into `pacman -U` and could downgrade the running kernel. **Fallback** (no manifest, or none of its entries are present): scope by the PKGBUILD's own pkgnames (filename-matched via `_parse_built_pkg_filename`, which anchors on the exact `<pkgname>-` prefix **and** requires a valid `[epoch:]pkgver-pkgrel-arch` tail — three hyphen-delimited fields, since `PKGBUILD(5)` forbids hyphens in pkgver/pkgrel/arch — so pkgname `linux` no longer matches `linux-custom` et al., 2.5.1-B3). This scoped set is returned **even when empty** — it never degrades to the full PKGDEST union: when the PKGBUILD parses cleanly and names its pkgnames but none match (the renamed-away case, absent a manifest), the caller fails loudly (`nothing to install`) rather than hand a shared PKGDEST of hundreds of unrelated packages to `pacman -U`. Only a PKGBUILD that yields *no* parseable pkgnames still degrades to the union (best-effort locate-everything), a case that fires solely with a local, unset `PKGDEST`. The manifest is deleted after a successful install so a later build in the same dir can't read a stale list.
- `snapshot_pkg_dir(pkgdest)` — records the set of `.pkg.tar.*` files currently in pkgdest before a build
- `batch_install_pkgs(pkgdest, pre_snapshot, ...)` — diffs the post-build pkgdest against the snapshot and installs all new packages in a single `sudo pacman -U`
- `read_pkgname_from_file(path)` — extracts `pkgname` from a built `.pkg.tar.*` via `bsdtar -xOqf <path> .PKGINFO`; returns `None` on failure
- `read_pkg_replaces_from_file(path)` — extracts the set of `replaces` names from a built `.pkg.tar.*`'s `.PKGINFO` (version constraints stripped); empty set on failure. Lets `filter_pkgs_to_installed` keep a conflict-mode renamed drop-in
- `filter_pkgs_to_installed(paths, installed)` — partitions pkg-file paths into `(keep, dropped)` by whether their `pkgname` is in the current installed set; used by `update` so split-pkgbase rebuilds don't add sub-packages the user never installed. A built artifact whose own `pkgname` is absent but whose `replaces` intersects the installed set is **kept** — the conflict-mode `-sysforge` rename case (`mesa --pgo=use` builds `mesa-sysforge`, which `replaces = mesa`), where the renamed drop-in would otherwise be dropped for matching no stock pkgname
- `collect_makedeps(paths)` (makedepends only) / `collect_builddeps(paths)` (`depends` + `makedepends` + `checkdepends`) / `filter_missing_deps(deps)` / `batch_install_makedeps(deps)` — build-dependency helpers. `prepare_deps`' repo arm uses `collect_builddeps`: the `-s`-stripped batch build needs runtime `depends` present too, not only makedepends. Both collectors share `_collect_dep_names`, which version-strips and skips un-evaluated shell tokens (`_looks_unresolved`)
- `get_installed_version(name)` — `pacman -Q <name>`; returns version string or `None`
- `get_all_installed_packages()` — `pacman -Q`; returns `{name: version}`
- `get_foreign_packages()` — `pacman -Qm`; returns names not from any sync DB
- `get_pacman_sync_version(name)` — `pacman -Si <name>`; returns version from sync DB or `None`
- `get_installed_facts(root=None)` — `{name: (version, installed_size)}` for every installed package in one local-DB pass (pyalpm's `pkg.isize` rides along free; falls back to a parsed `pacman -Qi` when pyalpm is unavailable). Feeds `change_report.snapshot()`. `root` is accepted for the target-root case but a non-`None` value raises `NotImplementedError` today — implementing it for a target root is future work; the guard exists so a caller can never silently receive live-root data for a target root it asked to snapshot. A failed `-Qi` fallback (`returncode != 0`) raises `RuntimeError` rather than returning `{}` — unlike `get_all_installed_packages()`, this is a diff input: an installed Arch system with zero packages does not exist, so an empty result is unambiguously a read failure, and a silent `{}` here would make `change_report.diff()`/`classify()` misreport a failed read as a genuine no-op.

The five read-only queries above (`get_installed_version`, `get_all_installed_packages`, `get_foreign_packages`, `get_pacman_sync_version`, `filter_missing_deps`) check for an importable `pyalpm` and route through libalpm bindings when available — direct local-DB and sync-DB access is faster than spawning a `pacman` subprocess per call. The fallback path is the original subprocess shell-out, so installs without `pyalpm` are unaffected. `pyalpm` is shipped as `[project.optional-dependencies] extra` (`uv sync --extra extra`) or installed via the system package. `SYSFORGE_PACMAN_NO_PYALPM=1` forces the subprocess path even when pyalpm is present (used by `tests/conftest.py` so existing subprocess-mocking tests keep driving the query). Mutating paths (`pacman -U`, `pacman -S --needed`) and the `pacman -Fq` files-DB lookup in `provides_lookup.py` remain subprocess-based.

Constants: `BATCH_STRIP_FLAGS` (flags removed from per-build makepkg calls during batch install — composed as `SYNC_FLAGS | INSTALL_FLAGS`, the two flag families defined in `makepkg_flags.py`), `BATCH_EXTRA_FLAGS`. `SYNC_FLAGS` (`{--syncdeps, -s}`) is the single source of truth for the dep-sync strip — reused by the toolchain stage's staged-deps passes (§`run toolchain` Dep resolution) so the two sites that suppress makepkg's `pacman -S` share one name. Both `INSTALL_FLAGS` and `SYNC_FLAGS` live in `makepkg_flags.py` (their natural home — flag-family constants); `pacman`/`toolchain` import them from there, and `makepkg_wrapper` re-exports `INSTALL_FLAGS` for back-compat.

**Pacman PostTransaction hooks.** Three libalpm hooks under `/usr/share/libalpm/hooks/sysforge-{kernel,toolchain,buildstate}.hook` invoke `/usr/lib/sysforge/pacman-hook-helper.sh` to drop a sentinel into `/var/lib/sysforge/sentinels/`. Targets: kernel hook fires on `linux*` (inclusive of `linux-firmware` and `linux-headers`); toolchain hook fires on `llvm*`, `clang`, `lld`, `compiler-rt`, `gcc`, `gcc-libs` and the lib32 variants; buildstate hook fires on `*`. The helper is failsafe — every error path exits 0 so it cannot break a pacman transaction. `cmd_update` calls `_consume_pacman_hook_sentinels()` at entry: kernel/toolchain sentinels become `_log.warn` reminders, then unlink; buildstate is unlinked silently because the existing `BuildState.sync_with_installed()` already runs. The sentinel directory is created via tmpfiles.d and pre-created during the bootstrap configure stage; the consumer skips silently when the directory is absent so older installs that predate the hooks still work.

### `change_report.py`

Post-build change summary for pipeline stages. A leaf primitive: imports `pacman` and `render`
only, never reaches up into `pipeline` — the runner calls it, not the other way around.

**Why snapshot diffing, not stage-side bookkeeping.** `sysforge update` already reports package
transitions because it builds through `BuildOutcome`, which carries version pairs. The pipeline
stages (`packages`, `kernel`, `toolchain`) build through `makepkg_run` directly and never construct
one, so there was nothing to read a summary from. Diffing two snapshots of the pacman local DB
taken around the stage sidesteps that gap and is authoritative in a way per-stage instrumentation
isn't: it catches split package members and dependencies pulled in mid-build with no extra
bookkeeping in the stage itself.

**Public API:**
- `snapshot(root=None) -> dict[str, PkgFacts]` — wraps `pacman.get_installed_facts()`; raises
  `SnapshotError` if the local DB can't be read, so the caller classifies the run as unknown rather
  than mistaking a read failure for "no changes".
- `diff(before, after) -> list[ChangeRow]` — changed rows (version and/or installed-size delta),
  then added, then removed, name-sorted within each group. A package whose version is unchanged but
  whose installed size moved (a same-pkgrel rebuild that shrinks the package) still counts as a
  change.
- `classify(rows, *, stage_failed, unavailable=None) -> ChangeOutcome` — `unavailable` wins over
  everything; otherwise a failed stage yields `PARTIAL` (rows non-empty) or `NONE_APPLIED` (rows
  empty), and a clean run yields `COMPLETE` or `NO_CHANGES`. `ChangeOutcome` is reporting-only and
  never influences exit codes — `stage.run()` raising remains the sole authority over build
  success, so a misclassification here can mislead but never breaks a build.
- `render(rows, *, stage, outcome, extras=(), reason=None, emit=print) -> None` — renders the
  summary line-by-line through `emit`. Defaults to `print` so tests capture stdout, mirroring
  `update_summary`; the runner passes a `log.ui` binding so the summary also lands in the unified
  log. `ExtraBlock(label, lines)` lets a stage append its own pre-formatted lines below the
  version rows without `render` needing to know what they mean.

`PkgFacts(version, isize)` is what one installed package contributes; `ChangeRow(name, old, new)`
is one package's transition (`old` `None` = added, `new` `None` = removed).

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

**Installed version of a split pkgbase** (`_oldest_installed_ver` / `_produced_pkgnames` in `update_version.py`). A pkgbase's members are normally lock-stepped, but they can drift: disabling a subpackage stops rebuilding it while pacman keeps the last-built copy installed indefinitely. Because `pkgnames` reaches `_check_one_pkgbase` in set-iteration order (`_assemble_package_set` builds `target_names` as a set), taking the first installed member made the verdict depend on the process hash seed — the same `sysforge update` reported a phantom `NEEDS_REBUILD` or `UP_TO_DATE` at random. The version compared is therefore the **oldest** member (a rebuild is what brings a laggard forward, so the laggard decides), taken over only those members the next build will actually produce. That set is read from the persisted `PKGBUILD.sysforge` — the patched copy the last build ran from, whose `pkgname=()` array is an exact, stage-agnostic record of the active subpackage toggles (see `patch_kernel_subpackages`). A member absent from it is an orphan no rebuild can refresh, so counting it would pin a permanently stuck advisory; a member still listed and genuinely behind still drives `NEEDS_REBUILD`. The filter falls back to the full member list when the patched copy is absent, unparseable, or names no member of this pkgbase, and the extra parse is skipped entirely when the installed members already agree. The pacman and non-`--devel` VCS fast-paths share `_oldest_installed_ver` (no patched PKGBUILD applies to either).

### `pkgbuild_patcher.py`

All PKGBUILD mutation. Active when `build_mode` is `"source_built"` (legacy token `"patched_pkgbuild"`, normalized on read) or `"kernel"` on the resolved profile — tested via the shared predicate `profile.build_mode_uses_extracted_profile` (the one home for this membership; don't re-spell the tuple inline).

**Flag extraction** (`extract_pkgbuild_profile`) scans all function bodies and extracts bare, `export`, and `+=` assignments to known flag variables. Strips self-references (`$CFLAGS` in CFLAGS), skips complex bash expressions (e.g. `${CFLAGS/-g /-g1 }`), expands packed `-Wl,a,b,c` tokens into individual sub-tokens. Returns a synthetic profile dict used as the implicit chain root in `merge_extends` — forming the chain: `pkgbuild_extracted → bare → standard → optimized`.

**Conditional block handling** (`_extract_conditional_blocks`) finds `if...fi` blocks containing extractable key assignments using depth-tracked scanning. Entire blocks are removed from the patched PKGBUILD, never partially.

**Patching** (`apply_patch_pkgbuild`) writes `PKGBUILD.sysforge` with all managed flag assignments and conditional blocks removed. The original is untouched. Artifacts persist on build failure for diagnosis; `cleanup_patch_artifacts` removes them on success. On failure, the warning only mentions `pkgbuild_extracted_profile.toml` if it was actually written (non-empty extraction).

Inline `make VAR=val` and `cmake -DKEY=val` lines are only removed when the key is in `_EXTRACTABLE_KEYS` — keys that sysforge manages. This prevents accidental removal of kernel build commands like `make LOCALVERSION=...` or `make INSTALL_MOD_PATH=...` which are real build invocations, not flag assignments.

### `kconfig_plan.py`

Sole home for the kernel PKGBUILD's kconfig region (2.5.1-F1). Six ordered slots —
`BASE_SEED`, `FRAGMENT_MERGE`, `GENERATE`, `HOTPLUG_MERGE`, `REVIEW`, `VERIFY` — cover everything a
kernel `prepare()` needs done to its config: seed a base `.config`, overlay the sysforge fragment,
run the configured generation targets, re-enable hotplug drivers a minimizer stripped, let the
operator review the result, then verify the requested symbols actually landed. `SLOT_ORDER` is the
**sole ordering authority**: contributors fill a
`KconfigPlan` by slot key via `contribute(Step)`, so the order calls run in cannot affect the
rendered result — refilling an already-filled slot raises. No step reads another step's output;
the `# sysforge: kconfig-resolve` sentinel that the pre-refactor patchers used to tell their own
text apart from the packager's is gone, along with the four independent patchers it coordinated.

Each slot renders into one of two splice regions, `PRE` or `POST` (`SLOT_REGION`). Two regions are
needed, not one, because `BASE_SEED`/`FRAGMENT_MERGE` and `HOTPLUG_MERGE`/`GENERATE`/`REVIEW`
attach to different lines: the seed/overlay follows the kconfig-*setup* line (`_KCONFIG_SETUP_RE`,
else the `.config`-creation line via `_CONFIG_WRITE_RE`), while the hotplug re-enable must follow
the *last* surviving kconfig line and still precede a packager-owned review target — the same
three-way anchor problem the old `patch_hotplug_fragment_merge` solved ad hoc. Both anchors insert
*after* the line they match (`_find_kconfig_anchor` returns the offset past the matched line's
newline), and a packager-owned minimizer target is itself one of `_KCONFIG_SETUP_RE`'s
alternatives, so a minimizer is always followed by the seed/overlay, never preceded by it. When a
configured `GENERATE` sequence is present both regions resolve to the same offset and render as one
contiguous block (the common case).

`Step` is a frozen dataclass holding a slot's indent-free rendered lines plus three optional
behaviour flags: `skip_if_present` (a substring already in the PKGBUILD that makes the step
redundant — the idempotency guard), `noninteractive_rewrite` (lines substituted for `lines` on an
unattended run instead of dropping the step outright — used by `ui_target_step` so a *configured*
UI review target still resolves the config non-interactively rather than vanishing — the shared
TTY pause it renders alongside the target therefore vanishes with it on an unattended run), and
`owns_generation` (set by both `generate_step` and `ui_target_step`, since a UI-only configured
sequence lands entirely in `REVIEW` yet still means sysforge owns kconfig generation — it gates the
removal pass below).

`KconfigPlan.install(path)` renders every filled slot into the PKGBUILD's kconfig region exactly
once — deliberately render-once, not idempotent, since `PKGBUILD.sysforge` is regenerated per
build. Four passes:

1. **Drop rules.** A cooperating PKGBUILD (already calling `merge_config.sh`) drops the PRE slots.
   A slot whose `skip_if_present` marker is already in the text drops itself. A non-interactive run
   drops `REVIEW` (or rewrites it via `noninteractive_rewrite` when set). A PKGBUILD with its own
   review target drops an *injected* `REVIEW` but not a *configured* one (`owns_generation`), since
   pass 2 is about to remove that packager review line as part of the configured sequence.
2. **Removal**, whenever any filled step has `owns_generation` set (`generate_step` or
   `ui_target_step`, alone or together — a UI-only configured sequence still owns generation even
   though `GENERATE` itself ends up unfilled): every packager-owned kconfig line is removed
   (spliced last-to-first so earlier offsets stay valid), since the configured sequence is the sole
   authority for kconfig generation. Raises `RuntimeError` when none exist — fails closed rather
   than building with a half-patched config step.
3. **Rewrite** of any surviving packager-owned UI target to `olddefconfig` on an unattended run —
   a no-op after pass 2 removed every kconfig line, which is why pass 2's anchor offset can be
   recorded up front and reused.
4. **Splice** the two regions into the text, high offset first so the lower offset stays valid.

**Requested-symbol survival check (`VERIFY`, `verify_step`, 2.6.1-F23).** SysForge writes its
fragments as plain text, and until this slot never checked that the symbols it asked for actually
landed. Three mechanisms void a fragment line with no fragment-level signal: a value illegal for
the symbol's type (`=m` on a `bool` — kconfig discards the whole assignment and warns mid-build), a
symbol upstream has renamed or removed (dropped in silence), and an unmet *host-tooling* dependency
(`CONFIG_RUST`, whose `scripts/rust_is_available.sh` probe fails and leaves the symbol unset —
`3.0.0-F1` prevents that case up front, this catches it when that preflight is absent or wrong, so
neither supersedes the other). `2.6.1-B17` was the first two at once, and each survived because the
only evidence was a warning line scrolling past during a 20-minute build, erased from `.config` by
the next `make olddefconfig`. `merge_config.sh` already models the check (`Value requested for
CONFIG_X not in final .config`); `verify_step` does the equivalent for sysforge's own fragments —
a shell function rendered into `prepare()` that walks `sysforge.config` and `sysforge.hotplug.config`,
parses each `CONFIG_X=v` / `# CONFIG_X is not set` line, and warns per symbol whose requested value
differs from the resolved one (an absent symbol and an `is not set` line both read as `n`, which is
what kconfig means by them). Four properties:

- **Shell, not Python** — the resolved `.config` exists only inside makepkg's build tree, which
  sysforge never reads back.
- **Last in `SLOT_ORDER`, after `REVIEW`** — it reports on the `.config` the build actually uses,
  and an operator editing the config in `nconfig` can drop a requested symbol just as
  `olddefconfig` can.
- **Warn, never fail** — a dropped symbol is a silent loss of intent, but hard-failing a kernel
  build over one stale entry in a curated table is worse. `kernel_safety.py` remains the only hard
  gate.
- **Type-agnostic** — it compares literal requested against literal resolved values, so it needs no
  table of symbol types and cannot drift as the kernel tree changes.

It is contributed unconditionally by the kernel build path, file-guarded (a runtime no-op when
`kconfig_merge = false` wrote no fragment), idempotent via the `VERIFY_MARKER` substring, and
errexit-safe under makepkg's `set -e` (every failing command sits in an `if` condition, and the
call itself is `|| true`). `tests/test_kconfig_plan.py::TestVerifyShellBehaviour` executes the
rendered function under `bash -e` against real fragment/`.config` pairs — the runtime behaviour is
the whole point of the step and none of it is observable from the rendered text.

Three rendered-text divergences from the pre-refactor patchers are deliberate and owner-approved,
each pinned by its own test: the `# sysforge: kconfig-resolve` sentinel is no longer emitted (it
existed only for one patcher to recognise another's output); the hotplug block now indents to
match its anchor line rather than column 0 (the old code took the indent of the line *after* the
insertion point — for a stock PKGBUILD, the closing `}`); and with both `GENERATE` and `REVIEW`
filled, the hotplug merge renders *before* the TTY review pause rather than after it (the pause
announces the merged `.config` is assembled, which is only true once the hotplug merge has run).

**Subshell toolchain env reset** (`patch_subshell_env_reset`) injects `unset CC CXX LD` at the top of every subshell function body (`funcname() (...)`) in `PKGBUILD.sysforge`. Subshell functions are isolated helper builds (musl bootstrap, embedded grub, wimboot, etc.) that should use the system-default compiler and linker, not the sysforge profile toolchain or inherited shell overrides. Without this, `CC=clang` from the profile and `LD=ld.lld` from the shell env leak into sub-builds that expect gcc/ld.bfd and produce broken toolchain wrappers or linker script failures. Considers two sources: profile toolchain keys (CC, CXX) and inherited shell env (CC, CXX, LD). Only injects when at least one key differs from the system default (gcc/g++/ld). Called from `_run_build` after PKGBUILD.sysforge is created, on all build paths (both patched and group-only).

**Package rename** (`patch_pkgbase_rename` + `patch_package_suffix`) — the one home for the PKGBUILD rename, shared machinery `_patch_rename` parametrized by a token mapping. `patch_pkgbase_rename(path, new_pkgbase, mode)` (F40) renames to an *arbitrary* pkgbase with base-replacement token mapping: literal `pkgname` members embedding the old base are rewritten in place (`linux-zen-headers` → `linux-mine-headers`, boundary-safe — `linux-zenith-docs` survives), `$pkgbase` references cascade untouched, split `package_<name>()` functions track their renamed members, and `mode="conflict"` injects `provides`/`conflicts`/`replaces` over the stock names exactly as the suffix path does. `patch_package_suffix(path, suffix, mode)` is a thin wrapper with an *append-suffix* mapping (every literal member gets `-{suffix}`, embedding the base or not) plus the load-bearing idempotency collapse (a pkgbase already ending in `-{suffix}` is a no-op). Both return the same rename-info dict (`origin_*`/`renamed_*`/`mode`; the wrapper adds `"suffix"`) consumed by `build_state` and the rename-aware `validate_patched_pkgbuild(rename=…)`, whose `_validate_rename` has a suffix branch (names must carry the suffix) and an exact-match branch for arbitrary renames.

**LLVM target filtering** (`patch_llvm_targets` + `is_llvm_pkgbase`) injects `-DLLVM_TARGETS_TO_BUILD="<list>"` into the cmake invocation of LLVM-toolchain PKGBUILDs (`llvm`, `clang`, `compiler-rt`, `lld` — gated by `is_llvm_pkgbase` on `pkgbase`). **`lib32-*` LLVM packages are exempted** at the `makepkg_wrapper._maybe_patch_llvm_targets` call site: they ship no headers of their own and compile against the all-target 64-bit `/usr/include/llvm` headers, so reducing their target set strands lib32-clang's offload tools (`clang-nvlink-wrapper`/`clang-sycl-linker`) on `InitializeAllTargets()` symbols the reduced lib32 libLLVM doesn't export (`ld.lld: undefined symbol: LLVMInitialize…AsmParser`). They always build the full set, matching the headers. The patcher is invoked at the end of `apply_patch_pkgbuild`. The target list is resolved by `primitives/llvm_targets.resolve_llvm_targets` in this order: `[llvm] targets` in `toolchain.toml` (explicit override; `targets = []` disables filtering) → `[hardware] llvm_targets` in `hardware_profile.toml` (autodetected from `uname -m` + `gpu_vendors`) → `None` (no filtering, build all targets). Idempotent: re-running on a PKGBUILD already carrying the same value is a no-op; replacing an existing `-DLLVM_TARGETS_TO_BUILD=` arg preserves the upstream PKGBUILD style. On a no-cmake-found PKGBUILD (upstream switched to meson), logs a warn and leaves the file unchanged. The `LLVM_EXPERIMENTAL_TARGETS_TO_BUILD` flag is intentionally untouched.

### `render.py`

The one home for the presentation vocabulary shared by every tag-gutter report block: `sysforge update`'s summary (`update_summary`), both pre-flights (`llvm_state.render_preflight`, `toolchain_preflight.render_preflight`), and the pipeline's post-build change summary (`change_report.py`). Public API: `arrow()`, `em_dash()`, `ellipsis_glyph()`, `version_pair(old, new, *, equal_marker=True)`, `tag_header(tag)`, `fmt_bytes(n)`.

`version_pair` renders a transition as `old → new`, collapsing to `ver (=)` when both sides are known and identical (`equal_marker=False` keeps the arrow form unconditionally — the built-package and stage-owned-update rows read as a report of what was done, so an unchanged version is still a transition there). An unknown side renders as `—`. `tag_header` returns the `  [TAG]` prefix padded to the shared 17-column gutter. `em_dash()` is `arrow()`'s counterpart for a standalone `—` (e.g. a failure message's clause separator) — any renderer wanting one goes through here rather than hardcoding the glyph. `fmt_bytes(n)` formats a byte count as a human-readable binary-prefix string (`142.3 MiB`); promoted from `cache_probe._fmt_bytes` so the cache report and the change summary's size column format identically. `ellipsis_glyph()` is the same for a `…` (used by truncated report blocks — `… and N more`); it carries the `_glyph` suffix because bare `ellipsis` names a Python builtin type. All glyph-bearing helpers route through `log.downgrade_glyphs` so they degrade together under the Unicode gate.

**Glyphs are resolved at format time**, not left to the emit path. Every pre-flight block now emits through `log.ui` (`update.py`, `build_cmd.py`, `fetch.py`, `pipeline/stages/toolchain.py`), which applies `log.downgrade_glyphs` and mirrors the block into the unified run-log — the bare `print()` sites that let a hardcoded `→` survive under `TERM=linux` are gone. Resolving early is kept regardless: `downgrade_glyphs` is idempotent, so it costs nothing, and it keeps a renderer's return value correct for any caller that formats without emitting. It covers the `—` placeholder in the same pass. Leaf module: imports only `sysforge.log`, so any layer may use it.

### `llvm_state.py`

Sole entry point for inspecting LLVM-toolchain source trees before a command touches them. Surfaces, per LLVM pkgbase in scope: variant, source origin (`repo` / `aur` / `user` / `missing`), dirty state with reason, divergence vs upstream (cheap path: compare HEAD against `SourceMetaCache.head_commit`; opt-in `probe_fetch=True` runs `git_fetch_and_compare`), pacman install origin + version, parsed PKGBUILD version, and the resolved `build_mode` from rule matching. PGO profdata mismatch is cross-checked via `makepkg_wrapper._resolve_pgo_state` for any pkgbase with `build_mode = "pgo_llvm_toolchain"`.

Public API: `is_llvm_in_scope(pkgnames)`, `collect_llvm_state(pkgnames, config, *, probe_fetch=False, offline=False)`, `is_actionable_state(state)`, `render_preflight(report, *, verbose=False)`, `evaluate_strict(report, *, allow_dirty=False)`. Read-only — never clones, never mutates. PKGBUILD resolution mirrors steps 1-3 of `config.find_pkgbuild` without the auto-clone branch.

Wiring: `fetch` / `update` / `build` / `converge` render the report informationally (suppress with `--no-llvm-preflight` or `[safety] llvm_preflight = false`). `render_preflight` shows only **actionable** rows (`is_actionable_state`): a row is worth printing when it has an active concern (dirty / diverged / PGO profdata-mismatch), was built locally (`install_origin == "foreign"`), or sysforge intends to build it from source (any non-`pacman` build_mode). A repo-origin package installed from the binary repo with no source build_mode has every source-state column empty/`unknown`, so its row is dropped (it would otherwise read as noise); when every row is non-actionable the whole block collapses to nothing. `--verbose` overrides the filter and renders every row. The filter is display-only — `evaluate_strict` still sees the full state set, so the `run toolchain` blocker path is unchanged. `run toolchain` calls `evaluate_strict` after `_resolve_all_pkgbuilds` and refuses-by-default on dirty or diverged trees; bypass per-run with `--allow-dirty-llvm`, or actually overwrite the local trees with `--cleansrc-force` (which uses the new dirty classifier so trees in the `diverged_upstream` state — upstream force-pushed, no local commits authored by the local git user — are reported as clean and don't even need the bypass). The dirty-reason string distinguishes `"N commits ahead of upstream"` (`ahead`, real unpushed work) from `"diverged from upstream (N local / M upstream)"` (`diverged_user`, the histories have a common ancestor but the local user authored at least one commit on the local side). PGO profdata version mismatches are never bypassable — building against stale profdata silently corrupts the output.

Reuses (do not duplicate from caller code): `pkgbuild_patcher.is_llvm_pkgbase`, `aur.git_is_dirty`, `aur.git_fetch_and_compare`, `source_meta.SourceMetaCache.get`, `pkgbuild_meta.parse_pkgbuild`, `version.format_version`, `pacman.get_foreign_packages` / `get_installed_version`, `profile.match_rules` / `get_build_mode`, `makepkg_wrapper._resolve_pgo_state`. Log tag: `[LLVM]`.

### `toolchain_preflight.py`

Batch-time toolchain availability check, runs in `cmd_update` after `to_build` is finalised and before `batch_install_makedeps` (the helper is `update._toolchain_preflight_for_batch`). For every package in the batch the helper resolves the active `consumes` set (`profile.resolve_consumes` over `parse_pkgbuild` + `match_rules` + `resolve_profile`), then reduces those plus the lib32-* subset **and the set of resolved compilers** (`resolved["CC"]`/`["CXX"]`) to a required-toolchain token set via `collect_required_toolchains`. Token grammar: `rust:native`, `rust:cross:<target>`, `rust:cross:<target>@<toolchain>`, `cmake`, `meson`, `cc:<name>`.

**Compiler-health probe (`cc:<name>`).** `_probe_cc` verifies the resolved compiler actually *runs* (`<cc> --version`) and, for clang, that the whole LLVM lockstep suite shares one pkgver (`pacman -Q`). A half-installed / mismatched LLVM toolchain — clang built against a libLLVM that no longer exports a symbol it needs, or a partial upgrade that leaves some suite members behind — makes clang fail to even start with a dynamic-link symbol error (`undefined symbol: LLVMInitialize…Target`). Without this probe that surfaces only as N separate per-package `Unknown compiler(s): [['clang']]` failures with no captured cause; with it, the batch aborts up front with a `sudo pacman -Syu …` / `sysforge run toolchain` remediation. The skew arm sweeps **every installed member** of `LLVM_LOCKSTEP_SUITE` (`llvm`, `llvm-libs`, `clang`, `lld`, `compiler-rt`, `polly`, `openmp`) comparing pkgver only (pkgrel is stripped — a packaging bump like `lld 22.1.5-3` next to `clang 22.1.5-1` is not a skew), so a stranded `compiler-rt` is caught and the suggested `pacman -Syu` lists every member that needs resyncing rather than a hardcoded four. `spirv-llvm-translator` (its own version scheme) and `lib32-*` (separate multilib lineage / epoch) are deliberately excluded. `LLVM_LOCKSTEP_SUITE` is the single source of truth, shared with the toolchain stage's `_verify_llvm_install` (`_LLVM_VERSION_MATCH_SET` imports it) so the everyday-`update` probe and the post-install verifier never diverge — the probe stays a lightweight primitives-layer check rather than importing the pipeline-layer verifier. The pinned `@<toolchain>` form is emitted when the package's PKGBUILD exports `RUSTUP_TOOLCHAIN=<name>` inside `build()` / `check()` (regex scan over the parsed function body — `lib32-gstreamer` pins `stable` this way and would otherwise be probed against the workstation default). Currently only `rust:cross:i686-unknown-linux-gnu[@...]` is emitted (any lib32-* package with `rust` in consumes); other cross targets plug in at `collect_required_toolchains` when added.

Probes are sub-second: `rust:native` is `rustc --version`, `rust:cross:<target>[@<toolchain>]` writes a `fn main(){}` to a tempdir and runs `rustc --target <target> --emit=metadata` with `RUSTUP_TOOLCHAIN=<toolchain>` overlayed on the env when a pin is present. `--emit=metadata` skips codegen/linking but still requires the std crate, which is exactly the hurdle meson's own rust sanity check fails on when the i686 target is missing from the toolchain the build will use. Without a pin the probe uses `$RUSTUP_TOOLCHAIN` or `rustup show active-toolchain` for the effective toolchain name.

The clang probe additionally checks the `LLVM_LOCKSTEP_SUITE` for a pkgver skew, and reports it **structurally** on `ToolchainCheck.versions` — a tuple of `(label, installed_ver, target_ver)` rows where every version group is stated against the newest installed pkgver (resolved with `vercmp`, falling back to a lexical max if the pacman binary is unavailable, since a display helper must never abort a batch). `render_preflight` turns those rows into an indented `clang/lld: 22.1.5 → 22.1.6` table under the check's detail line, collapsing the already-current group to `llvm/llvm-libs: 22.1.6 (=)`. The probe reports facts; the renderer owns formatting. Checks with nothing version-shaped to say carry an empty `versions` and render exactly as before — a healthy suite is one detail line, not seven rows.

Auto-remediation: failures with `auto_remediable=True` (currently `rustup target add` only) get an interactive y/n prompt via `primitives.prompt.prompt_choice`; on accept the command is executed and the failed probe is re-run. Non-interactive runs (`--non-interactive`, `--noconfirm`, or no TTY) print the fix block and exit 1 instead. Per-package profile-resolution failures are caught and warned — preflight is best-effort and never blocks a build the real wrapper could still succeed at.

Public API: `collect_required_toolchains(per_pkg, lib32_pkgs, rust_toolchain_pins=None, compilers=None)`, `run_preflight(required)`, `render_preflight(report)`, `auto_remediate(report, *, non_interactive=False)`. Wiring: `update` only, behind `--no-toolchain-preflight`. The companion `primitives.build_diag.diagnose` runs in `makepkg_wrapper.invoke_makepkg` on non-zero exit and matches known failure signatures (E0463 missing std crate, gstreamer PTP-no-rust, meson "Unknown options" → stale build dir, `cuda:host-gcc-too-new` → nvcc rejecting a system gcc newer than the CUDA toolkit supports, `toolchain:lib32-reduced-target` → a link-time `ld.lld: undefined symbol: LLVMInitialize…` against a lib32 libLLVM (matched with an `-m32` / `lib32/libLLVM` signal) — lib32-clang's offload tools referencing all-target init symbols the reduced lib32 libLLVM lacks, pointing at building lib32 with the full target set rather than a spurious version skew; `toolchain:lib32-clang-libgcc` → a lib32 (`-m32`) build where `ld.lld` can't find the 32-bit `libgcc_s` clang implicitly links (`unable to find library`/`cannot find -lgcc_s`, gated on an `-m32`/`lib32` signal so a 64-bit miss doesn't match), which CMake/meson surfaces as the compiler being "broken" even though clang is fine — points at a per-package `--cc gcc --cxx g++` override (gcc `-m32` links its own multilib libgcc); `toolchain:llvm-broken` → a clang/libLLVM mismatch where clang can't run, matched on `undefined symbol: LLVMInitialize…` / `symbol lookup error: …clang` / meson's `Unknown compiler(s): [['clang']]` (the lib32 reduced-target case is matched first and suppresses this generic one); `pkgconfig:version-gate` → meson's canonical `Dependency lookup for <mod> with method 'pkg-config' failed: Invalid version, need '<mod>' ['>= X'] found 'Y'` line, which is accurate but leaves the operator to work out by hand who owns the `.pc` and whether the repos can satisfy the floor at all — the matcher parses module/floor/found, resolves the owner of `<mod>.pc` via `pacman.owners_of` over `/usr/lib`, `/usr/share` and `/usr/lib32` pkgconfig dirs, and reports the repo version (`pacman.get_pacman_sync_version` + `version.vercmp` against the floor) plus whether an AUR `<owner>-git` exists (`aur.aur_info`). Every probe is best-effort — a failure degrades the message, never the diagnosis. It emits **no** `fix_cmd` by design: waiting for the repo bump and adopting the `-git` dep have materially different costs (the latter forfeits the distro's soname-rebuild guarantee, for which sysforge has no general equivalent), and a copy-pasteable command would pre-pick the riskier one) in the captured output and any `meson-logs/meson-log.txt` **or `CMakeFiles/CMakeError.log`** under the build directory; deduped on signature, never masks the real error. The CUDA matcher reads the toolkit's `crt/host_config.h` `#if __GNUC__ > N` gate and the highest installed `/usr/bin/g++-≤N` to emit a concrete `NVCC_APPEND_FLAGS='-ccbin …'` fix. Each `FixSuggestion`'s `signature`/`fix_cmd` is also carried up the exception (`.diagnosis`) and persisted to `build_state.toml`'s `[failures]` table by `sysforge update` (see §`build_state.py`). **Interactive builds** inherit the TTY so makepkg's stdout is never captured; on failure `invoke_makepkg` instead runs `diagnose([], _effective_build_dir(...))` over the side-car logs (resolving `$BUILDDIR/<pkgbase>` when `BUILDDIR` redirects the build out-of-tree) and threads the result through the user-abort RuntimeError, so `state failed` records a real signature rather than "Aborted by user". Log tags: `[PREFLIGHT]`, `[DIAG]`.

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
6. If `source_built` (legacy `patched_pkgbuild`) or `kernel` mode: extract PKGBUILD flags, write extracted profile, apply patch
7. If `kernel` mode and not `interactive`: patch interactive kconfig targets in `PKGBUILD.sysforge` to `olddefconfig`
8. If `kernel` mode: detect effective CC; if clang, inject `LLVM=1 LLVM_IAS=1` into build env
9. Emit complete temp `makepkg.conf` (merged system conf + profile overrides; kernel mode omits `CFLAGS`/`CXXFLAGS`/`LDFLAGS`/`CPPFLAGS`/`DEBUG_*` profile overrides — system conf values preserved verbatim)
10. Resolve env vars for subprocess injection
11. Invoke `makepkg -p PKGBUILD.sysforge` with temp conf and injected env

The `AlreadyBuilt` *policy* (what a catch site does next) lives in
`already_built.py` (`resolve_already_built`, postures `reuse`/`review-gated`)
— decide-only, no tag of its own (logs under the caller's tag); the exception
itself stays in `makepkg_invoke.py`.

**System conf merge:** `emit_makepkg_conf` reads `/etc/makepkg.conf` as a baseline and writes a complete self-contained temp conf — system keys pass through verbatim, profile keys override their counterparts inline, new profile keys are appended. No `. /etc/makepkg.conf` sourcing at runtime.

**Flag guards in `emit_makepkg_conf`:** before writing the conf, several scrubs run so a known-broken flag never reaches makepkg:
- **Linker guard** — when the effective linker (from `-fuse-ld=` in profile-then-system `LDFLAGS`) is not `lld`, lld-only tokens (`--icf=all/safe/none`, bare or inside `-Wl,…`) are stripped from *profile-override* `LDFLAGS` via `_strip_lld_flags` so configure-time link tests against the system linker don't break.
- **GCC+LTO guard** — GCC's `.gnu.lto_*` bitcode is incompatible with lld; when the effective compiler is GCC, `-flto=thin` is rewritten to `-flto`, and if the linker is lld, LTO is disabled entirely (clear `LTOFLAGS`, strip `-flto*`, flip `lto`→`!lto` in `OPTIONS`).
- **lib32 guards** — for `is_lib32=True` builds, 64-bit-only `-march=` tokens are scrubbed from `CFLAGS`/`CXXFLAGS` and lld `--icf=*` tokens from `LDFLAGS`, at **both** the profile-override site and the system-conf passthrough. The icf scrub is **unconditional on the effective linker** (unlike the linker guard above): 32-bit identical-code-folding breaks links for some lib32 packages (e.g. `lib32-lzo`) even when lld is active. This keeps the `bare` profile (priority-30 destination for `lib32-*`, silent on these keys) from letting the system conf's host-arch flags through to an i686 build — the guard lives at conf emission, not in a per-profile rule.
- **musl-static guards** — for `is_musl_static=True` builds (a static-musl bootstrap like `pacman-static`, detected by `pkgbuild_meta.is_musl_static_build`: a `musl`/`kernel-headers-musl` makedepend **and** a build-time `CC=musl-gcc` or `-static` LDFLAGS append), the **bfd** linker is forced (`-fuse-ld=lld`→`-fuse-ld=bfd` via `_inject_linker`), lld-only tokens are stripped (`_strip_lld_flags`), and PGO flags are scrubbed (`_strip_pgo_flags`) from `CFLAGS`/`CXXFLAGS`/`LDFLAGS`, across **both** profile-override and system-conf passthrough (resolved once into an owned override). `-fuse-ld=lld` + `-static` + musl produces a binary that **segfaults at startup** (configure's conftest crashes → "cannot run C compiled programs"), and musl-gcc cannot consume a clang `.profdata`. The musl analogue of the lib32 guard; same home (`emit_makepkg_conf`), reusing the same strip helpers.

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
- `git_fetch_and_compare(pkgbuild_dir, *, timeout=30, limiter=None, is_vcs=False)` — **full-history** fetch of the tracked upstream (`git fetch <remote> <branch>`, adding `--unshallow` when the repo is shallow) followed by a HEAD compare, then an `--ff-only` merge when HEAD is an ancestor of `FETCH_HEAD`. A `--depth=1` fetch is deliberately *not* used: it grafts the fetched tip as a parent-less root (and marks the repo shallow), which makes the `merge-base --is-ancestor` check — and the `rev-list` counts in `classify_head_vs_upstream` — see no shared history, so every routine upstream advance would falsely report `diverged`. Packaging repos carry only PKGBUILD/metadata, so a full fetch is cheap. **Non-destructive**: never runs a reset/rebase; returns a `GitFetchOutcome(status, head_before, head_after, error)` where `status ∈ {"up_to_date", "fetched", "diverged", "failed", "skipped_no_tracking"}`. Divergence (a genuine non-fast-forward — local commits or a force-push upstream) is reported, not auto-recovered here — the scheduler decides whether to reset (see `source_sync.py`). Honours the limiter's `wait_before_fetch()` / `Retry-After` budget when supplied. `is_vcs` (threaded from `source_sync.is_vcs_pkgbase(pkgbase)`, and from `llvm_state` for the same reason) governs **only the operator-facing message** on the dirty-tree branch: a `-git` checkout whose sole working-tree change is makepkg's `pkgver()` auto-bump plus the regenerated `.SRCINFO` is a build artifact, not operator work, so the `WARN … working tree has local modifications` is downgraded to an `INFO` noting the caller will reset. The fast-forward gate itself stays VCS-blind on purpose: `git merge --ff-only` aborts against a dirty tree whenever the upstream commit also touched `PKGBUILD` (the common AUR case), which would return `failed` — a scheduler blocker — where `diverged` is the outcome `source_sync`'s clean-tree hard-reset already heals correctly.
- `is_transient_git_error(stderr)` / `is_rate_limit_error(stderr)` — shared stderr classifiers used by both the scheduler and legacy retry paths.
- `_classify_head_vs_upstream(pkgbuild_dir)` — single classifier consumed by both `git_is_dirty` and `llvm_state._dirty_reason`. Returns `(state, n_local, n_upstream)` where `state ∈ {"not_a_repo", "no_head", "no_tracking", "clean", "behind", "ahead", "diverged_user", "diverged_upstream"}`. The two `diverged_*` states distinguish "upstream rewrote history (force-push), no local commits authored by the local git user" (`diverged_upstream` → not dirty) from "HEAD and upstream have a common ancestor but at least one of HEAD's divergent commits is authored by the local user" (`diverged_user` → dirty). The local user identity is read from `git -C <dir> config user.email` (with global fallback). `ahead` = HEAD is a strict descendant of upstream; `behind` = HEAD is an ancestor of upstream (the only "out of date but clean" case).
- `git_is_dirty(pkgbuild_dir, *, is_vcs=False)` — wrapper over the classifier: returns `True` for `no_tracking`, `ahead`, `diverged_user` (plus uncommitted tracked changes detected separately via `git status`); returns `False` for `clean`, `behind`, `no_head`, `not_a_repo`, `diverged_upstream`. Untracked files (build artifacts) are intentionally ignored. The `diverged_upstream` exemption fixes the false-positive on workstations whose Arch packaging clones get force-pushed every release. **`is_vcs=True`** (passed for `-git`/`-svn`/`-hg`/`-bzr` packaging repos) additionally filters out makepkg's `pkgver()` churn from the uncommitted-tracked check: a PKGBUILD whose diff is restricted to `pkgver=`/`pkgrel=` lines (the auto-bump), and **any** change to the generated `.SRCINFO` (treated as a build artifact — `pkgver()` rewrites its version-pinned `depends`/`provides` lines too, so a line-level filter can't distinguish a mechanical bump from a real edit; deliberate edits live in PKGBUILD, which is still checked). Deliberate edits to other PKGBUILD lines / other tracked files still count as dirty.
- `purge_src(pkgbuild_dir, *, force=False, is_vcs=False)` — `rm -rf` the directory after a `git_is_dirty` safety check. Raises `RuntimeError` if the clone holds local work that would be destroyed; non-git directories are purged unconditionally; non-existent paths are a silent no-op. `force=True` skips the dirty check and purges unconditionally — used by the `--cleansrc-force` CLI path. Used by `sysforge build --cleansrc[/-force]`, `sysforge update --cleansrc[/-force]`, `sysforge fetch --cleansrc[/-force]`, `sysforge run toolchain --cleansrc[/-force]`, and the source-sync recovery paths. **Detached-HEAD fix:** a `source = "repo"` checkout pinned to a release tag (see `source_sync.py` below) sits on a detached HEAD with no `@{u}` tracking branch — the pre-pin refusal logic treated that shape identically to a genuine local-only repo and refused to purge it. The refusal check (`_purge_refusal_message`) now calls `_head_reachable_from_remote` (`git branch -r --contains HEAD`) before citing "no upstream tracking branch": a detached HEAD that is still reachable from some remote branch is upstream's own history, not local-only work, and does not block the purge; a genuinely local-only commit (unreachable from any remote ref) still refuses.
- `purge_srcdest(pkgbase, srcdest_dir, *, pkgbuild_dir=None)` — companion to `purge_src` for `--cleansrc`: the checkout rmtree never touched makepkg's `SRCDEST` tarball cache, so a purged-and-re-cloned package could still rebuild against a stale cached source tarball instead of the freshly re-pinned/re-cloned tree. Deletes `<pkgbase>-<digit…>*` entries (matching makepkg's `name-version.ext` tarball naming, so a dash-prefixed sibling pkgbase like `foo-tools` is never clobbered by a `foo` purge) from `srcdest_dir`. No-ops when `srcdest_dir` is unset/missing, or resolves inside `pkgbuild_dir` (the makepkg-default layout, already covered by the checkout rmtree). Best-effort hygiene, not a safety gate: no dirty-tree guard, no force flag, failures warn and never raise. Returns the count removed. Called from both `SourceSyncScheduler` cleansrc branches (the explicit `--cleansrc` purge and the missing-PKGBUILD recovery purge) alongside `purge_src`.
- `pkgctl_checkout(name, dest, *, timeout=60)` — `pkgctl repo clone --protocol=https <name>` run in `dest.parent`; fetches official Arch packaging repo. Output is streamed line-by-line to `_build_log.debug` so progress is visible at `-vvv` (cloning from gitlab.archlinux.org can take minutes on a fresh checkout). Raises `RuntimeError` on failure or timeout. `find_pkgbuild` passes `[git] clone_timeout` from `sysforge.toml`; `0` disables.
- `pkgctl_switch_version(dest, version, *, timeout=60)` — `pkgctl repo switch <version>` run inside an existing `pkgctl_checkout` clone; `pkgctl` owns the pacman-version → git-tag translation (epoch and pkgrel included), so callers pass `pacman.get_pacman_sync_version()` output verbatim. Leaves the checkout on a detached HEAD at the tag. Raises `RuntimeError` on failure or timeout (including a missing `pkgctl` binary — hints at `devtools`). Sole caller: `source_sync.SourceSyncScheduler._pin_repo_checkout` (see below).
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
- `SyncResult(pkgbase, status, head_before=None, head_after=None, error=None)` — output. Status constants: `STATUS_UP_TO_DATE`, `STATUS_FETCHED`, `STATUS_CLONED`, `STATUS_DIVERGED`, `STATUS_RATE_LIMITED`, `STATUS_FAILED`, `STATUS_FROZEN`, `STATUS_SKIPPED_OFFLINE`, `STATUS_SKIPPED_NO_TRACKING`, `STATUS_SKIPPED_LOCAL`, `STATUS_PURGE_REFUSED`. `STATUS_FROZEN` is a **per-package blocker**: the clone and fetch call sites consult `net_policy.get_policy().check(...)` before touching the network and catch `NetworkFrozen`, turning a denied AUR clone, `pkgctl repo clone` checkout (`build_prep.pkgctl_checkout`, `KIND_REPO_CHECKOUT` — a distinct kind from `KIND_AUR_CLONE`, since the origins and their trust stories differ) or source-sync fetch into this status rather than an exception — the run continues past the blocked package but exits non-zero with the blocked set named (see §Config Layer → `[security] freeze_sources`). The two `vcs_pkgver.py` seams (`evaluate_vcs_pkgver`, `peek_upstream_commit`) are gated the same way and must stay gated together — both consult the policy before any network call, and both take an explicit `pkgbase=` (as `git_ops.git_fetch_and_compare` does) so `--thaw <pkgbase>` still lifts them when the checkout directory has been renamed. `config.find_pkgbuild`'s repo branch calls `pkgctl_checkout` outside the scheduler and therefore propagates `NetworkFrozen` rather than converting it, matching its AUR sibling's fail-closed `RuntimeError`.

Flow per request:
1. **RPC gate.** On the first AUR-source request of a batch, fire one `aur_info([…all AUR names…])` call and cache the results in `SourceMetaCache`.
2. **Short-circuit.** If the cached `rpc_version` / `rpc_last_modified` match the local HEAD's recorded values **and** the package is not a VCS `-git` / `-svn` / `-hg` / `-bzr` (forced-fetch) type **and** `force_fetch=False`, return `STATUS_UP_TO_DATE` without touching the network. This is the common-case path.
3. **Clone.** If the dir is missing or not a git repo, dispatch via the limiter — `pkgctl_checkout` for `source="repo"` (Arch packaging repo via `gitlab.archlinux.org`), `aur_clone` for `source="aur"`/`"git"`. The repo path is never translated to `STATUS_RATE_LIMITED` (gitlab.archlinux.org doesn't enforce AUR's 429/503 budget).
4. **Fetch.** Otherwise call `git_fetch_and_compare` — full-history fetch + HEAD compare + `--ff-only` merge, never resets/rebases. Works for both AUR and repo sources because pkgctl-cloned dirs are plain git repos with a tracking branch.
5. **Divergence.** A packaging repo's job is to mirror upstream, so `STATUS_DIVERGED` is auto-resolved **when the work-tree is clean** per the VCS-aware `git_is_dirty(is_vcs=…)` check: the scheduler hard-resets to `FETCH_HEAD` (`_reset_hard_fetch_head`) and the result becomes `STATUS_FETCHED`. This applies to **all non-local sources** (AUR and repo alike) — routine upstream force-pushes/amends no longer require `--cleansrc`. Only a **dirty** tree (real operator work — uncommitted non-pkgver edits, unpushed commits, or `diverged_user`) keeps `STATUS_DIVERGED`: the work-tree is untouched, the build continues against the local PKGBUILD, and the operator decides whether to `--cleansrc` next run.
6. **Rate limit.** `RateLimited` aborts the remaining batch via `_abort_remaining`, which populates pending results with `STATUS_RATE_LIMITED` so the UI can show per-package status instead of a single global error.
7. **Repo-track pin (`source = "repo"` only).** Gated by `config.resolve_repo_track` (`sysforge.toml [build] repo_track`, default `"stable"`) — a `"main"` track never pins and this step is a no-op. `_pin_repo_checkout(pkgbase, pkgbuild_dir)` looks up `pacman.get_pacman_sync_version(pkgbase)` and, when found, runs `pkgctl_switch_version(pkgbuild_dir, version)` to check out the matching release tag (detached HEAD); no candidate in the sync DB is **not** an error — it warns and leaves the checkout on `main` (the package may live only in a custom repo not tracked by pacman). A `pkgctl_switch_version` failure returns `STATUS_FAILED` with the error attached. Two call sites: **clone-pin** — immediately after a fresh `pkgctl_checkout` in `_clone`, before the head is recorded in `SourceMetaCache`. **Fetch-repin** — in `_fetch`, on every subsequent sync of an already-pinned checkout: because a pinned checkout is a detached HEAD with no tracking branch, `git_fetch_and_compare` reports `no_tracking` for it; the repo-stable branch treats that as the steady state, refreshes tags with a plain `git fetch --tags origin` (`_fetch_repo_tags` — `git_fetch_and_compare` would otherwise bail before ever seeing new tags), then falls through to re-run `_pin_repo_checkout` so the checkout follows the sync DB's release forward. If the work-tree is dirty (`git_is_dirty(is_vcs=…)`) the re-pin is skipped and the result is `STATUS_DIVERGED` with `error="local edits present — not re-pinning"` — real operator work is never silently discarded by the pin logic. When the pin moves HEAD, the delta is reported as `STATUS_FETCHED` (`head_before`/`head_after` bracket the switch) rather than `STATUS_UP_TO_DATE`.

Singletons:
- `get_scheduler(*, state_dir=None, offline=False, cleansrc=False, cleansrc_force=False, force_devel=False, min_fetch_interval_ms=None, rate_limit_abort_s=None, fetch_timeout=None, clone_timeout=None)` — returns the process-wide scheduler, constructing it on first call. Subsequent calls with the same args are memoised; dedup keys: `(pkgbase)` — any given pkgbase is synced at most once per process. `cleansrc_force=True` implies `cleansrc=True` and propagates to `purge_src(force=True)` so `STATUS_PURGE_REFUSED` cannot occur — the operator has explicitly opted in to overwriting local work. `force_devel` only gates the forced-fetch behaviour for VCS pkgbases that *reach* the scheduler; the higher-level filter in `update.py:_sync_sources` is what keeps VCS pkgbases out of the request batch entirely when `--devel` is off (so `--cleansrc` never purges a `-git` tree the user hasn't opted in to rebuild).
- `reset_scheduler()` — test-only hook. Tests that need fresh state call this between runs.

### `build_state.py`

Build state persistence. `/var/lib/sysforge/build_state.toml` is a **superset of `pacman -Q`** — every installed package has an entry, regardless of whether sysforge built it. The `build_mode` field distinguishes them:

- `"source_built"` — built by sysforge from source (the value formerly written as `"profiled"`; the read-side normalization was removed in 3.0.0, so a pre-rename file now reads `"profiled"` verbatim — `sysforge state repair` rewrites it to `"source_built"` in place). Carries `pkgver`, `pkgrel`, `epoch`, `pkgbase`, `pkgbuild_dir`, `flags_string` (serialized resolved compiler flags, newline-separated `KEY=value` lines), `built_at` (ISO 8601 UTC timestamp), and optionally `built_upstream_commit` (40-char SHA of the just-built upstream tree, populated only for single-git-source VCS packages — read by `sysforge update --devel` to short-circuit `pkgver()` resolution via `git ls-remote`; absent for non-VCS, multi-git-source, or any PKGBUILD whose `source=()` has unresolved bash interpolation), `source` (`"aur"` / `"repo"` / `"git"` / `"local"` — the origin classification at build time, read by `sysforge update`'s source resolver so previously-built packages keep their origin across runs instead of being re-derived from live pacman + overrides every invocation; absent for back-compat entries written before the field existed; `"local"` means a hand-maintained PKGBUILD with no upstream remote, source-sync is skipped for it), `owner_stage` (e.g. `"kernel"` or `"toolchain"` — set by a pipeline stage that owns the package's lifecycle, so `sysforge update` skips it by default and points the user at the owning stage; `--include-stage-owned` overrides the skip; both the kernel and toolchain stages stamp it, each with a config-file bootstrap fallback in `primitives/stage_ownership.py` for the pre-first-build window), `toolchain_variant` (`"gcc"` / `"stock_llvm"` / `"pgo_llvm"` — the active toolchain identity at build time, read by `sysforge update` to detect toolchain drift; absent for back-compat entries and for builds that ran with no toolchain stage configured), and `build_seconds` (a bounded CSV ring of the last 5 whole-second build durations, oldest dropped first — recorded by `_record_build_state` in `makepkg_wrapper` on every successful build; feeds `primitives/build_estimate.py`'s median-based build-time estimate and the post-build estimated-vs-actual line, see §Pipeline Layer → *Build-time estimate and pre-build snapshot*; absent for never-built packages, which the estimate reports as unknown rather than zero). `source`, `owner_stage`, and `toolchain_variant` are *sticky* — `BuildState.record()` preserves the prior value when the caller doesn't pass one, so a rebuild through a code path that doesn't know about them won't erase the provenance. Split packages (multiple `pkgname` from one `pkgbase`) each get their own entry, all pointing at the same `pkgbuild_dir`.
- `"pacman"` — installed via pacman, not built through sysforge. Carries only `pkgver`, `pkgrel`, `epoch` parsed from `pacman -Q`; `pkgbase`, `pkgbuild_dir`, and `flags_string` are absent. Synthesised by `sync_with_installed()`.
- `"pgo_llvm_toolchain"` — LLVM toolchain packages built with profdata reuse: `makepkg_wrapper` injects `-fprofile-use=<saved-profdata>` when a compatible `clang.profdata` exists, otherwise prompts plain build / skip (default skip). See **PGO toolchain packages** below.

`BuildState.sync_with_installed(installed)` keeps the file in lockstep with `pacman -Q`: it adds a pacman-mode entry for every newly installed package and prunes entries for packages that are no longer installed. The prune pass also removes zombie entries left by pre-superset parser runs — e.g. legacy keys containing literal `$_pkgname` that can never match a `pacman -Q` name. `sysforge update` calls this at the start of every run and saves if anything changed.

`BuildState.reconcile_external_installs(external_names)` demotes a `source_built` entry back to a plain `pacman` marker when the package was reinstalled from the repo outside sysforge (e.g. `sudo pacman -S mesa` after `sysforge build mesa`). The version identity (`pkgver`/`pkgrel`/`epoch`/`pkgbase`) is kept; the source provenance (`pkgbuild_dir`, `flags_string`, `source`, `built_upstream_commit`, `toolchain_variant`, `reviewed_commit`, `origin_pkgbase`) is stripped. Demote (not delete) preserves the superset invariant; **stage-owned** entries (`owner_stage` set) are exempt. `external_names` is computed by `primitives/install_reconcile.external_install_targets` (the buildstate pacman-hook targets minus sysforge's own `pacman -U` self-install targets) and applied by `sysforge update`'s `_reconcile_external_demotions` before the superset sync. See `install_reconcile.py` below.

Read by `sysforge update` for version drift detection (every installed AUR package is iterated regardless of `build_mode`; source-built entries carry the prior `pkgver` for change-detection, pacman-mode entries are checked against PKGBUILD freshness) and for flag drift detection in Phase 4.3 (source-built entries only — including, via the build-state-wide fold, source-built entries outside the run's package walk; pacman-mode entries are silently skipped). Follows the same atomic write-then-rename pattern as `pipeline/state.py`. Records must carry `build_mode`; the previous compatibility fallback that treated missing `build_mode` as source-built was removed.

On the write path, after a successful build `makepkg_wrapper.py` derives `pkgver`/`pkgrel`/`epoch` from the produced `.pkg.tar.*` filenames rather than the static PKGBUILD parse. The static parser intentionally leaves shell parameter-expansion forms (e.g. `${_ver/[a-z]/.${_ver//[0-9.]/}}`) untouched so it never produces a misleading partial substitution, but a built package's filename always carries the fully resolved version. Falling back to filenames prevents source-built entries from storing literal `$...` strings that would mismatch every subsequent vercmp and cause the package to be flagged for rebuild on every `sysforge update` run.

**Build failures** live in a reserved top-level `[failures]` table (keyed by pkgbase), held apart from the per-package install mirror so `all_packages()` / `sync_with_installed()` stay a clean superset of `pacman -Q` (the `failures` key is popped into a private dict on load and re-serialized separately; a package literally named `failures` would collide but none exists in practice). Each entry carries `failed_at` (ISO 8601 UTC), `error` (the failure message tail — last ~6 lines / 600 chars), and optionally `pkgver`, `signature`, and `fix_cmd` (the latter two from `build_diag` postflight diagnosis when a known pattern matched). API: `record_failure(pkgbase, *, error, pkgver=None, signature=None, fix_cmd=None, failed_at=None)`, `clear_failure(pkgbase) -> bool`, `all_failures() -> {pkgbase: record}`. A successful `record()` calls `clear_failure(pkgbase)` so the failure list self-heals on the next good build. `sysforge update`'s build fan-out records failures via `_record_build_failure` (opening a fresh `BuildState` so loop-time success writes aren't clobbered, and pulling `signature`/`fix_cmd` from the exception's `.diagnosis`, attached by `makepkg_wrapper`). Surfaced by `sysforge state failed`.

Public helpers: `parse_pacman_version(ver_str)` splits a `[epoch:]pkgver-pkgrel` string into a `(epoch, pkgver, pkgrel)` tuple; used by `sync_with_installed()`.

### `install_reconcile.py`

The single home for the two pacman-hook sentinels under `/var/lib/sysforge/sentinels/` that drive **external-install demotion** (a `source_built` package reinstalled from the repo via `pacman -S` should stop being rebuilt from source). The `buildstate` sentinel is appended by the shipped `sysforge-buildstate.hook` (PostTransaction, `NeedsTargets`) and records every pacman transaction's target list; the `self-install` sentinel is appended by `record_self_install()` from the single `pacman -U` chokepoint (`pacman.batch_install_pkgs`) and records sysforge's own artifact installs. `external_install_targets(sentinel_dir)` returns `buildstate − self-install` — the packages a *user* installed, not sysforge — which `sysforge update` feeds to `BuildState.reconcile_external_installs`. The self-install marker is essential because `sysforge build` does not consume sentinels at end-of-run: without it, the next `update` would demote the package the user just built. All writes are best-effort (never fail a transaction); the event format (timestamp line, then pkgname lines, then a blank separator) is shared with `tools/pacman-hook-helper.sh` and parsed only here. The functions take a `sentinel_dir` so `update.py`'s `_SENTINEL_DIR` stays the one effective path and the one test patch-point.

### `pacman_hooks.py`

The single home for **installing/refreshing sysforge's libalpm hooks** (the producers of the `install_reconcile` sentinels above). The three shipped hooks + `pacman-hook-helper.sh` are normally installed by the PKGBUILD `package()` step, which leaves a source-checkout dev workflow (or an edited hook) with missing/stale copies that silently disable `update`'s reminder/auto-demote logic. `shipped_sources()` resolves the canonical content — repo checkout (`etc/pacman.d/hooks/` + `tools/pacman-hook-helper.sh`, two parents up from the module) preferred, else the wheel's bundled `sysforge/_data/` copy shipped via a `force-include` in `pyproject.toml`. `diff_status()` byte-compares each artifact against its install destination (`/usr/share/libalpm/hooks/*.hook`, `/usr/lib/sysforge/pacman-hook-helper.sh`) returning `ok`/`missing`/`stale` (pure read). `provision()` writes the missing/stale artifacts via `fs_provision._run_priv` (`install -Dm<mode>` from a temp file — no second sudo path, no dependence on root reading the checkout); a privileged failure raises `FsProvisionError`. Two consumers: `setup_cmd` (provision, after the IgnoreGroup step) and `doctor._collect_hook_findings` (read-only warnings on the `--pacman` axis). `tools/check_shipped.py::check_hooks` asserts `HOOK_NAMES` matches the shipped `.hook` files and that the `force-include` ships the hooks dir + helper.

### `artifacts.py`

Inventory primitive for **user-authored** system artifacts — shell scripts, systemd units,
pacman hooks — the personally-created files scattered across `~/scripts`,
`/etc/systemd/system/`, `/etc/pacman.d/hooks/`, etc. that `build_state.py`/`pacman_hooks.py`
never touch because they aren't package- or sysforge-owned. Three locations, three distinct
roles: `USER_DATA_DIR/artifacts/` (see `paths.py`'s fourth XDG root, `$XDG_DATA_HOME/sysforge`)
holds the **authoritative** content — irreplaceable, the one copy sysforge treats as truth;
`<state_dir>/artifacts.toml` (`ArtifactRegistry`, same state-dir chain as `build_state.py`:
explicit arg > `SYSFORGE_STATE_DIR` > `/var/lib/sysforge`) holds **metadata only, never
content** — regenerable by re-hashing, so the authoritative copy has exactly one home and the
two cannot silently diverge; the **live filesystem** is the deploy target, owned by the OS.
`ArtifactRegistry.load()` raises `ArtifactRegistryError` on a corrupt/unreadable
`artifacts.toml` rather than folding it into "empty" — this file is the sole record of managed
artifacts, and a subsequent `save()` writes the complete entry set, so silently returning `{}`
would let the next save discard every managed artifact with no diagnostic.

**Artifact classes** are an explicit registry field (`class = "script" | "systemd-unit" |
"pacman-hook"`, `CLASS_SCRIPT`/`CLASS_UNIT`/`CLASS_HOOK`) rather than inferred from the
destination path at use time — adding a class is a table entry, not a new branch in every code
path.

**Discovery (`scan()`)** walks config-declared roots (`roots_from_config()`, reading `[artifacts]
roots` from `sysforge.toml`, falling back to `DEFAULT_ROOTS` — `~/scripts`,
`/etc/systemd/system`, `/etc/pacman.d/hooks`) through a three-stage filter:

1. **Structural noise excluded by rule** — `.pacnew`/`.pacsave`/`.pacorig` (pacman conflict
   artifacts), `*~` (editor backups), `.#*` (editor lock files), any `.wants`/`.requires`
   subdirectory (systemd's `systemctl enable` symlink dirs — enablement *state*, not authored
   files; adopting one would yield a dead managed copy and a `remove` that fights `systemctl
   disable`), and **any symlinked entry** (`/etc/systemd/system` holds `systemctl enable`/alias
   links pointing at unit sources elsewhere — same enablement-vs-authored rationale; adopting one
   would copy the *target's* content into a managed regular file and a later deploy would clobber
   the link with that copy).
2. **Package-owned files excluded** via `pacman.owners_of()` — a batched `pacman -Qo` lookup
   with a tri-state contract distinct from same-module `owners_of_paths()`: a path mapped to a
   package name is owned, mapped to `None` is *definitively* unowned, and **absent from the dict
   entirely** means ownership could not be determined at all (the `pacman -Qo` call failed
   wholesale). `scan()` must not collapse absent into unowned — that would present system files
   as user-authored candidates — so it labels absent paths `owner = "unknown"` rather than
   `"you"`.
3. **sysforge's own hooks kept but labelled read-only** — `_sysforge_owned_paths()` derives the
   three `pacman-hook`-class exclusions from `pacman_hooks.HOOK_NAMES`/`HELPER_DEST` (no
   duplicated list, so a fourth shipped hook updates the guard automatically) and surfaces them
   as `owner = "sysforge"` candidates instead of hiding them.

**Status (`status_of()`) is computed, never stored** — the filesystem is the untrusted side; it
must be read, not remembered. It is a three-way comparison of `auth_hash` (the authoritative
copy's hash, stored in the registry), `deployed_hash` (what sysforge last wrote to `dest`, also
stored), and the live file's hash (read fresh every call via `hash_file()`, which folds
unreadable into `None` — a root-owned file we can't read is indistinguishable from absent for
status purposes). `deployed_hash` is what makes a difference *attributable*: with only
authoritative-vs-live there is one bit of information ("same or different") and no way to tell
which side moved. Five states result: `ok` (neither moved), `pending` (authoritative moved, live
didn't — the managed copy was edited but not yet deployed), `drifted` (live moved, authoritative
didn't — something changed the deployed file outside sysforge), `conflict` (both moved, or
nothing was ever deployed but `dest` already holds unrelated content), `missing` (no live file).
A later slice makes `deploy` **refuse** on `drifted`/`conflict` rather than overwrite — sound
only because these two states are distinguishable from each other and from `pending`.

**Unified view, not unified registry.** `unified_rows()` — backing `sysforge artifact list` — is
the join point for the CLI: it emits one row per registry entry (`status_of()` against each) plus
one row per sysforge-owned hook, but the sysforge-owned rows are rendered by *delegating* to
`pacman_hooks.diff_status()` (mapped onto the same status vocabulary via
`_HOOK_STATE_TO_STATUS`) rather than being entered into the registry. Folding them into the
registry would create a second authority over files `pacman_hooks.py` already governs, and would
expose registry-only verbs (`edit`/`remove`, later slices) on files whose removal breaks
`sysforge update`'s auto-demote path. Each artifact keeps exactly one authority; this is a
presentation-layer join.

**`script_root_on_path()`** is a tri-state PATH check (`True`/`False`/`None`) over the
`script`-class root: comparison is on `Path.resolve()` so `$HOME/scripts` vs. an absolute path
vs. a symlinked entry all match, but it returns `None` — abstains — under an escalated
(`SUDO_USER` set) invocation, where the process `PATH` is sudo's `secure_path` rather than the
user's PATH; warning off that would be a confident false positive. `ArtifactListVerb` surfaces a
`warn` only on a confirmed `False`.

Wired by `verbs/artifact.py` → `ArtifactListVerb` (`sysforge artifact list [--unmanaged]`,
`requires_sentinel = False`, no sentinel — read-only, all logic delegated to this module).
`--unmanaged` additionally prints `scan()` candidates not already in the registry and not
sysforge-owned.

**Adoption (`adopt(registry, src, cls=None)`)** copies a live file's bytes into
`registry.content_path(name)` — the source is read, never moved, so the live file is untouched and
adoption cannot itself cause drift. `cls` defaults to `class_for_path()` (inferred from which
config-declared root contains `src`); an unresolvable or invalid class raises `ArtifactError`. The
`CLASS_HOOK`-scoped sysforge-ownership guard from `scan()` step 3 is re-checked here too — adopting
a sysforge-owned hook by name is rejected with a pointer to `sysforge setup` / `doctor --pacman`,
mirroring the same one-authority rule `unified_rows()` enforces for display. **`deployed_hash` is
seeded equal to `auth_hash`** (both hashes of the just-copied bytes): at the moment of adoption the
live file *is* the last-deployed state by definition, so a fresh entry reads `ok` rather than a
false `pending`. A name already present in the registry raises rather than silently overwriting.

**Editing (`rehash(registry, name)`)** re-hashes the managed copy after `artifact edit` has handed
it to an external editor (`primitives/editor.py::resolve_editor()` / `run_tty_argv()`) and persists
the new `auth_hash`, leaving `deployed_hash` (and the live file) untouched — this is what makes a
plain edit surface as `STATUS_PENDING` (authoritative moved, live didn't) rather than mutating
anything outside the managed copy. `ArtifactEditVerb` refuses (exit 1, no editor launched) when
`name` isn't in the registry or no editor is resolvable, and reports the post-edit `status_of()`
result — with a `sysforge artifact deploy <name>` hint specifically on `STATUS_PENDING`, since that
is the only status a successful edit can produce.

Both are wired by `verbs/artifact.py` → `ArtifactAdoptVerb` (`sysforge artifact adopt <path>
[--class C]`) and `ArtifactEditVerb` (`sysforge artifact edit <name>`), both
`requires_sentinel = False` — they touch only `USER_DATA_DIR/artifacts/` and `artifacts.toml`,
never the live filesystem, so they carry none of the privileged/destructive surface `deploy`/
`remove` carry.

**Per-class deploy/remove contracts.** Three module-level tables/functions encode what "push to
the live system" and "remove from the live system" mean per `class`, so a class's behaviour is a
table entry, not a branch scattered across `deploy`/`remove`:

- **`_LIVE_MODE`** — the mode written at the live destination: `CLASS_SCRIPT` 0755 (executable,
  user-owned), `CLASS_UNIT`/`CLASS_HOOK` 0644 (data files root reads, doesn't execute directly).
- **`_PRIVILEGED_CLASSES`** (`CLASS_UNIT`, `CLASS_HOOK`) — destinations under root-owned system
  dirs. `write_live()` branches on membership: `CLASS_SCRIPT` writes directly (`mkdir` + `write_bytes`
  + `chmod`, no escalation — it's the user's own tree); a privileged class stages the content through
  a `NamedTemporaryFile` (mode 0600, owned by the invoking user — readable by root, not by other
  unprivileged users) and installs it via `run_privileged(["install", "-Dm<mode>", tmp, dest])`, then
  unlinks the temp file in a `finally`.
- **`post_deploy(art)`** — class post-write action. `CLASS_UNIT` runs `systemctl daemon-reload`
  (privileged) so systemd picks up a new/changed unit file immediately; the other two classes have
  no post-action (a hook is read fresh by pacman's own hook loader on the next transaction; a script
  needs nothing).
- **`unit_is_enabled(unit)`** — unprivileged, non-raising `systemctl is-enabled --quiet` probe;
  any error (including a missing `systemctl`) reads as "not enabled," which is the correct default
  for `pre_remove`'s purposes (nothing to stop).
- **`pre_remove(art)`** — class pre-unlink action. `CLASS_UNIT`, when currently enabled, runs
  `systemctl disable --now <unit>` (privileged) *before* the file is removed, so a running/enabled
  service doesn't survive its unit file's disappearance in a half-stopped state. `CLASS_HOOK` gets
  no systemd-equivalent quiesce step — the verb layer (`ArtifactRemoveVerb`) instead emits a warning
  that removing a hook changes what happens on the next pacman transaction, since there is nothing to
  "stop" about a hook.
- **`remove_live(art)`** — unlinks the live file, `run_privileged(["rm", "-f", dest])` for the two
  privileged classes, a direct `unlink(missing_ok=True)` for `CLASS_SCRIPT`.

**`deploy(registry, name, *, force=False, adopt_live=False) -> str`** pushes the managed copy to
`dest`. `force` and `adopt_live` are mutually exclusive (`ArtifactError` if both are set). On
`STATUS_OK` it is a no-op (nothing changed on either side); on `STATUS_PENDING`/`STATUS_MISSING` it
writes unconditionally — those states have exactly one side that could plausibly be wrong, and it
isn't the live file's own unrelated content. On `STATUS_DRIFTED`/`STATUS_CONFLICT` it **refuses**
(`ArtifactError`, no write) unless given an explicit resolution — **there is deliberately no
default**, because silently picking a side is data loss in the other direction:

- **`--force`** — managed copy wins; the live edit that caused the drift is discarded on write.
- **`--adopt-live`** — live file wins first: its bytes are read and written back into the managed
  copy (`registry.content_path(name)`), `rehash()` recomputes `auth_hash` from them, and *then* the
  (now-matching) content is written to `dest` and `post_deploy` runs — so the managed copy absorbs
  what the live file already had rather than sysforge overwriting a live edit it didn't author.
  Because it overwrites the authoritative copy, `--adopt-live` is **fenced** to the states where
  that is the intent: `drifted`/`conflict` (the drift it resolves) and `ok` (a harmless no-op). It
  **refuses** (`ArtifactError`, nothing written) on `STATUS_PENDING` — where it would silently
  discard an undeployed managed edit with older live content — and on `STATUS_MISSING` — where
  there is no live file to adopt (this also fences the read, so a missing live file never surfaces
  as an unguarded traceback).

A successful, non-no-op deploy re-hashes the written bytes and persists `auth_hash == deployed_hash`
(both the freshly-hashed content) plus `deployed_at = now`, so the artifact reads back `ok`
immediately after — `deployed_hash` is exactly "what sysforge last wrote," and a deploy is the only
thing that advances it.

**`remove(registry, name, *, purge=False, force=False) -> None`** runs `pre_remove` then
`remove_live`. It **refuses** on `STATUS_DRIFTED`/`STATUS_CONFLICT` unless `force`, symmetric with
`deploy`: the live file carries edits made outside sysforge that exist nowhere else, so unlinking
it (a privileged `rm` for units/hooks) would destroy them silently — `--force` proceeds anyway, and
`deploy --adopt-live` first is the escape hatch that saves the live version into the managed copy
before removal. `ok`/`pending`/`missing` remove without force — the live file is either reproducible
from the managed copy or already gone (an already-missing live file is a successful no-op unlink,
not an error). Without `purge`, the managed copy and registry row survive with
`deployed_hash`/`deployed_at` cleared back to `None` — removal from the live system is not the same
decision as discarding the content, so the artifact can be `deploy`ed again later without
re-adopting it. With `purge`, the managed copy's content file is also unlinked and the registry
entry dropped entirely — the artifact is fully forgotten.

Wired by `verbs/artifact.py` → `ArtifactDeployVerb` (`sysforge artifact deploy <name>|--all
[--force|--adopt-live]`) and `ArtifactRemoveVerb` (`sysforge artifact remove <name> [--purge]
[--force]`), both
`requires_sentinel = True` (they mutate the live filesystem) and both supply a `journal_target`
(the artifact name, or `"all"` for a batched deploy) so `journalctl SYSFORGE_TARGET=<name>` finds
them (§Standards row 20). `--all` deploys every registered artifact in one run, continuing past a
per-artifact failure (accumulates a failure count, nonzero exit if any failed, rather than aborting
the batch on the first refusal) rather than stopping the whole run on one drifted artifact. After the
loop, `ArtifactDeployVerb` emits the same PATH warning `artifact list` does — **once per run, not
per artifact**, and **only when a script actually landed** (`CLASS_SCRIPT` deployed this run **and**
`script_root_on_path()` is confirmed `False`, never on the `None` abstain) — the problem is concrete
at deploy time (something was just installed by name and it may not be runnable), so it earns a
narrower, deploy-specific warning rather than reusing `artifact list`'s unconditional check.
`ArtifactRemoveVerb` warns before removing a `CLASS_HOOK` artifact (the pre-remove hint noted above)
and reports `purge`d vs. plain removal in its confirmation line.

### `pkgbuild_review.py`

The PKGBUILD review gate. Before a package is built, compares the source clone's HEAD against the `reviewed_commit` recorded in `build_state.toml` (the clone HEAD at the last successful build — stamped sticky by `makepkg_wrapper`'s single `record()` site, so dep builds and pipeline stages are covered without caller threading) and, on a difference, shows the **full source-tree diff** — not just the PKGBUILD, so changes hiding in `.install` files, patches, or new sources are visible — and prompts: `[v]iew` (full patch through `pager.maybe_pager`) / `[a]ccept` / `[s]kip package` / `a[b]ort run`. The prompt reads a single keypress via `prompt.prompt_key` — no Enter needed. EOF/Ctrl-C at the prompt aborts (no answer is not consent). A package with no recorded `reviewed_commit`, or whose recorded sha vanished (purge + re-clone), is reviewed against git's empty tree — a full-content review. The comparison is commit-based (recorded → HEAD), deliberately not worktree-based: upstream changes arrive as commits via source sync, while uncommitted local edits are user-authored (the STATUS_DIVERGED case) and are not re-presented to their author. Auto-accept paths (logged, never prompt): non-interactive runs (stdin or stdout not a TTY), and callers passing `interactive=False` — `sysforge update`'s default mode. Owns the `[REVIEW]` tag. API: `head_commit(dir)`, `commit_exists(dir, sha)`, `review_target(pkgbase, dir, reviewed_commit, interactive=True) -> DECISION_*`, `review_deps(deps, interactive=True) -> DECISION_*`.

**Dependency gate (`review_deps`)** — the batched counterpart for AUR dependency PKGBUILDs built by `prepare_deps`. Takes `[(name, pkgbuild_dir, reviewed_commit), ...]`, runs the same HEAD-vs-reviewed-commit comparison per dep (empty-tree fallback included), and presents the changed ones as one summary block (short shas + `git diff --shortstat`) with a single prompt: `[v]iew diffs` (each dep's full patch through the pager) / `[a]ccept all` / `a[b]ort run`. Deliberately **no per-dep skip** — dropping a dependency breaks the package that needs it, so the decision is all-or-nothing. Auto paths mirror `review_target`: `interactive=False` emits one batched `auto-accepted N dependency change(s)` notice; non-TTY runs auto-accept with a warning. Returns `DECISION_CLEAN` when nothing changed.

**One home for the gate:** `build_core.build_and_install(review="prompt"|"auto"|"off")` runs it for every target *before* dep prep and the build loop — a skip never installs that package's makedeps; an abort returns `BuildOutcome(aborted=True)` with nothing built or installed (callers exit cleanly, no exception, so the verb sentinel clears normally). `build` defaults to `"prompt"` (the deliberate, targeted verb); `update` defaults to `"auto"` — changes are auto-accepted with a per-package `[REVIEW] auto-accepted` notice so a batch update stays unattended — and `--review` opts update back into prompting. For the `build` path (`sync_source=True`, where the wrapper's inline sync would otherwise run after the gate) the targets are pre-synced through `source_sync.get_scheduler()` first — the wrapper's later request dedups against the scheduler cache, and the gate sees the post-fetch HEAD. Disabled entirely (`"off"`) via `--no-review` (both verbs) or `[build] review = false` in packages.toml.

**Config-backed `build` flags:** `--abi-check`, `--cache-report`, and `--persist-log` each fall back to a packages.toml `[build]` default (`abi_check`, `cache_report`, `persist_log`) when the CLI flag isn't passed, resolved via `config.resolve_flag_default` (same config→flag precedence pattern as `[build] review`) — the CLI flag always wins when explicitly set.

Dependencies are covered too: `prepare_deps` receives the same `review` mode and runs `review_deps` over the resolved AUR deps (looking up each dep's `reviewed_commit` in build_state) between resolution and `build_resolved_deps`; an abort there returns `False` from `prepare_deps`, which `build_and_install` surfaces as the same clean `BuildOutcome(aborted=True)` return.

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

Read-only pacman / system-integrity checks for `doctor --pacman`. Public API: `collect_system_findings() -> list[Finding]`. Internal checks: `_check_db_consistency` (`pacman -Dk`), `_check_stale_lock` (`/var/lib/pacman/db.lck`), `_check_pacfiles` (`*.pacnew`/`*.pacsave` under `_ETC`, split by whether the base file still exists: those with a live base are `pacnew_unmerged` and advise `pacdiff`; those whose base is gone are `pacsave_orphaned` and advise manual removal, since `pacdiff` no-ops on them), `_check_orphans` (`pacman -Qtdq`). Strictly local-database — never issues a sync (`-Sy`), so a `doctor` run cannot change the installed package set. Module-level `_PACMAN_DB_LOCK` / `_ETC` are repointable for tests.

### `state_probe.py`

Read-only inspection of sysforge's own persisted state for `doctor --state`. Public API: `collect_state_findings(state_dir=None, installed=None) -> list[Finding]`. Surfaces `BuildState.all_failures()` (each a `warn` carrying the stored `build_diag` signature/`fix_cmd`), an interrupted stage sentinel via `StageSentinel.get_active()` (an `error` with the recorded `recovery_cmd`), and build-state drift computed from `BuildState.all_packages()` vs the live pacman db (an `info` for zombie entries). Never calls `BuildState.save()` or the recovering `stage_sentinel.check_and_recover_stale_sentinel` — drift is computed without mutating the in-memory state. Source-sync `STATUS_*` is omitted (the scheduler cache is per-process; a standalone `doctor` has none).

### `init_notice.py`

The one home for the **first-install bootstrap reminder (F1)**. A fresh package install drops an empty marker `<state_dir>/.sysforge-init-notice` from the PKGBUILD `post_install` scriptlet (`sysforge.install`, wired via `install=` in both PKGBUILDs, gated by `check_shipped`'s install-scriptlet existence check + the byte-identical `pkgbuild_parity` of the `install=` key). Public API: `maybe_emit_init_notice(state_dir=None) -> "advised"|"cleared"|None` and `notice_path(state_dir=None)`. Called once per invocation from `cli.main()` immediately after the stale-sentinel gate (skipped for `completions` so machine-readable output stays clean). State-dir resolution matches `StageSentinel`/`PipelineState` (explicit > `SYSFORGE_STATE_DIR` > `/var/lib/sysforge`). Behaviour: absent marker → no-op; present and the `reconfigure`/`hardware` stages (read via `PipelineState.stage_status`) not both `done` → advisory naming the still-pending stages (`sysforge run <stage>`), marker persists so the advice repeats until setup completes or the operator deletes the file to dismiss; present and both `done` → silently unlink the marker. Pure UX — never blocks a command, never raises (defensive catch-all logs at debug). sysforge only ever *reads/deletes* the marker (creation is the scriptlet's job), so deletion is an unambiguous dismissal that is never resurrected.

### `runtime_probe.py`

Read-only services / runtime-health checks for `doctor --services`. Public API: `collect_runtime_findings() -> list[Finding]`. `_check_failed_units` (`systemctl --failed` → one `error` per unit) and `_check_missing_firmware` (best-effort `journalctl -k -b` parse for "Direct firmware load … failed" → one deduped `warn`). DKMS health is checked in the `boot` axis (running kernel), not here, to avoid double-reporting. Every external command is guarded so an absent tool or permission error yields no findings (`run_axes` isolates exceptions as a backstop).

### `audio_probe.py`

Read-only audio / sound-stack checks for `doctor --audio`. Public API: `collect_audio_findings() -> list[Finding]`. `_check_failed_audio_units` (`systemctl --user --failed`, filtered to the PipeWire/WirePlumber unit family via `_AUDIO_UNIT_RE` → one `error` per failed unit) and `_check_audio_sinks` (`pactl list short sinks` → one `warn` when only the dummy `auto_null` device remains, the "audio device vanished" symptom). The sound stack is **user-scoped**: when `doctor` runs under `sudo` there is no reachable session bus, so both commands exit nonzero and the probe degrades to **no findings** rather than reporting a phantom "no audio device" — false-positive-averse by design (a missed finding is recoverable, a false alarm is not). Input sources are deliberately **not** flagged (a machine with no microphone is normal). Mutates nothing — never restarts a unit. Every external command is guarded (absent tool → no findings; `run_axes` isolates exceptions as a backstop).

### `network_probe.py`

Read-only network / connectivity checks for `doctor --network`. Public API: `collect_network_findings() -> list[Finding]`. **No active network calls** — it never performs a DNS lookup or a ping (those would hang or flap with real connectivity); it inspects local routing/manager/DNS *configuration* only. Three checks: `_check_default_route` (`ip route show default` empty → `warn` `no_default_route`); `_check_manager_conflict` (more than one mutually-exclusive connection manager in `_CONNECTION_MANAGERS` — NetworkManager/systemd-networkd/dhcpcd/connman/netctl — reports `enabled` via `systemctl is-enabled` → `warn` `network_manager_conflict`, the "two managers fighting over the interface" class); and `_check_dns_conflict` (`systemd-resolved` active but `/etc/resolv.conf` is a static file rather than the resolved stub symlink → `warn` `dns_resolved_unmanaged`). The manager-conflict check is the unique value of this axis: the `services` axis only reports units already in the `failed` state, but a manager conflict typically presents as *flapping*, not a hard failure. False-positive-averse: an absent tool / nonzero exit / managed-symlink / missing resolv.conf all degrade to **no findings**, and "no connection manager enabled at all" is deliberately **not** flagged (a host may use a manager we don't enumerate). Mutates nothing — never enables/restarts a unit or rewrites resolv.conf.

### `os_release.py`

The **single home for distro identity** (`os-release(5)`, Standards row 23), consumed by the `doctor --distro` axis. Public API: `DistroIdentity`, `read_os_release(paths=None) -> dict[str, str]`, `identify(paths=None) -> DistroIdentity`, `collect_distro_findings(*, explicit=False, paths=None) -> list[Finding]`. Pure file reads — no subprocess, no network. Parses `/etc/os-release` then `/usr/lib/os-release` (the local-admin copy wins; only the vendor copy is guaranteed present) as shell-style `KEY=value` with `#` comments skipped and one layer of matching quotes stripped; `ID` defaults to `linux` per the spec and `ID_LIKE` becomes the space-separated parent tuple. A file that exists but parses to **nothing** is treated as unreadable, so a truncated `os-release` cannot masquerade as `ID=linux` via that default — `DistroIdentity.known` is the caller-visible distinction between "no os-release" and "unfamiliar distro". `is_arch_derived` is `ID == arch` or `arch ∈ ID_LIKE`; derivatives are **in scope** because sysforge's packaging invariants forbid the repo-name and toolchain-default assumptions that would break them. `collect_distro_findings` reports a support tier, not a health defect: silent on plain Arch unless `explicit` (the user passed `--distro`, so the identity line is the answer to the question), `info` for a derivative naming what is and is not validated there, `warn` for a distro declaring no Arch base and for an unreadable `os-release`. **No finding is ever an `error`** — a support tier must not change doctor's exit code. Identity is never inferred from `pacman.conf` section names, `/etc/arch-release`, `/etc/lsb-release`, or a hostname; the `check_standards` `distro_portability` group fails the build on any such read outside this module. sysforge has **no distro-conditional behaviour** — this module exists to *report* identity, not to branch on it.

### `pkgfiles_probe.py`

Read-only package-file integrity checks for the opt-in `doctor --integrity` axis. Public API: `collect_integrity_findings(packages=None) -> list[Finding]`. Shells `pacman -Qkk [pkg…]` through a bare read-only subprocess seam (`_run`, `LC_ALL=C`, `check=False`) — the unprivileged local query, not the privilege seam — and parses both stdout and stderr discrepancy lines into one Finding per path, severity = worst discrepancy. It mirrors pacman's own classification: `backup file:`-prefixed edits (config files in a package's `backup` array, excluded from pacman's altered count) are `info` (`integrity_backup_edited`); a non-backup missing file is `error` (`integrity_missing`); other non-backup drift (size/hash/type/mode/uid/gid) is `warn` (`integrity_altered`); a non-backup modification-time-only deviation is downgraded to `info` (a benign identical rewrite). Run unprivileged (the default), pacman cannot read root-only files and reports access-error reasons (`failed to calculate SHA256 checksum`, `Permission denied`) instead of drift; these are stripped from each path's reason set before classification, and a path left with no other reason is access-limited, not drift — it is counted and rolled into a single `integrity_partial_coverage` `info` advisory (`"N package-owned file(s) were unreadable without root; coverage is partial"`, remediation `sudo sysforge doctor --integrity`) rather than emitted per-path; a path that also shows genuine drift (e.g. `Size mismatch` from a successful stat) keeps its real severity with the access-error reason dropped from the message. The per-package summary line (`N total files, M altered file`) carries no path and is ignored. Exit code 1 (mismatches found) is the normal signal, not an error. Advisory only — findings carry `pacman -S <pkg>` remediation text but no `fix_cmd`; the axis never restores a file. The **deliberate complement** to `artifacts.py`: that inventory manages only user-authored content and excludes package-owned files at discovery, so the two populations are disjoint and jointly exhaustive. Opt-in (`integrity ∈ doctor._OPT_IN_AXES`) because a whole-system `-Qkk` re-hashes every packaged file; `doctor --integrity PKG…` scopes it.

### `rust_probe.py`

Read-only Rust-toolchain **provenance** checks for the opt-in `doctor --rust` axis. Public API: `collect_rust_findings(config, packages=None) -> list[Finding]`, composed of `collect_active_findings()` (the effective toolchain) plus `collect_pin_findings(config, packages)` (per-target `rust-toolchain.toml` pins). All external commands go through a bare read-only subprocess seam (`_run` → `subprocess.run(..., check=False)`, guarding `FileNotFoundError`/`OSError` → `None`); tests monkeypatch `rust_probe._run`. **Advisory only — every finding is `info` or `warn`, never `error`**, so the axis can never drive a non-zero exit or gate a build; it mutates nothing and never rewrites a pin. Three concerns are otherwise invisible: (1) the *effective* toolchain — `_which("cargo")`/`rustc` is resolved to its owning package via `_owner_pkg` (`pacman -Qo`); a `rustup` owner reports `rustup show active-toolchain` (`rust-active`, `info`), any other owner reports the distro `rust` package version, and no `cargo`/`rustc` at all is a single `rust-none` `info` (clean). An **unowned** binary (`_owner_pkg` → `None`, the upstream shell installer rather than the `rustup` package) is disambiguated by `_is_rustup_layout`, which tests the resolved path against `RUSTUP_HOME`, `CARGO_HOME`, then `~/.cargo`: inside a rustup tree it takes the rustup branch and reports `rustup (user-local)` (the non-`stable` warning applies to it too), outside one it falls through to the distro-package branch as before; (2) a **non-`stable` active default** — `_channel_of` classifies the active line's channel and a `nightly`/`beta`/numeric-pinned default fires `rust-nightly-default` (`warn`, remediation `rustup default stable`), since every Rust build then silently uses it; (3) a tree-level **`rust-toolchain.toml` pin** — for each named package target, `config.find_pkgbuild` resolves the source dir, `_pin_for_dir` reads the `[toolchain] channel` (via `tomllib`; a present-but-unreadable file raises → `rust-pin-unreadable` `warn` naming the file), and `_toolchain_installed` (`rustup toolchain list`) decides between `rust-pin` (`info`, installed) and `rust-pin-missing` (`warn`, uninstalled → rustup would fetch it mid-build, remediation `rustup toolchain install <channel>`). Opt-in (`rust ∈ doctor._OPT_IN_AXES`): the pin checks only matter when the user names package targets, and the axis is a build-time provenance report rather than a system-health defect. Distinct from `toolchain_preflight`/`llvm_state`, which cover the **C/LLVM** toolchain; this is the Rust analog.

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
- `find_fallback_kernels(exclude_pkg=None)` / `verify_boot_artifacts(pkgname, bootloader)` / `check_dkms_for_kernel(kver)` (a module counts as present when `dkms status` reports it `installed`, **or** merely `built` while its `.ko` is actually in `<kver>`'s `updates/dkms` tree — newer dkms can leave a loaded, working module at `built`, so trusting the status word alone false-flags it) / `list_dkms_modules()` / `check_mkinitcpio_hooks(topology)` / `check_boot_mount_space(min_mb=200)` — the Gate 1/Gate 3 fact-gatherers (fallback presence, post-install vmlinuz+initramfs+boot-entry, DKMS rebuild coverage, mkinitcpio HOOKS vs topology, `/boot` headroom).

- `diff_requested_kconfig(requested, resolved)` / `diff_kconfig(old, new)` — the two kconfig comparison axes. The first is requested-vs-resolved and iterates only sysforge's own intent, normalising an absent option to `n` (correct kernel semantics for "did my merge survive"). The second is build-to-build, walks the union of both key sets, and classifies `added`/`removed`/`changed` *without* that normalisation — on this axis "the symbol did not exist in that kernel" and "the symbol existed and was off" are different facts. Kept as siblings rather than one parameterised function because the normalisation difference is a semantic split, not a flag.

The primitive must not import the pipeline layer; the kernel stage owns the abort/warn decisions.

### `kconfig_history.py`

The archive of recent resolved kernel `.config` files behind the kernel stage's build-to-build
kconfig diff (§Pipeline layer, Kernel kconfig blocks). Nothing else in sysforge retains a resolved
`.config` once the build tree is cleaned, so this module is its only home.

Layout: `<state_dir>/kconfig-history/<pkgname>-<release>.config.gz`, newest `KEEP` (5) per pkgname,
pruned on every write — after the write, so a failed write never costs an archive it would have
replaced. A resolved config gzips to a few tens of KiB, keeping the whole archive in the low
hundreds of KiB; small enough that there is deliberately no enable switch. Both `pkgname` and
`release` reach the filesystem here, so both are constrained to a path-safe charset rather than
trusted.

Public API: `history_dir(state_dir)`, `archive_path(state_dir, pkgname, release)`,
`archive(state_dir, pkgname, release, config_path, keep=KEEP) -> Path | None`,
`previous(state_dir, pkgname, *, exclude_release=None) -> tuple[str, dict] | None`. Every function
is best-effort — a full disk, an unwritable state dir, or a corrupt archive yields `None` or is
stepped over, never raised. `previous` returning `None` on a first build is a real answer, not an
error: there is nothing to compare against, and the summary says so.

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
build_mode = "source_built"   # legacy "patched_pkgbuild" read-accepted, warns, gone in 4.0.0

[profiles.kernel]
extends = "bare"
build_mode = "kernel"
batch = true
makepkg_flags = ["--noconfirm", "--syncdeps", "-f", "-c"]
```

### `build_mode` vocabulary

`build_mode` is set in two layers and `profile.py` carries the single documented
enumeration of every token (next to `_OPTIMIZED_BUILD_MODES`). The profile layer
(`profiles.toml`, user-set) uses `source_built` | `pgo_llvm_toolchain` | `kernel`
(omit for a standard build); the build_state layer (`build_state.toml`, stamped at
build time) uses `source_built` | `pacman` | `kernel` plus the optimization modes
(`pgo_mesa`/`pgo`/`autofdo_kernel`/`propeller_kernel`/`bolt_llvm`). The two layers
share one value space: `source_built` means "a plain from-source build" in both —
the profile layer extracts the PKGBUILD's embedded profile, build_state stamps it
as the rebuild-on-update marker.

The profile layer normalizes its own legacy token **on read only** (never written
into new data): `patched_pkgbuild → source_built` (`profile.normalize_build_mode`,
applied at the `get_build_mode` read chokepoint). That alias is a registered
`compat` deprecation as of 2.6.1-STD3 — reading it warns once per run and it is
**removed in 4.0.0** (standards row 24; a `compat` removal may only land on an
`X.0.0`, and 3.0.0 ships with the surface live). The build_state layer's
equivalent read alias, `profiled → source_built` (formerly `BuildState.__init__`),
was removed in 3.0.0 — a pre-rename `build_state.toml` entry now reads `profiled`
verbatim until `sysforge state repair` rewrites it. The "does this mode extract
the embedded PKGBUILD profile?" gate has one home —
`profile.build_mode_uses_extracted_profile` (`source_built`/`kernel`, legacy
profile-layer token accepted) — consumed by `flag_drift` and `makepkg_wrapper`;
don't re-spell the membership inline.

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

### Build throttling

Build CPU/IO/memory throttling has **one home**: `primitives/build_throttle.py`. Five knobs — `nice`, `ionice`, `cpu_quota`, `jobs`, `mem_limit` — keep packages from saturating the machine. Each is a global default in `sysforge.toml [build]` (see §Config Layer) and a per-profile override: all are in `profile.SYSFORGE_KEYS`, so a profile may carry them but they are **never** written to the conf or env. `resolve_throttle(resolved_profile, config, override=None)` resolves them — a key present on the profile wins over the global default, an absent key falls back to it. The resolver never raises; every malformed value (bad niceness range, non-`N%` quota, junk size, etc.) is dropped with a warning so a typo never fails a build.

`cpu_quota` accepts either an absolute `"N%"` (100% = one core) **or** a decimal fraction of the host's total cores (`0.5` on a 16-core box → `800%`), translated against `os.cpu_count()` at resolution — the same config stays portable across machines. Only a value carrying a decimal point is read as a fraction; a bare integer without `%` stays an error, being too ambiguous against a percent. Both forms converge on a single resolved percentage, which is checked against `cpu_count*100`: a quota above the host's core count (a typo, or a config copied from a bigger box) is **kept but warned** — systemd's effective cap does the harmless clamping, so the value is honoured while the user still gets a signal (2.3.0-F7).

Two **run-scoped overrides** short-circuit that resolution, set once at CLI startup from the global flags (`cli._resolve_throttle_override` → `set_run_override`, mirroring `log.set_color_mode`) and read by `resolve_throttle` when no explicit `override` is passed — so `--no-throttle`/`--turbo` stay routed through this one home rather than threading a parameter through every makepkg call site:

- `--no-throttle` → `"bypass"`: returns a no-op throttle, ignoring config and profile.
- `--turbo` → `"boost"`: constructs `BuildThrottle(nice=_BOOST_NICE, ionice="best-effort")` directly, bypassing the `0..19` niceness clamp because a boost is an explicit request for *higher* than default priority (no CPU ceiling or job cap). `--turbo` is the stronger request and wins over `--no-throttle`. Lowering niceness may need privilege; `wrapper_argv`'s best-effort `nice` front-end simply runs at the current priority if the kernel refuses.

Two delivery channels, by mechanism:

- **Invocation wrapper** (`nice`/`ionice`/`cpu_quota`/`mem_limit`) — `wrapper_argv(throttle)` builds an argv prefix prepended to the `makepkg` command at the single subprocess chokepoint (`makepkg_invoke.invoke_makepkg`, where `cmd` is assembled). A transient `systemd-run --scope --user` is opened whenever **either** a `cpu_quota` or a `mem_limit` is set (2.3.0-F9), carrying `-p CPUQuota=N%` and/or `-p MemoryMax=<bytes>` as configured, with `nice`/`ionice` folded in front. The scope is the primary tier for both ceilings because a cgroup `CPUQuota`/`MemoryMax` is kernel-enforced hierarchically over makepkg's whole fork tree, whereas an `RLIMIT_AS` preexec leaks across the fork tree and is escapable. The scope keeps the controlling TTY, so the interactive path still gets prompts. Each tool is guarded by `shutil.which` — a missing `systemd-run` drops the hard cap (a `cpu_quota` downgrades to soft nice/ionice with a warning; a `mem_limit` falls to the `RLIMIT_AS` preexec below). Throttling is best-effort and **must never fail a build**.
- **MAKEFLAGS** (`jobs`) — `apply_jobs_to_makeflags` rewrites the `-jN` token in the `MAKEFLAGS` value at conf emit (`emit_makepkg_conf(jobs=…)`, threaded from `_run_build`), normalising to short `-jN`; appends when absent (make honours the last `-j`, so a `-j$(nproc)` baseline is still capped).
- **Child preexec / cgroup** (`mem_limit`) — a per-build memory ceiling with a **dual mechanism** so the cap is enforced by exactly one path. `_coerce_mem_limit` parses a byte count or binary-suffixed size (`"24G"`) to bytes. `_scope_owns_mem_cap(throttle)` decides which mechanism applies — true iff `mem_limit` is set **and** `systemd-run` is available, the exact condition under which `wrapper_argv` emits a `MemoryMax` scope. When the scope owns the cap, `resolve_child_mem_cap` returns `None` (an `RLIMIT_AS` set in the systemd-run *client*'s preexec would never reach the scoped payload — a child of PID 1 — so applying it would be silently ineffective *and* risk double-counting). Otherwise — the non-systemd host — `resolve_child_mem_cap(throttle)` returns those bytes and `resource_guard.make_child_preexec(cap)` (the shared `preexec_fn`, replacing the three raw `lift_for_child` sites) clamps `RLIMIT_AS` in the makepkg child. Keying on scope emission rather than on `cpu_quota` (2.3.0-F9) also closes a prior gap: a `cpu_quota` set on a host **without** `systemd-run` no longer silently drops the memory cap. The clamp is best-effort (never above the current hard limit, never raising into the child).

Don't add a parallel `nice`/`systemd-run`/`-j`/`RLIMIT_AS` path elsewhere.

### `[package_compiler_overrides]`

An auto-managed table, keyed by `pkgbase`, that records a compiler/linker swap
recovered from an interactive build-failure (see pipeline-layer → Makepkg
Wrapper → Interactive recovery menu). Each row is an inline table:

```toml
[package_compiler_overrides]
some-pkgbase = { cc = "clang", cxx = "clang++", ld = "lld" }
```

**Applied last** in `resolve_profile` — after `merge_extends` and
`_expand_toolchain` — so it wins over whatever the matched profile (and its
`toolchain` expansion) resolved. The single read home is `resolve_profile`
(via `_apply_package_compiler_override`, keyed on the package's `pkgbase`,
falling back to `pkgname` only when no `pkgbase` field is present). `cc`/`cxx`
set `CC`/`CXX` directly; `ld` is **folded into `LDFLAGS`** as a `-fuse-ld=<ld>`
token (replacing any existing `-fuse-ld=` token) rather than carried as a
standalone key — linker selection is always conf-delivered through LDFLAGS, so
this keeps the override on the same delivery channel as a hand-written
profile.

The single write home is `profile_writer.write_package_compiler_override`
(line-level, comment-preserving — it never round-trips the whole document
through a TOML emitter, mirroring `packages_cmd._rewrite_packages_toml`). The
sole caller is the makepkg wrapper, persisting a successful recovery-menu
compiler swap; don't add a second writer for this table or write it from
anywhere else.

### Flag guards

`emit_makepkg_conf` runs a series of guards after profile overrides are applied but before the conf is written. Each guard detects and reconciles toolchain incompatibilities, logging at `[WARN][CONF]` (the conf module narrates its own flag adjustments; the underlying transforms stay pure in `makepkg_flags`). Guards run in this order:

1. **Linker guard** — detects the effective linker from `-fuse-ld=X` in LDFLAGS (default: `ld`/bfd). Strips lld-only flags (`--icf=*`) when the effective linker is not lld.

2. **RUSTFLAGS linker reconciliation** — if RUSTFLAGS declares `-C link-arg=-fuse-ld=X` with a different linker than LDFLAGS, overrides it to match. Handles both spaced (`-C link-arg=...`) and compact (`-Clink-arg=...`) forms. Prevents LTO link failures from mismatched linkers (e.g. mold cannot process LLVM bitcode produced with lld).

3. **GCC thin-LTO rewrite** — `-flto=thin` is clang-only. When GCC is in effect, rewrites `-flto=thin` → `-flto` in LTOFLAGS, CFLAGS, CXXFLAGS, and LDFLAGS. Falls back to system conf values when the profile doesn't override a key.

4. **GCC + lld LTO disabling** — GCC LTO produces `.gnu.lto_*` bitcode that only GNU ld/gold can process; lld cannot read it. When GCC is in effect and the effective linker is lld, LTO is disabled entirely: LTOFLAGS cleared, `-flto*` stripped from flag keys, and `lto` flipped to `!lto` in OPTIONS (prevents makepkg's `${LTOFLAGS:--flto}` fallback).

5. **Full LTO stripping** (PGO only) — strips `-flto`/`-flto=full` from CFLAGS/CXXFLAGS/LDFLAGS and clears LTOFLAGS during PGO passes.

6. **lib32 march scrub** — when `invoke_makepkg` detects a `lib32-*` build (`pkgbuild_path.parent.name.startswith("lib32-")`), `emit_makepkg_conf` strips host-CPU-specific or 64-bit-only `-march=` tokens from CFLAGS and CXXFLAGS in both profile overrides and system-conf passthrough. Stripped values: `-march=native` (resolves to the host's amd64 microarch — `znver3` on Zen 3), `-march=x86-64`, `-march=x86-64-v2`, `-march=x86-64-v3`, `-march=x86-64-v4` (microarch levels defined only for 64-bit code). Other `-march=` values (e.g. `-march=i686`) and all non-`-march` flags are preserved. Without this guard a `[profiles.bare]` lib32-* build inherits the system conf's `-march=native` unchanged, and multilib GCC then refuses the compile with a confusing "unrecognized target arch" error rather than a clear "host flag stripped for lib32" log line.

7. **lib32 PGO scrub** — for a `lib32-*` build, `emit_makepkg_conf` strips PGO profile flags (`-fprofile-use`/`-fprofile-instr-use`/`-fprofile-generate`/`-fprofile-instr-generate`, via `makepkg_flags._strip_pgo_flags`) from CFLAGS/CXXFLAGS/LDFLAGS. This runs *after* the `compiler_flags_extra` injection, so it catches the toolchain stage's injected `-fprofile-use=<store>/clang.profdata` too. The profile is trained on the x86_64 clang self-build and is discarded by an i686 (`-m32`) build (clang emits `-Wbackend-plugin "count discarded"`), so it adds nothing and must not reach the lib32 build. See pipeline-layer → *lib32 is not toolchain-managed* for why lib32 isn't built by the toolchain stage at all by default.

8. **PKGBUILD `options=()` opt-outs** — `emit_makepkg_conf` honors the parsed `globals["options"]` array (via `pkgbuild_meta.options_list_disabled`, which applies makepkg's later-wins override semantics so `options=('!lto' 'lto')` leaves `lto` enabled). Two toggles matter to the flag layer (F9):
   - **`!lto`** — the author declared LTO breaks this package (the cosmic-edit/onig mold-failure class). makepkg's own `!lto` only suppresses *its* LTOFLAGS injection; profile-baked `-flto` in CFLAGS/CXXFLAGS/LDFLAGS still reaches the compiler. So `emit_makepkg_conf` strips **every** `-flto` variant — including clang-PGO-friendly `-flto=thin` — via `makepkg_flags._strip_all_lto` (distinct from `_strip_full_lto`, which preserves thin for the PGO passes) and clears LTOFLAGS.
   - **`!buildflags`** — makepkg discards CFLAGS/CXXFLAGS/CPPFLAGS/LDFLAGS from the conf entirely, so the resolved profile flags never reach the build. `flag_drift.resolve_flag_drift` short-circuits to `STATUS_BUILDFLAGS_IGNORED` (not `STATUS_DRIFTED`), so `update`'s Phase 4.3 never false-triggers a rebuild when those flags change — a change that cannot affect the built package.

Guards 3–4 fire when any of the following is true:
- **Profile CC is GCC** — `cc_override` (CLI `--cc`) > `resolved_profile["CC"]` resolves to a non-`clang` compiler.
- **PKGBUILD hardcodes GCC (proactive)** — `pkgbuild_meta.has_hardcoded_gcc()` statically scans every PKGBUILD(5) build-time function — `prepare()`, `build()`, `check()`, `package()`, and any `package_<pkgname>()` split-package variant — for direct `gcc`/`g++` invocations, `ccache gcc`, or `CC=gcc`/`CXX=g++` assignments. Quoted forms (`export CC='gcc -m32'`, `CXX="g++"`) are handled. `verify()` is excluded — it authenticates sources, never compiles. Conservative: ignores `$CC`/`${CXX}` references, `-lgcc` library references, and comments. False is not authoritative — a Makefile checked out in `src/` may still hardcode `g++`.
- **`lib32-*` package (proactive)** — Arch's multilib has no `lib32-clang`; every `lib32-*` package compiles with 32-bit GCC by construction. `invoke_makepkg` triggers the guard whenever `pkgbuild_path.parent.name` starts with `lib32-`, even when `has_hardcoded_gcc()` returns False. The directory name (rather than parsed `pkgname`) is used because real-world `lib32-*` PKGBUILDs interpolate (`pkgname=lib32-$_basename`), which the static parser does not expand.
- **Reactive GCC fallback (post-failure retry)** — set when the previous invocation of makepkg failed with a clang-flag-rejected-by-GCC error and `_run_build` is re-entering the conf emit path. See [Toolchain-mismatch auto-retry](#toolchain-mismatch-auto-retry).

The `[WARN][FLAG]` rewrite log records which trigger fired so the cause is visible in the per-package log. The effective linker is determined by guard 1 and shared with subsequent guards.

### build()-hardcoded linker reconciliation

The conf layer writes linker flags into the generated `makepkg.conf` (guard 1 above), but it cannot reach linker flags the PKGBUILD **re-appends inside `build()`** — e.g. `RUSTFLAGS+=" -C link-arg=-fuse-ld=mold"`. These `build()`-level assignments win over what the conf layer set, silently overriding the effective linker. The patch layer closes this gap.

- **Detection** — `pkgbuild_meta.hardcoded_build_linker(parsed)` scans the parsed `build()` function body for `-fuse-ld=<name>` tokens and returns the hardcoded linker name, or `None` if none is present. Linker only — compiler assignments stay with `has_hardcoded_gcc`.
- **Effective-linker resolution** — `makepkg_flags.resolve_effective_linker(*, ld_override, profile_ldflags, system_ldflags)` is the shared definition of the effective linker consulted by **both** the conf layer (guard 1) and the patch layer. `ld_override` (CLI `--ld`) wins; then the first `-fuse-ld=` token in `profile_ldflags`, then `system_ldflags`; falls back to `"ld"` (bfd).
- **Rewrite** — `pkgbuild_patcher.patch_build_linker(path, target_linker)` rewrites every `-fuse-ld=<old>` token in the PKGBUILD's `build()` body to `-fuse-ld=<target_linker>`. Returns `{"old": …, "new": …, "count": …}` on success, `None` on no-op. Validated by the existing `validate_patched_pkgbuild` (G1 identity/deps unchanged, G2 managed `-D` — no new validator needed).
- **Wiring** — `makepkg_wrapper._maybe_patch_build_linker(path, pkgmeta, resolved_profile, ld_override)` is the sole call-site. It calls `hardcoded_build_linker`, then `resolve_effective_linker`, and only rewrites when `hardcoded != effective`. Returns the patch dict or `None`.

**Conf-vs-patch layer boundary:** the conf layer owns env flags it writes into `makepkg.conf`; the patch layer owns linker flags the PKGBUILD re-appends in `build()` — the case the conf layer structurally cannot reach (shell `+=` assignments inside a function body run after makepkg sources the conf).

Real-world trigger: `xdg-desktop-portal-cosmic-git` ships `RUSTFLAGS+=" -C link-arg=-fuse-ld=mold"` inside `build()` alongside a `# use mold ...` comment. The comment line carries no `-fuse-ld=` token so it is left intact; only the active flag line is rewritten.

---

## Makepkg Wrapper

### Environment isolation

SysForge treats the calling shell environment as untrusted for build tool vars. All keys in the `makepkg` and `toolchain` conf types (`CC`, `CXX`, `CFLAGS`, `CXXFLAGS`, `LDFLAGS`, `MAKEFLAGS`, etc.) are stripped from the inherited shell env before makepkg is invoked. The temp conf is the sole authority — shell vars set by `.zshrc`, `.bashrc`, or upstream tooling cannot bleed through and override profile settings. Each stripped key is logged individually under `[INFO][ENV]` with its old shell value, so the full before/after state is visible in the log. If `extra_env` (the profile's env-type keys) would override a shell var that was *not* in the strip set, a `[WARN][ENV]` is emitted.

SysForge bootstrap vars (`SYSFORGE_STATE_DIR`, `SYSFORGE_CONFIG_DIR`) are explicitly exempt from this rule — they are SysForge's own interface, not build tool vars.

Any build tool override needed at invocation time should use the corresponding SysForge flag (`--cc`, `--cxx`, `--ld`), not a shell export. This applies to both `sysforge build` and `sysforge pipeline`.

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

`AlreadyBuilt` (carries the offending pkgbuild path) is raised when the makepkg run exits 13 (`E_ALREADY_BUILT`) or its stdout contains `"A package has already been built"` — covers chroot wrappers that may rewrite the exit code. Distinct from `CalledProcessError` so callers can act on it instead of marking the build failed.

Interpretation is centralized (2.5.1-F2): every catch site routes its decision
through `primitives/already_built.resolve_already_built` — a decide-only policy
seam with two postures. `"reuse"` (build_core's batch loop, the toolchain
passes) treats the existing PKGDEST artifact as the product and proceeds — but
the batch loop reaches the seam only for *unforced* targets, since a target
built with `-f` (drift promotion, `build --rebuild`) cannot legitimately report
exit 13 and fails hard instead (3.0.0-B9);
`"review-gated"` (kernel stage) preserves the B5 semantics — the skipped build
also skipped the promised in-prepare() kconfig review, so interactive runs get
an install-as-built / rebuild-with-`-f` / abort prompt while unattended runs
proceed. The unattended arbitration (caller interactivity ∧ `--non-interactive`
∧ TTY) lives only in the seam. `makepkg_wrapper`'s own `except AlreadyBuilt`
(manifest capture for renamed builds) is a side-effect that fires regardless of
policy, then re-raises.

`PGOBuildSkipped` is the third wrapper-specific exception: raised from `_run_build` when a `pgo_llvm_toolchain` build needs profdata that's absent/incompatible and the user (or non-interactive default) chose to skip.

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

- `source_built` mode (legacy token `patched_pkgbuild`, deprecated — removed in 4.0.0, row 24): `PKGBUILD.sysforge` and `pkgbuild_extracted_profile.toml` are preserved. A `[WARN][PATCH]` line is emitted noting their location.
- Groups-only mode (non-patch builds): `PKGBUILD.sysforge` is also preserved on failure with a `[WARN][BUILD]` message.

On success, all patch artifacts are cleaned up in both modes.

### Interactive recovery menu

When `_invoke_with_retry` is not in batch mode and a build fails with no
built `.pkg.tar*` artifacts found (the "install-only failure" branch above
doesn't apply), it hands off to `makepkg_invoke._run_recovery_menu` — the
**one home** for interactive build-failure recovery. This replaces the old
bare "fix the PKGBUILD and press Enter" prompt with a small menu:

```
Recover:
  [e] edit PKGBUILD in $EDITOR — retries automatically on exit
  [c] retry with a different compiler / linker
  [r] retry as-is            (Enter)
  [a] abort
```

Above the menu, the failure summary reports the toolchain the build actually
used: `Toolchain used:  CC=…  CXX=…  LD=…`. The `LD` field comes from
`_summary_linker`, which reuses the single `makepkg_flags.resolve_effective_linker`
authority over the profile's LDFLAGS *and* the system makepkg.conf LDFLAGS — so a
conf-level `-fuse-ld=` swap (e.g. the clang config's lld) is surfaced, not just a
profile-level one. Since linker choice (`-fuse-ld=…`, mold/lld swaps) is a frequent
failure cause, showing it alongside `CC`/`CXX` makes the diagnostic complete; the
resolver's PATH guard means a declared-but-missing linker degrades to `ld`.

`[c]` is offered only when the caller supplied a `reemit_conf` closure (see
below) — a caller with no conf-emission seam (e.g. a test harness) degrades
to `[e]/[r]/[a]`.

- **`[e]`** snapshots the PKGBUILD to a sibling `<name>.orig` (once, on first
  edit — never overwritten by a later edit) before launching `$EDITOR`
  (`primitives/editor.py`'s `resolve_editor`/`editor_usable`, the same
  resolution chain as `config merge`), via the shared `/dev/tty` passthrough
  `run_tty_argv`. On editor exit it retries the build automatically; a
  still-failing retry re-shows the menu rather than raising.
- **`[c]`** presents the compiler choice as a **coherent toolchain unit**
  (`_prompt_toolchain_swap` → `_TOOLCHAIN_UNITS`): the user picks `gcc`
  (`CC=gcc CXX=g++`) or `clang` (`CC=clang CXX=clang++`), never two independent
  free-text compilers — a mixed `cc`/`cxx` pair (e.g. `gcc` + `clang++`) only
  produces an incoherent override that fails the retry confusingly. The menu
  enumerates **only installed** toolchains (`_available_toolchain_units` gates
  on `shutil.which` for *both* `cc` and `cxx`), marks the current one, and offers
  `[m]` (hand-enter a `cc`/`cxx` pair — the advanced escape hatch) and `[b]`
  (back to the top menu). `LD` is prompted separately (linker choice is
  orthogonal to the compiler). It then calls the caller-supplied
  `reemit_conf(cc, cxx, ld)` context manager to get a freshly emitted conf
  path and retries against it. `makepkg_invoke` never imports the conf
  emitter itself — `reemit_conf` is a closure the wrapper builds over its own
  `emit_makepkg_conf(...)` call (same `_conf_kwargs` as the main build), so
  the layering stays one-directional: `makepkg_wrapper` depends on
  `makepkg_invoke`, never the reverse. A successful swap is reported back as
  `RecoveryOutcome(action="retry", overrides={"cc", "cxx", "ld"})`.
- **`[r]`** retries unchanged; **`[a]`** aborts.

`_run_recovery_menu` returns a `RecoveryOutcome` (`action: "retry"|"abort"`,
optional `overrides`) only on a successful retry or an explicit abort — it
never returns mid-failure, it loops. `_invoke_with_retry` raises the
`[build_failed]` error on `action == "abort"`.

**Read-once channel back to the wrapper.** `_invoke_with_retry` runs one
import-cycle below `makepkg_wrapper`, which is the layer that owns the
profiles.toml writer — so the menu can't call the writer directly without
inverting the dependency. Instead a successful swap's `RecoveryOutcome` is
stashed in a `contextvars.ContextVar` (`_LAST_RECOVERY`) and the wrapper
drains it once via `take_last_recovery()` right after the build's `with
emit_makepkg_conf(...)` block exits successfully — `take_last_recovery`
resets the var on read, so a stale outcome from an earlier package can never
leak into the next one's persistence check.

**Persistence is best-effort.** `makepkg_wrapper._persist_recovery_overrides`
(called once per successful build, keyed on the package's `pkgbase`) drains
`take_last_recovery()`; if it carries `overrides` it calls
`profile_writer.write_package_compiler_override` — the sole profiles.toml
writer for this table (see §Flag/Profile System →
`[package_compiler_overrides]`) — wrapped so a read/write failure is logged
and swallowed rather than failing an otherwise-successful build. A swap with
any of `cc`/`cxx`/`ld` missing is skipped with a warning instead of writing a
partial row.

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
- Default: `[ERROR]` only (plus `ui()` primary output, which is verbosity-immune)
- `-v`: adds `[WARN]`
- `-vv`: adds `[INFO]`
- `-vvv`: adds `[DEBUG]` — full body dumps of every loaded config, resolved profile, conflict groups, inference map, and temp makepkg.conf

**The level rubric** (the audit authority — every call site is classified against it):

| Level | Fn | Gate | Reserved for |
|---|---|---|---|
| UI | `ui()` | always | The primary output the user ran the command to see — final summaries, `doctor` findings, `state`/`log`/`env` bodies, prompts, tables. **Not** progress narration. |
| ERROR | `error()`/`fatal()` | always | Failures that abort or degrade the run. |
| WARN | `warn()` | `-v` | Recoverable anomalies: skips, fallbacks, soname/ABI mismatches that don't block. |
| INFO | `info()` | `-vv` | Progress/status narration: "syncing X", "wrote temp conf", "building 3/7". |
| DEBUG | `debug()` | `-vvv` | Full body dumps: config/profile/conf contents, resolved argv, env snapshots. |

Decision test for each site: *is this the answer, or narration about producing the answer?* The answer → `ui()`; narration → `info()` (or `debug()` for full dumps). `ui()` is verbosity-immune and reserved for primary output only — it is **not** a "make this always show up" escape hatch. File logs are unaffected: every level is always written to file regardless of stderr gating, so a demotion never loses forensic detail.

**Configurable default verbosity.** The stderr level when no flag is passed is resolved once at CLI entry by `cli._resolve_verbosity(args)` (mirroring `_resolve_color_mode`), which calls the single `log.set_verbosity` seam — no resolution logic leaks into `log.py`. Precedence (highest first):

1. Global `--quiet` → level 0 (wins over everything; distinct `quiet_global` dest so it never clobbers `doctor`'s local `--quiet/-q`, and it is not hoisted ahead of the `doctor` subcommand).
2. Else `-v/-vv/-vvv` (argparse `count` > 0) → that level.
3. Else the `[log] verbosity` key in `sysforge.toml` (integer 0–3, clamped; non-int/unreadable ignored → never aborts startup).
4. Else 0.

The shipped default stays 0; the config key lets a user opt into a quieter or more verbose baseline without changing it. Golden-output regression tests assert that default-level (`verbosity=0`) output of a representative dry-run carries no `[INFO]`/`[WARN]` lines — the primary guard against future features re-leaking narration into `ui()`. Two stages anchor the guard: the day-to-day `packages` stage (`_load_packages`) and, extending the sweep to the interactive/bootstrap path, the `configure` stage's dry-run (both in their respective stage tests). The re-levelling audit covers every stage — `partition`/`install`, `hardware`, `configure`, `reconfigure`, `kernel`, `toolchain` — demoting progress narration ("Probing hardware…", "Building kernel…", "[PGO] 3/4 complete", per-step "wrote/synced/installed" confirmations) to `info()` while keeping prompts, plan tables, section headers, dry-run previews, and check results as `ui()`. Tests run at verbosity 2 (all messages visible).

### Colour

`log.py` is the single colour authority for the whole codebase. `log.use_color()` is the one gate every output site consults, and `log.bold()` / `dim()` / `red()` / `green()` / `yellow()` / `cyan()` are the shared helpers that wrap text only when the gate is on — no site hand-writes escape codes. `ui/headers.py` and `ui/progress.py` import these rather than carrying their own ANSI constants.

Resolution precedence in `use_color()`:

1. Colour **mode** (`log.set_color_mode`, set once at CLI entry): `"never"` → off; `"always"` → on (beats the environment, so colour survives being piped into a pager or colour-aware tool).
2. Mode `"auto"` (default): `NO_COLOR` (any non-empty value) disables; then `FORCE_COLOR` (any non-empty value) forces on; otherwise colour follows whether the active stream is a TTY.

The mode is resolved at startup as **`--color=auto|always|never` flag > `[ui] color` config (`sysforge.toml`) > `"auto"`** (`cli._resolve_color_mode`); a junk value degrades to `"auto"`. File logs are always written plain regardless of the gate. Because the decision is per-call, output piped through the pager is coloured up front (the review diff passes `git diff --color=always` when the gate is on, then `less -R` carries the ANSI through).

### Glyph downgrade

`log.use_unicode()` is the single capability gate for decorative non-ASCII glyphs (arrows, `✓`/`✗`, box-drawing, ellipsis, block-bar fills), parallel to `use_color()`. When it returns false, `log.downgrade_glyphs(text)` rewrites those code points to ASCII fallbacks (`→`→`->`, `✓`→`[OK]`, `─`→`-`, …) via a single `str.translate` table; when true it is a pass-through. This exists because a Linux framebuffer/VT console (`TERM=linux`) loads a console font that maps only a subset of code points, so the install-time pipeline rendered missing-glyph boxes on bare-metal/VM consoles.

Resolution precedence in `use_unicode()`:

1. Unicode **mode** (`log.set_unicode_mode`): `"never"` → off; `"always"` → on.
2. Mode `"auto"` (default): `SYSFORGE_ASCII` (any non-empty value) disables; a stream whose `encoding` is a known non-UTF value disables; `TERM=linux` disables; otherwise Unicode is allowed (an unknown/`None` encoding stays Unicode so test capture sinks aren't over-stripped).

Downgrade happens **only at the terminal-output chokepoints** — `log.ui()`, `log._format_line()` (error/warn/info/debug), `prompt.py`'s prompt strings, `ui/progress.py::_paint`, and the `partition` stage's plan-table `print()` — never at every call site. The UTF-8 file logs are written from the caller's original text and therefore always keep the real glyphs. In `progress._paint` the downgrade precedes the column clamp because ASCII fallbacks change string length.

### File logging

File logging runs at full verbosity regardless of the `-v` level — every `[INFO]`, `[WARN]`, and `[ERROR]` line is written to file even when the terminal shows only errors. Never let file I/O break a build: all file write errors are silently swallowed.

**Unified log** — one consolidated file for the entire run.

- Default path: `<state_dir>/sysforge.log` (i.e. `/var/lib/sysforge/sysforge.log`). Per verb: `sysforge update` → `<state_dir>/sysforge-update.log`; `sysforge build` → `<state_dir>/sysforge-build.log`. Every other substantial verb (`doctor`, `fetch`, `revert-to-stock`, `uninstall`, `setup`, `config merge`, `state repair/forget`) opts into the same pattern — `sysforge-<name>.log` — via the base-class flag `Verb.wants_run_log`, opened/closed by the verb runner (purged before `execute`, kept on success; a failed run is closed with a `FAILED` marker in the log). Trivial list/passthrough printers (`env`, `log`, `search`, `completions`, `packages list`, `state list/failed`) and read-only reports (`resolve`, `state orphans`) stay opted out; `doctor` opts back out for `--apply`, which delegates its rebuild to `sysforge update`'s own log. A standalone `run <stage>` invocation writes `sysforge-run-<stage>.log` and keeps it on success (an operator-inspection artifact); the full `run pipeline` keeps its own shared `sysforge.log`.
- `sysforge update` and `sysforge build` always truncate at run start and **keep** the log afterwards (so a multi-package run leaves one consolidated record next to the per-package logs). `sysforge run pipeline` appends across runs and clears on success; standalone `run <stage>` keeps its per-stage log on success instead.
- A `# log cleared after successful run` marker is left in the file after truncation.
- `--log-dir <path>` overrides the directory.
- `--purge-log` (`run pipeline` only) truncates before the run starts.
- `--persist-log` suppresses truncation on success. Use when you want to keep the log for post-run analysis.
- `--no-unified-log` (`run pipeline` only) disables the unified log for this run.

The lifecycle primitive (`log.open_unified_log` / `close_unified_log`) is the single home; its callers differ by verb shape. `run pipeline` opens it inside the pipeline runner (`run_pipeline`); standalone `run <stage>` opens it inside `run_stage_standalone`, deriving `sysforge-run-<stage>.log` and passing `persist=True` so it survives success; `update` opens it inside `cmd_update` (it owns a fine-grained success calc over failed/install/pacman state). Every other substantial verb declares only a basename via the base-class flag `Verb.wants_run_log` (or, for a conditional/non-derived basename, the `unified_log_basename(args)` override) and the **verb runner** (`verbs/runner.py`) opens it purged before `execute` and closes it kept afterwards — `build`, `doctor`, `fetch`, `revert-to-stock`, `uninstall`, `setup`, `config merge`, and `state repair/forget` all opt in this way (`build` routes through `build_core`, not the pipeline runner, so it would otherwise have no run-level log). `doctor` uses the `unified_log_basename(args)` override rather than the bare flag so it can opt back out for `--apply`, whose rebuild delegates to `cmd_update`'s own `sysforge-update.log` on the same process-global handle. Trivial list/passthrough printers and read-only reports (`resolve`, `state orphans`) leave `wants_run_log` at its `False` default to opt out.

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

### journald mirror (2.3.0-F6)

The unified run-log is the authoritative, user-facing capture. Complementing it,
every **sentinel-gated** (system-mutating) verb also emits one structured record
to the systemd journal via `primitives/journal.py`, so SysForge's changes appear
in `journalctl` alongside everything else that touched the system — where an
admin looks during incident review. This is additive and never load-bearing: on
a non-systemd host (no journal socket) it is a silent no-op.

Records carry queryable fields:

    journalctl -t sysforge                 # all SysForge mutations
    journalctl SYSFORGE_VERB=build         # just build invocations
    journalctl SYSFORGE_TARGET=pkg:mesa    # mutations touching a package
    journalctl SYSFORGE_TARGET=mode:repair # a subjectless state operation
    journalctl -p err -t sysforge          # failed mutations (PRIORITY=3)

Fields are `SYSFORGE_`-prefixed to avoid colliding with journald's reserved
well-known names. Emission is keyed off `Verb.requires_sentinel` in
`verbs/runner.py`, so any future mutating verb is mirrored automatically.

`SYSFORGE_TARGET` is supplied by every sentinel-gated verb via a
`Verb.journal_target(args)` override (`build`, `uninstall`, `revert-to-stock`,
`state forget`, `state failed --clear`, `state repair`, `state orphans --prune`),
and is namespaced so a consumer can tell a package subject from a whole-state
operation without knowing which verb produced the record:

- `pkg:<space-joined names>` — the subject is one or more packages.
- `mode:<subcommand>` — a subjectless state operation (the mode is the only
  meaningful discriminator; the token is derived from the verb name).

The `pkg:`/`mode:` prefixes are formatted in one place — `journal.pkg_target` /
`journal.mode_target` in `primitives/journal.py`. `build`'s value was namespaced
into this scheme in 2.4.0-F1: it now emits `pkg:<names>` where it previously
emitted the bare names.

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
- **`tools/gen_options.py`** — walks `cli._build_parser()`'s subparser tree depth-first (so `packages add`, `run kernel`, etc. each get a `## <name>` section) — top-level commands ordered by `cli.tiered_command_order()` so the COMMANDS sections stay in lockstep with the `sysforge --help` usage tiers (§CLI Verb Framework), sub-commands in registration order — emits a synopsis line plus one definition block per positional/option, escapes scdoc formatting characters in help text, and performs the splice itself (no sed). Subparsers registered without `help=` (the internal `completions` data sink) are excluded. Each command section also gets a `*Configuration:*` / `*Environment:*` trailer from the hand-maintained `_VERB_CONFIG` dict at the top of the script (qualified command name → config files / env vars consumed; commands without an entry get no trailer). The FILES and ENVIRONMENT sections of the template carry the inverse index ("Read by: …") — when a verb gains or loses a config source, update both `_VERB_CONFIG` and the template. The trailer is man-page-only by design: it is not wired into argparse epilogs, so it never appears in `--help` output (which has its own tiered `COMMAND` grouping — §CLI Verb Framework).
- **`man/sysforge.1.scd`** — intermediate, gitignored. **`man/sysforge.1`** — committed, so AUR-built tarballs ship the page without build-time tooling; the PKGBUILD `package()` installs the committed file directly and needs no man-page makedepend.
- Makefile target: `make man` (pins `COLUMNS=80` so any argparse-derived wrapping is deterministic). `scdoc` is a dev-machine dependency only (the `DEV_DEPS_CORE` set, installed by `make dev-deps` / `make dev`); `python-argparse-manpage` is no longer used anywhere.
- Release gate: the `manpage` group in `tools/check_shipped.py` reruns the exact same two-step pipeline into temp files and diffs against the committed page, so option-help drift in `cli.py` without a `make man` commit blocks the release. Both sides pass through `_normalize_roff` first, which neutralises the three scdoc-version-specific artifacts — the `.TH` date header, the `.\" Generated by scdoc <version>` banner, and hyphen escaping (1.11.5 emits `\-` where 1.11.4 emitted `-`; roff renders both identically). Without that the committed artifact is pinned to whichever scdoc build last ran `make man`, and a contributor on a different version sees hundreds of phantom diff lines. The guard verifies man-page *content* against the argparse tree, not renderer bytes.

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

**LLVM target derivation.** The hardware stage also writes `host_arch` (from `uname -m`) and an autodetected `llvm_targets` list — CPU backend from arch (`x86_64`→`X86`, `aarch64`→`AArch64`, `armv7l`→`ARM`, `riscv64`→`RISCV`, `ppc64le`→`PowerPC`) plus GPU backends from `gpu_vendors` (`amd`→`AMDGPU`, `nvidia`→`NVPTX`; `intel` contributes nothing because the Mesa Intel drivers don't depend on an LLVM backend). **Plus a mandatory `AMDGPU` baseline (`SYSTEM_LIBLLVM_CONSUMER_TARGETS`) on every recognised arch — even nvidia/intel-only hosts:** the *system* `mesa` package links the `AMDGPU` (radeonsi) and host-CPU (llvmpipe) target-init symbols from `libgallium` **unconditionally**, so a rebuilt system `llvm-libs` that dropped `AMDGPU` leaves mesa with `undefined symbol: LLVMInitializeAMDGPU…` and bricks every EGL/GL consumer (the whole desktop). An unrecognised arch yields an empty list — "no filtering", i.e. upstream builds all targets, which is also safe for mesa. Consumed by `pkgbuild_patcher.patch_llvm_targets` when building any LLVM-toolchain package.

**The baseline is enforced at *resolution* time, not only at derivation.** `derive_llvm_targets` bakes `AMDGPU` into a freshly-derived list, but the actual build resolves `LLVM_TARGETS_TO_BUILD` from `hardware_profile.toml` (or an explicit `toolchain.toml [llvm] targets`) via `llvm_targets.resolve_or_detect_llvm_targets` — both of which *bypass* derivation. A profile cached before the baseline existed, or one a user hand-edited, would otherwise silently reintroduce a brick. So `resolve_llvm_targets`/`resolve_or_detect_llvm_targets` re-apply `SYSTEM_LIBLLVM_CONSUMER_TARGETS` (via `_ensure_system_consumer_targets`) to **any** non-None, non-empty resolved set, from any source. The single opt-out is `[llvm] targets = []` ("build all", which already includes `AMDGPU`), which resolves to `None` and skips the enforcement. This is the layer the bricked-desktop regression slipped through: the fix had lived only in derivation while the build read the cached file.

**Mesa driver derivation (the meson analogue).** The hardware stage also writes `mesa_gallium_drivers` / `mesa_vulkan_drivers` from the same `gpu_vendors` (`derive_mesa_drivers`): `amd`→`radeonsi`/`amd`, `intel`→`iris,crocus`/`intel,intel_hasvk`, `nvidia`→`nouveau`/`nouveau`. These trim mesa's `-D gallium-drivers=all` / `-D vulkan-drivers=<every-driver>` (every ARM-SoC/mobile GPU mesa ships) down to what the host runs — a real build-time win when sysforge source-builds mesa. The invariant is the *inverse* of the LLVM `AMDGPU` one: where that guards against reducing *too little*, mesa's mandatory software baseline (`MESA_MANDATORY_GALLIUM` = `llvmpipe`/`softpipe`/`zink`, `MESA_MANDATORY_VULKAN` = `swrast`/lavapipe) guards against reducing *too much* — a build with no software renderer bricks headless/VM/GPU-reset-recovery sessions. The baseline rides every derived/resolved set, even a no-GPU host (which yields baseline-only). **Unlike LLVM filtering, mesa filtering is opt-in** (`[mesa] filter_drivers = true` in `sysforge.toml`, default off); resolution (`mesa_drivers.resolve_or_detect_mesa_drivers`) and baseline enforcement (`_ensure_mesa_software_baseline`) mirror the LLVM path, and a gallium reduction also intersects `gallium-rusticl-enable-drivers` with the built set (rusticl drivers must be a subset), falling back to meson's own `auto` when the intersection is empty — not a concrete driver like `llvmpipe`, which is a gallium driver but is **not** rusticl-capable and is rejected by meson ("not in allowed choices: auto, asahi, freedreno, radeonsi"). Consumed by `pkgbuild_patcher.patch_mesa_drivers` (gated by `profile.is_mesa_pkgbase`) when building any mesa-family package; lib32-mesa **is** filtered (vendor- not arch-coupled, unlike lib32-llvm). **Packaging is hardened alongside the meson rewrite** (`_harden_mesa_packaging`, applied only when a reduction actually changed the file): a trimmed driver set means some `libvulkan_<drv>.so` are never built, but mesa's `package_*()` functions relocate every upstream driver unconditionally — `_pick … $libdir/libvulkan_<drv>.so` in `package_mesa()`, then `mv <tag>/* "$pkgdir"` in the split package — so packaging would `mv` a missing file / unmatched glob and abort. Rather than map driver→pkgname (1:1-breaking and version-fragile: `amd`→`vulkan-radeon`→`libvulkan_radeon.so`), the patcher makes the PKGBUILD self-healing: it inserts `[ -e "$f" ] || continue` into the `_pick()` loop (skip an unbuilt source) and wraps each split `mv <tag>/* "$pkgdir"` in `if compgen -G "<tag>/*"` (no-op when the staging dir is empty). Filtered-out split packages then build as zero-file packages (makepkg warns, doesn't fail) and aren't installed. Idempotent; a structural no-op on a PKGBUILD without the `_pick`/split-`mv` shape.

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

**Where the mandatory-baseline tables live.** `SYSTEM_LIBLLVM_CONSUMER_TARGETS` and `MESA_MANDATORY_GALLIUM` / `MESA_MANDATORY_VULKAN` are defined in **`primitives/hardware_tables.py`**, not in the hardware stage that derives from them. Both derivation (`stages/hardware.py`) and resolution (`primitives/llvm_targets.py`, `primitives/mesa_drivers.py`, `primitives/pkgbuild_patcher.py`) must agree on them, and the resolvers are primitives — the leaf layer, which must not import upward into a stage. It did until 2.6.1-F8, through function-level imports that existed purely to dodge an import cycle; because `pipeline/stages/__init__.py` instantiates every stage at import, reaching up for one driver tuple pulled all eleven stage modules in behind it, and an unrelated import error in `stages/packages.py` surfaced as a traceback inside mesa driver resolution. `hardware_tables.py` imports nothing from sysforge and must stay that way; `tests/test_module_layering.py` fails the build if a module under `primitives/` grows a new `pipeline.stages` import.

### Drift report (before overwrite)

The stage re-probes from scratch every run and overwrites `hardware_profile.toml` unconditionally — but before the write it reports how the freshly-detected hardware summary differs from the existing file, so a meaningful change (a swapped GPU, a CPU upgrade, a new arch) is surfaced rather than silently replaced. This mirrors the flag-drift reporting pattern (`primitives/flag_drift.py`): the comparison is advisory, never a hard failure that blocks the refresh.

Two pure helpers in `pipeline/stages/hardware.py` back it: `_load_hardware_summary(path)` reads the `[hardware]` table from the existing file (returning `None` when absent or unparseable — a corrupt prior profile is treated as "no baseline"), and `_diff_hardware_summary(old, new)` diffs the two summaries over the ordered `_HARDWARE_DRIFT_FIELDS` set, emitting `  <field>: <old> → <new>` lines. Only the scalar `[hardware]` summary is compared; the `[kconfig]`/`[kconfig_devices]`/`[[devices]]` tables churn on every probe (device addresses, kbuild-map width) and are not a stable drift surface. When fields changed, the stage logs a `WARN` header plus one line per field; when nothing changed it logs an `INFO` "unchanged" line.

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

`--cache-report` on both `build` and `pipeline` subcommands prints a structured per-package and totals summary to stderr at end of run, regardless of verbosity. This is the only user-visible output that ignores verbosity gating. The report is emitted through `log.ui` (not raw `print`), so it is captured by the unified run-log and its divider passes the Unicode gate like every other renderer.

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

**Reduced LLVM targets must keep `AMDGPU`.** When the toolchain stage rebuilds Stage-1 `llvm`/`llvm-libs` with a reduced `LLVM_TARGETS_TO_BUILD` (see §Hardware detection → *LLVM target derivation*), the set **must** include `AMDGPU` even on nvidia/intel-only hosts. Stage-4 `mesa` links the `AMDGPU` (radeonsi) and host-CPU (llvmpipe) target-init symbols from `libgallium` **unconditionally**; a system `libLLVM` missing them fails to load with `undefined symbol: LLVMInitializeAMDGPU…`, taking down every EGL/GL client (`cosmic-comp`, the greeter — the whole desktop), with healthy kernel/KMS still presenting as a black screen. `hardware.derive_llvm_targets` guarantees this via `SYSTEM_LIBLLVM_CONSUMER_TARGETS`, and `llvm_targets.resolve_or_detect_llvm_targets` re-asserts it at resolution time so a stale/edited `hardware_profile.toml` can't drop it (see §Hardware detection → *LLVM target derivation*).

**Defense in depth (the desktop must never black-screen from a toolchain rebuild).** Three layers back the rule above: (1) **prevent** — the resolution-time AMDGPU baseline; (2) **catch pre-install** — toolchain **Gate 2** (`toolchain_safety.check_system_consumer_symbols`) `ldd -r`-diffs the freshly-built `libLLVM` against installed mesa consumers (`libgallium`/DRI/Vulkan) and aborts *before* `pacman -U` if any `LLVMInitialize*@LLVM_x.y` symbol they import would go missing — the live graphics stack is untouched; (3) **verify post-install** — **Gate 3** re-runs the diff against the now-installed libLLVM and triggers the snapshot auto-rollback on a miss (see §`pipeline-layer` → toolchain gates). Separately, `sysforge doctor --graphics` surfaces the same fact (`graphics_probe._check_mesa_llvm_symbols` → `check_installed_consumer_symbols`) so a system already in this state self-diagnoses in one line instead of presenting only as a black screen.

**Two stranding classes — only one is a hard block.** The Gate-2/3 consumer-symbol diff (`_diff_consumers_against_libllvm`) splits its findings by whether any missing symbol is an `LLVMInitialize*` target-init entry point:

- **Target-init drop (unhealable).** A dropped LLVM backend (e.g. a reduced `LLVM_TARGETS_TO_BUILD` without `AMDGPU`). The symbol would not exist anywhere — rebuilding the consumer cannot recover it. Gate 2 hard-aborts before install; the AMDGPU baseline is the real fix. **Unchanged behaviour.**
- **`std::` re-export drop (healable).** A drop of *only* non-target-init `LLVM_*`-versioned symbols. These are libstdc++ `std::__cxx11::basic_string` methods that LLVM's `global: *` version script globs into the `LLVM_<ver>` node as out-of-line weak copies. The official `llvm-libs` exports them; mesa links them as `…@LLVM_<ver>`. A PGO (`-fprofile-use`) `libLLVM` **inlines those weak copies away**, so the optimized lib — *at the same soname* — no longer exports them and mesa is stranded. This is healable: rebuilding mesa against the new `libLLVM` re-links the symbols to libstdc++ (`@GLIBCXX_*`), exactly as a distro does on an llvm rebump.

For the healable class Gate 2 does **not** abort. It captures the installed libLLVM consumers via `toolchain_safety.libllvm_abi_consumers` (the reverse-dep `%DEPENDS%` walk factored out of `assess_libllvm_soname_impact` into `libllvm_soname_consumers`, here **not** gated on a soname change) and rebuilds them after Gate 3 through the same `_rebuild_soname_consumers` path used for a soname bump — gated by the existing `rebuild_soname_consumers` mode (`prompt` default | `auto` | `off`). Gate 3 tolerates the healable miss (mesa is not rebuilt until *after* Gate 3, so a rollback there would revert the very libLLVM the rebuild is about to make coherent); only target-init misses still trip the auto-rollback. The system self-stabilises: once mesa links the optimized libLLVM it stops importing the `…@LLVM_<ver>` `std::` symbols, so a subsequent `run toolchain` sees no stranding and queues no rebuild. Note `assess_libllvm_soname_impact` still short-circuits on a same-version (`old_mm == target_mm`) refresh — the std:: drift is owned by this Gate-2/3 + `libllvm_abi_consumers` path, not the soname-bump gate.

---

## Release Process

- **GitHub:** public from day one; source of truth for all code.
- **Per-release change history** lives in `docs/release-notes/vX.Y.Z.md` (see *Release notes* below). This section documents the *process* that cuts a release, not the history of past ones.

### Pre-release checklist

**The operational runbook is `docs/RELEASE-CHECKLIST.md`** — the paste-able, stage-by-stage command
sequence with tick boxes, kept standalone so it is readable at release time without wading through
design prose. It is the single home for the checklist; do not duplicate its steps here. This
section documents only *why* the gates are split the way they are.

Two gate runners exist and **neither is a superset of the other**. `make pre-release` holds the
slow, version-*independent* checks (lint, typecheck, full suite, and the five shared `check-*`
gates) so they can run on any branch at any time. `tools/release.sh` preflight holds the
version-*dependent* ones (`check-bump`, `check-standards-at`) — they need the target version, which
does not exist until a bump level is chosen — plus the repo and signing preconditions (on `main`,
clean tree, chroot present, GPG key usable). The five gates both runners share are re-run by the
script deliberately: it cannot assume `pre-release` was run recently, and a stale `DESIGN.md`
discovered *after* the tag is created costs a `make release-resume` cycle.

Three things sit in **neither** runner and are therefore the ones most easily skipped: coverage
(`make coverage-ratchet`), the CVE audit (`make audit`), and the runtime tiers — the container tier
(`make container-smoke`, `container-smoke-cachyos`) and the VM tier (`make vm-test`, plus the `git`
flavor and the stable↔git `conflicts=()` swap). Both runtime tiers install a genuinely built
package and so depend on `make vm-pkg-stable` / `vm-pkg-all` first. They are deliberately outside
the gates — they need a booted VM or a working podman and network, neither of which can be a hard
prerequisite of a lint run — but a minor or major release that skips them is untested against a
real install. See `tools/vm/README.md` and `tools/container/README.md`.

### AUR publishing process

Releases are driven by three Makefile targets — `make release-major`, `make release-minor`, `make release-patch` — each of which calls `tools/release.sh --bump=<level>`. The script handles the full flow end-to-end with a single up-front summary + approval prompt and one mid-run pause for the manual tag push. Phases:

1. **Bump, commit, tag.** Rewrites `pyproject.toml` (the single source of truth for version), `PKGBUILD` `pkgver=`, `PKGBUILD-git` `pkgver=` (leading `X.Y.Z` only — the `.r0.g0000000` suffix is preserved as the placeholder for the dynamic `pkgver()`), the `<!--version-->vX.Y.Z<!--/version-->` markers in `README.md`, `DESIGN.md`, **and the generated marker's source `docs/design/00-header.md`** (DESIGN.md is generated — rewriting only the output would be reverted by the next `make design`), regenerates `uv.lock` (via `uv lock`) and `man/sysforge.1` (via `make man`), renames the release-notes accumulator `docs/release-notes/unreleased.md` to `vX.Y.Z.md` (title stamped with version + date) and reseeds a fresh accumulator, then makes a single **GPG-signed** `release: vX.Y.Z` commit (which includes both the renamed `docs/release-notes/vX.Y.Z.md` and the reseeded `unreleased.md` — see *Release notes* below) and creates a **signed annotated tag** (`git tag -s`, immediately verified with `git tag -v`).
2. **Push pause.** Prints `git push origin main && git push origin vX.Y.Z` and waits on ENTER. The user pushes manually (releases are deliberate, not background events). The script verifies the tag is on `origin` before continuing.
3. **Post-tag artifacts + signed release.** Downloads the GitHub tarball, records its sha256 **and GPG-signs it** (detached armored `.asc`), updates `sha256sums=` in `PKGBUILD` to the two-element form (tarball hash + `SKIP` for the signature source), **publishes the GitHub release** (`gh release create`, or `gh release upload --clobber` on resume) with the `.asc` + `SHA256SUMS` + `SHA256SUMS.asc` assets, then **validates both `PKGBUILD` and `PKGBUILD-git` in a clean chroot** (the release must exist first — the stable PKGBUILD's signature source points at the release download URL, so `makepkg` fetches and verifies the `.asc` during validation), regenerates `.SRCINFO` and `.SRCINFO-git` (both gitignored — local artifacts only), and makes a second signed `release: vX.Y.Z sha256` commit (the `.SRCINFO` files do not get committed).
4. **Final instructions.** Confirms the signed GitHub release is published, then prints `git push origin main` and the `git clone`/`cp`/`commit`/`push` sequence for the `sysforge` and `sysforge-git` AUR repos. The user runs those manually.

**Release signing & downstream verification.** From this release onward every release is GPG-signed end to end with the maintainer key: the commit, the annotated tag, and the source tarball. `tools/release.sh` preflight hard-fails before touching any file if signing is not usable — `git user.signingkey` unset, `commit.gpgsign` not `true`, the secret key not in the keyring, or `gh` missing — so an *unsigned* or unpublishable release can never be produced. The stable `PKGBUILD` declares the maintainer fingerprint in `validpgpkeys` and adds the detached signature as a second `source` entry (`…releases/download/vX.Y.Z/sysforge-X.Y.Z.tar.gz.asc`, paired with `SKIP` in `sha256sums`), so `makepkg` verifies the maintainer signature at install time — closing the gap where the only integrity link was a hash the release script computed itself. The repo ships a placeholder fingerprint sentinel (`REPLACE_WITH_MAINTAINER_KEY_FINGERPRINT`); `check_shipped` tolerates it so dev gates pass before a key exists, but the release preflight refuses to publish while it is present. The `-git` (VCS) package tracks a git clone rather than a release tarball, so it carries no `validpgpkeys`/`.asc` (its provenance is the signed tag); `check_shipped pkgbuild_parity` allows `validpgpkeys` to be stable-only, and the `pkgbuild` group permits `SKIP` only when paired with a `.asc`/`.sig` source. Users verify releases per the *Verifying releases* section of `README.md`.

**Release notes.** Notes are authored **incrementally**, not reconstructed at release time. Each landing commit that completes a ROADMAP item appends its entry — under the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) category headings (`Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`) — to the running accumulator `docs/release-notes/unreleased.md` in that same commit. An entry carries ROADMAP.md's own entry shape: it leads with its roadmap ID (``- **`<ID>` — <title sentence>.** <body>``) and is separated from its neighbour by a `---` rule, so one item reads on its own rather than running into the next, and an item looks the same on the backlog as it does in the notes. One item routinely ships several distinct user-visible changes, so when its ID files more than one entry **within the same section** each lead carries a `(n/N)` facet suffix (``- **`<ID>` (n/N) — <title sentence>.**``), numbered in document order — the entry then states up front that it is one facet of a larger item rather than reading as a duplicate filing. Cross-section repeats carry none: one Keep a Changelog entry cannot span `Changed` and `Removed`, and a `(2/3)` under a heading the reader arrives at with no memory of `(1/3)` explains nothing. `make check-standards` lints the accumulator under the same vocabulary — plus the ID-first lead, the facet suffix (present iff the ID repeats in-section, `1..N` once each, denominator matching the actual count) and the ascending-ID ordering, with the facet as the same-ID tiebreak — so entries are validated as they land. The pre-flight in `tools/release.sh` **hard-fails** when the accumulator is missing or holds no authored `## ` sections, mirroring the check-shipped/check-personal/check-design gates. Phase 1 **renames** the accumulator to `docs/release-notes/vX.Y.Z.md`, stamps its `# ` title with the version + ISO date, reseeds a fresh accumulator, and commits both as part of the `release: vX.Y.Z` commit; Phase 4 prints the `gh release create` command that publishes the versioned file. Before releasing, the `/release-notes` repo skill reconciles/lints the accumulated entries against this section's framing (a hookify rule reminds before any `make release-*` invocation); it no longer authors the file from scratch.

If interrupted between phases (Ctrl-C at the push pause, or a transient failure), re-running the same `make release-*` command resumes correctly: the script detects that the tag for the *current* `pyproject.toml` version already exists at HEAD and skips Phase 1. That auto-detection cannot fire once a post-tag failure (e.g. chroot validation) needs fix commits *on top of* the release commit — the tag is no longer at HEAD, and re-running a `--bump` invocation would compute a fresh bump from the already-bumped version. For that case `make release-resume` (`tools/release.sh --resume`) explicitly finishes the release for the current version: it requires the `vX.Y.Z` tag to exist and be an *ancestor* of a clean HEAD (`git merge-base --is-ancestor`), performs no bump, and re-enters at Phase 3. The ancestor check is deliberately gated behind the explicit flag: after any *completed* release its tag is also an ancestor of HEAD, so ancestor-based auto-detection would misclassify every subsequent fresh release as a resume. To keep a transient post-tag failure from reading as an unrecoverable one, the script arms an `ERR` trap the moment the release becomes *in-flight* (a `--resume`/auto-detected-resume run, or the instant Phase 1 creates the signed tag): any later non-zero exit prints an advisory that the bump/commit/tag are already in place (and possibly pushed/published) and that `make release-resume` finishes it idempotently — so `make`'s bare `Error 255` is reframed rather than mistaken for a from-scratch failure. Failures *before* the tag exists (bad args, pre-flight, version bump) keep the trap silent and exit plainly, since there is nothing tagged to resume.

The version markers in `README.md` and `DESIGN.md` wrap the single live version token (`<!--version-->vX.Y.Z<!--/version-->`); only it rotates per release. Each document must carry exactly one such marker. The `versions` check group below enforces lockstep across all marker locations; release pre-flight refuses to run if the markers (or the `pkgver` lines, or any other version-bearing field) are out of sync.

**Shipped-file pre-release checks.** Phase 1 of `tools/release.sh` (and the standalone `make check-shipped` / `make pre-release` targets) gate on `tools/check_shipped.py`, which validates every artifact the PKGBUILD ships:

- **`configs`** — every `etc/sysforge/*.toml` is parsed through its real runtime loader (`load_config`, `load_sysforge_toml`, `load_bootstrap`, the stage `_load_*` helpers); unknown top-level sections/keys against a per-file allowlist (`_KNOWN_SECTIONS` / `_KNOWN_TOP_KEYS`) are an error; missing `tests/data/etc/sysforge/` counterpart for every shipped TOML (except per-host `bootstrap.toml`) is an error. **Fixture↔shipped key-inventory lockstep** (`_check_fixture_lockstep`): for the flat stage/global configs (`_LOCKSTEP_FILES` = `kernel.toml`, `toolchain.toml`, `sysforge.toml`), the *set* of documented keys (active assignments + commented `# key =` examples + section headers) must match between the shipped file and its fixture — values may differ, the key set may not. This is the only cross-check tying the tracked fixtures to shipped reality now that the personal live config is fully decoupled; the rich-body configs (`packages.toml`, `profiles.toml`) are excluded because their fixtures legitimately carry test-specific `[[package]]` / profile bodies. The complementary **allowlist↔stage-code parity** guard lives in `tests/test_check_shipped.py` (`TestAllowlistCodeParity`): the allowlist must equal the keys each stage actually reads via `kernel_cfg.get(...)` / `tcfg.get(...)` (helper-resolved keys like `pgo_store` accounted for), and every read key must be documented in the shipped file — this is what catches a new config key that the code reads but nobody allowlisted or documented (the `base_config` class of regression).
- **`pkgbuild`** — every `install -Dm…` source in `PKGBUILD` must exist in the working tree; every `$pkgdir/etc/…` install target must be declared in `backup=()`, and vice versa (no stale `backup=` entries); each `sha256sums` entry is paired with its `source` and may only be `SKIP` when that source is a detached signature (`.asc`/`.sig`) — an all-zero/`DRYRUN…` value, or a `SKIP` on a hashable source, is a placeholder error; `validpgpkeys` must be declared and each entry must be a 40-hex fingerprint (or the `REPLACE_WITH_MAINTAINER_KEY_FINGERPRINT` dev sentinel).
- **`pkgbuild_parity`** — `PKGBUILD` and `PKGBUILD-git` parse to the same dict (via `pkgbuild_meta.parse_pkgbuild`) except for a tightly-scoped allowlist of keys that are *supposed* to differ (`pkgname`, `pkgver`, `pkgrel`, `pkgdesc`, `source`, `sha256sums`, `conflicts`, `provides`, `validpgpkeys` — stable-only, since the VCS package has no release `.asc`). `depends` / `makedepends` / `optdepends` / `backup` arrays must be byte-identical.
- **`hooks`** — every `etc/pacman.d/hooks/sysforge-*.hook` `Exec` line must invoke `tools/pacman-hook-helper.sh` and pass a subcommand the helper documents (`kernel`, `toolchain`, `buildstate`).
- **`completions`** — every verb and every long-flag in the argparse parser tree (reached via `sysforge.cli._build_parser`) must appear in both `completions/_sysforge` and `completions/sysforge.bash`; stale top-level verb entries in the zsh case statement (function-suffix matches case-word but parser doesn't know the verb) are an error. Mirrors the `completions-cli-parity` subagent's audit; this is the mechanical layer that runs every release.
- **`completion_widths`** — every description in `completions/_sysforge` must fit an 80-column listing (`COMPLETION_TARGET_COLUMNS`). zsh renders a match as `<match><pad>  -- <description>`, padding the match column to the longest match in that generator call; once a row exceeds the terminal width zsh abandons the inline two-column table and emits every description as its own list entry, so the whole block renders with names and descriptions detached (the 3.0.0-B2 symptom). The budget is therefore per-call and shared: `COLUMNS - (longest match in the block) - 4`, computed separately for each `_arguments` spec list and each `_describe` array (`_completion_blocks`). One over-wide row degrades every row beside it, and adding a long *option name* to a verb tightens the budget for every *description* in it — `update` is the tightest block in the file at 48 characters, bounded by `--rebuild-on-toolchain-drift`. `completions/sysforge.bash` is not checked: it emits bare `compgen -W` word lists with no descriptions and cannot exhibit this failure.
- **`versions`** — `pyproject.toml` `[project] version` must equal `PKGBUILD` `pkgver=`, the leading `X.Y.Z` of `PKGBUILD-git` `pkgver=`, and every `<!--version-->vX.Y.Z<!--/version-->` marker in `README.md` and `DESIGN.md` (literal `vX.Y.Z` placeholder strings in prose are filtered out by the `\d+\.\d+\.\d+` constraint).
- **`manpage`** — regenerates `man/sysforge.1` via the scdoc-hybrid pipeline `make man` uses (`tools/gen_options.py` splices the argparse-derived COMMANDS sections into `man/sysforge.1.scd.in`, then `scdoc` renders) into temp files and diffs against the committed page; any difference is an error, with the fix `make man && git add man/sysforge.1`. Both sides are passed through `_normalize_roff` before diffing, so scdoc-version-specific artifacts aren't findings: the `.TH … "DATE"` header (daily date change), the `.\" Generated by scdoc <version>` banner, and hyphen escaping (`\-` vs `-`, functionally inert in roff). This keeps the gate coupled to the CLI surface rather than to the local scdoc build. Skipped with a `warn` if `scdoc` isn't on PATH. See the Man Pages section.

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

*Flag drift.* Same package version but a different resolved compiler configuration — e.g. profile changed, new flag added, or build mode switched. At build time the resolved flags string is recorded per package in `build_state.toml`; Phase 4.3 re-resolves the current profile for each source-built package and diffs the result against the stored string (via `primitives/flag_drift.resolve_flag_drift`). Flag drift is **reported by default** and is network-free (`--offline --dry-run` gives a read-only report); `--rebuild-on-flag-drift` rebuilds the drifted packages, and `--rebuild-on-drift` is the umbrella over both the flag and toolchain-variant axes. See §`update.py` → *Phase 4.3 — Flag drift*.

**Config defaults.** All three drift-rebuild flags (`--rebuild-on-drift`, `--rebuild-on-toolchain-drift`, `--rebuild-on-flag-drift`) fall back to a sysforge.toml `[update]` default (`rebuild_on_drift`, `rebuild_on_toolchain_drift`, `rebuild_on_flag_drift` respectively) via `_resolve_drift_axes`, which resolves each axis through `config.resolve_flag_default`. The CLI flag always wins when explicitly passed; the config value only supplies the default when it isn't.

**PGO toolchain packages** (`build_mode = "pgo_llvm_toolchain"`) are handled specially during update. `makepkg_wrapper.run()` reads `toolchain.toml → pgo_store`, checks for a saved `clang.profdata` and its `clang.profdata.version` sidecar, and compares the sidecar's LLVM major version against the PKGBUILD's `pkgver` major. If they match, `-fprofile-use=<profdata>` is injected and the build proceeds as a PGO-optimised build. If profdata is absent or version-mismatched (e.g. after a major LLVM bump), the user is prompted: **[p]lain build or [s]kip (default: skip)**. In non-interactive mode the build is skipped automatically. Skipped packages are counted separately in the update summary and do not count as failures. To rebuild profdata after a major version bump, run `sysforge run toolchain`. The toolchain stage itself also reuses compatible profdata — see the **Profdata reuse** section under stage 6.

**Stale-profraw post-build check.** After every non-PGO-managed build, `makepkg_wrapper.run()` globs `pgo_store` for `*.profraw` files. Any file with `mtime >= build_start - 1s` is treated as **fresh** — it was written by the build just completed, which means an instrumented LLVM is still installed on the system and the build was leaking profile data. The wrapper fatals, telling the user to reinstall `llvm`/`llvm-libs` or run `sysforge run toolchain`. Files strictly older than `build_start` are **orphans** left behind by a prior failed or partial toolchain run whose instrumented binaries the user has since cleaned up; these are unlinked in place and an info line is logged. The split makes the safety net self-healing: once the system is clean, the next build purges the residue automatically instead of requiring manual cleanup of `pgo_store`.

Build-state-wide flag-drift coverage (profiled entries outside the walk) is handled by Phase 4.3's fold — detect/report-only, with a `sysforge build` hint for rebuilds (see §`update.py` → *Phase 4.3 — Flag drift*).

`build_state.toml` is the shared source of truth for both drift axes. Written by `makepkg_wrapper.run()` after each successful build.

**`sysforge doctor`** completes the picture — it is read-only and catches the drift class the others don't: **ABI / linkage drift** on already-installed packages, e.g. a partial graphics-stack rebuild leaving `steam` linked against a `libfoo.so.N` that the system no longer exposes. See the `doctor.py` subsection for the full algorithm. Together: `update` → version + flag drift, `doctor` → ABI drift.

DAG stages are categorised as **bootstrap-only** (install, configure) or **repeatable** (hardware, reconfigure, toolchain, packages, kernel). Only repeatable stages participate in drift-driven rebuild runs. `hardware` is repeatable because re-detecting after a hardware change (e.g. GPU swap) is safe and needs no root.

---

## Known Gaps

Implemented behaviour that is incomplete or has known limitations. These are not deferred features — they are holes in currently active code. Planned and abandoned work is tracked in `/ROADMAP.md`, not here.

**`sysforge update` tracks every package sysforge source-built (build_state authority); `repo_mode = "build_from_source"` additionally surfaces drift for *unbuilt* repo packages.** `sysforge update` walks the union of: every installed AUR package (`pacman -Qm`); every package sysforge source-built (build_state `build_mode != "pacman"`, classified `repo_class = "source"` for repo origins) — so `sysforge build mesa` is durable, rebuilt from source on every update; any repo package whose override sets a behavior-changing field (`enable_build_from_source`, `cache`, `reason`); and, with `repo_mode = "build_from_source"`, every remaining installed repo package. Source-built / overridden entries go through `pkgctl repo clone` (via `source_sync._sync_one` calling `pkgctl_checkout` on first visit and `git_fetch_and_compare` on subsequent runs, with a clean-tree hard-reset to upstream when the local clone diverges) and into the source-build loop. The remaining unbuilt, unmodified repo packages (`repo_class = "pacman"`, only present under `repo_mode = "build_from_source"`) take a fast path: one batched `checkupdates` call (`primitives.pacman.checkupdates_map`) resolves their pending-upgrade versions in a single subprocess; vercmp against the installed version emits `NEEDS_PACMAN_UPGRADE`; one terminal `sudo pacman -Syu` after Phase 6 (install) does the actual upgrade. This split is what makes "track every installed package" tolerable on a maintained workstation — without it, every repo package would mean an individual `git fetch` against the Arch packaging tree on every update run. The post-install ordering matters: source-built artifacts hit the system first so the `IgnoreGroup = sf-build` line added by `sysforge setup` protects them when `pacman -Syu` runs. If `checkupdates` is missing (no `pacman-contrib`), pacman-class packages report `SKIPPED_NO_CHECKUPDATES` and no `pacman -Syu` is dispatched. **Remaining limitation:** a repo package installed via plain `pacman -S` (never built by sysforge, no override) is not source-tracked unless you build it once (`sysforge build <pkg>`), add an override, or set `repo_mode = "build_from_source"`. `repo_mode` also governs the packages-stage bootstrap build path; one key, two surfaces.

**`sysforge build` already routes repo packages through `pkgctl_checkout` automatically.** `find_pkgbuild` (`primitives/config.py:91`) checks `is_repo_package()` before AUR-clone fallback, so `sysforge build firefox` Just Works for any repo package — no `repo_mode` plumbing required on the build side.

**`repo_mode = "build_from_source"` is the canonical repo-handling key.** The `[build] repo_mode = "pacman" | "build_from_source"` setting in `packages.toml` is parsed and honoured by `run packages` / `run pipeline` (repo packages with `repo_mode = "build_from_source"`, or per-package `enable_build_from_source = true`, are built from source via `_build_aur()` using `find_pkgbuild` → `pkgctl_checkout`) and at steady-state by `sysforge update` (where `repo_mode = "build_from_source"` pulls every installed repo package into the bulk drift-surfacing walk — distinct from build_state, which independently tracks whatever sysforge has already source-built). `sysforge build` consults `find_pkgbuild` independently.

**Build-env authority: the temp conf, not a precedence stack.** Build tool vars (`CC`, `CFLAGS`, `LDFLAGS`, etc.) are stripped from the inherited shell env in `invoke_makepkg` before makepkg runs — the temp conf is the sole authority for all makepkg-managed keys. Shell env bleed-through is not a configurable priority; it is prevented entirely. SysForge bootstrap vars (`SYSFORGE_STATE_DIR`, `SYSFORGE_CONFIG_DIR`) are exempt — they are SysForge's own interface, not build tool vars, and are not stripped. (The cancelled `[env_precedence]` priority-stack alternative is recorded in `/ROADMAP.md`.)

---

## Scope & Non-Goals

SysForge's lane is **build/package optimization and the health of what it
builds**. It is a profiled package builder and the maintainer of the packages it
produces — not a general configuration-management, backup, or system-provisioning
tool. This boundary is what keeps the project focused; it is stated here (rather
than rediscovered per feature) so proposed work can be measured against it.

**In scope.** Anything that affects what SysForge builds/installs or the
steady-state health of a SysForge-managed system:

- full-system upgrade / partial-upgrade avoidance (owned by the `update` verb);
- orphan and unused-package cleanup, package-cache reclaim (`paccache`);
- `.pacnew` / `.pacsave` handling (the analogue of the `.sfnew` config-merge verb);
- failed systemd units and boot/journal errors surfaced read-only;
- mirror and keyring freshness — both directly affect what gets built/installed.

Read-only health checks of the above belong on `doctor` axes; anything that
mutates the system stays behind an explicit verb with the same sentinel/gate
discipline the rest of SysForge uses.

**Out of scope.** Concerns that belong to general system administration rather
than a package builder:

- backups, snapshot-as-policy, and disk-space *strategy* (the user's own
  btrfs/snapshot/backup tooling — the only adjacency is an optional pre-build
  snapshot);
- user-data hygiene;
- the broader general-recommendations territory — networking, user management,
  locale/input configuration, and security hardening as a whole.

**Distribution support tiers.** SysForge assumes `pacman`/`makepkg` and nothing
narrower. Three tiers, mirroring the tested-hardware tier language:

| Tier | Distro | What is validated |
|------|--------|-------------------|
| primary | **Arch Linux** | everything, including the VM-only concerns: bootstrap/install, kernel staging, graphics/DKMS probes, restart detection |
| validated derivative | **CachyOS** | each minor, via the container tier: packaging invariants, dependency resolution, the `makepkg.conf` merge, version compare and already-built fingerprints (Standards row 23). The VM-only concerns above are **not** validated here |
| expected | **other Arch derivatives** | unvalidated, but expected to work: Standards row 23 forbids the repo-name, toolchain-default, and distro-identity assumptions that would break them |

Non-Arch-derived distributions are out of scope. So is distro-conditional
*behaviour*: row 23 forbids assumptions, it does not introduce per-distro code
paths. A genuine behavioural divergence is a new roadmap item, not a branch.

**North star.** When deciding what SysForge should help *set up, monitor, and
debug*, the Arch wiki's
[System maintenance](https://wiki.archlinux.org/title/System_maintenance) and
[General recommendations](https://wiki.archlinux.org/title/General_recommendations)
pages are the reference for which maintenance topics are worth covering — filtered
through the in/out-of-scope boundary above. New maintenance work should trace back
to a concrete topic on those pages rather than diverging from them.
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

#### Externally-sourced

Standards defined outside sysforge, which the project conforms to.

| # | Standard | Scope | Status | How it is enforced |
|---|----------|-------|--------|--------------------|
| 1 | [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/) | User dirs (`~/.config`, `~/.cache`, `~/.local/state`, `~/.local/share`) | enforced | `primitives/paths.py` (`_xdg_base`); `check_standards` `paths` group; `tests/test_paths.py` |
| 2 | Filesystem Hierarchy Standard + systemd `file-hierarchy(7)` | System roots (`/etc`, `/var/lib`, `/var/cache`, `/run`) | enforced | `paths.py` (`CONFIG_BASE`), `pipeline/state.py`, `makepkg_pgo.py`; `check_standards` `paths` group |
| 3 | [Semantic Versioning 2.0.0](https://semver.org/) | Project version scheme **and declared bump selection** | enforced | Two facets. **Format + cross-file parity**: `tools/check_shipped.py` `versions` group. **Bump selection** (§§6–8: patch for a compatible fix, minor for a compatible addition, major for an incompatible change): the required bump is derived from the release-notes accumulator's Keep a Changelog sections (row 13) — a `**Breaking:**` bullet forces major, `## Added` minor, the rest patch — and `tools/release.sh` preflight refuses a `--bump` weaker than the derived value. `## Removed` is the one section the heading alone cannot settle, because row 24 splits a removal two ways: deleting a `compat` surface breaks a configuration that currently works (major), while deleting a `shim` deletes something that already failed (minor). `derive_bump` therefore resolves each `## Removed` entry against the deprecation registry and weakens to minor only when the entry names at least one known surface and every surface it names is a `shim`. The record is deleted by the same commit that does the removal, so `_historical_registry` recovers it from the last revision of `deprecations.py` that carried it (`git show` + the same AST reader — no tombstone table to keep in sync), newest revision winning and the working tree applied last. Every failure mode — no repo, no git, an unrecognised surface, one that predates the registry — lands on major: **the derivation only ever guesses in the strengthening direction.** This is what makes row 24's shim/compat distinction operative rather than decorative; before `3.0.0-STD3` the two rows contradicted each other, since row 24 mandates the very `## Removed` entry row 3 read as unconditionally breaking, printing the derived value with the evidence line that produced it so the inference is auditable. Planning-time counterpart: every `ROADMAP.md` Planned entry carries a `Bump:` tag (`tools/gen_roadmap_table.py`, sole parser). `make next-bump` prints the derived value; `check_standards` `semver_bump` group + `tests/test_check_standards_bump.py` |
| 4 | POSIX Utility Conventions + GNU long-options | CLI argument grammar (`-h/--help`, `-V/--version`, `--`) | followed | argparse in `cli.py`; `tests/test_standards_compliance.py` |
| 5 | [NO_COLOR](https://no-color.org/) + `FORCE_COLOR` | Terminal colour control | enforced | `log.use_color()` (single authority); `tests/test_standards_compliance.py` |
| 6 | stdout/stderr separation + exit-code contract | CLI behaviour (data→stdout, diagnostics→stderr; 0/1/2) | followed | `log._out()`, `verbs/runner.py`; `tests/test_standards_compliance.py` |
| 7 | [TOML 1.0.0](https://toml.io/en/v1.0.0) | Config + state file format | followed | `tomllib` everywhere; `check_shipped` `configs` group |
| 8 | RFC 3339 / ISO 8601 (UTC) | Timestamps in state files | followed | central `_now_iso()` helpers; `tests/test_standards_compliance.py` |
| 9 | UTF-8 | Text file encoding | enforced | explicit `encoding="utf-8"`; `check_standards` `encoding` group (ruff `PLW1514 --preview` is the one-shot fixer) |
| 10 | PEP 517 / 518 / 621 / 508 | Python packaging metadata | followed | `pyproject.toml` (hatchling backend, `[project]` table) |
| 11 | `PKGBUILD(5)` · `.SRCINFO` · `alpm-hooks(5)` · `makepkg.conf` + [Arch package guidelines](https://wiki.archlinux.org/title/Arch_package_guidelines) / [VCS package guidelines](https://wiki.archlinux.org/title/VCS_package_guidelines) | Arch packaging artefacts + conventions; **also** the on-disk shape (`.hook` sections/keys) of *user-authored* pacman hooks that the artifact inventory discovers, adopts, and deploys — sysforge inventories these, not only ships its own | enforced | `pkgbuild-spec-check`/`pkgbuild-edit` skills; `check_shipped` `pkgbuild`/`hooks` groups; `primitives/artifacts.py` (`CLASS_HOOK` discovery/deploy); `check_shipped` `config_comments` group extends this to the shipped configs' *prose*: no comment may name a `*.toml` or a `[section]` that does not exist, and a key whose validator accepts multiple surface forms must show every form (`_GRAMMAR_DOCS`, a hand-maintained table — widening a `_coerce_*` grammar updates it in the same commit, since no static signal distinguishes an accepted-form branch from any other conditional) |
| 12 | `man-pages(7)` via scdoc | Manual page | enforced | `make man`; `check_shipped` `manpage` group |
| 13 | [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) | Release notes | enforced | `docs/release-notes/vX.Y.Z.md` + `unreleased.md` accumulator category vocabulary; `check_standards` `changelog` group |
| 14 | [REUSE](https://reuse.software/) / SPDX (license: **MIT**) | Per-file licensing | enforced | SPDX headers + `LICENSES/MIT.txt` + `REUSE.toml`; `check_standards` `spdx` group (`reuse lint`) |
| 15 | Reproducible builds | Builds SysForge produces | followed | does not strip reproducibility OPTIONS / honours `SOURCE_DATE_EPOCH`; `tests/test_standards_compliance.py` |
| 16 | OpenPGP signing (RFC 4880) + makepkg `validpgpkeys` | Release provenance (signed commits, tags, tarball) | followed | `tools/release.sh` (signing preflight + `git tag -s` + tarball `.asc`); `check_shipped` `pkgbuild` group (`validpgpkeys` + signature-aware `SKIP`); verified downstream by `makepkg` |
| 19 | [`systemd.resource-control(5)`](https://www.freedesktop.org/software/systemd/man/systemd.resource-control.html) (cgroup-v2 `CPUQuota`/`MemoryMax` via `systemd-run(1)`) | Build resource enforcement (CPU/memory ceilings on the makepkg fork tree) | enforced | a configured `cpu_quota`/`mem_limit` is enforced by a kernel-level cgroup `systemd-run --scope` (hierarchical over all build descendants), not solely an escapable `RLIMIT_AS` preexec, whenever `systemd-run` is available; `primitives/build_throttle.py` (`wrapper_argv`/`_scope_owns_mem_cap`/`resolve_child_mem_cap`); `tests/test_standards_compliance.py` |
| 20 | [`systemd.journal-fields(7)`](https://www.freedesktop.org/software/systemd/man/systemd.journal-fields.html) + native journal socket protocol (`sd_journal_send(3)` wire format) | System-mutating operations mirrored to the journal | enforced | every sentinel-gated verb emits one structured record (`SYSFORGE_VERB`/`SYSFORGE_TARGET`/`SYSFORGE_EXIT` + `MESSAGE`/`PRIORITY`/`SYSLOG_IDENTIFIER`), additively alongside the unified run-log, no-op when journald absent; `SYSFORGE_TARGET` is supplied by every mutating verb via `Verb.journal_target` and namespaced `pkg:<names>` / `mode:<subcommand>` (one-home formatters `journal.pkg_target`/`journal.mode_target`); `primitives/journal.py` (`journal_send`/`record_verb`), `verbs/runner.py`; `tests/test_standards_compliance.py` + `tests/test_journal.py` |
| 21 | `systemctl(1)` unit lifecycle (`daemon-reload`, `is-enabled`, `disable --now`) | Deploying/removing a user-authored systemd unit through the artifact inventory | enforced | `primitives/artifacts.py` (`post_deploy` runs `daemon-reload` after a unit write via `run_privileged`; `unit_is_enabled` queries `systemctl is-enabled --quiet` unprivileged; `pre_remove` runs `systemctl disable --now` before unlinking an enabled unit); `tests/test_artifacts.py` |
| 22 | `pacman -Qk`/`-Qkk` package-file verification against libalpm's stored `mtree` | Verifying package-owned files still match what the package declared (existence, size, mode, hash, type) | followed | read-only `doctor --integrity` axis consumes `pacman -Qkk` with pacman's own backup-vs-altered classification (backup-array edits → `info`, non-backup drift → `warn`, missing → `error`, mtime-only → `info`); run unprivileged, access-error reasons (`failed to calculate SHA256 checksum`, `Permission denied`) are stripped before classification — a path with only access-error reasons is access-limited, not drift, and rolls into one counted `integrity_partial_coverage` `info` advisory rather than a per-path finding, while a path with genuine signal alongside an access error keeps its real drift severity; `primitives/pkgfiles_probe.py` (`collect_integrity_findings`); `tests/test_pkgfiles_probe.py` + `tests/test_standards_compliance.py` |
| 23 | [`os-release(5)`](https://www.freedesktop.org/software/systemd/man/os-release.html) | Arch-derivative portability: repo, toolchain-default, and distro-identity assumptions on an Arch-derived host | enforced | Three sub-invariants. **(a) No hardcoded sync-repo names** — a derivative carries its own sync DBs, often ordered ahead of `core`/`extra`; the `["core", "extra"]` literal in `primitives/pacman.py` is the sole allowlisted occurrence and only as an I/O fallback when `/etc/pacman.conf` is unreadable. Repo membership is *asked* of pacman (`aur.repo_packages` → `pacman -Si`), never inferred — this is the `build_core.prepare_deps` repo-vs-AUR makedep split, the failure class behind the exit-8 regression at `build_core.py:268`. **(b) The system `makepkg.conf` is the merge baseline, never replaced** — `config.SYSTEM_MAKEPKG_CONF` / `config.parse_system_makepkg_conf` is the only reader of that path (values returned verbatim, unnormalized), and `primitives/makepkg_conf.py::emit_makepkg_conf` both loads the system assignments and emits them, substituting profile keys inline, so a derivative's own `-march`/LTO defaults survive profile-key override intact. **(c) Distro identity is read from `os-release(5)` through one primitive** — `primitives/os_release.py`: `/etc/os-release` then `/usr/lib/os-release`, shell-style `KEY=value` with quote stripping, `ID` defaulting to `linux`, `ID_LIKE` as the space-separated parent list; never inferred from `pacman.conf` section names, `/etc/arch-release`, `/etc/lsb-release`, or a hostname. Surfaced by the `doctor --distro` axis, which reports the support tier (Arch = primary; `ID_LIKE=arch` = derivative, packaging invariants validated; otherwise `warn`) and never contributes an error, so a support tier cannot change doctor's exit code. Scope note: the row forbids *assumptions*; it does not introduce per-distro code paths — sysforge has no distro-conditional behaviour. `check_standards` `distro_portability` group (all three, statically) + `tests/test_standards_compliance.py` + `tests/test_distro_portability.py` (behavioural, synthetic derivative input) + `tests/test_os_release.py`; validated against a real derivative each minor by the container tier (`make container-smoke-cachyos`, release preflight section 9) |

#### SysForge-exclusive

House policies with no external specification. They are enforced exactly like
the rows above and share one global counter with them — the next row is 27
regardless of which subsection it joins, because the number is the row's
identity (it is cited from code, tests, and published release notes) while the
subsection is only presentation.

| # | Standard | Scope | Status | How it is enforced |
|---|----------|-------|--------|--------------------|
| 17 | Subprocess-seam discipline (argv-list execution) | External-command execution (all `subprocess` sites) | enforced | argv-**list** form only, `shell=True` needs justified `# noqa: S602`; `primitives/run.py` (`run_or_raise`) sanctioned seam, direct callers a documented carve-out for streaming/returncode/stdout-parsing; ruff `S602` + `check_standards` `run_seam` group |
| 18 | Privilege-escalation seam | Root-escalating subprocess invocations | enforced | `primitives/privilege.py` (`privileged_argv`/`run_privileged`) is the sole home for `sudo`-prefixed escalation; raw `["sudo", …]` argv outside it is forbidden except the allowlisted auth-probe (`sudo -v`, `sudo -n true`) and drop-privilege (`sudo -u <user>`) forms; `check_standards` `privilege_seam` group + `tests/test_standards_compliance.py` |
| 24 | Deprecation registry (declared removal version + warn-on-use) | Every config key, state token, CLI flag, or path sysforge still honours only for backwards compatibility | enforced | `primitives/deprecations.py` is the single home: each record carries `surface`/`kind`/`function`/`deprecated_in`/`removed_in`/`replacement`, and each compat read path calls `warn_used(surface)` so the warning text is built from the record and cannot drift from the version the gate enforces. Two `function` values, because removal is not uniformly breaking — a `compat` surface still works (removing it is breaking, so `removed_in` must be `X.0.0`, and presence is proven by its `warn_used` call sites), while a `shim` already fails and is kept only to name its replacement (removal is not breaking, `removed_in` may be `X.Y.0`, presence proven by an exact repo-relative `anchor`). `check_standards` `deprecations` group: registry↔call-site bijection both ways, major-only removal for `compat`, and an error when the release target is at or past a declared `removed_in` with the surface still present (`--target-version`); a registry that parses to zero records is itself an error, because a check that cannot fail is worse than no check. The release-note removal parity check matches the registry's literal surface string against the `## Removed` section body, so a removal note must spell the canonical identifier (e.g. `build_state.build_mode=profiled`), not just prose describing it. `tests/test_deprecations.py` (behavioural, incl. once-per-run dedup and per-surface resolution) + `tests/test_standards_compliance.py`. **First application:** the 2.6.1-F5 sweep removed all five `compat` surfaces the registry catalogued at `2.6.1-STD2` shipping time, proving the major-only removal gate end to end. The `shim` half was proven the same way at `3.0.0-STD3`: `doctor.flat_flags` declared `removed_in = 3.1.0`, and the release-target gate refused the 3.1.0 release until the hint table, its `cli.py` hook and the record itself were deleted — a scheduled removal the gate enforced rather than a reviewer remembering. One record now ships: the `compat` `profiles.build_mode=patched_pkgbuild` (`2.6.1-STD3`, due out in `4.0.0`) — `primitives/profile.py`'s `_LEGACY_BUILD_MODE_ALIASES` (`patched_pkgbuild` → `source_built`), a `profiles.toml` `build_mode` token honoured on read through `normalize_build_mode`, whose alias branch now calls `warn_used`. It predates the registry and `2.6.1-STD2` missed it, because the bijection walks registry→call-site and never code→registry: **an unregistered compat surface is invisible to the gate by construction.** A catalogued-empty `compat` half is therefore never proof that none exist, and finding the next one is a review obligation, not a tooling guarantee — a code→registry direction is unimplemented (no reliable static signal distinguishes a compat alias table from any other lookup dict). Its `removed_in` is `4.0.0` rather than `3.0.0` because 3.0.0 ships with the surface present, and the gate refuses a release at or past a declared removal with the surface still live. |
| 25 | Log-level rubric (UI / ERROR / WARN / INFO / DEBUG) | Every call site that emits user-visible output | followed | `docs/design/12-logging.md` is the single home: the five-level table (fn, gate, reserved-for), the decision test that resolves most call sites (*is this the answer, or narration about producing the answer?*), and the explicit anti-pattern that `ui()` is not a "make this always show up" escape hatch — `ui()` is the answer the user ran the command to see, `info()`/`warn()` are narration and are gated behind `-vv`/`-v`. Enforcement is deliberately partial and the row does not claim otherwise. The one mechanical guard is the `quiet_at_default` fixture (`tests/conftest.py`), a golden-output regression that runs a stage at the shipped default (`verbosity=0`) and again at `-vv`, and fails if the default run emitted an `[INFO]` or `[WARN]` line. That catches the failure mode that matters — narration leaking into always-visible output — but nothing checks WARN-vs-INFO or INFO-vs-DEBUG classification, which stays a review obligation. Coverage is per-stage opt-in: the fixture is anchored on `packages` (`_load_packages`, `tests/test_stage_packages.py`) and `configure`'s dry run (`tests/test_stage_bootstrap.py`). A stage added without requesting the fixture is unguarded by construction, so **coverage thins with every stage that does not opt in** — requesting it is part of adding a stage, not a later cleanup. |
| 26 | Lint scope (every Python tree in the repo) | `sysforge/`, `tests/`, and `tools/` — all Python the repo owns, not only the shipped package | enforced | `make lint-py` runs `ruff check sysforge/ tests/ tools/`, and `.claude/hooks/ruff-on-edit.sh` blocks on the same three trees. The two must name the identical set: before 2.6.1-STD8 the gate covered `sysforge/` alone while the hook already blocked on `tests/`, so 189 violations accumulated in a tree nothing reported on and the hook fired on pre-existing debt in files an author had merely touched — enforcement strict enough to interrupt, scoped so it could only ever surface someone else's backlog. Rule selection is uniform (`[tool.ruff.lint] select`); the only per-tree relaxation is `[tool.ruff.lint.per-file-ignores]` `"tests/**" = ["S", "PTH", "SIM117"]`, each with a stated reason — `S` because test code is not production attack surface (2.3.0-F1), `PTH` because it fires on the `sys.path` import bootstrap repeated across ~25 test files, `SIM117` because collapsing stacked monkeypatch/mock context managers costs a line-length violation per test and reads worse. A new relaxation is a per-file-ignores entry with a comment, never a directory dropped from the gate. Scope note: `tools/` is deliberately **not** relaxed — it holds `check_standards.py`, `check_shipped.py`, and `release.sh`'s Python siblings, which the release process depends on. |

### Notes on selected standards

**XDG / FHS (1, 2).** User-side roots resolve through `_xdg_base(env, default)`
in `paths.py` — config under `$XDG_CONFIG_HOME`, regenerable cache under
`$XDG_CACHE_HOME`, fallback runtime state under `$XDG_STATE_HOME`, and
authoritative user-authored data under `$XDG_DATA_HOME`. System state
lives at `/var/lib/sysforge` (FHS application state) with the XDG state dir as a
non-root fallback; the regenerable PGO profdata cache lives at
`/var/cache/sysforge` (override: `SYSFORGE_PGO_STORE`). See **Config Layer** and
**Directory Structure**.

**SemVer (3).** Versions are strict `X.Y.Z`; the `-git` package carries the
`X.Y.Z.rN.gHASH` VCS suffix. `make release-{major,minor,patch}` is the only
bump path and keeps `pyproject.toml`, `PKGBUILD`, `PKGBUILD-git`, and the
`<!--version-->` doc markers in lockstep.
Which of the three bumps is correct is not a judgement call: it is derived from
the accumulated release notes and enforced in preflight (see row 3's enforcement
column). A ROADMAP entry declares its expected impact via its `Bump:` tag, but
that tag is gone by release time — the landing commit removes the entry — so the
accumulator is the authoritative record. Removing a deprecated surface is the
common cause of a forced major; see row 24.

**Keep a Changelog (13).** `docs/release-notes/vX.Y.Z.md` *is* the changelog
(there is no separate top-level `CHANGELOG.md` to drift). Entries use the Keep a
Changelog category headings: `Added`, `Changed`, `Deprecated`, `Removed`,
`Fixed`, `Security`. Those
headings are also load-bearing for versioning: the required SemVer bump is
derived from which of them the accumulator carries (row 3).
Notes are authored **incrementally**: each landing commit
that completes a ROADMAP item appends its entry to the running accumulator
`docs/release-notes/unreleased.md` in that same commit. At release,
`tools/release.sh` Phase 1 renames the accumulator to `vX.Y.Z.md`, stamps its
`# ` title with the version + ISO date, and reseeds a fresh accumulator; the
`release-notes` skill only reconciles/lints the accumulated entries. The
`changelog` check lints `unreleased.md` under the same vocabulary so entries are
validated as they land, not just at release.

**REUSE / SPDX (14).** SysForge is MIT-licensed (`LICENSE`). First-party source
files carry per-file SPDX headers (a copyright tag plus the `MIT` license
identifier); generated/data files are covered in bulk by `REUSE.toml`; license
texts live under `LICENSES/`. `reuse lint` (when installed) is the authoritative
check, with a header-presence grep fallback.

**Reproducible builds (15).** SysForge must not *undermine* the reproducibility
of packages it builds: it does not inject non-deterministic data, preserves
reproducibility-relevant `OPTIONS`, and passes `SOURCE_DATE_EPOCH` through to the
build environment unmodified.

**Arch packaging (11).** Two tiers underlie this row. The **machine-checkable
specs** — `PKGBUILD(5)`, `.SRCINFO`, `alpm-hooks(5)`, `makepkg.conf` — are
guarded by `check_shipped` (`pkgbuild`/`hooks` groups) and the `pkgbuild-*`
skills, and are the authority any parser/patcher change cross-checks (array vs
string fields, escape and brace-expansion rules, the `_<arch>` array families).
SysForge doesn't just ship `alpm-hooks(5)`-conforming files — it *fires* them:
the four libalpm PostTransaction hooks (kernel, toolchain, buildstate,
artifacts) each drop a sentinel that a corresponding `update` consumer picks up
on the next run (§update.md). `check_shipped`'s `hooks` group parity check
(`check_hooks`) already covers the new `artifacts` hook via `HOOK_NAMES`, so no
enforcement wiring beyond the existing group was needed.
Layered on top are the **prose conventions** that aren't mechanically lintable
but inform how SysForge generates and edits PKGBUILDs:

- Package guidelines — <https://wiki.archlinux.org/title/Arch_package_guidelines>
  (and the per-language sub-pages) — naming, `pkgrel`/`epoch` semantics, split-package
  layout, the `provides`/`conflicts`/`replaces` triad. The split-package handling
  (`match_rules` matching `pkgbase`, the `-sysforge` rename keeping every
  `package_<name>()` function) follows from here.
- VCS package guidelines — <https://wiki.archlinux.org/title/VCS_package_guidelines>
  — `-git` naming, the `pkgver()` auto-bump, full-history fetch (never `--depth=1`,
  which makes every advance look diverged). SysForge's `vcs_pkgver`/`source_sync`
  invariants implement this.
- Authoritative manual pages: [PKGBUILD(5)](https://man.archlinux.org/man/PKGBUILD.5),
  [makepkg(8)](https://man.archlinux.org/man/makepkg.8), and the upstream
  [package-guidelines manual](https://manual.archlinux.page/package-guidelines/).

These are reference conventions, not a separate gate — they back the existing
parser/patcher invariants in `sysforge/CLAUDE.md` (PKGBUILD
parsing/detection/patching, source-sync) rather than adding a parallel check.

**Privilege-escalation seam (18).** Escalation is `sudo`-based and per-operation;
`privileged_argv` makes the single "prepend sudo unless already root" decision so
there is one audit point. Auth probes (`sudo -v`, `sudo -n true`) and
drop-privilege (`sudo -u`) are not escalation and are allowlisted structurally.
Polkit/`pkexec` was evaluated and declined for this tool's TTY-bound execution
model; the seam is the insertion point should that change. See §22.

**CLAUDE.md citation freshness.** Guardrail files (`CLAUDE.md` at the repo root
for process conventions; `sysforge/CLAUDE.md` for code-seam invariants, loaded
lazily per-directory) cite concrete paths and `module.symbol` seams. The
`check_standards` `claude_md` group verifies every backticked citation still
resolves — missing paths and renamed symbols fail; tokens that cannot be mapped
to a repo file are skipped (fail-safe, no prose false positives).

**Roadmap ID collision check.** The `check_standards` `roadmap_ids` group
cross-checks the three homes of one ID namespace: `ROADMAP.md` (open items),
`docs/ROADMAP-ABANDONED.md` (abandoned items — a retired number is never
reissued) and `docs/release-notes/` (shipped items). Errors: an open ID reusing
a shipped number, an ID listed both as Planned and as Abandoned (a cross-file
check since `2.5.1-F4` split the sections), an `## Abandoned` heading back in
`ROADMAP.md`, a shipped `Q`-typed ID. Warn: sequence gaps within the active
`pyproject.toml` version prefix. Allocate the next ID with
`python tools/check_standards.py --next-id <version>-<TYPE>`. Known limitation:
a release-note that mentions a still-Planned ID in prose (a forward or "see
also" reference) is indistinguishable from a shipped citation and will trip
the collision check (check 1) — author release-notes to reference only IDs
that actually shipped in that note.

**OpenPGP signing (16).** Releases are signed end to end with the maintainer key:
the `release: vX.Y.Z` commit (`commit.gpgsign`), an annotated tag (`git tag -s`,
verified with `git tag -v`), and a detached signature of the source tarball
(`sysforge-X.Y.Z.tar.gz.asc`) uploaded to the GitHub release. The stable
`PKGBUILD` declares the maintainer fingerprint in `validpgpkeys` and lists the
`.asc` as a second `source` (paired with `SKIP`), so `makepkg` verifies the
maintainer signature at install time. `tools/release.sh` preflight refuses to run
without a usable signing key, and refuses to publish while the placeholder
fingerprint sentinel is still in the PKGBUILD. See **Release Process** and the
*Verifying releases* section of `README.md`.

### Adding or changing a standard

This list has one home — this file. To add a standard: add a row (with its
enforcement mechanism), wire the mechanical check into `check_standards.py` or a
behavioural test, and update the `check-standards` coverage. Do not maintain a
parallel standards list elsewhere; CLAUDE.md points here.

**Update this table in the same commit as any change that adopts, extends, or
alters conformance to an external spec** — the same in-commit discipline as the
`docs/design/*.md → make design` doc-update rule. If a change starts honouring a
new spec (or a new facet of one already listed), add or extend its row and wire
the enforcement in that commit; a row must never lag the behaviour it records.
Conversely, do not add a row for a spec the code does not yet conform to — those
live in `ROADMAP.md` as `Q`/`F`/`STD` items until the adopting change lands (each
such roadmap entry names its target row here).

The same discipline applies to the **SysForge-exclusive** subsection, whose rows
have no external spec to adopt: add or extend the row in the commit that
establishes or changes the policy, and wire its enforcement there. Choose the
subsection by whether an external specification defines the behaviour, not by
whether the row happens to carry a URL — rows 6 and 15 are externally grounded
without linking a document. New rows take the next number in the single global
sequence shared by both subsections; never renumber an existing row, because row
numbers are cited from code, tests, and published release notes.

---

## Privilege-Escalation Seam

`primitives/privilege.py` is the sole home for "run this as root" (2.3.0-F10 /
STD row 18). Every root-escalating subprocess invocation across the codebase
(pacman, update, provides_lookup, fs_provision, makepkg_invoke,
makepkg_wrapper, kernel, packages, toolchain, reconfigure) routes its argv
through this module, so escalation has a single audit point and one
consistent "am I already root?" decision — no per-callsite `os.geteuid()`
branch, no hand-rolled `["sudo", ...]` list.

### Two entry points

- **`privileged_argv(argv, *, noninteractive=False) -> list[str]`** — builds
  the escalated argv and hands it back; the caller runs it with its own
  `subprocess.run` (or equivalent). Use this at sites that need to stream
  output to the TTY, inspect the return code themselves, or otherwise need
  control over how the command executes. When already root (`euid == 0`) the
  argv is returned unchanged (no `sudo` prefix); `noninteractive=True` inserts
  `-n` so `sudo` fails fast instead of prompting, when not already root.
- **`run_privileged(argv, *, tag, **kwargs) -> subprocess.CompletedProcess`** —
  escalates via `privileged_argv` and executes it through
  `primitives/run.py`'s `run_or_raise` (raise-on-failure). This is the
  available convenience for a new site that just wants "run this, raise if it
  fails" with no further inspection. Note that the migrated sites do **not**
  use it: each deliberately retains its own `subprocess.run` call to preserve
  its established error handling and — critically — the per-module
  `subprocess.run` monkeypatch seams the test suite relies on. `run_privileged`
  therefore currently has no callers; it exists as the sanctioned raise-on-failure
  path for future code, and routing an escalation through it must not replace a
  site whose tests patch that site's own `subprocess.run`.

This mirrors the streaming/returncode carve-out already established by
`primitives/run.py` for the general subprocess seam (STD row 17):
`run_privileged` is to `privileged_argv` what `run_or_raise` is to a raw
`subprocess.run` call.

### Escalate / probe / drop-priv taxonomy

Not every `sudo`-prefixed argv is an escalation, and the seam only owns the
escalation case:

- **Escalate** — an operation genuinely needs root (installing packages,
  writing to `/etc`, provisioning directories). This is what
  `privileged_argv`/`run_privileged` are for; every such site must route
  through them.
- **Probe** — checking or refreshing sudo credentials without running a
  privileged command: `sudo -v` (credential refresh) and `sudo -n true`
  (non-interactive "am I already authenticated" check). These aren't
  escalating anything and stay as raw calls.
- **Drop-privilege** — `sudo -u <user> ...` runs a command as a *specific
  non-root* user (e.g. building a package as an unprivileged build user from
  a root-invoked entry point). This is the inverse of escalation and also
  stays as a raw call.

The `check_standards` `privilege_seam` group (`tools/check_standards.py`)
enforces the boundary: it walks every `sysforge/*.py` file (except
`primitives/privilege.py` itself, the sanctioned home) for AST list literals
whose first element is the string constant `"sudo"`, and flags them as an
error **unless** the argv tail structurally matches one of the allowlisted
non-escalation forms above (`-v`; `-n true`; `-u <any>`). A raw `["sudo",
"pacman", "-Syu"]` outside `privilege.py` fails the gate; `["sudo", "-v"]` and
`["sudo", "-u", user, ...]` do not. See `tests/test_standards_compliance.py`
for both the fixture-based checker tests and the `privileged_argv` behaviour
test (root passthrough vs. non-root `sudo`-prefixing).

### Polkit non-goal

Polkit/`pkexec` was evaluated as an alternative escalation mechanism and
declined for this tool's execution model: SysForge's privileged operations
run interactively from a TTY (build/update/setup sessions), where `sudo`'s
credential caching and terminal-native prompt fit naturally, whereas `pkexec`
targets GUI-mediated one-shot authorization and would add a second prompt
mechanism without a matching need. The seam is deliberately the single
insertion point for escalation — if a polkit-based mechanism became
warranted later (e.g. a GUI front-end), `privileged_argv`/`run_privileged`
are where that swap would happen, without touching call sites.
