# SysForge Design Document

SysForge is an AUR helper for Arch Linux with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles, tracks build state for update detection, and automates the full build lifecycle — from fetching PKGBUILDs to installing profiled packages. Pacman owns the package database; SysForge owns the build configuration layer above it.

Current release is **<!--version-->v1.2.0<!--/version-->**. v0.1.0 shipped the profiled AUR helper surface (install, update, and manage AUR and custom packages with system-tuned profiled builds); v0.2.0 added VM tooling and install-path fixes on top; v1.0 rounds out the system-bootstrapper milestone — the full bootstrap pipeline (stages 1–4: partition, base install, hardware detection, configure) is implemented and a fresh Arch install is automated from the ISO. See the [Release Plan](#release-plan) for the shipped-vs-remaining breakdown.

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
9. [Primitives Layer](#primitives-layer)
10. [Flag Profile System](#flag-profile-system)
11. [Makepkg Wrapper](#makepkg-wrapper)
12. [Logging](#logging)
13. [Man Pages](#man-pages)
14. [Hardware Detection](#hardware-detection)
15. [Cache Management](#cache-management)
16. [Graphics Stack Build Order](#graphics-stack-build-order)
17. [Release Plan](#release-plan)
18. [Re-converge](#re-converge)
19. [Known Gaps](#known-gaps)
20. [V1.x Roadmap](#v1x-roadmap)
21. [V2 Roadmap](#v2-roadmap)

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

**Import direction:** `cli.py` → `verbs/runner.py` → command modules (`update.py`, `converge.py`, `packages_cmd.py`, `resolve.py`, …) → `primitives/*`. Each command module defines a `*Verb(Verb)` subclass alongside its existing helpers; the runner dispatches uniformly across them. No command module imports from another command module. See [CLI Verb Framework](#cli-verb-framework).

---

## Directory Structure

### Development (local repo)

```
sysforge/
├── sysforge/
│   ├── __init__.py
│   ├── cli.py                         # CLI entry point and subcommand wiring
│   ├── log.py                         # structured logging (stderr + optional file output)
│   ├── ui/
│   │   ├── __init__.py
│   │   ├── headers.py                 # shared visual primitives (welcome banner, stage banners, stage list, closing rule)
│   │   └── progress.py                # bottom-anchored batch progress indicator (TTY scroll region + plain fallback)
│   ├── resolve.py                     # sysforge resolve subcommand
│   ├── update.py                      # sysforge update subcommand
│   ├── build_core.py                  # shared build engine behind `build` + `update` (dep prep, build loop, install)
│   ├── converge.py                    # sysforge converge subcommand (flag drift detection)
│   ├── doctor.py                      # sysforge doctor subcommand (ABI/linkage health check)
│   ├── fetch.py                       # sysforge fetch subcommand (download PKGBUILDs, no build)
│   ├── packages_cmd.py                # sysforge packages namespace (list/add/remove)
│   ├── state_cmd.py                   # sysforge state namespace (list/repair) — build_state.toml
│   ├── setup_cmd.py                   # sysforge setup subcommand (pacman IgnoreGroup = sf-build guard)
│   └── primitives/
│       ├── paths.py                   # config path constants + resolve_packages_path()
│       ├── config.py                  # TOML config loading, path constants, system conf parsing
│       ├── pacman.py                  # pacman queries, batch install, makedep helpers
│       ├── profile.py                 # profile resolution, rule matching, consumes
│       ├── pkgbuild_meta.py           # static PKGBUILD parser (read-only)
│       ├── pkgbuild_patcher.py        # PKGBUILD mutation + flag extraction
│       ├── prompt.py                  # shared interactive-prompt helpers (every stage uses these)
│       ├── makepkg_wrapper.py         # build execution: emit conf, invoke makepkg
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
│   ├── test_converge.py
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
~/.config/sysforge/
    profiles.toml                    # user overrides (optional; merges with system via extends_system)
    cache/aur-packages.txt           # AUR name cache (regenerable, refreshed every 24h)
    state/                           # fallback runtime state when /var/lib/sysforge is not writable
/var/lib/sysforge/
    pipeline_state.toml              # pipeline checkpoint state (created at runtime)
    build_state.toml                 # per-package build metadata (created at runtime, by sysforge build/update)
    source_meta.toml                 # per-package AUR RPC + git HEAD snapshot used by the source_sync scheduler
    sysforge.log                     # unified log (created at runtime, cleared on success)
```

---

## Package Manifest

`packages.toml` is the **declared system manifest**. It plays two roles depending on context:

1. **Bootstrap (pipeline `run packages` stage):** every entry is installed. The manifest *is* the install list, because the system has nothing installed yet beyond the pacstrap base.
2. **Steady-state (`sysforge update`, `sysforge build`):** entries act as **build-rule overrides** applied to the live install set. Pacman owns the install set; `build_state.toml` mirrors it. An entry whose package is not currently installed is an inert rule, not a "missing" item.

This dual role is intentional: the manifest captures your declared intent, but at steady-state we respect the live system rather than reconciling against the manifest.

The orthogonality of the two roles means:
- An entry for `mesa-git` can stay in the manifest even if you've rolled back to repo `mesa` — it's an inert rule at steady-state, but the next pipeline bootstrap of a fresh system would still install it.
- An installed AUR package without an entry uses default rules — `sysforge update` still walks it via `pacman -Qm`, just with no overrides applied.
- `profiles.toml` and the manifest stay orthogonal: sourcing/patching choices vs. compiler flag tuning.

Each entry overrides at most these fields (all optional except `name`):
- `source` — `repo` (pacman) vs `aur`. Optional metadata; classification falls through to pacman / AUR RPC if omitted. Set explicitly only when classification is ambiguous or you want to force routing. `source` alone is **inert** and does not trigger any sysforge command path (matches the `packages add` validator); pair it with a behavior-changing field (`pkgbuild_patch`, `cache`, `reason`) if you want the entry to take effect.
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
- `repo_mode` — default build mode for repo-source packages: `"pacman"` (install via `pacman -S --needed`) or `"profiled"` (build from PKGBUILD with sysforge flag profiles). Per-package `pkgbuild_patch = true` overrides to profiled regardless. `sysforge update` walks repo packages only when a per-package override sets a behavior-changing field (`pkgbuild_patch`, `cache`, `reason`), or when `repo_mode = "profiled"` is set globally — in which case every installed repo package is in scope, but only the overridden subset is source-built; the remainder takes a fast pacman path (`checkupdates` for upgrade detection, one terminal `sudo pacman -Syu` after the source-build loop). This avoids the per-package `pkgctl repo clone` that would otherwise fire for every installed repo package and is what makes the "track everything" mode tolerable on a maintained workstation. The legacy `update_repo_profiled = true` flag is a deprecated alias for `repo_mode = "profiled"` — the loader normalises it with a one-shot warning.

### Manifest lifecycle commands

`sysforge packages` is a small namespace for managing override entries:

- **`packages list`** (default when no subcommand) — tabulates entries: name and any override fields set. `--orphans` lists entries whose package is not currently installed (informational only; entries are still valid rules).
- **`packages add <pkg> [--source ...] [--pkgbuild-patch] [--no-cache] [--reason TEXT]`** — adds or updates an override entry. Requires at least one of `--pkgbuild-patch`, `--no-cache`, `--reason` (the *behavior-changing* override fields); calls with only `<pkg>` or `<pkg> --source` are rejected. `--source` is metadata that pins routing (`repo` vs `aur`) — it doesn't satisfy validation on its own, since classification arrives at the same value automatically. Entries with no behavior-changing override are auto-pruned on the next `packages.toml` write-back (`add` or `remove`).
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

### Directory layout

SysForge uses two roots: one global (FHS-required pair) and one user-side (single dir).

| Location | Purpose |
|----------|---------|
| `/etc/sysforge/` | Shipped config (read-only, package-owned) |
| `/var/lib/sysforge/` | Runtime state (build_state, pipeline_state, source_meta, hardware_profile, sysforge.log) |
| `~/.config/sysforge/` | User config overrides + regenerable cache + state fallback |
| `~/.config/sysforge/cache/` | AUR name cache (refreshed every 24h) |
| `~/.config/sysforge/state/` | Fallback for runtime state when `/var/lib/sysforge` is not writable |

On first run, `sysforge` migrates legacy paths (`~/.cache/sysforge`, `~/.local/state/sysforge`) into the new `~/.config/sysforge/` subdirs. Migration is idempotent and best-effort — a failure logs a warning but does not block startup.

### State directory

Pipeline state is written to `/var/lib/sysforge/` by default. Override via the `SYSFORGE_STATE_DIR` environment variable or `--state-dir` CLI flag; CLI takes priority. Both are logged when present. `SYSFORGE_STATE_DIR` is a SysForge bootstrap var and is intentionally not subject to the build tool env isolation rule.

The configure stage creates a `sysforge` system group and sets the state directory to `root:sysforge` with mode `02775` (setgid: files written into the dir inherit the `sysforge` group). The recursive chown also normalises any state files written earlier in the same pipeline (`sysforge.log`, `pipeline_state.toml`) — without it, those files stay `root:root` and block the post-reboot `--resume` for the primary user. `open_unified_log` further `chmod 0o664`s the log on creation so future appends by other group members succeed. The builder user is added to the group during bootstrap; additional admin users can be added via `usermod -aG sysforge <user>`. If `/var/lib/sysforge` is not writable (e.g. standalone usage without bootstrap), the state dir falls back to `~/.config/sysforge/state`.

### Profile conf override

Both `sysforge build` and `sysforge pipeline` accept `--profile-conf FILE` to substitute an alternate `profiles.toml` at runtime, bypassing the default user/system search paths. The override carries flag profiles, conflict groups, and consumes inference together (all sections live in the one file). If the specified file sets `extends_system = true`, the standard system config is still merged underneath it via the normal `extends_system` logic.

### Global settings (`sysforge.toml`)

`/etc/sysforge/sysforge.toml` holds global settings that don't belong in flag profiles or package manifests. Loaded by `load_sysforge_toml()` in `config.py`; returns `{}` if the file is missing.

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `[ui]` | `editor` | — | Editor for reconfigure stage (overridden by `SYSFORGE_EDITOR` env) |
| `[git]` | `fetch_timeout` | `30` | Seconds before a shallow `git fetch` times out during source sync (0 = no limit). Legacy alias: `pull_timeout` |
| `[git]` | `clone_timeout` | `60` | Seconds before `git clone` / `pkgctl repo clone` times out (0 = no limit) |
| `[aur]` | `min_fetch_interval_ms` | `500` | Minimum gap between consecutive git fetches against aur.archlinux.org (millisecond resolution) |
| `[aur]` | `rate_limit_abort_s` | `300` | If AUR returns a `Retry-After` ≥ this many seconds, the remaining sync batch is aborted rather than waited out |

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
```

**Configure stage (stage 4)** runs all one-time system identity steps inside `arch-chroot`:
- Hostname (`/etc/hostname`), locale (`locale-gen`), timezone (`ln -sf /usr/share/zoneinfo/...`), keymap (`/etc/vconsole.conf`), `ParallelDownloads` in `pacman.conf`
- Reflector mirrorlist (skipped gracefully if `reflector` absent in chroot)
- systemd-boot: `bootctl install`, `loader.conf`, `entries/arch.conf` (uses `root=LABEL=root`)
- `systemctl enable NetworkManager` + `systemctl enable sshd`
- `PermitRootLogin yes` in `/etc/ssh/sshd_config`
- `useradd -m -G wheel <username>` + `/etc/sudoers.d/wheel` drop-in
- Shell dotfiles: `.bashrc` + `.zshrc` for root (red prompt) and primary user (green prompt)
- Root and user passwords via `chpasswd` (warns if absent from bootstrap.toml)
- sysforge install in target via `makepkg -si` from the source tree's PKGBUILD, run as the build user with a temporary `NOPASSWD` sudoers drop-in (removed after install). The configure stage stages the source as `sysforge-$pkgver.tar.gz` so makepkg uses the local copy instead of fetching, runs with `--skipchecksums --skipinteg` since the tarball is locally produced, and ends with sysforge owned by pacman (`pacman -Q sysforge`). This replaces the earlier `uv pip install --system` path, which left files unowned and forced `pacman -U --overwrite='*'` on the first AUR-driven update.

The hardware stage (stage 3) needs no config — it auto-detects and writes `hardware_profile.toml` to `state_dir`. After reboot the file is at its natural path (`/var/lib/sysforge/hardware_profile.toml`) and the kernel stage picks it up automatically.

**Full device inventory.** Beyond the scalar CPU/GPU/NVMe summary, the stage enumerates every PCI and USB device via `primitives/device_probe.enumerate_devices()` and appends a `[[devices]]` array-of-tables to `hardware_profile.toml` (bus, address, modalias, class, description, bound driver, expected modules, suggested `CONFIG_*`). The device→module link is resolved against a complete **reference kernel**'s `modules.alias` (newest installed stock kernel, excluding any `custom` modules dir) — a custom kernel that omitted a driver can't resolve the modalias it lacks, so resolving against the reference surfaces the gap. Any present, functional device with no driver bound is WARNed at the stage and pointed at `sysforge doctor --hardware`. The `[[devices]]` block is emitted after the scalar `[hardware]`/`[kconfig]` tables; existing readers (`tomllib`) are unaffected.

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
source           = "local"           # "local" (default) | "aur" | "git"
                                     # "local" = hand-maintained PKGBUILD, no remote sync.
                                     # "aur"/"git" = PKGBUILD is a clone of an AUR/git remote.

# Boot safety (defaults shown; see §Kernel stage boot-safety):
require_fallback_kernel = true       # refuse to install a custom kernel as the only kernel
boot_audit              = true       # run /boot-space + pre-install resolved-.config audit
min_boot_free_mb        = 200        # minimum free MiB on /boot before building
capture_lsmod_snapshot  = true       # capture lsmod for `make localmodconfig`

[[kconfig]]                      # manual kconfig overrides (optional, repeatable)
option = "CONFIG_HZ_1000"        # must match CONFIG_[A-Z0-9_]+
value  = "y"                     # y | m | n | non-empty string
```

`srcdir` is needed when the PKGBUILD directory name differs from `pkgname` (e.g. `pkgname = "linux-sysforge"` but the repo is cloned as `~/builds/linux`). Defaults to `pkgname` if omitted.

**Kernel-stage compiler override:** `compiler = "gcc" | "llvm"` is independent of the toolchain stage. A system that keeps gcc system-wide can still build the kernel with LLVM (or vice versa). Resolution order: `--compiler` CLI flag > `kernel.toml compiler` > toolchain-stage pipeline state (cc/cxx set by stage 6) > profile defaults. When set to LLVM, the standard `LLVM=1 LLVM_IAS=1` env vars are injected by `makepkg_wrapper` automatically — no extra PKGBUILD changes needed. Note: `compiler = "llvm"` builds the kernel *with* clang but does **not** apply PGO profdata — the profdata trains the clang binary, not the linux target, so there is no kernel-PGO path here.

**Resolution summary.** After resolving compiler (+ its origin), variant, bootloader (+ whether the chosen one is detected installed), source, interactive mode, kconfig counts, and the boot-safety gate settings, the stage emits a single labelled "Kernel build plan:" block (`_log_resolution_summary`). It prints on every run (useful before a multi-hour build) and is the readable core of `--dry-run`, replacing decisions previously scattered across the log. The standalone interactive default also emits a one-line nudge pointing at `--non-interactive` for unattended runs.

**Variant-inheritance nudge.** When `compiler` is unset (neither CLI nor `kernel.toml`) and the toolchain-stage variant is `pgo_llvm`, the stage emits a WARN naming the inherited variant and recommending that the operator persist `compiler = "llvm"` in `kernel.toml` so the choice survives a future toolchain-stage disable (which clears `[stages.toolchain.result]`). `stock_llvm` gets the same nudge at INFO level. `gcc` and `system` variants are silent — gcc is the safe default and `system` means there's no opinion to project.

**Configured-vs-installed toolchain mismatch.** The variant nudge above reflects what the toolchain *stage* registered in pipeline state; this check reflects on-disk reality. When `toolchain.toml` requests a custom LLVM toolchain (`enabled = true`, `compiler = "llvm"`) but the installed LLVM is stock repo (`install_origin == "repo"` — a custom build is never in a sync DB) or its PGO profdata is version-skewed, the stage emits a WARN before the build. It uses `llvm_state.detect_toolchain_config_mismatch`, which is built strictly on `collect_llvm_state` (the sanctioned LLVM-inspection entry point) — this is **provenance reporting**, deliberately *not* a third toolchain *health* probe (those remain `_verify_llvm_install` and `toolchain_preflight._probe_cc`). The same detector backs `sysforge doctor --toolchain`. There is intentionally no persisted "toolchain is correct" flag: it would go stale the moment pacman replaces LLVM out-of-band, so the mismatch is computed on read from current install state.

**Per-kernel toolchain-drift check.** Stage entry compares the installed kernel's recorded `toolchain_variant` (from `build_state.toml`) against the active variant. On mismatch (e.g. installed kernel was built under `stock_llvm`, active is `pgo_llvm`), the stage emits a WARN before the build runs. This mirrors `sysforge update`'s drift sweep but covers the kernel package, which `update` excludes via the stage-ownership skip. Back-compat: no recorded variant → silent (older builds preceded the field).

**Bootloader-installed preflight.** Stage entry probes for systemd-boot (`/boot/loader/loader.conf`) and grub (`/boot/grub/grub.cfg`); falls back to `pacman -Qq systemd grub` when neither marker is present. When the resolved `bootloader` (≠ `none`) isn't in the detected set, a single non-fatal WARN surfaces the mismatch *before* the build runs — so a user on a grub-only system who left the default `systemd-boot` configured gets an early signal instead of a post-install `bootctl update` failure. False negatives on exotic setups (UKI, custom loaders) don't block the build; the post-install branch still tolerates the bootloader-update failure.

**Pkgname/pkgbase consistency check.** After the source sync, the stage static-parses the PKGBUILD via `parse_pkgbuild` and confirms the parsed `pkgbase` (or `pkgname` for non-split packages) matches `kernel.toml pkgname`. A typo or a cloned PKGBUILD whose `pkgbase` has drifted from the directory name raises a clear `RuntimeError` at stage entry instead of failing late at `makepkg --install` after a multi-hour build.

**Pkgname repo-collision check.** Immediately after the consistency check, the stage tests `kernel.toml pkgname` against the pacman sync DBs via `aur.is_repo_package` (one `pacman -Si`). A custom kernel should carry a unique name; if the name matches an official package (e.g. `linux`, `linux-lts`), building and installing it would overwrite the stock package on `pacman -U`. Interactive runs prompt for confirmation (`prompt_choice`, default no); unattended runs (`--non-interactive` or no TTY) abort; `--dry-run` warns without prompting.

**kconfig fragment:**

Hardware-driven kconfig entries come from `hardware_profile.toml [kconfig]` (emitted by the hardware stage). These include both positive `=y` enables (CPU/GPU/NVMe-driven) and architecture-disable `=n` umbrellas — when the host is x86_64, the hardware stage writes `# CONFIG_ARM64 is not set`, the same for RISC-V/PowerPC/MIPS top-level keys and a curated set of ARM64 SoC families, culling unreachable subtrees from `make nconfig`. See §Hardware Detection → *Architecture-aware kconfig disable* for the registry. Manual overrides from `kernel.toml [[kconfig]]` are merged on top — manual wins on conflict with a `[WARN]`, including for arch-disable entries (a cross-compile use case can re-enable `CONFIG_ARM64=y` per the override path). The combined result is written to `<pkgbuild_src_dir>/<srcdir>/sysforge.config` before `makepkg` runs. The PKGBUILD must merge this into its `.config`; a compatible PKGBUILD calls `scripts/kconfig/merge_config.sh` in `prepare()`.

Manual override validation: `option` must match `CONFIG_[A-Z0-9_]+`; `value` must be non-empty (`n` to disable); duplicates within `kernel.toml` are an error.

If neither source provides any kconfig entries, no fragment is written. The fragment is written *after* the source sync (so a `--cleansrc` re-clone doesn't wipe it) and *after* compiler resolution, so its banner carries a toolchain-provenance line (`# toolchain variant: <variant>  cc: <path>`) giving a `.config` diff between two builds a trail of which toolchain produced it.

**Base config (`base_config`):**

The fragment is an *overlay* — it does not define the build's starting `.config`. `base_config` selects that base: `"pkgbuild"` (default, no-op — the PKGBUILD provides its own base), `"running"` (the running kernel's config, read via `dep_analysis.read_running_kconfig_text` from `/proc/config.gz` then `/boot/config-$(uname -r)`), or a path to a `.config` file. For `"running"`/`<path>`, sysforge writes the resolved config to `<pkgbuild_src_dir>/<srcdir>/sysforge.base.config` before the build (dry-run aware). The cooperation contract mirrors the fragment: a compatible PKGBUILD's `prepare()` copies `sysforge.base.config` to `.config` (then runs `make olddefconfig`) **before** merging `sysforge.config`. sysforge never mutates tracked source files. A `"running"` source that resolves to nothing (no `/proc/config.gz`, no `/boot/config-*`) warns and falls back to the PKGBUILD base; an unknown non-path value raises. The resolved source appears in the "Kernel build plan:" summary (`base cfg:` line).

**lsmod snapshot:**

Before the build, `lsmod` output is captured to `<state_dir>/lsmod.snapshot` (unless `capture_lsmod_snapshot = false`). This lets the PKGBUILD run `make localmodconfig` reproducibly using a fixed module set from the running system rather than whatever is loaded at build time. `localmodconfig` strips drivers for hardware *not loaded at snapshot time* — Gate 1 warns about this and Gate 2 (below) is the backstop that catches a dropped root-path driver before install.

**Interactive kconfig (kernel-stage default):**

`sysforge run kernel` is interactive by default — the kernel stage passes `interactive=True` into `BuildOptions`, so `patch_noninteractive_kconfig` is skipped and the PKGBUILD's kconfig target (`make nconfig`/`menuconfig`/etc.) runs as written. The user reviews and edits the resolved config before the build proceeds. The default can be flipped via `kernel.toml interactive = false` or the `--non-interactive` CLI flag; both routes patch interactive targets (`oldconfig`, `nconfig`, `menuconfig`, `xconfig`, `gconfig`) to `make olddefconfig` for unattended runs. `olddefconfig` applies defaults for all new symbols without terminal interaction; VAR=val arguments before the target (e.g. `ARCH=x86_64`) and trailing comments are preserved. `--noconfirm` only controls makepkg's own prompts and has no effect on interactive make targets inside the PKGBUILD.

Note: when other verbs (`sysforge build`, `sysforge update`) build a kernel PKGBUILD with `build_mode = "kernel"` on the resolved profile, those paths still default to *non-interactive* — interactive-by-default is a kernel-stage-only contract because the stage is the user-driven kernel build entry point.

**Source sync via the scheduler:**

The kernel stage routes its source refresh through `source_sync.get_scheduler().request(SyncRequest(..., source=<kernel.toml source>))` ahead of the build, the same path as the toolchain stage. With the default `source = "local"`, the scheduler short-circuits (no RPC, no clone, no fetch) — only `--cleansrc` / `--cleansrc-force` would attempt a purge, but a hand-maintained tree has no remote to re-clone from, so users on the `local` path leave cleansrc unset. For `source = "aur"` / `"git"`, the normal sync runs: `--cleansrc` purges and re-clones (refusing on dirty/ahead/no-upstream clones); `--cleansrc-force` overrides that guard; cleansrc forces a sync even when `--no-update` is also set. `STATUS_FAILED` / `STATUS_RATE_LIMITED` / `STATUS_PURGE_REFUSED` raise.

`STATUS_DIVERGED` (upstream advanced but the local tree can't fast-forward — local commits or a dirty tree) gets stronger handling in the *kernel* stage than the plain warning the other verbs use, because building a kernel off stale or hand-edited source is exactly the easy-to-miss footgun. `_warn_and_confirm_diverged` enriches the WARN with ahead/behind counts (`classify_head_vs_upstream`) so the "upstream has new commits but the local repo is dirty" case is spelled out, then **gates the build**: an interactive run must confirm (`prompt_choice`, default no), and an unattended run (`--non-interactive` or no TTY) aborts. Either decline raises, leaving nothing built (the sync runs before the sentinel). `--cleansrc` to discard local edits is the suggested escape hatch.

The source sync (including the `--cleansrc` purge, which `purge_src` does as a non-atomic `shutil.rmtree`) runs **outside** the boot sentinel by design: it mutates only the src tree, nothing boot-critical, so wrapping it in the sentinel — whose `recovery_cmd` is `sudo mkinitcpio -P` — would be semantically wrong. The atomicity contract is "purge, then clone"; an interrupted purge leaves a missing/partial PKGBUILD that fails **loudly** at `_pkgbuild_path` on the next run (with a hint to re-run `--cleansrc` to re-clone), not a silent brick. No sentinel is needed because the running kernel was never touched.

**Stage-ownership stamp:**

After a successful build, the kernel stage stamps `owner_stage = "kernel"` and `source = "local"` (or the configured value) into `build_state.toml` via `BuildOptions`. `sysforge update` honours that marker and skips the kernel package by default — the canonical update path is `sysforge run kernel`, not a sweep through `update`. Before the first kernel-stage build has written that stamp, the bootstrap fallback in `update.py` reads `kernel.toml`'s `pkgname` and applies the same skip; split sub-packages collapse to the kernel `pkgbase` via the same `get_pkgbase()` lookup that handles other custom-built split packages. `--include-stage-owned` overrides the skip; naming the package explicitly on the `sysforge update` command line is treated as an opt-in for that run.

**Kernel stage boot-safety.**

The kernel stage must never leave the machine unbootable. Three gates wrap the build/install, backed by `primitives/kernel_safety.py` (the policy — what aborts vs warns — lives in the stage; the facts live in the primitive). Brick-class findings (`is_brick=True`) hard-fail; everything else warns.

To make a *pre-install* hard-fail possible, the build is **split from the install**: the stage builds with `BuildOptions.no_install=True` (the profile's `-i`/`--install` flags are stripped via `INSTALL_FLAGS`), audits the resolved `.config`, then installs the produced artifact via `makepkg_wrapper.install_built_packages()` (a `sudo pacman -U` of the built `.pkg.tar*`). Without the split, Arch's pacman hooks (`kernel-install`/mkinitcpio) would build the initramfs and boot entry *at install time* — before any audit could run. The build mutates nothing and runs **outside** the sentinel, so a Gate 2 abort leaves the system completely untouched (nothing installed, no sentinel set).

- **Gate 1 — preflight (before the build).** Cheap, read-only. Hard-fails on a missing **fallback kernel** (no stock `linux`/`linux-lts` with a boot image — installing a custom kernel as the only kernel has no recovery path; override with `--allow-no-fallback` / `require_fallback_kernel = false`) and on a **missing/too-full `/boot`** (`min_boot_free_mb`; part of `boot_audit`). Captures the root topology (FS / storage transport / crypt-LVM-RAID from `/proc/mounts` + `lsblk -s` + `/etc/crypttab` + `/proc/mdstat`) for Gate 2. Advisory warnings: localmodconfig strip, DKMS rebuild reminder, mkinitcpio `HOOKS` vs root topology. In `--dry-run` the hard-fails downgrade to warnings.
- **Gate 2 — resolved-`.config` audit (after build, before install).** Reads the resolved `.config` from the build tree and runs `kernel_safety.audit_resolved_config(config, topology, devices)`. This is the only placement that sees post-merge / post-`olddefconfig` / post-`nconfig` state, so it's the single catch for a **Kconfig dependency cascade** (e.g. `CONFIG_SND_PCI=n` silently dropping `CONFIG_SND_HDA_INTEL`). Brick-class drops — root filesystem, root storage controller, core boot infra (`CONFIG_MODULES`/`BLK_DEV_INITRD`/`DEVTMPFS`/…), systemd prerequisites, crypt/LVM/RAID stacking — **abort before install** (override: `--skip-boot-audit` / `boot_audit = false`). Device-driver gaps (present PCI/USB device with no enabled driver, from `device_probe`) and console/framebuffer drops are advisory.
- **Gate 3 — boot-readiness (after install + mkinitcpio + bootloader).** `verify_boot_artifacts` confirms `vmlinuz-<pkg>` + `initramfs-<pkg>.img` are present, non-trivial, and referenced by ≥1 boot entry (systemd-boot loader entry or `grub.cfg`) — a missing entry means the kernel installed but cannot be selected (the `bootctl update` ≠ boot-entry trap). `check_dkms_for_kernel` flags DKMS modules not rebuilt for the new release (nvidia → black screen, zfs root → unbootable). Brick findings raise; running inside the sentinel, that leaves the sentinel set so the next run is prompted to recover.

**CLI surface (`sysforge run kernel`):**

`--dry-run`, `--no-update`, `--cleansrc`, `--cleansrc-force`, `--non-interactive`, `--compiler {gcc,llvm}`, `--bootloader {systemd-boot,grub,none}`, `--allow-no-fallback`, `--skip-boot-audit`, `--no-pkg-logs`, `--persist-log`, `--log-dir`, `--cache-report`, `--abi-check`, `--state-dir`, `--profile-conf`.

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
- Hardware-gated packages skipped if `hardware_profile.toml` is absent or key is missing
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
# Pass 2 outputs land in stage2 and are used as CC/CXX in Pass 3.
pgo_staging1 = "/var/tmp/sysforge-llvm-stage1"
pgo_staging  = "/var/tmp/sysforge-llvm-stage2"

# PGO data dir: profraw files written here during Pass 2, merged to clang.profdata
pgo_store   = "/var/tmp/sysforge-llvm-pgo"

# Build-safety Gate 1 (LLVM path only; see Build-safety gates below)
min_build_free_gb = 40    # min free GiB per build filesystem (override: --skip-build-space-check)
require_multilib  = true  # require [multilib] enabled when any lib32-* is in scope

# Package lists — all have sane defaults, override only if needed
[packages]
pgo     = ["llvm", "llvm-libs", "clang", "lld"]
non_pgo = ["polly", "compiler-rt", "openmp", "spirv-llvm-translator"]
lib32   = ["lib32-llvm", "lib32-llvm-libs", "lib32-clang", "lib32-spirv-llvm-translator"]
```

When `compiler` is unset (or set to `"gcc"`), the toolchain stage is **register-only**: it writes the system `/usr/bin/gcc` and `/usr/bin/g++` paths into pipeline state and returns without building anything. Stock `gcc-libs` from pacman's `base-devel` provides the runtime. The 4-pass PGO architecture below only kicks in for the explicit `compiler = "llvm"` path. Building GCC from source has no meaningful performance gains and is error-prone, so the stage doesn't own that path — use `pacman -S gcc gcc-libs` (already in `base-devel`) if you need to (re)install it.

**`skip_build = true`:** registers the system compiler paths in pipeline state without building anything. Downstream stages (packages, kernel) will use the system compiler. Useful when the system compiler is already optimized and no rebuild is needed.

**Build-safety gates + build/install split (kernel-parity).** The LLVM path mirrors the kernel stage's three-gate / build-install-split structure so a broken or doomed build can never leave the live `/usr` toolchain inconsistent. The pure, unit-testable facts live in `primitives/toolchain_safety.py` (`ToolchainFinding(severity, check_id, message, remediation, is_brick)`); the toolchain stage owns the abort/warn *policy*. `toolchain_safety` imports `LLVM_LOCKSTEP_SUITE` from `toolchain_preflight` (both primitives — no layering issue) and is **not** a third health-check entry point: `_verify_llvm_install` (pipeline, post-install) and `toolchain_preflight._probe_cc` (primitives, update path) remain the only two, and `_verify_llvm_install`'s skew arm now draws from `toolchain_safety.detect_suite_skew`.

- **Gate 1 — pre-build preflight** (`_gate1_preflight`, outside the sentinel, runs for both PGO and non-PGO). Brick-class aborts *before any build time is spent*: PKGBUILD pkgver skew across the lockstep suite (`check_pkgver_lockstep`; `spirv-llvm-translator` + `lib32-*` are excluded so their legitimately-different versions don't false-positive — the bug the old whole-set `_check_pkgver_consistency` had); a non-functional clang / missing lld (`smoke_test_compilers`, now run on the non-PGO path too); insufficient build-filesystem headroom (`check_build_space`, deduped by `st_dev`); `[multilib]` disabled while a `lib32-*` is in scope (`check_multilib_enabled`). Each brick is overridable (`--allow-version-skew`, `--skip-build-space-check`, `require_multilib = false`) and **downgraded to a warning in `--dry-run`**. Advisory (warn-only): residual `-fprofile-generate` instrumentation; an incomplete rollback snapshot.
- **Build (outside the sentinel).** All passes build with `install=False`; the build functions return the built package map. A build-pass failure therefore mutates nothing and leaves **no sentinel** (matches kernel).
- **Gate 2 — pre-install ABI audit** (`_gate2_audit`, outside the sentinel, both paths). Scans the built `.pkg.tar*` for the `_ZNSt*@LLVM_*` hazard via `toolchain_safety.scan_abi_hazards`. A brick aborts before any `pacman -U` — nothing installed, no sentinel.
- **Snapshot.** Right before the install, `cached_pkg_files_for(lockstep suite ∪ built names)` (in `primitives/pacman.py`) locates each currently-installed member's `.pkg.tar*` in the pacman cache. This is the offline-undo source. Gate 1 warns up front when any member's archive is missing (auto-undo will fall back to `pacman -S`).
- **Gate 3 — post-install verify, inside the sentinel** (`_verify_llvm_install`). On failure, the stage **auto-restores** the prior-good toolchain from the snapshot in one `pacman -U` transaction (`_rollback_to_snapshot` → `batch_install_pkgs`): if restore succeeds the live `/usr` is whole again, so the sentinel is **cleared** and a `RuntimeError` is raised telling the user to investigate; if restore fails or the snapshot was incomplete, the sentinel is **kept** with `recovery_cmd` set to the snapshot restore (offline `pacman -U <cached>`, falling back to `pacman -S <suite>`).

The mutation window is therefore exactly install → Gate 3 → (rollback). The concurrent-run lock (`_pgo_lock`) wraps the whole build → audit → snapshot → install window, like the kernel stage's `kernel-build.lock`. A consolidated resolution summary (`_log_toolchain_resolution_summary`) prints the compiler/pgo/variant, package counts, staging paths, gate settings, and snapshot availability — the readable core of `--dry-run`.

**PKGBUILD resolution:** follows `find_pkgbuild` lookup order (local `pkgbuild_src_dir` → `pkgctl repo clone`) for every package. After path resolution, the stage routes each unique resolved `pkgbuild_dir` through `SourceSyncScheduler.sync_many()` so missing trees are cloned and pre-existing trees are refreshed against AUR/repo upstream — same RPC short-circuit, rate-limit, and dirty-tree handling as `sysforge update`. Pass `--no-update` to skip the sync step (use whatever is on disk verbatim). Blocker statuses (`STATUS_FAILED`, `STATUS_RATE_LIMITED`, `STATUS_PURGE_REFUSED`) abort the stage; `STATUS_DIVERGED` is a warning. Resolved paths are then displayed in a table and the user is prompted to confirm or abort. On abort, the resume command is printed (`sysforge pipeline --resume --state-dir <dir>`) so they can make manual modifications and return.

**LLVM PGO bootstrap (only when `pgo = true`):**

Every PGO pass runs makepkg with `--cleanbuild --force` so the prior pass's `.pkg.tar.zst` in PKGDEST never short-circuits the next build (each pass produces a different artifact at the same pkgver). `makepkg` runs without `--install` — sysforge controls when (and where) Pass outputs land. **Only the final Pass 3 install touches `/usr`.** Earlier passes go to staging prefixes; the live system is never made ABI-incoherent mid-run. A sudo keepalive thread refreshes credentials every 60 seconds throughout the sequence (the final `pacman -U` still needs root). `llvm-profdata` is invoked with `RLIMIT_AS` lifted (`resource_guard.lift_for_child`) so it is not constrained by the sysforge controller's 2 GiB virtual address space cap.

The sequence is **four builds** (Pass 1a, Pass 1b, Pass 2, Pass 3) that produce two on-disk staging prefixes (`pgo_staging1` → `pgo_staging`) before the final install:

1. **Pass 1a** — instrumented build of the pgo packages (`llvm`, `llvm-libs`) with the system compiler + `-fprofile-generate=<pgo_store>/`. Every output is **extracted to `pgo_staging1`** (no `pacman -U`, no live-root mutation), including the cmake-config / static-lib `llvm` package — Pass 1b's `find_package(LLVM)` needs those configs. The instrumented `.a` archives that land alongside surface `__llvm_profile_*` link errors for anything that consumes LLVM component targets; Pass 1b and Pass 2 work around that by injecting the clang profile runtime into LDFLAGS (see below). Spurious profraw from CMake feature probes is purged before later passes begin.

2. **Pass 1b** — **non-instrumented** build of the non_pgo packages (`clang`, `lld`, `compiler-rt`, `polly`, `openmp`, `spirv-llvm-translator`) against stage1. The Pass 1b environment sets `CMAKE_PREFIX_PATH=<staging1>/usr` so `find_package(LLVM)` finds stage1's headers and configs; the resulting binaries link against stage1's `libLLVM.so` and are ABI-coherent with it. **LD_LIBRARY_PATH is deliberately NOT set** for Pass 1b — that would force the host `/usr/bin/clang` to load stage1's libLLVM and recreate the version-skew failure mode this design exists to prevent. `linker_flags_extra = _profile_runtime_ldflag()` adds `-L<runtime_dir> -lclang_rt.profile-x86_64` so the instrumented static archives' `__llvm_profile_*` references resolve at link time. Pass 1b outputs are extracted into the same `pgo_staging1`, making it **self-sufficient**: stage1 now has a working clang and a working libLLVM, both built from the in-tree LLVM source, both ABI-coherent.

3. **Pass 2** — training run. CC is `<staging1>/usr/bin/clang` (built in Pass 1b), and the Pass-2 environment redirects dyld / cmake at stage1 via `LD_LIBRARY_PATH=<staging1>/usr/lib:…`, `CMAKE_PREFIX_PATH=<staging1>/usr:…`, `PATH=<staging1>/usr/bin:…`. The running clang and the libLLVM it loads are guaranteed coherent because they were built together — no possibility of version drift against `/usr`. Pass 2 builds pgo + non_pgo packages; the act of running stage1's clang against stage1's instrumented libLLVM generates profraw as a side effect. `LLVM_PROFILE_FILE` uses `%m_%p` (per-module-hash + per-PID) so parallel `make -j` clang processes each write their own `.profraw` file rather than contending on one; `CCACHE_DISABLE` and `SCCACHE_DISABLE` are set so neither cache tool bypasses the instrumented compiler. `linker_flags_extra` carries the same profile-runtime LDFLAGS so Pass 2's non_pgo `find_package(LLVM)` builds against stage1's instrumented `.a` archives still link cleanly. A background daemon merges profraw into `clang.profdata` every 15 seconds using adaptive batch sizing (starts at 128 files; halves on OOM; minimum batch 8). No install. After the build, Pass 2 binaries are extracted to `pgo_staging` (stage2). The stage2 outputs are **non-instrumented** since Pass 2 doesn't apply `-fprofile-generate`.

4. **Pass 3** — final optimized build of pgo + non_pgo + lib32 with `-fprofile-use=<clang.profdata>`. CC selection is conditional on whether `<pgo_staging>/usr/bin/clang` exists: when the PGO package set includes `clang` (so Pass 2 produces a staged clang), Pass 3 uses `CC=<pgo_staging>/usr/bin/clang` and the env redirects dyld / cmake at stage2 (`LD_LIBRARY_PATH=<pgo_staging>/usr/lib:…`, `CMAKE_PREFIX_PATH=<pgo_staging>/usr:…`, `PATH=<pgo_staging>/usr/bin:…`) — the staged clang's NEEDED libLLVM is stage2's libLLVM, so the redirect is ABI-coherent. The shipped default config only PGO-builds `llvm`/`llvm-libs`; clang is in `non_pgo` and lives in stage1, never stage2. In that **system-clang fallback** path Pass 3 uses `CC=/usr/bin/clang` (Arch's stock clang, linked against the full-target live `/usr/lib/libLLVM.so.22.1`) and the stage2 dyld/cmake redirect is **suppressed** — pointing system clang at stage2's `LLVM_TARGETS_TO_BUILD`-restricted libLLVM via `LD_LIBRARY_PATH` would recreate the Pass 1b version-skew failure (symbol lookup errors for absent target init functions like `LLVMInitializeBPFTarget`). Either way, `LLVM_PROFILE_FILE` is cleared so any inherited Pass-2 training env can't leak. Because stage2's LLVM is non-instrumented, **no profile-runtime LDFLAGS injection is needed** here — `find_package(LLVM)` sees no `__llvm_profile_*` references, and leaving `linker_flags_extra` unset prevents the Pass 1b/Pass 2 residual flag from leaking into the final optimized binaries. This is the **only** pass that runs `sudo pacman -U` against `/usr`. Staging prefixes are removed only **after** the post-install verify passes (see below), so a failed verify still has the stage2 prefix on disk for diagnostic inspection. Profdata is **preserved** at `<pgo_store>/clang.profdata`; a version sidecar `clang.profdata.version` (LLVM major integer, e.g. `22`) is written alongside it so `sysforge update` can check compatibility before reusing the profdata.

**Pre-install ABI hazard check (Gate 2).** Between the Pass-3 build and the final `sudo pacman -U`, `_gate2_audit` extracts each built `.pkg.tar*`'s shared libraries and scans their `nm -D` output via `toolchain_safety.scan_abi_hazards` (which uses `abi_check._undefined_versioned`). Any UND versioned symbol whose mangled name is in the C++ stdlib namespace (`_ZNSt*`) and whose version starts with `LLVM_` is a hard block: it means Pass 2's instrumented stage2 `libLLVM` leaked `std::string` (or similar) exports under the `LLVM_X.Y` version namespace and Pass 3's linker bound libclang-cpp's references to them. Installing those binaries would leave the live toolchain unable to resolve `std::string` methods at runtime (`symbol lookup error: libclang-cpp.so: undefined symbol _ZNSt..., version LLVM_22.1`). Gate 2 runs *outside* the sentinel, so a hazard aborts with the live `/usr` intact and **no sentinel**; the user is told to restart with `--rebuild-profdata`. (This scan moved out of `_pgo_install` — which now only installs — and is shared with the non-PGO path.)

**ABI-safety invariant (Path B).** The live `/usr` is observably coherent before and after every step except the single final `pacman -U`, and even that is now reversible: Gate 3 verifies the result and auto-rolls-back to the snapshot on failure. A run that aborts before install (build failure, Gate 1, or Gate 2) leaves the system exactly as `sysforge run toolchain` found it — nothing installed, no sentinel. A run whose install verifies-bad restores the prior-good suite from the pacman cache. No half-installed instrumented `libLLVM.so`, no orphaned `/usr/bin/clang` that can't resolve `LLVMInitializeBPFTarget@LLVM_22.1`. The only role `/usr/bin/clang` plays in the run is **as a bootstrap host compiler in Pass 1b** (compiling source into objects, never loading a different-version libLLVM); version drift between the in-tree LLVM source and the installed system packages is therefore no longer a failure mode.

**Stage ownership (`sysforge update` skip).** The install-bearing final pass (Pass 3, or the single pass when `pgo = false`) stamps `owner_stage = "toolchain"` into `build_state.toml` via `BuildOptions` — mirroring how the kernel stage stamps `owner_stage = "kernel"`. `sysforge update` honours that marker and skips the LLVM suite by default, pointing the user at `sysforge run toolchain` instead of rebuilding `llvm`/`clang`/`lld`/`compiler-rt` mid-sweep. Intermediate PGO passes (1a/1b/2) leave the marker unset so their transient, soon-overwritten staging writes don't claim ownership. Before the first toolchain-stage build has written that stamp — and for build_state entries written by older sysforge versions that predate the field — the bootstrap fallback in `update.py` (`_toolchain_owns_llvm()`) reads `toolchain.toml` and applies the same skip, but **only when** the stage is `enabled` *and* `compiler = "llvm"` (the default/unset `gcc` path is register-only and owns no LLVM, so stock pacman LLVM stays pacman-class and is left alone). Ownership is the **union** of `is_llvm_pkgbase` (prefix match: `llvm`/`clang`/`compiler-rt`/`lld` + `lib32-`) and the explicit `toolchain.toml [packages]` lists read by `_toolchain_owned_pkgbases()`. The configured set is what catches members `is_llvm_pkgbase` doesn't match by prefix — notably `spirv-llvm-translator` (and any custom-listed package) — so they're skipped too, not just the prefix set. `--include-stage-owned` overrides the skip; naming an LLVM package explicitly on the `sysforge update` command line is an opt-in for that run. This is the exact analogue of the `kernel.toml` bootstrap fallback (see the kernel stage's stage-ownership note).

**Pass 1b skipped when `non_pgo` is empty.** Minimal configs (tests, intentionally-narrow rebuilds) can set `[packages] non_pgo = []`. In that case stage1 has no clang, and Pass 2 falls back to `/usr/bin/clang` — recreating the bootstrap-host-clang behaviour, where the user is responsible for keeping system clang ABI-coherent with the in-tree LLVM source. The non-empty default (clang/lld/compiler-rt/...) is the supported path.

**Dep resolution for staged passes.** Pass 1a builds against the live `/usr` and keeps the profile-supplied `--syncdeps`, so missing build tools (cmake, ninja, python, z3, libffi, …) are pacman-installed normally. Pass 1b, Pass 2, and Pass 3 build against a stage prefix; `CMAKE_PREFIX_PATH=<staging>/usr` makes `find_package(LLVM)` see the staged headers and cmake configs, but pacman has no knowledge of those staged packages. `_build_pass(staged_deps=True)` therefore strips `--syncdeps`/`-s` from the resolved profile's makepkg flags and appends `--nodeps` for those three passes. Without that, makepkg's pre-build dep check would invoke `sudo pacman -S llvm=<pkgver>` and fail with "target not found" (the just-built version is not in any repo). The non-llvm build deps stay required — they're expected to already be on the system from Pass 1a's `--syncdeps` install.

**Concurrent-run lock.** `ToolchainStage.run` acquires an advisory `flock(2)` (`_pgo_lock`, the shared `build_lock` primitive) on `_pgo_lock_path(staging1)` = `<pgo_staging1>.parent/sysforge-pgo.lock` (typically `/var/tmp/sysforge-pgo.lock`) around the whole build → audit → snapshot → install window — not just the PGO passes, so the non-PGO path is guarded too (mirroring the kernel stage's `kernel-build.lock`). The sentinel scope guards re-entry on the state-dir but not the `/var/tmp` staging dirs or `~/pgo`, both of which two concurrent runs would corrupt. The lock file holds the owner's PID, so the loser surfaces "another sysforge PGO build is running (pid N)" rather than a confusing mid-flow failure. The path is in `staging1.parent` rather than inside `pgo_store` so the Pass-1 purge cannot delete it. Skipped in `--dry-run` (the lock file would be a side effect).

**Post-install libLLVM resolution check.** `_verify_llvm_install` runs `ldd /usr/bin/clang` and `ldd /usr/bin/lld` and asserts that any `libLLVM*.so` lines resolve under `/usr/lib`. A `/var/tmp/sysforge-llvm-stage*` path appearing in `ldd` of an installed binary means Pass 3 packaged a bad RPATH or the install is incomplete — `/usr` looks consistent until `/var/tmp` gets cleaned, at which point the live toolchain silently breaks. The verify-stage check catches that before the sentinel clears.

**Verify-failure diagnostic dump.** On a `_verify_llvm_install` failure, `ToolchainStage.run` calls `_dump_stage_dynsym_evidence(staging, state_dir)` before the recovery prompt. It writes `nm -D --defined-only` of stage2's `libLLVM.so.*` to `<state_dir>/llvm_abi_hazard.log`, with a filtered "suspicious symbols" header listing every line matching `_ZNSt` — direct evidence of which exports leaked into stage2's libLLVM under the LLVM version namespace. Staging removal is deferred until verify passes, so the evidence directory survives the failure path. The log path is surfaced in the WARN block alongside the suggested recovery command.

**Profdata reuse:** before purging `pgo_store`, the stage checks for an existing `clang.profdata` + version sidecar. The sidecar's LLVM major version is compared against the `pkgver` in the pgo PKGBUILDs (not the installed version — the toolchain stage builds a *new* version). If compatible (same major), passes 1a–2 are skipped entirely and only the optimized build (Pass 3) runs, using system clang as CC (which, after a prior successful run, is already PGO-optimized). Staging is not needed in this path. `--rebuild-profdata` forces a full 4-pass build regardless, e.g. after upstream codegen changes within the same major version.

**Sidecar write timing.** The version sidecar is written **right after Pass 2 completes** (after the final profraw merge produces `clang.profdata`, before Pass 3 starts) — not after a successful Pass 3 install. The sidecar's only invariant is "this profdata is for LLVM major N", which is determined entirely by what Pass 2 instrumented; Pass 3 success has no bearing on it. Writing it post-Pass-2 means a Pass-3 failure (e.g. a transient toolchain bug, an aborted run) still leaves recoverable profdata that the next invocation can reuse via `_check_existing_profdata` rather than being forced into a full 4-pass rebuild. The major itself is derived from the in-tree PGO PKGBUILD `pkgver` (`_pgo_target_major`), matching the value `_check_existing_profdata` will later compare against — symmetric with the reuse check, and correct across major bumps where `pacman -Q llvm` would report a stale value.

**Confirmation gating (PGO).** Unlike the rest of sysforge (which is automation-focused), the LLVM PGO sub-flow is fragile enough that wrong profdata silently mis-optimises the resulting compiler. Four decision points in `_build_llvm_pgo` therefore prompt the user before destructive or long-running work, all sharing a single `_pgo_confirm` helper:

1. **Reuse vs rebuild** — when compatible profdata is found, prompt `[Y/n]` to reuse; declining triggers a full 4-pass rebuild (and continues into prompts 2–3).
2. **Purge `staging/` and `pgo_store/`** — prompt `[y/N]` before `rmtree`; declining aborts PGO.
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

**Variant-driven linker soft default.** `emit_makepkg_conf` injects `-fuse-ld=lld` into `LDFLAGS` when (a) `toolchain_variant in {"stock_llvm", "pgo_llvm"}`, (b) no explicit `ld_override` was passed, (c) the resolved `LDFLAGS` (profile first, then system conf) declares no `-fuse-ld=` linker, (d) `lld` is on `PATH`, and (e) this is not a kernel build. The defaults-not-overrides rule is the key invariant: a profile that already declares `LDFLAGS="… -fuse-ld=mold"` keeps mold, and an explicit `BuildOptions.ld_override` still wins (hard override beats soft default). Effect: any build path that flows through `BuildOptions.toolchain_variant` — `packages` stage, `sysforge update`, `sysforge build` — picks up the toolchain's linker without each caller having to repeat the propagation. The kernel build path is opt-out because kernel linker selection is controlled by `LLVM=1`, not LDFLAGS. `gcc` and `system` variants skip the injection so sysforge doesn't override the user's makepkg.conf when it has no LLVM opinion.

**Stale-state wipe (disabled / absent stage).** When `toolchain.toml` is absent or has `enabled = false`, the stage clears any prior `[stages.toolchain.result]` from pipeline state before returning. This prevents the failure mode where a user runs `compiler = "llvm"`, disables the stage, and subsequent `packages`/`kernel` stages keep using the stale `cc=/usr/bin/clang`/`ld=lld` overrides — the disable opts out of all downstream LLVM propagation, not just the build.

**Interrupted-install protection.** Three layers wired into the LLVM build path (the GCC path is register-only and skips all three). Note the sentinel now wraps only the install → Gate-3 → rollback window (the build runs before it, outside the sentinel — see *Build-safety gates* above):

1. **Stage sentinel** (`primitives/stage_sentinel.py`) — writes `<state_dir>/stage_in_progress.toml` just before the `sudo pacman -U` of the run and clears it after Gate-3 verification passes (or after a successful auto-rollback, since the system is then whole again). Schema records `stage`, `started_at`, `compiler`, `pgo`, and a `recovery_cmd` string. The recovery command is **snapshot-aware** (`_snapshot_recovery_cmd`): when every suite member's prior `.pkg.tar*` is cached it's an offline `sudo pacman -U <cached files>`; otherwise it falls back to the online `sudo pacman -S <suite>`. On every subsequent sysforge invocation, `cli.main()` calls `check_and_recover_stale_sentinel()` before dispatching install-bearing commands (`build`, `update`, `converge`, `run *`, `setup`) — the gate is centralised in `cli._gate_sentinel_check(args)`, which also skips read-only invocations (`--dry-run`) so users can inspect the system without first running recovery. If a sentinel is found, the operator is prompted to auto-run the recovery command (`[y/N]`). **TTY-only prompt:** when stdin is not a TTY (background sessions, scripts, IDE wrappers), the prompt would silently auto-decline; `check_and_recover_stale_sentinel` instead emits an explicit error naming the sentinel file path and the recovery command, then returns False. **Verify-after-clear:** after `sentinel.clear()` runs, the recovery path checks that `sentinel.path.exists()` is False before printing "Recovery completed". A still-present file means the recovery cleared a different path (state-dir mismatch, namespace/chroot surprise) — the path is logged loudly so the operator can investigate instead of trusting a false-positive cleared message. Refusing recovery exits with status 2 and leaves the sentinel in place; success clears the sentinel and proceeds.

2. **Post-install verification (Gate 3)** — after the `pacman -U` of the LLVM run, `_verify_llvm_install()` checks: (a) `pacman -Q` versions across `_LLVM_VERSION_MATCH_SET` (which *is* `LLVM_LOCKSTEP_SUITE` from `toolchain_preflight` — `llvm`/`llvm-libs`/`clang`/`lld`/`compiler-rt`/`polly`/`openmp`) all agree, via `toolchain_safety.detect_suite_skew` (the canonical interrupted-install symptom — a mismatched `llvm-libs` is the exact failure mode that produced Keith's broken GUI), (b) `clang --version` and `lld --version` invoke cleanly without missing-symbol errors, (c) `ldd` of installed clang/lld resolves libLLVM under `/usr/lib` (`_check_llvm_link_resolution`), (d) when `[llvm] targets` is configured, `llvm-config --targets-built` is a superset. On failure the stage **auto-rolls-back** to the pre-install snapshot rather than prompting (the kernel-parity overhaul replaced the old interactive `_prompt_llvm_recovery`): a successful restore clears the sentinel and raises "prior toolchain was restored"; a failed/incomplete restore keeps the sentinel with the snapshot recovery command. This verification is comprehensive and fatal, but only runs inside `run toolchain`; if a toolchain run is interrupted before it (or a later partial pacman transaction reintroduces a skew), the broken state can still reach an everyday `sysforge update`. That gap is closed by the `cc:<name>` compiler-health probe in `toolchain_preflight` (see §`toolchain_preflight.py`), which re-detects the suite-wide pkgver skew / non-runnable clang before any package builds — deliberately a lightweight independent check sharing the `LLVM_LOCKSTEP_SUITE` constant rather than importing this pipeline-layer verifier into the primitives layer.

3. **Clean-exit SIGINT scope** (`primitives/interrupt.py`) — wraps the LLVM build dispatch in an `InterruptScope` context. The first Ctrl-C flips a flag without raising; the build code checks the flag at safe boundaries (between `makepkg` runs, between PGO passes) and raises `CleanExitRequested` to exit at the next safe point, sentinel intact. A second Ctrl-C falls through to default `SIGINT` handling (immediate termination) — the operator explicitly chose the unsafe path. `CleanExitRequested` subclasses `BaseException` so it propagates through `except Exception:` blocks without being silently swallowed.

The sentinel-installation and clean-exit machinery is exposed as a shared `sentinel_scope()` context manager in `primitives/stage_sentinel.py` (see Verb Framework below) so the same install-bearing protection used by the toolchain stage is also available to other stages and standalone CLI verbs.

**Sentinel coverage map.** The primitive is now used at every install-bearing stage entry, not just the LLVM toolchain. Current callers:

| Caller | `stage_name` | `recovery_cmd` | Notes |
|---|---|---|---|
| `pipeline/stages/toolchain.py` (LLVM path) | `toolchain` | snapshot-aware: offline `sudo pacman -U <cached suite>`, else `sudo pacman -S <suite>` | Sentinel scoped to install → Gate-3 verify → auto-rollback (build + Gates 1–2 run outside it). Full three-layer protection (sentinel + verify + clean-exit). |
| `pipeline/stages/kernel.py` | `kernel` | `sudo mkinitcpio -P` (regenerates initramfs — the boot-critical step) | Wraps `makepkg --install`, `mkinitcpio -P`, and the bootloader regen. |
| `pipeline/stages/packages.py` | `packages` | _none_ (no single command restores a partially-installed package set) | Wraps AUR-dep build + per-package install loop. Per-package failures are state-tracked and don't preserve the sentinel; only an interruption / unexpected exception does. |
| `pipeline/stages/reconfigure.py` (`_try_install_editor`) | `reconfigure-editor` | _none_ | Single-package install; sentinel is cheap consistency with the larger stages. |
| `verbs/runner.py` (any verb with `requires_sentinel=True`) | _verb name_ | per-verb (`verb.sentinel_recovery_cmd(args, pre)`) | Currently `build`, `update`, `converge`, `state repair`, `state orphans --prune`, `state failed --clear`/`--clear-all`. |

The kernel and packages stage sentinels close the audit gap where an interrupted `pacman -U linux-custom` followed by an unfinished `mkinitcpio -P` could leave the system unbootable: the sentinel now persists across the makepkg → mkinitcpio → bootloader window, and the next sysforge invocation blocks at the CLI-entry recovery prompt with `sudo mkinitcpio -P` queued for auto-execution.

---

## CLI Verb Framework

Every top-level CLI verb (`build`, `update`, `fetch`, `converge`, `doctor`, `resolve`, `env`, `setup`, `log`, `packages …`, `state …`, `run …`) is implemented as a `Verb` subclass in `sysforge/verbs/base.py` and dispatched through `run_verb()` in `sysforge/verbs/runner.py`. The framework is intentionally thin: three phases, two result types, one runner, one shared sentinel primitive. Argparse wiring in `cli.py` is unchanged — `args.func` is now a `Verb` factory rather than a bare function, and `main()` resolves it via `sys.exit(run_verb(args.func(), args))`.

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
| `converge` | load state + filter + conflict-group load | drift compare; optional rebuild | (build_state written by rebuild path) | only with `--apply` |
| `doctor` | load config + target expansion | depends/soname/ABI scan | invoke `BuildVerb` flow when `--apply` | delegated |
| `resolve` | load config | match rules + print | null | no |
| `env` | null | collect + format + print env chain | null | no |
| `setup` | read pacman.conf | check + patch IgnoreGroup | re-read confirms write | no |
| `log` | null | resolve unified/per-pkg log path; page through `$PAGER` | null | no |
| `packages {list,add,remove}` | load packages.toml + validate override fields | rewrite TOML | null | no |
| `state {list,repair,orphans}` | load state dir | inspect / repair / prune | null | `repair` only |
| `run …` namespace | build `RunOptions` | delegate to `pipeline.run_pipeline` / `run_stage_standalone` | pipeline framework | (pipeline owns it) |

**Why not unify with the pipeline `Stage` contract?** Stages already presume multi-stage DAG semantics, per-stage checkpoints, and an opinionated `PipelineState`. Most CLI verbs are single-shot and don't want a pipeline state file. The verb framework reuses `sentinel_scope` for install-bearing protection but otherwise stays independent, so `sysforge env` is not paying for pipeline machinery it doesn't need. The `run` namespace verbs are exactly the thin shim from CLI → pipeline.

### Shared build engine (`build_core.py`)

`build` is a strict subset of `update`: both route their actual building through one engine in `sysforge/build_core.py`, so the two paths cannot drift the way they once did (a `build` that left makepkg's `-s`/`--syncdeps` in place would have makepkg run `pacman -S` on an AUR-only dependency and fail, while `update` stripped those flags and pre-resolved every dep itself). `update` extends the shared core with the things that are genuinely its own — version checking, source-sync scheduling, `--install-only`, toolchain pre-flight, the bulk `pacman -Syu`, and the run summary — but the dependency prep, the per-package makepkg invocation, and the install are identical code.

- **`build_and_install(targets, *, sync_source, …) -> BuildOutcome`** — the engine. Runs `prepare_deps`, then a per-package build loop, then `install_built`, returning the built/failed/pgo-skipped lists and the install-failed flag. Each makepkg call uses `strip_flags = BATCH_STRIP_FLAGS` (`{-s, --syncdeps, -i, --install}`) and `force_batch` when non-interactive, so makepkg never resolves deps via pacman and never installs inline — sysforge owns both. `targets` is any object exposing `pkgbase`/`pkgnames`/`pkgbuild_path`/`source` (`update._UpdateResult` qualifies directly; `build` builds a `BuildTarget` from the parsed PKGBUILD via `target_from_pkgbuild`).
- **`prepare_deps(pkgbuild_paths, config, *, building_names, …)`** — pre-installs missing repo makedeps in one `pacman -S` transaction (`batch_install_makedeps`) and builds AUR/local deps in topo order (`resolve_aur_deps_batch` + `build_resolved_deps`), excluding the packages about to be built themselves. Both arms are best-effort — a failure warns and lets the build proceed, surfacing a genuinely-missing dep as a per-package build failure with a diagnosis rather than aborting the whole batch up front.
- **`install_built(built_pkg_files) -> (files, install_failed)`** — dedupe, re-fetch the installed set (makedep/AUR pre-install may have expanded it), `filter_pkgs_to_installed` for split-pkgbase safety, then one `pacman -U`. Reused by `update`'s `--install-only` artifact-scan branch.
- **`sync_source`** is the single deliberate caller difference: `update` passes `False` (Phase 2 already synced sources through the scheduler), `build` passes `not --no-update` to keep its inline per-package source sync (which itself routes through `source_sync.get_scheduler()` inside `makepkg_wrapper.run`). `_find_existing_artifacts` and `_record_build_failure` live here too (moved from `update.py`) since both the engine and `update`'s install-only scan use them.

---

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

- **TTY mode** — DECSTBM scroll region (`ESC[1;N-1r`) reserves the last row; other output (including subprocess output that inherits the TTY — `makepkg`, `git`, `pacman`) scrolls above it. `SIGWINCH` is wired to re-establish the region and redraw the last status on resize. An `atexit` hook releases the region on interpreter shutdown.
- **Plain mode** — selected when any of the following is true: `sys.stderr` is not a TTY, `_DRY_RUN`, `TERM=dumb`, `TERM=""`, `CI` set, or `NO_COLOR` set. Emits `[PROGRESS] [i/n] label` through `log.ui()` so the same data reaches logs and pipes without ANSI garbage.

Public API: `init()`, `shutdown()`, `render(current, total, label)`, `clear()`, and a `tracker(total, prefix)` context manager that yields a `tick(label)` callable (auto-clears on exit). `clear()` must be called before any `input()` prompt inside a batch loop so the prompt doesn't land inside the scroll region; the next `tick()` re-establishes the region automatically. Reservation is lazy: entering a tracker alone touches nothing — the first `tick()` call establishes the region.

Integration sites: `sysforge/pipeline/stages/packages.py` (build loop), `sysforge/primitives/aur_resolve.py::build_resolved_deps` (AUR deps), `sysforge/update.py` (sequential source sync via `source_sync` + threaded version check + build loop), `sysforge/fetch.py` (fetch loop). Interactive-prompt call sites in `pipeline/stages/packages.py` and `primitives/makepkg_wrapper.py` each call `progress.clear()` before invoking `prompt.prompt_choice()`.

### `prompt.py`

Single shared interactive-prompt helper. Every stage that needs user input goes through `prompt.prompt_text()` (free-form) or `prompt.prompt_choice()` (fixed-set), so the behaviour on empty input, unrecognized input, and EOF is consistent across the codebase. `is_interactive()` wraps `sys.stdin.isatty()` for stages that need to gate prompts on a TTY.

Key contract:

- `prompt_choice` re-prompts with a visible warning on unrecognized input (so typos / jibberish never silently fall through to the default), unless the call site passes `retry_on_invalid=False` — used only for destructive prompts (e.g. partition.py's literal-`yes` confirmation) where any non-confirming input must abort.
- `eof_default` is a separate kwarg from `default`. Most sites pass neither (EOF returns `default`). `toolchain.py`'s GCC-build override and `_confirm_or_abort` deliberately set `eof_default="y"` to preserve their long-standing "EOF means proceed unattended" semantic.
- Both helpers catch `EOFError` *and* `OSError`, since pytest's captured stdin and other unreadable-stdin scenarios raise the latter.
- Optional `tag`/`level` kwargs reuse `log.prompt_prefix(level, tag)` so prompts keep the standard `[SYSFORGE][LEVEL][TAG] ` format.

Call sites: `pipeline/stages/reconfigure.py` (11), `pipeline/stages/packages.py` (`_prompt_failed_packages`), `pipeline/stages/toolchain.py` (3), `pipeline/stages/partition.py` (1), `setup_cmd.py` (1), `primitives/makepkg_wrapper.py` (4). No stage may call `input()` directly.

### `paths.py`

Pure constants module — the canonical directory of every config file sysforge reads. `CONFIG_BASE` is derived from `$SYSFORGE_CONFIG_DIR` (falling back to `/etc/sysforge`), and the resolved path lists (`CONFIG_PATHS`, `CONFLICT_GROUP_PATHS`, `CONSUMES_INFERENCE_PATHS`) layer the user file (`~/.config/sysforge/…`) over the system file in `extends_system` order. The sole helper is `resolve_packages_path(config)`, which returns the `packages.toml` path the rest of the codebase should use (honouring `--packages` overrides in `config`). No I/O here — just path strings.

### `config.py`

TOML config loading and path resolution. Public API:
- `load_config(config_paths=None)` — loads `profiles.toml`, merges user onto system via `extends_system`, validates rule priorities
- `load_conflict_groups(paths=None)` — extracts the `[append_conflict_groups]` table from `profiles.toml`
- `load_consumes_inference(paths=None)` — extracts the `[consumes_inference]` table from `profiles.toml`
- `find_pkgbuild(pkg, config=None)` — resolves a bare package name, directory path, or PKGBUILD path to an absolute PKGBUILD path. Search order: (1) direct path or directory (resolves `dir/PKGBUILD`), (2) `<cwd>/<name>/PKGBUILD`, (3) `<config [paths] pkgbuild_src_dir>/<name>/PKGBUILD`, (4) auto-clone if not found locally — repo packages via `pkgctl repo clone --protocol=https`, AUR packages are routed through `get_scheduler().request(SyncRequest(...))` so the clone is deduplicated with any concurrent update/fetch request and shares the same rate-limit budget. Used by `sysforge build`, `sysforge resolve`, and the packages stage.

`[paths] pkgbuild_src_dir` in `profiles.toml` is the user-configured root for local PKGBUILDs (`~/src` by default). Auto-clone also targets this directory.
- `parse_system_makepkg_conf(path=None)` — parses `/etc/makepkg.conf` into `{key: raw_value_string}` for use in temp conf generation. Handles backslash line continuation (e.g. `CFLAGS="... \\\n  -flag"`) and multiline bash array values (e.g. `VCSCLIENTS=(...)` spanning multiple lines) by tracking paren depth across lines. Merges user conf (`$XDG_CONFIG_HOME/pacman/makepkg.conf`, `~/.makepkg.conf`) on top of system conf.

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
- `get_pkgdest()` — resolves the `PKGDEST` directory from makepkg.conf
- `snapshot_pkg_dir(pkgdest)` — records the set of `.pkg.tar.*` files currently in pkgdest before a build
- `batch_install_pkgs(pkgdest, pre_snapshot, ...)` — diffs the post-build pkgdest against the snapshot and installs all new packages in a single `sudo pacman -U`
- `read_pkgname_from_file(path)` — extracts `pkgname` from a built `.pkg.tar.*` via `bsdtar -xOqf <path> .PKGINFO`; returns `None` on failure
- `filter_pkgs_to_installed(paths, installed)` — partitions pkg-file paths into `(keep, dropped)` by whether their `pkgname` is in the current installed set; used by `update`/`converge` so split-pkgbase rebuilds don't add sub-packages the user never installed
- `collect_makedeps(pkgmeta)` / `filter_missing_deps(deps)` / `batch_install_makedeps(deps)` — makedependency helpers
- `get_installed_version(name)` — `pacman -Q <name>`; returns version string or `None`
- `get_all_installed_packages()` — `pacman -Q`; returns `{name: version}`
- `get_foreign_packages()` — `pacman -Qm`; returns names not from any sync DB
- `get_pacman_sync_version(name)` — `pacman -Si <name>`; returns version from sync DB or `None`

The five read-only queries above (`get_installed_version`, `get_all_installed_packages`, `get_foreign_packages`, `get_pacman_sync_version`, `filter_missing_deps`) check for an importable `pyalpm` and route through libalpm bindings when available — direct local-DB and sync-DB access is faster than spawning a `pacman` subprocess per call. The fallback path is the original subprocess shell-out, so installs without `pyalpm` are unaffected. `pyalpm` is shipped as `[project.optional-dependencies] extra` (`uv sync --extra extra`) or installed via the system package. `SYSFORGE_PACMAN_NO_PYALPM=1` forces the subprocess path even when pyalpm is present (used by `tests/conftest.py` so existing subprocess-mocking tests keep driving the query). Mutating paths (`pacman -U`, `pacman -S --needed`) and the `pacman -Fq` files-DB lookup in `provides_lookup.py` remain subprocess-based.

Constants: `BATCH_STRIP_FLAGS` (flags removed from per-build makepkg calls during batch install), `BATCH_EXTRA_FLAGS`.

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

**LLVM target filtering** (`patch_llvm_targets` + `is_llvm_pkgbase`) injects `-DLLVM_TARGETS_TO_BUILD="<list>"` into the cmake invocation of LLVM-toolchain PKGBUILDs (`llvm`, `clang`, `compiler-rt`, `lld`, lib32 variants — gated by `is_llvm_pkgbase` on `pkgbase`). The patcher is invoked at the end of `apply_patch_pkgbuild`. The target list is resolved by `primitives/llvm_targets.resolve_llvm_targets` in this order: `[llvm] targets` in `toolchain.toml` (explicit override; `targets = []` disables filtering) → `[hardware] llvm_targets` in `hardware_profile.toml` (autodetected from `uname -m` + `gpu_vendors`) → `None` (no filtering, build all targets). Idempotent: re-running on a PKGBUILD already carrying the same value is a no-op; replacing an existing `-DLLVM_TARGETS_TO_BUILD=` arg preserves the upstream PKGBUILD style. On a no-cmake-found PKGBUILD (upstream switched to meson), logs a warn and leaves the file unchanged. The `LLVM_EXPERIMENTAL_TARGETS_TO_BUILD` flag is intentionally untouched.

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

Public API: `collect_required_toolchains(per_pkg, lib32_pkgs, rust_toolchain_pins=None, compilers=None)`, `run_preflight(required)`, `render_preflight(report)`, `auto_remediate(report, *, non_interactive=False)`. Wiring: `update` only, behind `--no-toolchain-preflight`. The companion `primitives.build_diag.diagnose` runs in `makepkg_wrapper.invoke_makepkg` on non-zero exit and matches known failure signatures (E0463 missing std crate, gstreamer PTP-no-rust, meson "Unknown options" → stale build dir, `cuda:host-gcc-too-new` → nvcc rejecting a system gcc newer than the CUDA toolkit supports, `toolchain:llvm-broken` → a clang/libLLVM mismatch where clang can't run, matched on `undefined symbol: LLVMInitialize…` / `symbol lookup error: …clang` / meson's `Unknown compiler(s): [['clang']]`) in the captured output and any `meson-logs/meson-log.txt` **or `CMakeFiles/CMakeError.log`** under the build directory; deduped on signature, never masks the real error. The CUDA matcher reads the toolkit's `crt/host_config.h` `#if __GNUC__ > N` gate and the highest installed `/usr/bin/g++-≤N` to emit a concrete `NVCC_APPEND_FLAGS='-ccbin …'` fix. Each `FixSuggestion`'s `signature`/`fix_cmd` is also carried up the exception (`.diagnosis`) and persisted to `build_state.toml`'s `[failures]` table by `sysforge update` (see §`build_state.py`). **Interactive builds** inherit the TTY so makepkg's stdout is never captured; on failure `invoke_makepkg` instead runs `diagnose([], _effective_build_dir(...))` over the side-car logs (resolving `$BUILDDIR/<pkgbase>` when `BUILDDIR` redirects the build out-of-tree) and threads the result through the user-abort RuntimeError, so `state failed` records a real signature rather than "Aborted by user". Log tags: `[PREFLIGHT]`, `[DIAG]`.

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
6. For each AUR dep found: fetch its PKGBUILD (`find_pkgbuild`), recurse from step 1.
7. Deps not found in AUR or repos → warn and let makepkg fail naturally.
8. DFS topological sort with cycle detection (error on cycles).
9. Skip packages already installed at a satisfying version (`pacman -Q`) unless `-f`/`--force` is passed.

Build execution: iterate the topo-sorted list in order. Each dep gets full profile resolution (flag profiles, PKGBUILD patching) — same as any sysforge-managed build. Each dep is installed immediately after building so subsequent deps can link against it.

Integration points:
- **`sysforge build`** — resolve before building. `--track-deps` builds resolved AUR deps in topo order before the target.
- **`run packages` stage** — resolve before building each AUR/profiled package. `--track-deps` behaves the same.
- **`sysforge update`** — resolve after `collect_makedeps()`, before `batch_install_makedeps()`. AUR deps are built and installed first, then the main batch proceeds.
- **`sysforge converge`** intentionally does **not** invoke `aur_resolve.py`. Converge operates only on packages already recorded in `build_state.toml`; their AUR deps are assumed to already be present.
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
- *Optional LLVM target-init demotion.* When the bound lib defines the required version node but not the specific symbol, and the symbol is an optional LLVM target-registration entry point (`_is_optional_llvm_target_init`: `LLVMInitialize<Target>{Target,TargetInfo,TargetMC,AsmParser,AsmPrinter,Disassembler}@LLVM_*`), it is demoted to `benign_sink` + an info log rather than reported. libLLVM is routinely built with a reduced `LLVM_TARGETS_TO_BUILD` (notably multilib lib32-llvm: X86/NVPTX only), so Mesa gallium drivers reference target-init symbols for un-built backends (AMDGPU, AArch64, ARM, …); each is lazily bound and only dereferenced when that GPU target is the active driver. A genuine symbol-within-version break (e.g. a C++ stdlib `_ZNSt*@LLVM_*` from a PGO toolchain leak) does **not** match the pattern and stays a hard finding — see the *Pre-install ABI hazard check* under the toolchain stage, which guards that case independently.

**Arch-aware ldconfig lookups.** The ldconfig map is keyed by `(soname, ELF class)` rather than soname alone, because `ldconfig -p` lists both 32-bit and 64-bit variants of common sonames (e.g. `libc.so.6`) and first-hit-wins would collapse them. Each `.so` under check has its ELF class determined via `readelf -h` and NEEDED references are resolved against libs of matching arch. Without this, lib32 packages produce a flood of false-positive "undefined symbol" findings because their `unsigned int`-mangled requirements don't match the 64-bit `libc`'s `unsigned long`-mangled exports.

**Shim-library allowlist.** A small set of compat shims shipped by glibc (`libnsl.so.1`, `libc_malloc_debug.so`, `libc_malloc_debug.so.0`) are skipped by `_is_shim_lib`. Their "undefined" symbols are intentional: `libnsl`'s RPC API is implemented by `libtirpc` at runtime (not declared as NEEDED), and `libc_malloc_debug` uses weak-hook override patterns. Without this filter, every `doctor` run reports ~44 findings per glibc that bury the real signal.

**Vendored-binary package skip list.** `_ABI_CHECK_SKIP_PACKAGES` (public predicate `is_abi_check_skipped_package(pkgname)`) names packages that ship prebuilt vendored binaries which will never link cleanly against current system libs (e.g. `steam` carries its own CEF runtime, libcurl, etc. under `/usr/lib/steam/`). `doctor.py` skips the ABI/linkage check for these packages and emits a one-line `[ABI] skipped: vendored prebuilt binaries` note; the depends check still runs since depends drift on these is actionable. Applies at package granularity, not soname — a floor-level noise filter for `doctor --all` / `doctor -s <metapackage>` runs whose closures include these packages.

### `provides_lookup.py`

Reverse soname → package lookup backed by `pacman -Fq`. Used by `sysforge doctor --suggest` to convert a missing/broken soname (e.g. `libavcodec.so.62`) into the repo package(s) that would supply it. Public API:

- `files_db_present()` — true when `/var/lib/pacman/sync/*.files` is synced (from `pacman -Fy`). Callers short-circuit lookup when false.
- `suggest_for_soname(entry, *, lib32=False, installed_names=None)` — returns candidate `repo/pkg` strings for a soname entry, honouring `lib32` context (queries `usr/lib32/<soname>` vs `usr/lib/<soname>`). When `installed_names` is supplied, candidates whose bare pkgname (the part after the optional `repo/` prefix) is in the set are dropped — the load-bearing filter that stops `doctor --suggest` from re-recommending packages the user already has installed.

Log tag: `[PROV]`. Pure read-only — never runs `pacman -Fy`; emits a single `run sudo pacman -Fy` warning if the files db is absent.

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

**Makepkg flag passthrough:** makepkg short flags can be passed directly on the command line (`sysforge build ventoy -sfCci`) or explicitly via `-m "-sfci"`. Implicit passthrough applies to `build`, `update`, and `converge` — the preprocessing layer (`_extract_implicit_makepkg_flags`) rewrites bare flags into `-m` form before argparse runs. Excluded from implicit passthrough: `-h`, `-V`, `-p`, `-m`, `-D` (conflict with sysforge flags or take a value argument; `-v` is already hoisted). Combined short flags are expanded: `-sfci` → `[-s, -f, -c, -i]`.

**Subprocess stdio:** the non-interactive branch routes makepkg's stdout+stderr through `pty_runner.run_with_pty` so child tools that gate live UI on `isatty()` (cargo's "Building [n/m]" bar, configure-script spinners) still emit their progress animation. Bytes are forwarded verbatim to `sys.stdout.buffer` when sysforge itself is on a tty so the user sees the animation alongside the bottom-anchored `[SYSFORGE][PROGRESS]` indicator. The same byte stream is decoded and split on `\n` into lines for failure classification (`prepare`/`build`/`package`), missing-dep collection (`target not found:`), already-built detection, and clang→GCC toolchain-mismatch pattern matching (curly-quote tolerant). In verbose mode (`-vvv`) or when sysforge stdout is piped (`sysforge update | tee log.txt`), byte forwarding is suppressed; only the decoded lines reach the user, keeping captured logs free of `\r`/ANSI noise. A `MAKEPKG_HEARTBEAT_S`-cadence (default 30 s) idle callback writes `[heartbeat] <latest>` entries to the per-package log when no `\n` has crossed the boundary in that window — surfacing ninja's `\r`-redrawn `[X/Y] Building ...` status so a long compile phase doesn't look hung under `-vvv` / `sysforge log <pkg>`. The interactive branch still uses `subprocess.Popen` with inherited stdio so unbuffered prompts (sudo, gpg signing keys, pacman conflict resolution) reach the terminal immediately.

### `pty_runner.py`

Standalone helper: spawns a subprocess attached to a pty so child tools observe a tty on stdout+stderr. Reads raw bytes from the master fd, optionally forwards them verbatim to `sys.stdout.buffer` (preserving `\r`-based progress redraws), and delivers decoded lines to a callback for parent-side pattern matching. Splits lines on `\n` only — `\r` is left in place mid-line so cargo's redraws aren't shredded into spurious "lines". An optional `idle_callback` fires every `idle_timeout_s` seconds when no full line has been delivered; it receives `buf.split("\r")[-1]` (the latest in-place redraw segment) or `None` if the child is silent, without consuming the buffer — subsequent `\n` still delivers the original inter-newline content unchanged. The read loop wakes via `select(master_fd, …, idle_timeout_s)`, so the heartbeat is idle-driven (no spin). Handles SIGWINCH (chains to the previously installed handler so `ui/progress._on_sigwinch` continues to fire), EIO on child exit, and UTF-8 codepoints split across read boundaries (incremental decoder with `errors="replace"`). stdin is inherited from the parent so TTY-only prompts (sudo) keep working. Used by `makepkg_wrapper.py`'s non-interactive build path; reusable for any subprocess where preserving child-side ANSI animation matters.

### `cache_probe.py`

Passive monitoring of ccache/sccache/ThinLTO caches. Emits the `[CACHE]` log-tag lines that bracket each `makepkg` invocation with pre/post hit-miss deltas and, once per run, the ld.so cache mtime, pacman cache file count/size, and (per-package) the ThinLTO cache dir size extracted from `--thinlto-cache-dir=` in LDFLAGS. Never enables or disables caches — policy for that lives in `[cache]` of `profiles.toml`.

Public API covers three axes:
- **Per-build stats** — snapshot ccache/sccache counters before and after a build (`ccache --print-stats --format=tab`, `sccache --show-stats`), compute the delta, log hit rate when compilations occurred, say "no compilations recorded" when delta is zero.
- **System probes** — `emit_system_probes()` for the once-per-run ld.so / pacman cache measurements.
- **Session report** — the structured `--cache-report` summary accumulates per-package deltas and prints a totals block at end of run, regardless of verbosity (the only output that bypasses `-v` gating).

Each probe is skipped cleanly if the underlying binary is absent (e.g. sccache not installed).

### `aur.py`

AUR RPC queries, package source detection, git/pkgctl clone helpers, and GPG key import. Network-facing primitives optionally accept a `RateLimiter` (from `rate_limit.py`) so the scheduler can throttle RPC and git-fetch traffic under a single budget.

- `repo_packages(names)` — single `pacman -Si name1 name2 ...` invocation; returns the subset of names present in any sync DB. Use for batch classification (O(1) subprocesses). Parses stdout for `Name : <pkg>` lines; packages not found produce errors to stderr only.
- `is_repo_package(name)` — single-name wrapper around pacman -Si; returns `True` if found in any sync DB. Used by `find_pkgbuild` to route auto-clone: repo packages → `pkgctl_checkout`, AUR → `aur_clone`.
- `aur_info(names)` — single batch `GET https://aur.archlinux.org/rpc/v5/info?arg[]=…` for all names; returns `{name: result_dict}`. Silent on network/JSON errors (returns `{}`).
- `aur_clone(name, dest, *, ref=None, depth=None)` — `git clone https://aur.archlinux.org/<name>.git <dest>`; optional `ref` / `depth` support shallow / branch-pinned clones. Raises `RuntimeError` on failure.
- `git_fetch_and_compare(pkgbuild_dir, *, timeout=30, limiter=None)` — shallow (`--depth=1`) fetch of the tracked upstream followed by a HEAD compare. **Non-destructive**: never runs a merge/rebase/reset; returns a `GitFetchOutcome(status, head_before, head_after, error)` where `status ∈ {"up_to_date", "fetched", "diverged", "failed", "skipped_no_tracking"}`. Divergence (local commits or a force-push upstream) is reported, not auto-recovered — the scheduler leaves the work-tree intact and surfaces the status upward. Honours the limiter's `wait_before_fetch()` / `Retry-After` budget when supplied.
- `is_transient_git_error(stderr)` / `is_rate_limit_error(stderr)` — shared stderr classifiers used by both the scheduler and legacy retry paths.
- `_classify_head_vs_upstream(pkgbuild_dir)` — single classifier consumed by both `git_is_dirty` and `llvm_state._dirty_reason`. Returns `(state, n_local, n_upstream)` where `state ∈ {"not_a_repo", "no_head", "no_tracking", "clean", "behind", "ahead", "diverged_user", "diverged_upstream"}`. The two `diverged_*` states distinguish "upstream rewrote history (force-push), no local commits authored by the local git user" (`diverged_upstream` → not dirty) from "HEAD and upstream have a common ancestor but at least one of HEAD's divergent commits is authored by the local user" (`diverged_user` → dirty). The local user identity is read from `git -C <dir> config user.email` (with global fallback). `ahead` = HEAD is a strict descendant of upstream; `behind` = HEAD is an ancestor of upstream (the only "out of date but clean" case).
- `git_is_dirty(pkgbuild_dir)` — wrapper over the classifier: returns `True` for `no_tracking`, `ahead`, `diverged_user` (plus uncommitted tracked changes detected separately via `git status`); returns `False` for `clean`, `behind`, `no_head`, `not_a_repo`, `diverged_upstream`. Untracked files (build artifacts) are intentionally ignored. The `diverged_upstream` exemption fixes the false-positive on workstations whose Arch packaging clones get force-pushed every release.
- `purge_src(pkgbuild_dir, *, force=False)` — `rm -rf` the directory after a `git_is_dirty` safety check. Raises `RuntimeError` if the clone holds local work that would be destroyed; non-git directories are purged unconditionally; non-existent paths are a silent no-op. `force=True` skips the dirty check and purges unconditionally — used by the `--cleansrc-force` CLI path. Used by `sysforge build --cleansrc[/-force]`, `sysforge update --cleansrc[/-force]`, `sysforge fetch --cleansrc[/-force]`, `sysforge run toolchain --cleansrc[/-force]`, and the source-sync recovery paths.
- `pkgctl_checkout(name, dest, *, timeout=60)` — `pkgctl repo clone --protocol=https <name>` run in `dest.parent`; fetches official Arch packaging repo. Output is streamed line-by-line to `_build_log.debug` so progress is visible at `-vvv` (cloning from gitlab.archlinux.org can take minutes on a fresh checkout). Raises `RuntimeError` on failure or timeout. `find_pkgbuild` passes `[git] clone_timeout` from `sysforge.toml`; `0` disables.
- `import_pgp_keys(pkgmeta, pkgbuild_path)` — ensures all `validpgpkeys` listed in the PKGBUILD are in the GPG keyring before `makepkg` runs. Strategy: (1) import any bundled `.asc` files from `keys/pgp/` alongside the PKGBUILD, (2) check which keys are still missing via `gpg --list-keys`, (3) fetch remaining via `gpg --recv-keys`. Import failures are logged as warnings — makepkg surfaces a clearer error if a key is still absent at verification time.
- `fetch_aur_name_cache(force=False)` — downloads `https://aur.archlinux.org/packages.gz` and extracts it to `~/.config/sysforge/cache/aur-packages.txt`. Skips the download if the cache is less than 24 hours old unless `force=True`. Called as a side effect of `sysforge update`; read by `sysforge completions packages` to provide AUR package name completion.

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
4. **Fetch.** Otherwise call `git_fetch_and_compare` — shallow fetch + HEAD compare, never merges or rebases. Works for both AUR and repo sources because pkgctl-cloned dirs are plain git repos with a tracking branch.
5. **Divergence.** `STATUS_DIVERGED` is *reported*, not fixed: the work-tree is untouched, the build continues against the local PKGBUILD, and the operator decides whether to `--cleansrc` next run.
6. **Rate limit.** `RateLimited` aborts the remaining batch via `_abort_remaining`, which populates pending results with `STATUS_RATE_LIMITED` so the UI can show per-package status instead of a single global error.

Singletons:
- `get_scheduler(*, state_dir=None, offline=False, cleansrc=False, cleansrc_force=False, force_devel=False, min_fetch_interval_ms=None, rate_limit_abort_s=None, fetch_timeout=None, clone_timeout=None)` — returns the process-wide scheduler, constructing it on first call. Subsequent calls with the same args are memoised; dedup keys: `(pkgbase)` — any given pkgbase is synced at most once per process. `cleansrc_force=True` implies `cleansrc=True` and propagates to `purge_src(force=True)` so `STATUS_PURGE_REFUSED` cannot occur — the operator has explicitly opted in to overwriting local work. `force_devel` only gates the forced-fetch behaviour for VCS pkgbases that *reach* the scheduler; the higher-level filter in `update.py:_sync_sources` is what keeps VCS pkgbases out of the request batch entirely when `--devel` is off (so `--cleansrc` never purges a `-git` tree the user hasn't opted in to rebuild).
- `reset_scheduler()` — test-only hook. Tests that need fresh state call this between runs.

### `build_state.py`

Build state persistence. `/var/lib/sysforge/build_state.toml` is a **superset of `pacman -Q`** — every installed package has an entry, regardless of whether sysforge built it. The `build_mode` field distinguishes them:

- `"profiled"` — built by sysforge; carries `pkgver`, `pkgrel`, `epoch`, `pkgbase`, `pkgbuild_dir`, `flags_string` (serialized resolved compiler flags, newline-separated `KEY=value` lines), `built_at` (ISO 8601 UTC timestamp), and optionally `built_upstream_commit` (40-char SHA of the just-built upstream tree, populated only for single-git-source VCS packages — read by `sysforge update --devel` to short-circuit `pkgver()` resolution via `git ls-remote`; absent for non-VCS, multi-git-source, or any PKGBUILD whose `source=()` has unresolved bash interpolation), `source` (`"aur"` / `"repo"` / `"git"` / `"local"` — the origin classification at build time, read by `sysforge update`'s source resolver so previously-built packages keep their origin across runs instead of being re-derived from live pacman + overrides every invocation; absent for back-compat entries written before the field existed; `"local"` means a hand-maintained PKGBUILD with no upstream remote, source-sync is skipped for it), `owner_stage` (e.g. `"kernel"` or `"toolchain"` — set by a pipeline stage that owns the package's lifecycle, so `sysforge update` skips it by default and points the user at the owning stage; `--include-stage-owned` overrides the skip; both the kernel and toolchain stages stamp it, each with a config-file bootstrap fallback in `update.py` for the pre-first-build window), and `toolchain_variant` (`"gcc"` / `"stock_llvm"` / `"pgo_llvm"` — the active toolchain identity at build time, read by `sysforge update` to detect toolchain drift; absent for back-compat entries and for builds that ran with no toolchain stage configured). `source`, `owner_stage`, and `toolchain_variant` are *sticky* — `BuildState.record()` preserves the prior value when the caller doesn't pass one, so a rebuild through a code path that doesn't know about them won't erase the provenance. Split packages (multiple `pkgname` from one `pkgbase`) each get their own entry, all pointing at the same `pkgbuild_dir`.
- `"pacman"` — installed via pacman, not built through sysforge. Carries only `pkgver`, `pkgrel`, `epoch` parsed from `pacman -Q`; `pkgbase`, `pkgbuild_dir`, and `flags_string` are absent. Synthesised by `sync_with_installed()`.
- `"pgo_llvm_toolchain"` — LLVM toolchain packages built with profdata reuse: `makepkg_wrapper` injects `-fprofile-use=<saved-profdata>` when a compatible `clang.profdata` exists, otherwise prompts plain build / skip (default skip). See **PGO toolchain packages** below.

`BuildState.sync_with_installed(installed)` keeps the file in lockstep with `pacman -Q`: it adds a pacman-mode entry for every newly installed package and prunes entries for packages that are no longer installed. The prune pass also removes zombie entries left by pre-superset parser runs — e.g. legacy keys containing literal `$_pkgname` that can never match a `pacman -Q` name. `sysforge update` calls this at the start of every run and saves if anything changed.

Read by `sysforge update` for version drift detection (every installed AUR package is iterated regardless of `build_mode`; profiled entries carry the prior `pkgver` for change-detection, pacman-mode entries are checked against PKGBUILD freshness) and by `sysforge converge` for flag drift detection (profiled entries only; pacman-mode entries are silently skipped). Follows the same atomic write-then-rename pattern as `pipeline/state.py`. Records must carry `build_mode`; the previous compatibility fallback that treated missing `build_mode` as profiled was removed.

On the write path, after a successful build `makepkg_wrapper.py` derives `pkgver`/`pkgrel`/`epoch` from the produced `.pkg.tar.*` filenames rather than the static PKGBUILD parse. The static parser intentionally leaves shell parameter-expansion forms (e.g. `${_ver/[a-z]/.${_ver//[0-9.]/}}`) untouched so it never produces a misleading partial substitution, but a built package's filename always carries the fully resolved version. Falling back to filenames prevents profiled entries from storing literal `$...` strings that would mismatch every subsequent vercmp and cause the package to be flagged for rebuild on every `sysforge update` run.

**Build failures** live in a reserved top-level `[failures]` table (keyed by pkgbase), held apart from the per-package install mirror so `all_packages()` / `sync_with_installed()` stay a clean superset of `pacman -Q` (the `failures` key is popped into a private dict on load and re-serialized separately; a package literally named `failures` would collide but none exists in practice). Each entry carries `failed_at` (ISO 8601 UTC), `error` (the failure message tail — last ~6 lines / 600 chars), and optionally `pkgver`, `signature`, and `fix_cmd` (the latter two from `build_diag` postflight diagnosis when a known pattern matched). API: `record_failure(pkgbase, *, error, pkgver=None, signature=None, fix_cmd=None, failed_at=None)`, `clear_failure(pkgbase) -> bool`, `all_failures() -> {pkgbase: record}`. A successful `record()` calls `clear_failure(pkgbase)` so the failure list self-heals on the next good build. `sysforge update`'s build fan-out records failures via `_record_build_failure` (opening a fresh `BuildState` so loop-time success writes aren't clobbered, and pulling `signature`/`fix_cmd` from the exception's `.diagnosis`, attached by `makepkg_wrapper`). Surfaced by `sysforge state failed`.

Public helpers: `parse_pacman_version(ver_str)` splits a `[epoch:]pkgver-pkgrel` string into a `(epoch, pkgver, pkgrel)` tuple; used by `sync_with_installed()`.

### `version.py`

Version comparison utilities. `vercmp(a, b)` wraps the system `vercmp` binary and returns -1/0/1 (negative/zero/positive output from vercmp is clamped). `format_version(globals_)` assembles an `[epoch:]pkgver-pkgrel` string from parsed PKGBUILD globals, omitting the epoch prefix when it is `"0"` or absent.

### `vcs_pkgver.py`

`evaluate_vcs_pkgver(pkgbuild_dir, *, timeout=300) -> str | None` resolves a VCS PKGBUILD's effective `[epoch:]pkgver-pkgrel` by running `pkgver()` against the fetched upstream sources. Two-step makepkg invocation: (1) `makepkg -od --nobuild --noprepare --nodeps --skippgpcheck --noconfirm` updates VCS sources and runs `pkgver()`; (2) `makepkg --packagelist` prints the resolved filename, which is parsed via `_parse_built_pkg_filename` (the same helper `_find_existing_artifacts` uses) into `(epoch, pkgver, pkgrel)`. Returns `None` on any failure — non-zero exit, timeout, missing makepkg, unparseable output — with a WARN logged. Caller policy in `update.py`: `None` → `DEVEL_EVAL_FAILED` action, package skipped (not rebuilt). Used by `sysforge update --devel` to vercmp upstream-resolved against installed and only rebuild genuinely-stale VCS packages.

`peek_upstream_commit(pkgbuild_dir, *, timeout=30) -> str | None` and `read_built_upstream_commit(pkgbuild_dir, *, timeout=10) -> str | None` are the two halves of the `--devel` short-circuit cache. Both share a private `_single_git_source(globals_)` helper that parses `source=()` from `parse_pkgbuild`'s output, recognises `git+<url>`, `git://...`, and `<name>::<either>` forms (with `#commit=`/`#tag=`/`#branch=`/`#fragment=` fragments), and returns `(clone_name, url, fragment)` only when the PKGBUILD has exactly one git source and no remaining `${...}` interpolation. `peek_upstream_commit` runs `git ls-remote <url> <ref>` (or returns immediately for a `commit=<sha>` pin) to get the current upstream tip without fetching the working tree. `read_built_upstream_commit` runs `git -C <pkgbuild_dir>/src/<clone_name> rev-parse HEAD` to capture the SHA of the just-built tree, called from `makepkg_wrapper.py` immediately after a successful build so the SHA is persisted to `build_state.toml` as `built_upstream_commit`. Either helper returns `None` for multi-git-source / non-git / unresolved-variable / parse-failure / subprocess-failure cases, in which case the caller falls through to the canonical `evaluate_vcs_pkgver` slow path. The strict semantic — only-on-successful-build writes — means pre-existing build_state entries lacking the field stay slow until the package is naturally rebuilt; this is intentional, to keep the field's meaning unambiguous (it is the commit we built, not the commit we last observed).

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

Implements `sysforge update` — the update manager. The iteration scope is the **live install set**: every installed AUR package (`pacman -Qm`) plus any repo packages selected by overrides. `packages.toml` entries apply as overrides where present (see §Package Manifest). `build_state.toml` records prior build metadata used for change detection but does not gate iteration. Organized into 7 phases:

**Phase 0 — Init.** Load BuildState, config, `packages.toml` overrides. Open unified log (always truncated). Refresh AUR name cache (skipped with `--offline` or `--install-only`).

**Phase 1 — Package set assembly** (`_assemble_package_set`). Build a unified `{pkgname: entry}` dict by walking the live install set: AUR (`pacman -Qm`) is always walked; repo packages are walked when their `[[package]]` entry sets a behavior-changing field (`pkgbuild_patch`, `cache`, or `reason`), or when `[build] repo_mode = "profiled"` is set in `packages.toml` (the latter pulls every installed repo package into scope). Each repo entry is sub-classified into `repo_class = "source"` (has a behavior-changing override → goes through pkgctl-clone + makepkg) or `repo_class = "pacman"` (no override → fast pacman path via `checkupdates` + a single terminal `pacman -Syu`). A bare `source = "repo"` entry is inert metadata (matches the `sysforge packages add` validator) and is *not* a trigger; the loader emits a warn line so it gets cleaned up. The deprecated `[build] update_repo_profiled = true` is normalised to `repo_mode = "profiled"` with a one-shot deprecation warning. `packages.toml` entries are applied as override overlays (`source`, `pkgbuild_patch`, `cache`, `reason`); installed packages with no entry use defaults. Source classification is read from `build_state.toml`'s `source` field when present (set at build time) so a previously-built package keeps its origin across runs; falls through to override → pacman-foreign-inference for unrecorded packages. For AUR packages without a build_state record: bulk `aur_info` resolves the real `pkgbase` (split-package fix, e.g. `ob-xd-common` → pkgbase `ob-xd`). Apply positional PKG filter. Group by `pkgbase` to deduplicate split packages. Manifest entries whose package is not installed (e.g. a stored rule for `mesa-git` while repo `mesa` is installed) are not iterated — they are inert rules under the rules-not-install model.

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

**Phase 5 — Build.** Filter to buildable packages: `NEEDS_REBUILD`. (Phase 3 already converted VCS packages to `NEEDS_REBUILD` / `UP_TO_DATE` / `DEVEL_EVAL_FAILED` under `--devel`, so no separate `DEVEL` union is needed at this stage.) Batch makedeps pre-install (single `sudo pacman -S`). AUR dep resolution + build. Single build loop for all packages. `--cleanbuild` (`-C`) prepended by default (suppressed by `--no-cleanbuild`). `--syncdeps`/`-s` and `--install`/`-i` stripped; packages installed in phase 6. `AlreadyBuilt` raised by `makepkg_wrapper.run` (PKGDEST already holds the matching `.pkg.tar`) is treated as a successful build: `_find_existing_artifacts` locates the matching files in pkgdest and queues them for install, instead of marking the pkgbase failed.

With `--install-only` the build loop is replaced wholesale: no makedep batching, no AUR dep resolution, no `makepkg` invocation. For each buildable result, `_find_existing_artifacts(pkgdest_or_pkgbuild_dir, pkgnames, pkgbuild_ver, installed_ver=...)` is called directly. Hits are queued for phase 6; misses log a `[SKIP]` line and are counted alongside the existing `skipped` total.

`_find_existing_artifacts` is a two-stage lookup. First it tries the strict glob `{pkgname}-{pkgbuild_ver}-*.pkg.tar.*` — the common path for non-VCS packages where the static PKGBUILD parse equals the filename version. If that returns nothing it falls back to a pkgname-only glob `{pkgname}-*-*-*.pkg.tar.*`, parses each filename's `(epoch, pkgver, pkgrel)`, and picks the newest by `vercmp`. The fallback is required for VCS (`-git`/`-svn`/...) packages, where `pkgver()` bumps the version dynamically at build time (PKGBUILD `pkgver=0.1.0` → artifact `0.1.0.r45.g1234567`) so the static `pkgbuild_ver` never matches the filename. When `installed_ver` is supplied (always under `--install-only`), the fallback further excludes any artifact not strictly newer than installed, preventing redundant reinstalls or downgrades. The `AlreadyBuilt` call site omits `installed_ver`: makepkg has already proved the artifact exists in PKGDEST, so the lookup just needs to find it.

**Phase 6 — Install + finalize.** Filter the built `.pkg.tar.*` files with `filter_pkgs_to_installed` so only files whose `pkgname` is already present in `pacman -Q` reach `pacman -U` — split `-git` pkgbases emit one file per split pkgname, and rebuilding must not silently add sub-packages the user never installed (e.g. `pipewire-full-git` emits 16 files; only the 2 installed ones get installed). New dependencies built via `build_resolved_deps` are handled on a separate path and are unaffected by this filter. Single `sudo pacman -U` for the kept set. Cache report, final summary, close unified log.

Positional: `[PKG ...]` — optional package names to restrict the run to a subset of packages.

Flags: `--interactive`, `--packages`, `--dry-run`, `--devel`, `--offline`, `--install-only`, `--no-cleanbuild`, `--cleansrc`, `--state-dir`, `--profile-conf`, `--cache-report`, `--no-pkg-log`, `--persist-log`, `--log-dir`, `--makepkg`.

`--install-only` is mutually exclusive with the build-tuning flags `--makepkg`, `--no-cleanbuild`, `--cleansrc`, `--interactive`, and `--cache-report`; argparse rejects the combination. It implies `--offline`. Use it to install artifacts left in PKGDEST by a previous interrupted run, or by a manual `makepkg` invocation, without re-entering the build loop.

**Unattended full update.** `sysforge update` (no positional args) is the supported recipe for a hands-off "rebuild everything outdated" run: walks every installed AUR package plus any repo packages with profiled overrides, rebuilds those flagged `NEEDS_REBUILD`, and automatically clones any missing src dirs. Add `--cleansrc` to also discard divergent upstreams — this is destructive but per-package safe, since `purge_src` refuses any clone that holds uncommitted changes. `--cleansrc` also bypasses the RPC short-circuit so every AUR package in the run is re-cloned from scratch rather than trusting the cached metadata. VCS pkgbases are exempt from `--cleansrc` unless `--devel` is also passed (their checkouts are never touched in the default mode, since the build step skips them too). A refused package is counted as failed and skipped.

### `converge.py`

Implements `sysforge converge` — the flag drift detector. Algorithm:

1. Load `build_state.toml`. Group by `pkgbase`.
2. For each profiled package (`build_mode = "profiled"`): re-resolve the current profile via `parse_pkgbuild` → `match_rules` → `resolve_profile` → `serialize_flags`.
3. Diff the stored `flags_string` against the freshly resolved flags. Packages where the flags differ are `DRIFTED`; identical are `IN_SYNC`. Packages without a stored `flags_string` (built before this feature) are `NO_FLAGS`. Missing PKGBUILD → `NO_PKGBUILD`. Non-profiled packages are silently omitted.
4. Print a per-package summary with flag diffs for `DRIFTED` entries.
5. With `--apply`: rebuild all `DRIFTED` packages via `makepkg_wrapper.run()` with `update=False`. Post-build, the pkg-file set is run through `filter_pkgs_to_installed` before `batch_install_pkgs` so repairing drift on one split pkgname never installs sibling sub-packages the user never chose.

Without `--apply`, the command is read-only — it reports drift but does not rebuild. Positional `[PKG ...]` limits the drift check (and `--apply` rebuild) to the named pkgnames; names not in `build_state.toml` are warned and skipped, matching `update`'s positional behaviour. Flags: `--apply`, `--state-dir`, `--profile-conf`, `--no-pkg-log`, `--persist-log`, `--log-dir`, `--cache-report`.

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

`--hardware` runs a hardware/boot-readiness axis: it inventories all PCI/USB devices and flags any present device with no driver bound (`device_probe.check_unsupported_devices`), then audits the **running** kernel's `.config` (from `/proc/config.gz` or `/boot/config-$(uname -r)`) against the detected devices and root topology (`kernel_safety.audit_resolved_config`) — the on-the-spot diagnostic for "device X has no driver" / the `CONFIG_SND_PCI`-class trap. Unlike `--graphics`, `--hardware` needs no package targets and can be run on its own (`sysforge doctor --hardware`); it renders findings through `diagnostics.render_axis` in the `[SEV] check_id: message → remediation` format. `error`-severity findings (brick-class boot-config drops, carried via `is_brick`) contribute to the exit code; device-driver and degraded findings warn.

`--toolchain` runs a configured-vs-installed toolchain axis via `llvm_state.detect_toolchain_config_mismatch` (which wraps the sanctioned `collect_llvm_state` entry point — provenance reporting, not a third toolchain *health* probe). When `toolchain.toml` requests a custom LLVM toolchain (`enabled = true`, `compiler = "llvm"`) but stock repo LLVM is installed (`install_origin == "repo"`), or the PGO profdata is version-skewed, it reports the mismatch in the same `[SEV] check_id: message → remediation` format and contributes `error`-severity findings to the exit code. Like `--hardware`, it needs no package targets and runs standalone (`sysforge doctor --toolchain`); the two can be combined (their exit codes OR together). This is the standalone surface of the same check the kernel stage emits before a build.

`--pacman` runs a pacman / system-integrity axis (`primitives/system_probe.py::collect_system_findings`) — all read-only against the *local* database (never `-Sy`): `pacman -Dk` local-db dependency consistency (missing deps → `error`), a lingering `/var/lib/pacman/db.lck` from an interrupted transaction (`warn`), unmerged `*.pacnew`/`*.pacsave` config files under `/etc` (`warn`), and `pacman -Qtdq` true orphans (`info`). Standalone (`sysforge doctor --pacman`).

`--state` runs a sysforge state-integrity axis (`primitives/state_probe.py::collect_state_findings`) — read-only inspection of sysforge's *own* persisted state: recorded build failures from `build_state.toml`'s `[failures]` table (each `warn`, surfacing the `build_diag` signature + `fix_cmd` when present), an interrupted stage sentinel via `StageSentinel.get_active()` (`error`, carrying the recorded `recovery_cmd` — it does **not** call the recovering `check_and_recover_stale_sentinel`), and build-state drift vs the live pacman db (`info`, zombie entries for uninstalled packages). The last source-sync `STATUS_*` is intentionally *not* surfaced: the source-sync scheduler cache is per-process, so a standalone `doctor` run has no sync results. Standalone (`sysforge doctor --state`).

`--boot` runs a boot/kernel-runtime axis (`doctor._collect_boot_findings`, reusing `primitives/kernel_safety`) — the running-system analog of the kernel stage's gates 1/3: per-bootable-kernel boot-artifact verification (`verify_boot_artifacts`: vmlinuz + initramfs + a boot entry; gaps are brick-class `is_brick` → exit code), a recovery-fallback check (`find_fallback_kernels`; only one bootable kernel → `info`), `/boot` free space (`check_boot_mount_space`), and DKMS modules for the running kernel (`check_dkms_for_kernel(running_kernel_release())`). The running-kernel `.config` device audit stays in `--hardware` (it is device-driver coverage, distinct from boot-artifact readiness), so there is no double-report. Standalone (`sysforge doctor --boot`).

`--services` runs a services/runtime-health axis (`primitives/runtime_probe.py::collect_runtime_findings`): failed systemd units (`systemctl --failed` → each `error`) and firmware a driver requested but could not load this boot (best-effort parse of `journalctl -k -b` for "Direct firmware load … failed" → `warn`; degrades silently when the journal is unreadable). Standalone (`sysforge doctor --services`).

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

**`--apply` bridge.** `--apply` (implies `--suggest`) hands the REBUILD-classified candidates to `sysforge update` for actual rebuild. Drift-rebuild only in v1.x: install candidates (not yet installed) are surfaced as `→ run: sysforge build <pkg>` informational lines but never invoked. Repo packages outside `sysforge update`'s scope (no behavior-changing override, no `repo_mode = "profiled"`) are surfaced as `→ run: sudo pacman -S <pkg>` and skipped. Foreign packages — and repo packages eligible under `repo_mode = "profiled"` — are gathered into a single eligible list, the user is prompted (`--no-confirm` skips), and `cmd_update` is invoked with that list as the positional pkgname filter. `--dry-run` reports the rebuild list without invoking the build. `--apply`'s exit code dominates the doctor exit — a successful rebuild produces exit 0 even if doctor surfaced issues. The bridge is intentionally thin: rather than extracting `update.py`'s build loop into a separate primitive, doctor synthesizes a `cmd_update` args namespace and reuses the existing path verbatim.

> **Real-world status (2026-05-02): unit-tested only.** The unit tests
> (`tests/test_doctor.py::test_apply_*`) mock `cmd_update` entirely, so the
> end-to-end "doctor finds drift → update rebuilds → install succeeds" path
> has not been exercised against a live system yet. Treat the v1.x release
> as "ships --apply behind tested-by-mock semantics"; full integration
> verification is pending the next session.

Public API: `cmd_doctor(args)`. Positional `[PKG ...]` and flags `--graphics`, `--hardware`, `--toolchain`, `--pacman`, `--state`, `--boot`, `--services`, `--all`, `--repo`, `--shallow`, `--quiet` (suppress clean lines, show only issues), `--suggest` / `-s` (inline + end-of-run candidate lookup via files db), `--apply` (drift-rebuild bridge), `--no-confirm`, `--dry-run`. New axes register in `_SYSTEM_AXIS_ORDER` / `_AXIS_FLAGS` / `_system_axes` with a `_collect_<axis>_findings` producer (looked up through module globals so tests can monkeypatch them).

Log tag: `[DOC]`. Primitive lookup helper lives in `sysforge/primitives/provides_lookup.py` — see the `provides_lookup.py` subsection for the public API. NEEDED-soname extraction reuses `abi_check.needed_sonames` (public since doctor calls it directly for ABI-issue suggestions). System-state probes live in `sysforge/primitives/graphics_probe.py` — log tag `[GFX]`, public API `check_system_graphics(config, *, gpu_vendors=None)`; invoked from `cmd_doctor` when `--graphics` is set.

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
- **Pagination.** `state list`, `state orphans`, and `state failed` pipe their output through `$PAGER` (or `less -RFX` / `more` as fallbacks) when stdout is a TTY. `--no-pager` disables. The pager wrapper `_maybe_pager(use_pager)` lives in `state_cmd.py` and degrades gracefully when no pager binary is available.

Public API: `cmd_state_list(args)`, `cmd_state_repair(args)`, `cmd_state_orphans(args)`, `cmd_state_failed(args)`. All except `orphans` accept `--state-dir`; `state orphans` reads PKGDEST from the layered system makepkg.conf via `pacman.get_pkgdest()`. `list`, `orphans`, and `failed` accept `--no-pager`; `failed` also accepts `--clear`/`--clear-all`.

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

The explicit-sync check is the load-bearing one for NVIDIA-on-Wayland black-window breakage: when the compositor doesn't advertise `wp_linux_drm_syncobj_manager_v1`, XWayland games on NVIDIA fall back to implicit sync which is known-broken on the NVIDIA explicit-sync driver path. Note: the registry global is `wp_linux_drm_syncobj_manager_v1` — the protocol-document name `linux-drm-syncobj-v1` (i.e. the bare `wp_linux_drm_syncobj_v1` substring) never appears as an advertised global.

Log tag: `[GFX]`. No writes, no sudo, no network.

### `device_probe.py`

Full PCI/USB device inventory plus a driver-coverage check. Read-only; same `_run`/`_read_text`/frozen-finding idiom as `graphics_probe.py`. Walks `/sys/bus/{pci,usb}/devices`, reading each device's `modalias`, class, and the `driver` symlink (the bound-driver signal validates the *running* kernel). The device→module link is resolved against a complete reference kernel's `modules.alias` + `modules.builtin.modinfo` via `fnmatch` (exactly modprobe's matching), cached per reference dir; `find_reference_modules_dir()` picks the newest installed stock kernel (excludes any `custom` modules dir) so a custom kernel that omitted a driver doesn't hide its own gap. A curated `_MODULE_TO_KCONFIG` table maps common modules (audio/NIC/NVMe/USB/GPU) to their `CONFIG_*`; unknown modules degrade to "module name only".

Public API: `enumerate_devices(buses=("pci","usb")) -> list[Device]`; `check_unsupported_devices(*, devices=None) -> list[DeviceFinding]` (flags functional — non-bridge/hub — devices with no driver and a known expected module); `find_reference_modules_dir() -> Path | None`. `Device` carries `bus`/`address`/`modalias`/`class_id`/`description`/`driver`/`expected_modules`/`suggested_kconfig`; `DeviceFinding` mirrors `GraphicsFinding`. Consumers: the hardware stage (`[[devices]]` inventory + WARNs), `doctor --hardware`, and `kernel_safety.audit_resolved_config` (device-driver coverage). Filesystem roots (`_SYS_BUS`, `_MODULES_BASE`) are module-level for test repointing.

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
[profiles.bare]
# Fallback profile, no flags

[profiles.standard]
extends = "bare"
# Default profile uses system gcc + binutils. LLVM is opt-in — override
# CC/CXX in a user profile (and optionally set AR/NM/RANLIB/STRIP to the
# llvm-* variants) or use `sysforge run toolchain --compiler=llvm`.
CC = "gcc"
CXX = "g++"
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
pgo_store = "/var/tmp"

[profiles.patched]
extends = "optimized"
build_mode = "patched_pkgbuild"

[profiles.kernel]
extends = "bare"
build_mode = "kernel"
batch = true
makepkg_flags = ["--noconfirm", "--syncdeps", "-f", "-c"]
```

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

`emit_makepkg_conf` runs a series of guards after profile overrides are applied but before the conf is written. Each guard detects and reconciles toolchain incompatibilities, logging at `[WARN][FLAG]`. Guards run in this order:

1. **Linker guard** — detects the effective linker from `-fuse-ld=X` in LDFLAGS (default: `ld`/bfd). Strips lld-only flags (`--icf=*`) when the effective linker is not lld.

2. **RUSTFLAGS linker reconciliation** — if RUSTFLAGS declares `-C link-arg=-fuse-ld=X` with a different linker than LDFLAGS, overrides it to match. Handles both spaced (`-C link-arg=...`) and compact (`-Clink-arg=...`) forms. Prevents LTO link failures from mismatched linkers (e.g. mold cannot process LLVM bitcode produced with lld).

3. **GCC thin-LTO rewrite** — `-flto=thin` is clang-only. When GCC is in effect, rewrites `-flto=thin` → `-flto` in LTOFLAGS, CFLAGS, CXXFLAGS, and LDFLAGS. Falls back to system conf values when the profile doesn't override a key.

4. **GCC + lld LTO disabling** — GCC LTO produces `.gnu.lto_*` bitcode that only GNU ld/gold can process; lld cannot read it. When GCC is in effect and the effective linker is lld, LTO is disabled entirely: LTOFLAGS cleared, `-flto*` stripped from flag keys, and `lto` flipped to `!lto` in OPTIONS (prevents makepkg's `${LTOFLAGS:--flto}` fallback).

5. **Full LTO stripping** (PGO only) — strips `-flto`/`-flto=full` from CFLAGS/CXXFLAGS/LDFLAGS and clears LTOFLAGS during PGO passes.

6. **lib32 march scrub** — when `invoke_makepkg` detects a `lib32-*` build (`pkgbuild_path.parent.name.startswith("lib32-")`), `emit_makepkg_conf` strips host-CPU-specific or 64-bit-only `-march=` tokens from CFLAGS and CXXFLAGS in both profile overrides and system-conf passthrough. Stripped values: `-march=native` (resolves to the host's amd64 microarch — `znver3` on Zen 3), `-march=x86-64`, `-march=x86-64-v2`, `-march=x86-64-v3`, `-march=x86-64-v4` (microarch levels defined only for 64-bit code). Other `-march=` values (e.g. `-march=i686`) and all non-`-march` flags are preserved. Without this guard a `[profiles.bare]` lib32-* build inherits the system conf's `-march=native` unchanged, and multilib GCC then refuses the compile with a confusing "unrecognized target arch" error rather than a clear "host flag stripped for lib32" log line.

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

`--interactive` on `sysforge build` does two things: it strips `--noconfirm` from the profile's `makepkg_flags`, and it makes `invoke_makepkg` inherit the parent's stdout/stderr instead of piping them through the line-classification loop. Stdio passthrough is what keeps unbuffered prompts visible — pacman's conflict prompt (`Remove sysforge? [y/N]`) and similar `\r`/no-newline output reaches the terminal immediately rather than sitting in a pipe buffer until the user blindly presses Enter. The tradeoff is that line-based output classification (`failed_stage`, `missing_deps`, `toolchain_mismatch` auto-retry, stdout-match fallback for `AlreadyBuilt`, `captured_output` for `auto_repair`) is bypassed in this branch; exit-code-based detection (`returncode == 13` → `AlreadyBuilt`, `returncode == 8` → install failure) still fires. Useful during development to review makepkg prompts without editing the profile; not appropriate for `update` / `converge` batch flows, which depend on the classification path and therefore default `interactive=False`.

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

To make the pattern scan work for every build mode, `invoke_makepkg` uses a `Popen`-with-tee capture path for non-interactive builds: each line is matched against the patterns, then forwarded to stdout (or to `[DEBUG][MAKEPKG]` when verbosity ≥ 3). stdin remains inherited so sudo prompts still work. The capture path is **skipped entirely when `interactive=True`** (see §Interactive mode) — in that branch the child inherits stdout/stderr directly, so the toolchain-mismatch auto-retry is unavailable. Batch flows (`update`, `converge`, pipeline stages other than `kernel`) leave `interactive=False`, so they retain the retry.

### Build-failure auto-repair

> **Status: implemented (all 4 scenarios).** Lives in `sysforge/primitives/auto_repair.py`. `_run_build`'s outer loop catches `CalledProcessError`, walks `auto_repair.REGISTRY`, and on the first match runs the corresponding repair before retrying.

`invoke_makepkg`'s line-tee captures every stdout line into `captured_lines` and attaches the list to the raised `CalledProcessError` (and `ToolchainMismatchError`) as `captured_output`. `_run_build` wraps that buffer in a `BuildOutputAccumulator` (lines + optional `srcdir` for on-disk inspection) and feeds it to `auto_repair.apply_first_match`. Each scenario's `detect(accum)` returns a `MatchInfo` (or `None`); on match the wrapper consults `[failure_handling]` for the per-scenario behaviour, runs `repair(pkgbuild_dir, info)`, and re-enters the build loop. The set of already-fired scenarios is tracked per build (`_repaired_scenarios`) so a misdetected error cannot loop — once a scenario fires it is excluded from subsequent matches in the same build.

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
| `[FLAG]` | CLI toolchain overrides (`--cc`/`--cxx`/`--ld`), linker guard, conflict group firing, token replacement |
| `[GIT]` | git pull/rebase status during PKGBUILD updates |
| `[KERNEL]` | Kernel-specific flag handling in wrapper; kernel stage: lsmod snapshot, kconfig fragment, build, post-install |
| `[MAKEPKG]` | makepkg subprocess output capture |
| `[PATCH]` | PKGBUILD flag extraction, patching, artifact lifecycle; noninteractive kconfig target replacement |
| `[PGO]` | PGO profiling pass operations in wrapper and toolchain stage |

**Profile / config subsystem:**

| Tag | Covers |
|---|---|
| `[CONFIG]` | Config file loading (`profiles.toml`: flag profiles, conflict groups, consumes inference) |
| `[GROUPS]` | Package group resolution |
| `[PROFILE]` | Profile resolution, rule matching, extends chain |
| `[STATE]` | Pipeline state directory resolution |

**AUR / package management:**

| Tag | Covers |
|---|---|
| `[AUR]` | AUR name cache lifecycle, clone operations |
| `[DEP]` | Soname dependency graph checks |
| `[DOC]` | `sysforge doctor` — installed-package depends + linkage health check |
| `[FAILURE]` | Failure scenario dispatch |
| `[GFX]` | `graphics_probe` — system-state graphics checks (kernel params, compositor protocols, driver skew) |
| `[MANIFEST]` | AUR RPC queries |
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
| `[CLI]` | CLI entry point (invocation logging) |
| `[CONVERGE]` | `sysforge converge` — drift detection and rebuild |
| `[FETCH]` | `sysforge fetch` — PKGBUILD download/update |
| `[UPDATE]` | `sysforge update` — version check and rebuild |

---

## Man Pages

**Current (v1.0):** `argparse-manpage` generates `man/sysforge.1` from the argparse parser exposed via `_build_parser()` in `cli.py`. Generated during `make man` and during the PKGBUILD `build()` step (requires `python-argparse-manpage` makedepend). The generated page is checked into git so AUR-built tarballs ship with it without requiring AUR users to have `argparse-manpage` installed at unpack time; the release flow regenerates it via `make man` and commits the result alongside the version bump. Makefile target: `make man`.

**v1.0 planned migration — scdoc hybrid:**

Replace the auto-generated page with a hand-written scdoc template (`man/sysforge.1.scd.in`) covering SYNOPSIS, DESCRIPTION, FILES, EXAMPLES, and SEE ALSO, with OPTIONS sections auto-generated from the argparse parser by a small script (`tools/gen_options.py`) that walks `parser._subparsers` and emits scdoc-formatted option blocks. The Makefile combines them:

```
tools/gen_options.py → man/sysforge.1.scd.gen
sed -f .gen .scd.in  → man/sysforge.1.scd
scdoc               → man/sysforge.1
```

This gives hand-crafted prose with OPTIONS that stay automatically in sync with the CLI. `scdoc` becomes a makedepend; `python-argparse-manpage` is dropped.

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

**LLVM target derivation.** The hardware stage also writes `host_arch` (from `uname -m`) and an autodetected `llvm_targets` list — CPU backend from arch (`x86_64`→`X86`, `aarch64`→`AArch64`, `armv7l`→`ARM`, `riscv64`→`RISCV`, `ppc64le`→`PowerPC`) plus GPU backends from `gpu_vendors` (`amd`→`AMDGPU`, `nvidia`→`NVPTX`; `intel` contributes nothing because the Mesa Intel drivers don't depend on an LLVM backend). Consumed by `pkgbuild_patcher.patch_llvm_targets` when building any LLVM-toolchain package.

**`hardware_profile.toml` layout:**
```toml
[hardware]
cpu_vendor  = "AuthenticAMD"
cpu_family  = 25
cpu_model   = 33
host_arch   = "x86_64"
gpu_vendors = ["nvidia"]
llvm_targets = ["X86", "NVPTX"]
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
```

Written atomically (write-then-rename) to `<state_dir>/hardware_profile.toml`. The file has four readers:

- **`pipeline/stages/kernel.py`** — `_load_hardware_kconfig()` consumes `[kconfig]`; entries flow into the `sysforge.config` fragment merged into `.config` via `merge_config.sh`. Absence is non-fatal (entries skipped with an INFO log).
- **`primitives/llvm_targets.py`** — `_read_hardware_targets()` consumes `[hardware] llvm_targets`; resolves the `LLVM_TARGETS_TO_BUILD` cmake arg injected by `pkgbuild_patcher.patch_llvm_targets`.
- **`pipeline/stages/reconfigure.py`** — surfaces the file in the pre-build config review so the user can hand-edit before kernel build.
- **`commands/doctor.py`** — consumes `[hardware] gpu_vendors` to scope the `doctor --graphics` health checks.

### Architecture-aware kconfig disable

In addition to the positive `=y` enables above, the hardware stage emits an `=n` line for every CONFIG_* key owned by a kernel architecture domain that is **not** the host's domain. The data lives in two module-level constants in `pipeline/stages/hardware.py`:

- `_ARCH_OWNED_KCONFIG: dict[str, frozenset[str]]` — `domain → set of CONFIG_* keys that only make sense when the kernel is targeting that domain`. Domains: `x86`, `arm` (32-bit), `arm64`, `riscv`, `powerpc`, `mips`, `sparc`, `loongarch`. Keys are **curated, not exhaustive** — top-level architecture umbrellas (`CONFIG_X86`, `CONFIG_ARM64`, …) plus the major SoC family umbrellas under `arm64` (`CONFIG_ARCH_QCOM`, `CONFIG_ARCH_TEGRA`, `CONFIG_ARCH_ROCKCHIP`, etc.). The Kconfig system itself gates most SoC drivers via `depends on ARCH_<vendor>`, so disabling the umbrella culls the subtree from `make nconfig` automatically.
- `_HOST_ARCH_TO_KCONFIG_DOMAIN: dict[str, str]` — `uname -m → domain`. Covers `x86_64`/`i686`/`i386` → `x86`, `aarch64` → `arm64`, `armv7l`/`armv6l` → `arm`, `riscv64`/`riscv32` → `riscv`, `ppc64le`/`ppc64`/`ppc` → `powerpc`, `mips`/`mips64` → `mips`, `sparc`/`sparc64` → `sparc`, `loongarch64` → `loongarch`.

`_arch_disable_kconfig(host_arch)` resolves the host domain, then iterates every *other* domain in the registry and emits `{CONFIG_X: "n"}`. Keys appearing in the host's own domain set are filtered out as a defensive guard (no clobber if a future kconfig key gains a presence in multiple domains). Unknown `host_arch` returns an empty dict and logs a WARN.

The `=n` entries land in the same `[kconfig]` table as the existing `=y` enables, so the kernel stage's existing merge path — `merged = {**hw_kconfig, **manual_kconfig}` — applies unchanged. A user cross-compiling or otherwise wanting an arch-disabled key re-enabled puts an explicit `[[kconfig]] option = "CONFIG_ARM64" value = "y"` in `kernel.toml`; the existing manual-override-wins-with-WARN behaviour at `pipeline/stages/kernel.py:344-346` extends to arch-disable entries.

### Tested hardware scope

Design ambition is broad (every kconfig domain in the registry, every CPU/GPU brand the detection code recognises), but real-world validation is currently narrow. This section documents which paths have actually been exercised so users on untested hardware understand where they are taking implemented-but-unvalidated code paths.

**Tested on real iron** (Keith's dev box):
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

---

## Release Plan

- **GitHub:** public from day one; source of truth for all code
- **v0.1.0** (shipped) — profiled AUR helper. Userspace commands stable under real use: `build`, `fetch`, `update`, `resolve`, `doctor`, `converge`, `setup`, `packages` (list/add/remove/sync), `run pipeline`, `run reconfigure`, `run packages`. The `run toolchain` and `run kernel` stages shipped in this release; they were temporarily reclassified as experimental in v1.0 pending more testing and re-promoted in v1.x — see notes below. Marks the AUR publication milestone.
- **v0.2.0** (shipped) — follow-up release on the v0.1.0 surface: VM tooling (`tools/vm/`, `make vm-*` targets), install-path fixes for fresh Arch systems on Python 3.14, bulk-operation progress indicator, VCS detection and paging fixes, `doctor --graphics` scope refinement.
- **v1.0** (shipped) — system bootstrapper. Stages 1–4 fully implemented (partition, base_install, hardware, configure). Configure stage installs systemd-boot, enables NetworkManager/sshd, creates primary user with sudo, writes shell dotfiles, sets passwords, and sets the configured default login shell. Build-state persistence, `pacman -Q` superset coverage, and coloured CLI output all landed in 2026-04. v1.0 also reclassified `run toolchain`, `run kernel`, and the `sysforge update` PGO-toolchain profdata-reuse path (`build_mode = "pgo_llvm_toolchain"`) as **experimental, deferred post-1.0**: those code paths shipped but emitted a runtime `[WARN]` and were recommended-off for 1.0 users. The reclassification was lifted in v1.x once the implementations stabilised. Default `compiler` resolution is `gcc`; LLVM is opt-in. The shipped `[profiles.standard]` uses the system gcc/binutils; LLVM components (`clang`, `lld`, `llvm`, `compiler-rt`) are `optdepends` on the sysforge PKGBUILD and only required by the opt-in LLVM profile and `run toolchain --compiler=llvm`. Published to AUR via `tools/release.sh`.
- **v1.x:** `repo_mode = "profiled"` support in `sysforge update`; wrapping `pacman -Syu` inside `sysforge update` for a full AUR-helper experience; man page migration from `argparse-manpage` to a scdoc hybrid (hand-written narrative + auto-generated OPTIONS — see Man Pages section below); package groups (named DE sets for opt-in without enumerating every package); rule priority auto-calculation (CSS-specificity-style scoring from rule conditions); configure stage additions (btrfs snapshots, ccache/sccache init check, build time estimates); LLVM target filtering from hardware detection. The toolchain and kernel stages (and the `pgo_llvm_toolchain` update path) have been re-promoted from experimental — see the V1.x Roadmap *Landed* list below.

### AUR publishing process

Releases are driven by three Makefile targets — `make release-major`, `make release-minor`, `make release-patch` — each of which calls `tools/release.sh --bump=<level>`. The script handles the full flow end-to-end with a single up-front summary + approval prompt and one mid-run pause for the manual tag push. Phases:

1. **Bump, commit, tag.** Rewrites `pyproject.toml` (the single source of truth for version), `PKGBUILD` `pkgver=`, `PKGBUILD-git` `pkgver=` (leading `X.Y.Z` only — the `.r0.g0000000` suffix is preserved as the placeholder for the dynamic `pkgver()`), the `<!--version-->vX.Y.Z<!--/version-->` markers in `README.md` and `DESIGN.md`, regenerates `uv.lock` (via `uv lock`) and `man/sysforge.1` (via `make man`), then makes a single `release: vX.Y.Z` commit and tags it.
2. **Push pause.** Prints `git push origin main && git push origin vX.Y.Z` and waits on ENTER. The user pushes manually (releases are deliberate, not background events). The script verifies the tag is on `origin` before continuing.
3. **Post-tag artifacts.** Fetches the GitHub tarball sha256, updates `sha256sums=` in `PKGBUILD`, **validates both `PKGBUILD` and `PKGBUILD-git` in a clean chroot**, regenerates `.SRCINFO` and `.SRCINFO-git` (both gitignored — local artifacts only), and makes a second `release: vX.Y.Z sha256` commit (the `.SRCINFO` files do not get committed).
4. **Final instructions.** Prints `git push origin main` and the `git clone`/`cp`/`commit`/`push` sequence for the `sysforge` and `sysforge-git` AUR repos. The user runs those manually.

If interrupted between phases (Ctrl-C at the push pause, or a transient failure), re-running the same `make release-*` command resumes correctly: the script detects that the tag for the *current* `pyproject.toml` version already exists at HEAD and skips Phase 1.

The version markers in `README.md` and `DESIGN.md` are HTML comments (`<!--version-->vX.Y.Z<!--/version-->`) so they render invisibly. Only the marked token rotates per release — historical version mentions in prose (`v0.1.0`, `v0.2.0`, `v1.0`, `v1.x`) are deliberately not wrapped and stay frozen. The `versions` check group below enforces lockstep across all marker locations; release pre-flight refuses to run if the markers (or the `pkgver` lines, or any other version-bearing field) are out of sync.

**Shipped-file pre-release checks.** Phase 1 of `tools/release.sh` (and the standalone `make check-shipped` / `make pre-release` targets) gate on `tools/check_shipped.py`, which validates every artifact the PKGBUILD ships:

- **`configs`** — every `etc/sysforge/*.toml` is parsed through its real runtime loader (`load_config`, `load_sysforge_toml`, `load_bootstrap`, the stage `_load_*` helpers); unknown top-level sections/keys against a per-file allowlist are an error; missing `tests/data/etc/sysforge/` counterpart for every shipped TOML (except per-host `bootstrap.toml`) is an error.
- **`pkgbuild`** — every `install -Dm…` source in `PKGBUILD` must exist in the working tree; every `$pkgdir/etc/…` install target must be declared in `backup=()`, and vice versa (no stale `backup=` entries); `sha256sums` is not a placeholder (`SKIP`, all-zero, `DRYRUN…`).
- **`pkgbuild_parity`** — `PKGBUILD` and `PKGBUILD-git` parse to the same dict (via `pkgbuild_meta.parse_pkgbuild`) except for a tightly-scoped allowlist of keys that are *supposed* to differ (`pkgname`, `pkgver`, `pkgrel`, `pkgdesc`, `source`, `sha256sums`, `conflicts`, `provides`). `depends` / `makedepends` / `optdepends` / `backup` arrays must be byte-identical.
- **`hooks`** — every `etc/pacman.d/hooks/sysforge-*.hook` `Exec` line must invoke `tools/pacman-hook-helper.sh` and pass a subcommand the helper documents (`kernel`, `toolchain`, `buildstate`).
- **`completions`** — every verb and every long-flag in the argparse parser tree (reached via `sysforge.cli._build_parser`) must appear in both `completions/_sysforge` and `completions/sysforge.bash`; stale top-level verb entries in the zsh case statement (function-suffix matches case-word but parser doesn't know the verb) are an error. Mirrors the `completions-cli-parity` subagent's audit; this is the mechanical layer that runs every release.
- **`versions`** — `pyproject.toml` `[project] version` must equal `PKGBUILD` `pkgver=`, the leading `X.Y.Z` of `PKGBUILD-git` `pkgver=`, and every `<!--version-->vX.Y.Z<!--/version-->` marker in `README.md` and `DESIGN.md` (literal `vX.Y.Z` placeholder strings in prose are filtered out by the `\d+\.\d+\.\d+` constraint).
- **`manpage`** — regenerates `man/sysforge.1` via `argparse-manpage --module sysforge.cli --function _build_parser` (the exact invocation `make man` uses) into a temp file and diffs against the committed page; any difference is an error, with the fix `make man && git add man/sysforge.1`. The `.TH … "DATE"` header is normalised before diffing so the daily date change isn't a finding. Skipped with a `warn` if `argparse-manpage` isn't on PATH.

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

## Re-converge

Two commands address drift in sysforge-managed packages:

**`sysforge update [PKG ...]`** (implemented) — handles **version drift**. After the `source_sync` scheduler refreshes each PKGBUILD dir (one batched AUR RPC call followed by per-package clone or shallow fetch as needed), it compares the new `pkgver`/`pkgrel`/`epoch` against the installed version via `vercmp`. Packages where the PKGBUILD is newer are rebuilt with the current profile. VCS packages (`-git`/`-svn`/`-hg`/`-bzr`) are reported as `DEVEL` and skipped by default; with `--devel`, each VCS package's `pkgver()` is resolved up-front via `vcs_pkgver.evaluate_vcs_pkgver` (one `makepkg --nobuild` pass per VCS pkgbase) and the resulting version is vercmp'd against installed — only genuinely-stale packages are rebuilt; up-to-date packages are reported as `UP_TO_DATE` and skipped. Resolution failures (broken `pkgver()`, transient network) report `DEVEL_EVAL_FAILED` and are also skipped. One or more package names may be given as positional arguments to restrict the run to a subset of sysforge-managed packages; unrecognised names are warned and skipped.

**PGO toolchain packages** (`build_mode = "pgo_llvm_toolchain"`) are handled specially during update. `makepkg_wrapper.run()` reads `toolchain.toml → pgo_store`, checks for a saved `clang.profdata` and its `clang.profdata.version` sidecar, and compares the sidecar's LLVM major version against the PKGBUILD's `pkgver` major. If they match, `-fprofile-use=<profdata>` is injected and the build proceeds as a PGO-optimised build. If profdata is absent or version-mismatched (e.g. after a major LLVM bump), the user is prompted: **[p]lain build or [s]kip (default: skip)**. In non-interactive mode the build is skipped automatically. Skipped packages are counted separately in the update summary and do not count as failures. To rebuild profdata after a major version bump, run `sysforge run toolchain`. The toolchain stage itself also reuses compatible profdata — see the **Profdata reuse** section under stage 6.

**Stale-profraw post-build check.** After every non-PGO-managed build, `makepkg_wrapper.run()` globs `pgo_store` for `*.profraw` files. Any file with `mtime >= build_start - 1s` is treated as **fresh** — it was written by the build just completed, which means an instrumented LLVM is still installed on the system and the build was leaking profile data. The wrapper fatals, telling the user to reinstall `llvm`/`llvm-libs` or run `sysforge run toolchain`. Files strictly older than `build_start` are **orphans** left behind by a prior failed or partial toolchain run whose instrumented binaries the user has since cleaned up; these are unlinked in place and an info line is logged. The split makes the safety net self-healing: once the system is clean, the next build purges the residue automatically instead of requiring manual cleanup of `pgo_store`.

**`sysforge converge`** (implemented) — handles **profile/flag drift**. Same package version but different compiler configuration — e.g. profile changed, new flag added, or build mode switched. At build time, `makepkg_wrapper.run()` stores the resolved flags string per package in `build_state.toml`. `converge` re-resolves the current profile for each package and diffs the result against the stored flags string; packages where the flags have changed are reported with a flag diff. Without `--apply` the command is read-only; `--apply` rebuilds all drifted packages.

`build_state.toml` is the shared source of truth for both commands. Written by `makepkg_wrapper.run()` after each successful build.

**`sysforge doctor`** is the third drift-surface command and completes the picture — it is read-only and catches the drift class neither of the above detects: **ABI / linkage drift** on already-installed packages, e.g. a partial graphics-stack rebuild leaving `steam` linked against a `libfoo.so.N` that the system no longer exposes. See the `doctor.py` subsection for the full algorithm. Together: `update` → version drift, `converge` → flag drift, `doctor` → ABI drift.

DAG stages are categorised as **bootstrap-only** (partition, base_install, configure) or **repeatable** (hardware, reconfigure, toolchain, packages, kernel). Only repeatable stages participate in re-converge runs. `hardware` is repeatable because re-detecting after a hardware change (e.g. GPU swap) is safe and needs no root.

---

## Known Gaps

Implemented behaviour that is incomplete or has known limitations. These are not deferred features — they are holes in currently active code.

**`sysforge update` iterates installed repo packages opt-in via `[build] repo_mode = "profiled"`, but splits them into source-build vs pacman-upgrade based on per-package overrides.** Under the rules-not-install model, `sysforge update` always walks every installed AUR package (`pacman -Qm`) plus any repo packages whose override sets a behavior-changing field (`pkgbuild_patch`, `cache`, `reason`). With `repo_mode = "profiled"` in `packages.toml`, every installed repo package is iterated as well — but only the *overridden* subset (`repo_class = "source"`) goes through `pkgctl repo clone` (via `source_sync._sync_one` calling `pkgctl_checkout` on first visit and `git_fetch_and_compare` on subsequent runs, with a clean-tree hard-reset to upstream when the local clone diverges) and into the source-build loop. The remaining unmodified repo packages (`repo_class = "pacman"`) take a fast path: one batched `checkupdates` call (`primitives.pacman.checkupdates_map`) resolves their pending-upgrade versions in a single subprocess; vercmp against the installed version emits `NEEDS_PACMAN_UPGRADE`; one terminal `sudo pacman -Syu` after Phase 6 (install) does the actual upgrade. This is what makes "track every installed package" tolerable on a maintained workstation — without the split, every repo package would mean an individual `git fetch` against the Arch packaging tree on every update run. The post-install ordering matters: source-built artifacts hit the system first so the `IgnoreGroup = sf-build` line added by `sysforge setup` protects them when `pacman -Syu` runs. If `checkupdates` is missing (no `pacman-contrib`), pacman-class packages report `SKIPPED_NO_CHECKUPDATES` and no `pacman -Syu` is dispatched. Default behaviour (`repo_mode = "pacman"` or unset) is unchanged — only override-tagged repo packages are in scope, no pacman -Syu side effect. The legacy `update_repo_profiled = true` is a deprecated alias normalised by the loader. `repo_mode` is the same key consulted by the packages-stage build path; one key governs both surfaces.

**`sysforge build` already routes repo packages through `pkgctl_checkout` automatically.** `find_pkgbuild` (`primitives/config.py:91`) checks `is_repo_package()` before AUR-clone fallback, so `sysforge build firefox` Just Works for any repo package — no `repo_mode` plumbing required on the build side.

**`repo_mode = "profiled"` is the canonical repo-handling key.** The `[build] repo_mode = "pacman" | "profiled"` setting in `packages.toml` is parsed and honoured by `run packages` / `run pipeline` (repo packages with `repo_mode = "profiled"`, or per-package `pkgbuild_patch = true`, are built from source via `_build_aur()` using `find_pkgbuild` → `pkgctl_checkout`) and by `sysforge update` (which iterates every installed repo package under the same key). `sysforge build` consults `find_pkgbuild` independently. The legacy `update_repo_profiled` key is a deprecated alias the update loader normalises with a one-shot warning.

**`packages.toml [build] pkgbuild_src_dir` and `profiles.toml [paths] pkgbuild_src_dir` are separate.** The pipeline's `_resolve_pkgbuild` prefers `[build] pkgbuild_src_dir`; falls back to `[paths] pkgbuild_src_dir`. They can point to different directories or the same one — there's no enforcement that they match.

**`[env_precedence]` config table — design cancelled.** The original design proposed a priority stack (wrapper profile = 100, makepkg.conf = 80, shell passthrough = 20, PKGBUILD export = 10) and an `[env_precedence]` TOML table to configure it. This design is superseded. The current model is simpler and more predictable: build tool vars (`CC`, `CFLAGS`, `LDFLAGS`, etc.) are stripped from the inherited shell env in `invoke_makepkg` before makepkg runs — the temp conf is the sole authority for all makepkg-managed keys. Shell env bleed-through is not a configurable priority; it is prevented entirely. SysForge bootstrap vars (`SYSFORGE_STATE_DIR`, `SYSFORGE_CONFIG_DIR`) are exempt — they are SysForge's own interface, not build tool vars, and are not stripped. The `[env_precedence]` table will not be implemented.

**`[FLAG]` tag — partial coverage.** Emitted for: CLI toolchain overrides (`--cc`, `--cxx`, `--ld`), linker token replacement and injection, linker guard stripping, RUSTFLAGS linker reconciliation, GCC thin-LTO rewrite, GCC+lld LTO disabling, conflict group firing (logs group name, evicted tokens, inserted token), and prefix-match token replacement during `merge_extends`. Not emitted for: `apply_patch_pkgbuild` token changes (those use `[PATCH]`).

**`[CACHE]` ThinLTO probe is per-build, not per-run.** `emit_system_probes()` (ld.so mtime, pacman cache size) runs once at the start of each pipeline or build invocation. ThinLTO cache size is probed inside `_run_build()` because it requires the resolved profile's LDFLAGS — those are per-package, not available at run start. So ThinLTO appears in `[CACHE]` lines once per package that configures `--thinlto-cache-dir=` in its LDFLAGS.

---

## V1.x Roadmap

Post-v1.0 enhancements that build on existing infrastructure. Not required for the v1.0 release.

- **Package groups** — named DE sets (e.g. `[group.cosmic]`, `[group.gnome]`) so users can opt into a curated desktop environment without manually listing every component. Expands to constituent packages at build time. Pacman groups (`gnome`, `kde-applications`) are often incomplete and don't include AUR/git packages — COSMIC alone has 20+ git packages on AUR that users should not have to enumerate by hand.
- **Rule priority auto-calculation** — auto-calculate a baseline specificity score from rule conditions (mirrors CSS specificity: more AND'd conditions = higher weight), with manual `priority` override for ties. Deferred until enough real rules exist to validate whether auto-priority causes ordering problems in practice.
- **Configure stage additions** — btrfs snapshot before build runs, ccache/sccache initialisation check, estimated build time heuristic.
- **Build-failure auto-repair** — detect narrow recurring failure modes (missing vendored deps, missing PGP keys, `.SRCINFO` drift, checksum mismatch) and run a deterministic repair before falling through to the manual-correction prompt. Extends the toolchain-mismatch auto-retry pattern documented under Makepkg Wrapper. Checksum repair is prompted, not silent — supply-chain safety.
- **Graphics runtime debugging refinement** — tighten the graphics/doctor diagnostics surface (exact scope TBD). Tracked as a follow-up to the 1.0 doctor work; not blocking but a candidate when revisiting graphics-related code.

### Landed in v1.x

- **Toolchain / kernel stages re-promoted from experimental** *(landed)* — Stages 6 (toolchain) and 8 (kernel), plus the `sysforge update` `build_mode = "pgo_llvm_toolchain"` profdata-reuse path, no longer emit the runtime `[WARN]` at entry and are no longer flagged as deferred post-1.0. Default `enabled = false` is preserved in both `toolchain.toml` and `kernel.toml` — building a custom toolchain or kernel remains an opt-in decision rather than the experimental framing that previously gated it. The implementations (4-pass PGO bootstrap, kconfig merge, bootloader updates, profdata version sidecar) were stable enough through v1.x iteration to drop the warning surface.
- **LLVM target filtering** *(landed)* — Hardware stage emits `host_arch` and an autodetected `llvm_targets` list (CPU backend from `uname -m`, GPU backends from `gpu_vendors`: `amd`→`AMDGPU`, `nvidia`→`NVPTX`; Intel contributes nothing because the Mesa Intel drivers don't depend on an LLVM backend). `pkgbuild_patcher.patch_llvm_targets` injects `-DLLVM_TARGETS_TO_BUILD="<list>"` into the cmake invocation of any LLVM-toolchain PKGBUILD (`llvm`, `clang`, `compiler-rt`, `lld`, lib32 variants). Resolution order: `[llvm] targets` in `toolchain.toml` (explicit override; empty list disables filtering) → `hardware_profile.toml [hardware] llvm_targets` (autodetect) → no filtering. The `LLVM_EXPERIMENTAL_TARGETS_TO_BUILD` flag is left untouched so opt-in experimental backends still build.
- **Verbose skip messaging in `sysforge update`** *(landed)* — `_sync_sources` now stores `(status, error)` tuples in `sync_failures`, and `_check_one_pkgbase` dispatches on the status to emit one of `PULL_FAILED` (genuine clone/fetch error), `RATE_LIMITED` (`STATUS_RATE_LIMITED`), or `PURGE_REFUSED` (`STATUS_PURGE_REFUSED`). The summary header always lists per-category counts (`5 need rebuild, 30 up to date, 7 skipped (3 rate-limited, 2 devel, 1 pull failed, 1 purge refused)`); per-package detail is suppressed by default for non-actionable statuses (UP_TO_DATE / DEVEL / RATE_LIMITED / PURGE_REFUSED / PULL_FAILED) and surfaced under `-v`. `NEEDS_REBUILD` and `DOWNGRADE` always render per-package because they are actionable. STATUS_DIVERGED remains a warn-level informational, not a skip — the build proceeds against the local PKGBUILD (DESIGN.md §`source_sync.py`).
- **Pacman integration** *(landed, both paths)* —
  - *pyalpm read path*: `primitives/pacman.py` wraps `get_installed_version`, `get_all_installed_packages`, `get_foreign_packages`, `get_pacman_sync_version`, and `filter_missing_deps` with libalpm bindings when `pyalpm` is importable. Installed via `[project.optional-dependencies] extra = ["pyalpm>=0.10.6"]` (`uv sync --extra extra`) or as a system package. The pyalpm handle is memoized; sync DB names are parsed from `/etc/pacman.conf` section headers (Include / SigLevel are ignored — read-only enumeration only). Set `SYSFORGE_PACMAN_NO_PYALPM=1` to force the subprocess path for parity testing — used by `tests/conftest.py` so existing subprocess-mocking tests keep working. Mutating paths (`pacman -U`, `pacman -S --needed`) and the `pacman -Fq` files-DB lookup remain subprocess-based.
  - *PostTransaction hooks*: three `.hook` files under `/usr/share/libalpm/hooks/` (`sysforge-kernel.hook`, `sysforge-toolchain.hook`, `sysforge-buildstate.hook`) invoke `/usr/lib/sysforge/pacman-hook-helper.sh` to drop sentinels under `/var/lib/sysforge/sentinels/`. The kernel hook fires on `linux*` (intentionally inclusive of `linux-firmware` / `linux-headers`); the toolchain hook fires on `llvm*`, `clang`, `lld`, `compiler-rt`, `gcc`, `gcc-libs`, and the lib32 variants; the buildstate hook fires on `*` and serves as a build-state staleness signal. `cmd_update` calls `_consume_pacman_hook_sentinels()` at entry: kernel/toolchain sentinels become `_log.warn` reminders; the buildstate sentinel is consumed silently because the existing `BuildState.sync_with_installed()` pass already runs. The helper is failsafe (every error path exits 0 to avoid breaking pacman transactions). The sentinel directory is shipped via `tmpfiles.d` and pre-created during the bootstrap configure stage so older installs that predate the hooks still work — and so the consumer skips silently when the directory is absent.

---

## V2 Roadmap

V2 goal: advanced AUR helper features beyond the v1.0 scope.

V2 candidates:
- **PKGBUILD review** — present diffs to the user before building an AUR package
- **System maintenance scope expansion** — grow sysforge beyond build/package management into a unified system-maintenance helper: track and manage user-owned system artifacts that currently live ad-hoc across `~/scripts`, `/etc/systemd/system/`, `/etc/pacman.d/hooks/`, etc. Candidate primitives: inventory of tracked files, source-of-truth dir under repo control, install/sync command, drift detection vs filesystem, integration with the existing config/profile/manifest layers.
