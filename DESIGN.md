# SysForge Design Document

SysForge is an AUR helper for Arch Linux with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles, tracks build state for update detection, and automates the full build lifecycle — from fetching PKGBUILDs to installing profiled packages. Pacman owns the package database; SysForge owns the build configuration layer above it.

Current release is **v1.0.0**. v0.1.0 shipped the profiled AUR helper surface (install, update, and manage AUR and custom packages with system-tuned profiled builds); v0.2.0 added VM tooling and install-path fixes on top; v1.0 rounds out the system-bootstrapper milestone — the full bootstrap pipeline (stages 1–4: partition, base install, hardware detection, configure) is implemented and a fresh Arch install is automated from the ISO. See the [Release Plan](#release-plan) for the shipped-vs-remaining breakdown.

---

## Table of Contents

1. [Philosophy](#philosophy)
2. [Distribution Model](#distribution-model)
3. [Architecture Overview](#architecture-overview)
4. [Directory Structure](#directory-structure)
5. [Package Manifest](#package-manifest)
6. [Config Layer](#config-layer)
7. [Pipeline Layer](#pipeline-layer)
8. [Primitives Layer](#primitives-layer)
9. [Flag Profile System](#flag-profile-system)
10. [Makepkg Wrapper](#makepkg-wrapper)
11. [Logging](#logging)
12. [Man Pages](#man-pages)
13. [Hardware Detection](#hardware-detection)
14. [Cache Management](#cache-management)
15. [Graphics Stack Build Order](#graphics-stack-build-order)
16. [Release Plan](#release-plan)
17. [Re-converge](#re-converge)
18. [Known Gaps](#known-gaps)
19. [V1.x Roadmap](#v1x-roadmap)
20. [V2 Roadmap](#v2-roadmap)

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

**Import direction:** `cli.py` → command modules (`update.py`, `converge.py`, `packages_cmd.py`, `resolve.py`) → `primitives/*`. No command module imports from another command module.

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
│   │   └── progress.py                # bottom-anchored batch progress indicator (TTY scroll region + plain fallback)
│   ├── resolve.py                     # sysforge resolve subcommand
│   ├── update.py                      # sysforge update subcommand
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
│       ├── makepkg_wrapper.py         # build execution: emit conf, invoke makepkg
│       ├── aur_resolve.py             # recursive AUR dependency resolution + topo sort
│       ├── dep_analysis.py            # pre-build soname dependency checks
│       ├── abi_check.py               # post-build versioned-symbol ABI check (.so cross-ref)
│       ├── provides_lookup.py         # reverse-lookup soname → package (pacman -Fq)
│       ├── graphics_probe.py          # system-state graphics checks (NVIDIA, Wayland, Steam)
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
│           ├── hardware.py            # stage 3: CPU/GPU/NVMe detection → hardware_profile.toml
│           ├── configure.py           # stage 4: hostname, locale, timezone, bootloader, user, services (arch-chroot)
│           ├── reconfigure.py         # stage 5: pre-build checkpoint
│           ├── toolchain.py           # stage 6: LLVM/GCC toolchain build (optional 3-pass PGO)
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
- `source` — `repo` (pacman) vs `aur`. Optional; classification falls through to pacman / AUR RPC if omitted. Set explicitly only when classification is ambiguous or you want to force routing.
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
- `repo_mode` — default build mode for repo-source packages: `"pacman"` (install via `pacman -S --needed`) or `"profiled"` (build from PKGBUILD with sysforge flag profiles). Per-package `pkgbuild_patch = true` overrides to profiled regardless. `sysforge update` walks repo packages only when an override entry exists for them; default repo packages stay pacman-managed.

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
6. **toolchain** — *experimental, deferred post-1.0*; fully implemented (LLVM/GCC, optional 3-pass PGO bootstrap, compiler propagation to packages/kernel)
7. **packages** — fully implemented
8. **kernel** — *experimental, deferred post-1.0*; fully implemented

Stages 1–4 are **bootstrap-only** — they run once from a live install environment. Stages 5–8 are **repeatable** and run on the installed system. Use `sysforge run pipeline --start-from reconfigure` to run the pre-build checkpoint on a live system; use `--start-from packages` to skip straight to builds. Stages 5–8 are also available as standalone `sysforge run <stage>` commands for repeated, out-of-pipeline use (e.g. `sysforge run packages`). The toolchain (6) and kernel (8) stages are shipped but reclassified as experimental for 1.0 — both emit a runtime `[WARN]` at stage start and default to `enabled = false`; treat them as opt-in for early adopters until they are re-promoted post-1.0.

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

**`bootstrap.toml`** (`/etc/sysforge/bootstrap.toml`) configures stages 1–4:

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

The hardware stage (stage 3) needs no config — it auto-detects and writes `hardware_profile.toml` to `state_dir`. After reboot the file is at its natural path (`/var/lib/sysforge/hardware_profile.toml`) and the kernel stage picks it up automatically.

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

> **Status: experimental — deferred to post-1.0.** The implementation is shipped, but the kconfig merge, interactive-to-olddefconfig patching, and bootloader update paths need more real-world testing than 1.0 can cover. The stage emits a `[WARN]` at entry when enabled and defaults to `enabled = false`; 1.0 users should leave it off and use a stock pacman kernel.

Builds a custom kernel from a PKGBUILD. The stage is a clean no-op if `/etc/sysforge/kernel.toml` is absent, so systems using a stock pacman kernel skip it without needing `--start-from`.

**`kernel.toml` structure:**

```toml
pkgname          = "linux-custom"
pkgbuild_src_dir = "~/src"       # parent dir; PKGBUILD is at <pkgbuild_src_dir>/<srcdir>/PKGBUILD
srcdir           = "linux"       # source directory name if different from pkgname (optional)
bootloader       = "systemd-boot"    # systemd-boot | grub | none  (default: systemd-boot)

[[kconfig]]                      # manual kconfig overrides (optional, repeatable)
option = "CONFIG_HZ_1000"        # must match CONFIG_[A-Z0-9_]+
value  = "y"                     # y | m | n | non-empty string
```

`srcdir` is needed when the PKGBUILD directory name differs from `pkgname` (e.g. `pkgname = "linux-custom"` but the repo is cloned as `~/builds/linux`). Defaults to `pkgname` if omitted.

**kconfig fragment:**

Hardware-driven kconfig entries come from `hardware_profile.toml [kconfig]` (emitted by the hardware stage). Manual overrides from `kernel.toml [[kconfig]]` are merged on top — manual wins on conflict with a `[WARN]`. The combined result is written to `<pkgbuild_src_dir>/<srcdir>/sysforge.config` before `makepkg` runs. The PKGBUILD must merge this into its `.config`; a compatible PKGBUILD calls `scripts/kconfig/merge_config.sh` in `prepare()`.

Manual override validation: `option` must match `CONFIG_[A-Z0-9_]+`; `value` must be non-empty (`n` to disable); duplicates within `kernel.toml` are an error.

If neither source provides any kconfig entries, no fragment is written.

**lsmod snapshot:**

Before the build, `lsmod` output is captured to `<state_dir>/lsmod.snapshot`. This lets the PKGBUILD run `make localmodconfig` reproducibly using a fixed module set from the running system rather than whatever is loaded at build time.

**Noninteractive kconfig:**

Driven by `build_mode = "kernel"` on the resolved profile — no explicit flag needed from the kernel stage or CLI. `patch_noninteractive_kconfig` runs on `PKGBUILD.sysforge` after normal patching, replacing interactive config targets (`oldconfig`, `nconfig`, `menuconfig`, `xconfig`, `gconfig`) with `make olddefconfig`. `olddefconfig` applies defaults for all new symbols without terminal interaction. VAR=val arguments before the target (e.g. `ARCH=x86_64`) and trailing comments are preserved. `--noconfirm` only controls makepkg's own prompts and has no effect on interactive make targets inside the PKGBUILD.

When `--interactive` is passed to `sysforge build`, kconfig patching is skipped entirely — the PKGBUILD's config targets run as-is, allowing interactive kernel configuration.

**Post-install steps** (run after `makepkg` succeeds):
1. `sudo mkinitcpio -P`
2. Bootloader update: `bootctl update` (systemd-boot), `grub-mkconfig -o /boot/grub/grub.cfg` (grub), or skipped (`none`)

### Packages stage (stage 7)

Walks `packages.toml` in order:
- `source = "repo"` → `sudo pacman -S --needed --noconfirm`
- `source = "aur"` / `"git"` → `_resolve_pkgbuild()` → `makepkg_wrapper.run()`. PKGBUILD lookup order: `packages.toml [build] pkgbuild_src_dir` → `profiles.toml [paths] pkgbuild_src_dir` → AUR clone.
- Hardware-gated packages skipped if `hardware_profile.toml` is absent or key is missing
- Non-fatal per-package failures: build continues, failures recorded in state
- Summary at end: `Total | Built | Failed | Skipped`

### Toolchain stage (stage 6)

> **Status: experimental — deferred to post-1.0.** The implementation is shipped, but the 3-pass PGO bootstrap (instrumented-symbol reconciliation, profraw merge daemon, staging/profdata lifecycle) has edge cases that need more real-world testing than 1.0 can cover. The stage emits a `[WARN]` at entry when enabled and defaults to `enabled = false`; 1.0 users should leave it off and use the system compiler.

**Opt-in:** stage is a clean no-op if `/etc/sysforge/toolchain.toml` is absent or has `enabled = false`. Systems that skip this stage use whatever compiler is already installed; packages and kernel stages proceed normally.

**`toolchain.toml` structure:**

```toml
enabled     = true   # must be true to activate the stage
compiler    = "llvm" # "llvm" or "gcc"
pgo         = true   # only meaningful when compiler = "llvm"; ignored for gcc
skip_build  = false  # skip build; just register compiler paths in pipeline state

# Staging prefix: Pass 2 binaries extracted here and used as CC/CXX in Pass 3
pgo_staging = "/var/tmp/sysforge-llvm-stage2"

# PGO data dir: profraw files written here during Pass 2, merged to clang.profdata
pgo_store   = "/var/tmp/sysforge-llvm-pgo"

# Package lists — all have sane defaults, override only if needed
[packages]
pgo     = ["llvm", "llvm-libs", "clang", "lld"]
non_pgo = ["polly", "compiler-rt", "openmp", "spirv-llvm-translator"]
lib32   = ["lib32-llvm", "lib32-llvm-libs", "lib32-clang", "lib32-spirv-llvm-translator"]
```

For `compiler = "gcc"`, the default package set is `["gcc", "gcc-libs"]`.

**`skip_build = true`:** registers the system compiler paths in pipeline state without building anything. Downstream stages (packages, kernel) will use the system compiler. Useful when the system compiler is already optimized and no rebuild is needed.

**PKGBUILD resolution:** follows `find_pkgbuild` lookup order (local `pkgbuild_src_dir` → `pkgctl repo clone`) for every package. At stage start, resolved paths are displayed in a table and the user is prompted to confirm or abort. On abort, the resume command is printed (`sysforge pipeline --resume --state-dir <dir>`) so they can make manual modifications and return.

**LLVM PGO bootstrap (three passes, only when `pgo = true`):**

Every pass runs makepkg with `--cleanbuild`. `makepkg` is invoked without `--install`; a direct `sudo pacman -U` call (from sysforge) installs each pass's output. A sudo keepalive thread refreshes credentials every 60 seconds throughout the sequence. `llvm-profdata` is invoked with `RLIMIT_AS` lifted (`resource_guard.lift_for_child`) so it is not constrained by the sysforge controller's 2 GiB virtual address space cap.

1. **Pass 1** — build pgo packages with the system compiler + `-fprofile-generate=<pgo_store>/`. Selectively install to the live system via `sudo pacman -U`: shared-lib and binary packages (e.g. `llvm-libs`, `clang`, `lld`) are installed; cmake-config packages (e.g. `llvm`, which contains instrumented `.a` archives) are intentionally excluded. This prevents a separate `clang` PKGBUILD's `find_package(LLVM)` in Pass 2 from linking against instrumented static libs and failing to resolve `__llvm_profile_*` symbols. The installed `/usr/bin/clang` is instrumented and writes `.profraw` files on use. Spurious profraw from CMake feature probes is purged before Pass 2 begins.

2. **Pass 2** — build pgo packages with `CC=/usr/bin/clang` (the instrumented Pass-1 binary; no extra flags). Before the pass starts, sysforge checks whether the system `libLLVMSupport.a` is still instrumented (via `nm`) — a residual from a prior run before the cmake-config exclusion was in place. If so, it locates the clang profile runtime (`clang --print-runtime-dir`) and injects `-L<runtime_dir> -lclang_rt.profile-<arch>` into `LDFLAGS` via the generated makepkg.conf, allowing separate-PKGBUILD components (e.g. clang) that link against instrumented LLVM static libs to satisfy `__llvm_profile_*` references at link time. `LLVM_PROFILE_FILE` uses `%m_%p` (per-module-hash + per-PID) so parallel `make -j` clang processes each write their own `.profraw` file rather than contending on one. A background daemon merges profraw into `clang.profdata` every 15 seconds using adaptive batch sizing (starts at 128 files; halves on OOM; minimum batch 8). No system install. After the build, Pass 2 binaries are extracted to `pgo_staging`.

3. **Pass 3** — build all packages (pgo + non_pgo + lib32) with `CC=<pgo_staging>/usr/bin/clang` + `-fprofile-use=<clang.profdata>`. Install all packages to the system. Staging prefix is removed on success. Profdata is **preserved** at `<pgo_store>/clang.profdata`; a version sidecar `clang.profdata.version` (LLVM major integer, e.g. `22`) is written alongside it so `sysforge update` can check compatibility before reusing the profdata.

**Profdata reuse:** before purging `pgo_store`, the stage checks for an existing `clang.profdata` + version sidecar. The sidecar's LLVM major version is compared against the `pkgver` in the pgo PKGBUILDs (not the installed version — the toolchain stage builds a *new* version). If compatible (same major), passes 1–2 are skipped entirely and only the optimized build (Pass 3) runs, using system clang as CC (which, after a prior successful run, is already PGO-optimized). Staging is not needed in this path. `--rebuild-profdata` forces a full 3-pass build regardless, e.g. after upstream codegen changes within the same major version.

**`pgo = false` path:** single build pass, all packages built and installed together. No profdata, no staging, no daemon.

**GCC path (`compiler = "gcc"`):** single build pass. `pgo` field is ignored. Produces `/usr/bin/gcc` and `/usr/bin/g++`.

**Compiler propagation:** on completion the toolchain stage writes the resolved compiler paths into pipeline state:

```toml
[stages.toolchain.result]
cc  = "/usr/bin/clang"   # or "/usr/bin/gcc"
cxx = "/usr/bin/clang++" # or "/usr/bin/g++"
ld  = "lld"              # llvm only; absent for gcc
```

The packages and kernel stages read these values and inject them into the build environment, overriding any profile-level `CC`/`CXX` defaults. If the toolchain stage was skipped, these keys are absent and stages fall back to the profile.

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

### `ui/progress.py`

Bottom-anchored status line for batch operations (`[3/10] building htop`). Dual-mode renderer picked once at `progress.init()` (called from `cli.main` right after `log.set_verbosity`):

- **TTY mode** — DECSTBM scroll region (`ESC[1;N-1r`) reserves the last row; other output (including subprocess output that inherits the TTY — `makepkg`, `git`, `pacman`) scrolls above it. `SIGWINCH` is wired to re-establish the region and redraw the last status on resize. An `atexit` hook releases the region on interpreter shutdown.
- **Plain mode** — selected when any of the following is true: `sys.stderr` is not a TTY, `_DRY_RUN`, `TERM=dumb`, `TERM=""`, `CI` set, or `NO_COLOR` set. Emits `[PROGRESS] [i/n] label` through `log.ui()` so the same data reaches logs and pipes without ANSI garbage.

Public API: `init()`, `shutdown()`, `render(current, total, label)`, `clear()`, and a `tracker(total, prefix)` context manager that yields a `tick(label)` callable (auto-clears on exit). `clear()` must be called before any `input()` prompt inside a batch loop so the prompt doesn't land inside the scroll region; the next `tick()` re-establishes the region automatically. Reservation is lazy: entering a tracker alone touches nothing — the first `tick()` call establishes the region.

Integration sites: `sysforge/pipeline/stages/packages.py` (build loop), `sysforge/primitives/aur_resolve.py::build_resolved_deps` (AUR deps), `sysforge/update.py` (sequential source sync via `source_sync` + threaded version check + build loop), `sysforge/fetch.py` (fetch loop). Interactive-prompt call sites in `pipeline/stages/packages.py` and `primitives/makepkg_wrapper.py` each call `progress.clear()` before `input()`.

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

Constants: `BATCH_STRIP_FLAGS` (flags removed from per-build makepkg calls during batch install), `BATCH_EXTRA_FLAGS`.

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

### `dep_analysis.py`

Pre-build dependency checks. Runs before `_run_build` in `makepkg_wrapper.run()`. Two check categories:

**Soname checks** (`check_soname_deps`): filters `.so` and `.so=N` entries from `depends`, parses ldconfig -p output, and checks presence and major version. `libcap.so=2` means libcap.so.2 must be present in ldconfig's cache. Version constraint checking (pacman -Q / vercmp) was intentionally omitted — makepkg already does this and any pre-check adds false-positive risk without meaningful value.

**Makedep runtime probes** (`check_makedep_runtime`): tests makedepends with known runtime requirements beyond package installation. Currently probes: `libguestfs` (appliance boot via `guestfish add /dev/null : run` with `LIBGUESTFS_DEBUG=1`). Each probe runs with a 15-second timeout; failure or timeout triggers diagnostic parsing. For libguestfs, `_diagnose_guestfs` parses the debug output for known patterns (e.g. "waiting for root UUID") and cross-references `/proc/config.gz` to identify the exact missing kernel config options (e.g. `CONFIG_SCSI_VIRTIO=m`). Version constraints on makedepends are stripped before lookup. Packages not in `_PROBED_MAKEDEPS` are silently skipped. Not-installed packages (FileNotFoundError) are skipped.

All functions accept injectable callables for testing. Non-fatal by default; configurable via `abi_mismatch` and `makedep_probe_failed` in `[failure_handling]`.

The soname match predicate (`soname_satisfied(entry, available_set)`) is exposed at module scope so `doctor.py` can reuse the `libfoo.so` / `libfoo.so=N` matching rules without duplicating them.

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

Post-build ABI compatibility checker. For each shared library (`.so.*`) in a built package, cross-references undefined versioned symbol requirements (`nm -D` `U sym@VER`) against the exported versioned symbols (`nm -D` `sym@@VER`) of its NEEDED runtime libraries (`readelf -d`) as currently resolved by `ldconfig -p`. Catches ABI breakage at build time — e.g. a library built against `libfoo` exporting `sym@@FOO_2.0` when the installed `libfoo` still only exports `sym@@FOO_1.0`.

Two-layer API:
- `check_so_files(so_paths: list[Path]) -> list[str]` — pure .so-level core. Takes any list of on-disk shared libraries and returns warning strings for unsatisfied versioned symbols and missing NEEDED sonames. Used both by the build path (through `check_package_abi`) and by `doctor.py` on installed `.so` files.
- `check_package_abi(pkg_path: Path) -> list[str]` — archive wrapper. Lists `.so.*` members with `bsdtar -t`, extracts them with `bsdtar -x` to a temp dir, calls `check_so_files`. Invoked from `makepkg_wrapper.run()` when `--abi-check` is passed to `sysforge build`.

Symbol names are demangled through `c++filt` for readability. Missing NEEDED sonames (NEEDED lib not in `ldconfig -p`) produce a distinct warning from undefined versioned symbols. Packages with no shared libraries return an empty list (no-op).

**Arch-aware ldconfig lookups.** The ldconfig map is keyed by `(soname, ELF class)` rather than soname alone, because `ldconfig -p` lists both 32-bit and 64-bit variants of common sonames (e.g. `libc.so.6`) and first-hit-wins would collapse them. Each `.so` under check has its ELF class determined via `readelf -h` and NEEDED references are resolved against libs of matching arch. Without this, lib32 packages produce a flood of false-positive "undefined symbol" findings because their `unsigned int`-mangled requirements don't match the 64-bit `libc`'s `unsigned long`-mangled exports.

**Shim-library allowlist.** A small set of compat shims shipped by glibc (`libnsl.so.1`, `libc_malloc_debug.so`, `libc_malloc_debug.so.0`) are skipped by `_is_shim_lib`. Their "undefined" symbols are intentional: `libnsl`'s RPC API is implemented by `libtirpc` at runtime (not declared as NEEDED), and `libc_malloc_debug` uses weak-hook override patterns. Without this filter, every `doctor` run reports ~44 findings per glibc that bury the real signal.

**Vendored-binary package skip list.** `_ABI_CHECK_SKIP_PACKAGES` (public predicate `is_abi_check_skipped_package(pkgname)`) names packages that ship prebuilt vendored binaries which will never link cleanly against current system libs (e.g. `steam` carries its own CEF runtime, libcurl, etc. under `/usr/lib/steam/`). `doctor.py` skips the ABI/linkage check for these packages and emits a one-line `[ABI] skipped: vendored prebuilt binaries` note; the depends check still runs since depends drift on these is actionable. Applies at package granularity, not soname — a floor-level noise filter for `doctor --all` / `doctor -s <metapackage>` runs whose closures include these packages.

### `provides_lookup.py`

Reverse soname → package lookup backed by `pacman -Fq`. Used by `sysforge doctor --suggest` to convert a missing/broken soname (e.g. `libavcodec.so.62`) into the repo package(s) that would supply it. Public API:

- `files_db_present()` — true when `/var/lib/pacman/sync/*.files` is synced (from `pacman -Fy`). Callers short-circuit lookup when false.
- `suggest_for_soname(entry, *, lib32=False)` — returns candidate `repo/pkg` strings for a soname entry, honouring `lib32` context (queries `usr/lib32/<soname>` vs `usr/lib/<soname>`).

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

**Makepkg flag passthrough:** makepkg short flags can be passed directly on the command line (`sysforge build ventoy -sfCci`) or explicitly via `-m "-sfci"`. Implicit passthrough applies to `build`, `update`, and `converge` — the preprocessing layer (`_extract_implicit_makepkg_flags`) rewrites bare flags into `-m` form before argparse runs. Excluded from implicit passthrough: `-h`, `-V`, `-p`, `-m`, `-D` (conflict with sysforge flags or take a value argument; `-v` is already hoisted). Combined short flags are expanded: `-sfci` → `[-s, -f, -c, -i]`.

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
- `git_is_dirty(pkgbuild_dir)` — `True` if the dir is a git repo with uncommitted tracked changes, unpushed commits, or no upstream tracking branch. Untracked files (build artifacts) are intentionally ignored.
- `purge_src(pkgbuild_dir)` — `rm -rf` the directory after a `git_is_dirty` safety check. Raises `RuntimeError` if the clone holds local work that would be destroyed; non-git directories are purged unconditionally; non-existent paths are a silent no-op. Used by `sysforge build --cleansrc`, `sysforge update --cleansrc`, and the source sync recovery paths in `sysforge update`.
- `pkgctl_checkout(name, dest)` — `pkgctl repo clone --protocol=https <name>` run in `dest.parent`; fetches official Arch packaging repo. Raises `RuntimeError` on failure.
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
- `SyncRequest(pkgbase, pkgbuild_dir, source="aur", force_fetch=False)` — input.
- `SyncResult(pkgbase, status, head_before=None, head_after=None, error=None)` — output. Status constants: `STATUS_UP_TO_DATE`, `STATUS_FETCHED`, `STATUS_CLONED`, `STATUS_DIVERGED`, `STATUS_RATE_LIMITED`, `STATUS_FAILED`, `STATUS_SKIPPED_OFFLINE`, `STATUS_SKIPPED_NO_TRACKING`, `STATUS_PURGE_REFUSED`.

Flow per request:
1. **RPC gate.** On the first AUR-source request of a batch, fire one `aur_info([…all AUR names…])` call and cache the results in `SourceMetaCache`.
2. **Short-circuit.** If the cached `rpc_version` / `rpc_last_modified` match the local HEAD's recorded values **and** the package is not a VCS `-git` / `-svn` / `-hg` / `-bzr` (forced-fetch) type **and** `force_fetch=False`, return `STATUS_UP_TO_DATE` without touching the network. This is the common-case path.
3. **Clone.** If the dir is missing or not a git repo, dispatch `aur_clone` through the limiter.
4. **Fetch.** Otherwise call `git_fetch_and_compare` — shallow fetch + HEAD compare, never merges or rebases.
5. **Divergence.** `STATUS_DIVERGED` is *reported*, not fixed: the work-tree is untouched, the build continues against the local PKGBUILD, and the operator decides whether to `--cleansrc` next run.
6. **Rate limit.** `RateLimited` aborts the remaining batch via `_abort_remaining`, which populates pending results with `STATUS_RATE_LIMITED` so the UI can show per-package status instead of a single global error.

Singletons:
- `get_scheduler(*, state_dir=None, offline=False, cleansrc=False, force_devel=False, min_fetch_interval_ms=None, rate_limit_abort_s=None, fetch_timeout=None, clone_timeout=None)` — returns the process-wide scheduler, constructing it on first call. Subsequent calls with the same args are memoised; dedup keys: `(pkgbase)` — any given pkgbase is synced at most once per process.
- `reset_scheduler()` — test-only hook. Tests that need fresh state call this between runs.

### `build_state.py`

Build state persistence. `/var/lib/sysforge/build_state.toml` is a **superset of `pacman -Q`** — every installed package has an entry, regardless of whether sysforge built it. The `build_mode` field distinguishes them:

- `"profiled"` — built by sysforge; carries `pkgver`, `pkgrel`, `epoch`, `pkgbase`, `pkgbuild_dir`, `flags_string` (serialized resolved compiler flags, newline-separated `KEY=value` lines), `built_at` (ISO 8601 UTC timestamp), and optionally `built_upstream_commit` (40-char SHA of the just-built upstream tree, populated only for single-git-source VCS packages — read by `sysforge update --devel` to short-circuit `pkgver()` resolution via `git ls-remote`; absent for non-VCS, multi-git-source, or any PKGBUILD whose `source=()` has unresolved bash interpolation). Split packages (multiple `pkgname` from one `pkgbase`) each get their own entry, all pointing at the same `pkgbuild_dir`.
- `"pacman"` — installed via pacman, not built through sysforge. Carries only `pkgver`, `pkgrel`, `epoch` parsed from `pacman -Q`; `pkgbase`, `pkgbuild_dir`, and `flags_string` are absent. Synthesised by `sync_with_installed()`.
- `"pgo_llvm_toolchain"` — experimental, deferred post-1.0.

`BuildState.sync_with_installed(installed)` keeps the file in lockstep with `pacman -Q`: it adds a pacman-mode entry for every newly installed package and prunes entries for packages that are no longer installed. The prune pass also removes zombie entries left by pre-superset parser runs — e.g. legacy keys containing literal `$_pkgname` that can never match a `pacman -Q` name. `sysforge update` calls this at the start of every run and saves if anything changed.

Read by `sysforge update` for version drift detection (every installed AUR package is iterated regardless of `build_mode`; profiled entries carry the prior `pkgver` for change-detection, pacman-mode entries are checked against PKGBUILD freshness) and by `sysforge converge` for flag drift detection (profiled entries only; pacman-mode entries are silently skipped). Follows the same atomic write-then-rename pattern as `pipeline/state.py`. Records must carry `build_mode`; the previous compatibility fallback that treated missing `build_mode` as profiled was removed.

On the write path, after a successful build `makepkg_wrapper.py` derives `pkgver`/`pkgrel`/`epoch` from the produced `.pkg.tar.*` filenames rather than the static PKGBUILD parse. The static parser intentionally leaves shell parameter-expansion forms (e.g. `${_ver/[a-z]/.${_ver//[0-9.]/}}`) untouched so it never produces a misleading partial substitution, but a built package's filename always carries the fully resolved version. Falling back to filenames prevents profiled entries from storing literal `$...` strings that would mismatch every subsequent vercmp and cause the package to be flagged for rebuild on every `sysforge update` run.

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

**Phase 1 — Package set assembly** (`_assemble_package_set`). Build a unified `{pkgname: entry}` dict by walking the live install set: AUR (`pacman -Qm`) is always walked; repo packages are walked when an override entry indicates a profiled build for them. `packages.toml` entries are applied as override overlays (`source`, `pkgbuild_patch`, `cache`, `reason`); installed packages with no entry use defaults. For AUR packages without a build_state record: bulk `aur_info` resolves the real `pkgbase` (split-package fix, e.g. `ob-xd-common` → pkgbase `ob-xd`). Apply positional PKG filter. Group by `pkgbase` to deduplicate split packages. Manifest entries whose package is not installed (e.g. a stored rule for `mesa-git` while repo `mesa` is installed) are not iterated — they are inert rules under the rules-not-install model.

**Phase 2 — Source sync** (`_sync_sources`). Ensures every iterated AUR package has an up-to-date local PKGBUILD. Skipped entirely with `--offline` (and with `--install-only`, which forces offline since no rebuild will happen). Delegates to the `source_sync.SourceSyncScheduler` singleton (`get_scheduler(...)`), issuing one `SyncRequest` per AUR-source package:

1. **RPC-first.** The scheduler batches one `aur_info` call for all AUR packages in the run. For every package whose cached `rpc_version` / `rpc_last_modified` still matches the local HEAD metadata, the request short-circuits to `STATUS_UP_TO_DATE` — no `git fetch` executes at all.
2. **Clone on miss.** Missing / empty / non-git dirs hit `aur_clone` through the shared rate limiter.
3. **Shallow fetch + compare.** Everything else runs `git_fetch_and_compare` (depth-1 fetch, non-destructive HEAD compare). VCS packages (`-git`/`-svn`/`-hg`/`-bzr`) always fetch regardless of RPC metadata.
4. **Divergence is surfaced, not fixed.** A local-plus-upstream divergence (e.g. force-push, local commits) yields `STATUS_DIVERGED`; the build proceeds against the local PKGBUILD and an operator can opt into `--cleansrc` on the next run.
5. **Rate-limit aware.** AUR `Retry-After` is honoured; runs where the remaining penalty exceeds `[aur] rate_limit_abort_s` mark all pending packages `STATUS_RATE_LIMITED` rather than hanging for minutes.

Statuses treated as sync failures for the downstream buildability filter: `STATUS_FAILED`, `STATUS_RATE_LIMITED`, `STATUS_PURGE_REFUSED` (collected in `_SYNC_BLOCKING_STATUSES`). `STATUS_DIVERGED` is a warning, not a blocker.

Repo-source packages (`source = "repo"`) are skipped entirely (no local PKGBUILD to sync).

**Phase 3 — Version check.** Parse PKGBUILD, look up installed version from `pacman -Q`, compare with `vercmp`. Produces a unified `_UpdateResult` per iterated package. Actions: `NEEDS_REBUILD`, `UP_TO_DATE`, `DEVEL`, `DEVEL_EVAL_FAILED`, `DOWNGRADE`, `PULL_FAILED`. (`NOT_INSTALLED` is no longer emitted: under the live-install-set iteration model, only installed packages reach Phase 3.) For VCS packages (`-git`/`-svn`/`-hg`/`-bzr`) the static PKGBUILD `pkgver` is just a seed; without `--devel` the package is reported `DEVEL` and skipped. With `--devel`, the worker first attempts a cheap short-circuit: if `build_state.toml` carries a `built_upstream_commit` for the pkgbase, `vcs_pkgver.peek_upstream_commit` runs `git ls-remote` against the PKGBUILD's `source=()` URL and on a SHA match the package is reported `UP_TO_DATE` without ever touching makepkg. On a cache miss (no stored SHA, multi-git-source, ls-remote failure, or differing SHA) the canonical resolver `vcs_pkgver.evaluate_vcs_pkgver` runs `pkgver()` against the fetched upstream sources and the resulting `[epoch:]pkgver-pkgrel` is vercmp'd against installed (`NEEDS_REBUILD` / `UP_TO_DATE` / `DOWNGRADE`). Resolution failures are reported as `DEVEL_EVAL_FAILED` and skipped — the package is left untouched on the assumption that a missed update is preferable to wasted rebuilds when `pkgver()` is transiently broken (broken bash, dropped network mid-fetch, missing makedeps for `prepare()`).

**Phase 4 — Summary + dry-run gate.** Print per-package status. Exit if `--dry-run`.

**Phase 5 — Build.** Filter to buildable packages: `NEEDS_REBUILD`. (Phase 3 already converted VCS packages to `NEEDS_REBUILD` / `UP_TO_DATE` / `DEVEL_EVAL_FAILED` under `--devel`, so no separate `DEVEL` union is needed at this stage.) Batch makedeps pre-install (single `sudo pacman -S`). AUR dep resolution + build. Single build loop for all packages. `--cleanbuild` (`-C`) prepended by default (suppressed by `--no-cleanbuild`). `--syncdeps`/`-s` and `--install`/`-i` stripped; packages installed in phase 6. `AlreadyBuilt` raised by `makepkg_wrapper.run` (PKGDEST already holds the matching `.pkg.tar`) is treated as a successful build: `_find_existing_artifacts` locates the matching files in pkgdest and queues them for install, instead of marking the pkgbase failed.

With `--install-only` the build loop is replaced wholesale: no makedep batching, no AUR dep resolution, no `makepkg` invocation. For each buildable result, `_find_existing_artifacts(pkgdest_or_pkgbuild_dir, pkgnames, pkgbuild_ver, installed_ver=...)` is called directly. Hits are queued for phase 6; misses log a `[SKIP]` line and are counted alongside the existing `skipped` total.

`_find_existing_artifacts` is a two-stage lookup. First it tries the strict glob `{pkgname}-{pkgbuild_ver}-*.pkg.tar.*` — the common path for non-VCS packages where the static PKGBUILD parse equals the filename version. If that returns nothing it falls back to a pkgname-only glob `{pkgname}-*-*-*.pkg.tar.*`, parses each filename's `(epoch, pkgver, pkgrel)`, and picks the newest by `vercmp`. The fallback is required for VCS (`-git`/`-svn`/...) packages, where `pkgver()` bumps the version dynamically at build time (PKGBUILD `pkgver=0.1.0` → artifact `0.1.0.r45.g1234567`) so the static `pkgbuild_ver` never matches the filename. When `installed_ver` is supplied (always under `--install-only`), the fallback further excludes any artifact not strictly newer than installed, preventing redundant reinstalls or downgrades. The `AlreadyBuilt` call site omits `installed_ver`: makepkg has already proved the artifact exists in PKGDEST, so the lookup just needs to find it.

**Phase 6 — Install + finalize.** Filter the built `.pkg.tar.*` files with `filter_pkgs_to_installed` so only files whose `pkgname` is already present in `pacman -Q` reach `pacman -U` — split `-git` pkgbases emit one file per split pkgname, and rebuilding must not silently add sub-packages the user never installed (e.g. `pipewire-full-git` emits 16 files; only the 2 installed ones get installed). New dependencies built via `build_resolved_deps` are handled on a separate path and are unaffected by this filter. Single `sudo pacman -U` for the kept set. Cache report, final summary, close unified log.

Positional: `[PKG ...]` — optional package names to restrict the run to a subset of packages.

Flags: `--interactive`, `--packages`, `--dry-run`, `--devel`, `--offline`, `--install-only`, `--no-cleanbuild`, `--cleansrc`, `--state-dir`, `--profile-conf`, `--cache-report`, `--no-pkg-log`, `--persist-log`, `--log-dir`, `--makepkg`.

`--install-only` is mutually exclusive with the build-tuning flags `--makepkg`, `--no-cleanbuild`, `--cleansrc`, `--interactive`, and `--cache-report`; argparse rejects the combination. It implies `--offline`. Use it to install artifacts left in PKGDEST by a previous interrupted run, or by a manual `makepkg` invocation, without re-entering the build loop.

**Unattended full update.** `sysforge update` (no positional args) is the supported recipe for a hands-off "rebuild everything outdated" run: walks every installed AUR package plus any repo packages with profiled overrides, rebuilds those flagged `NEEDS_REBUILD`, and automatically clones any missing src dirs. Add `--cleansrc` to also discard divergent upstreams — this is destructive but per-package safe, since `purge_src` refuses any clone that holds uncommitted changes. `--cleansrc` also bypasses the RPC short-circuit so every AUR package in the run is re-cloned from scratch rather than trusting the cached metadata. A refused package is counted as failed and skipped.

### `converge.py`

Implements `sysforge converge` — the flag drift detector. Algorithm:

1. Load `build_state.toml`. Group by `pkgbase`.
2. For each profiled package (`build_mode = "profiled"`): re-resolve the current profile via `parse_pkgbuild` → `match_rules` → `resolve_profile` → `serialize_flags`.
3. Diff the stored `flags_string` against the freshly resolved flags. Packages where the flags differ are `DRIFTED`; identical are `IN_SYNC`. Packages without a stored `flags_string` (built before this feature) are `NO_FLAGS`. Missing PKGBUILD → `NO_PKGBUILD`. Non-profiled packages are silently omitted.
4. Print a per-package summary with flag diffs for `DRIFTED` entries.
5. With `--apply`: rebuild all `DRIFTED` packages via `makepkg_wrapper.run()` with `update=False`. Post-build, the pkg-file set is run through `filter_pkgs_to_installed` before `batch_install_pkgs` so repairing drift on one split pkgname never installs sibling sub-packages the user never chose.

Without `--apply`, the command is read-only — it reports drift but does not rebuild. Positional `[PKG ...]` limits the drift check (and `--apply` rebuild) to the named pkgnames; names not in `build_state.toml` are warned and skipped, matching `update`'s positional behaviour. Flags: `--apply`, `--state-dir`, `--profile-conf`, `--no-pkg-log`, `--persist-log`, `--log-dir`, `--cache-report`.

### `doctor.py`

Implements `sysforge doctor` — health-check an installed package's depends + linkage against the current system. Diagnoses the class of breakage where a partial rebuild leaves an installed package referencing ABIs that no longer exist (e.g. graphics-stack drift: mesa, vulkan, libglvnd, GPU driver). Read-only — never rebuilds or installs.

For each target package, reads `/var/lib/pacman/local/<pkg>-<ver>/` directly: `files` for package-owned paths (filtered to `.so`/`.so.*`), `desc` for the `%DEPENDS%` array. Then runs two checks per package:

- **Depends check.** For each depends entry: versioned package deps verified via `pacman -T` + `vercmp`; `libfoo.so` and `libfoo.so=N` entries verified via `dep_analysis.soname_satisfied` against the `ldconfig -p` set.
- **ABI/linkage check.** Calls `abi_check.check_so_files` on the installed `.so` files — same symbol cross-check logic that `sysforge build --abi-check` runs, pointed at `/usr/lib/...` instead of a fresh archive.

Closure walk: by default, BFS over the target's `%DEPENDS%` transitively so one command covers the full dependency neighbourhood (the typical Steam-black-window pattern is a breakage one or two levels down from the root the user names). `--shallow` restricts to direct depends only. BFS dedupes on the resolved real pkgname from `pacman -Q` to collapse `provides`/virtual-package cycles. Output groups issues by the package the issue was found in, not by the root that triggered the walk, so overlapping closures from multiple roots produce one report per affected package.

Per-package headers and the final summary both tag each package with its installation origin — `[aur]` for foreign packages (`pacman -Qm`) and `[repo]` for non-foreign. Example: `== steam 1.0.0.79-1 [aur] ==` and `Affected: steam [aur] (62), mesa [repo] (3)`. The tag reflects where the *currently installed* copy came from, not where updates might be available; an AUR package that's also shipped by a repo still reads `[aur]`. This directly distinguishes the rebuild surface: `[aur]` findings are fixed by a rebuild through sysforge's own build path; `[repo]` findings require a `-Syu` that includes a maintainer rebuild. Not-installed roots read `(not installed)` without an origin tag.

`--graphics` expands to a curated stack: always `mesa[-git]`, `lib32-mesa[-git]`, `vulkan-icd-loader`, `lib32-vulkan-icd-loader`, `libglvnd`, `lib32-libglvnd`, `egl-wayland[-git]`, `xwayland[-git]` / `xorg-xwayland[-git]`, `wayland` + `lib32-wayland`, `libdrm` + `lib32-libdrm`, `libva` + `lib32-libva`, `libvdpau` + `lib32-libvdpau`, `gamescope`; plus per-vendor additions driven by the hardware overlay's `gpu_vendors` list (`nvidia` → active `nvidia-*` / `nvidia-open*-dkms` driver + `lib32-nvidia-utils` + `nvidia-settings`; `amd` → `vulkan-radeon`, `lib32-vulkan-radeon`, `libva-mesa-driver`; `intel` → `vulkan-intel`, `lib32-vulkan-intel`, `intel-media-driver`). The list is filtered against `pacman -Q` so only installed variants are actually verified — avoids false negatives on boxes that don't have lib32 counterparts. The expansion table lives in `doctor.py::GRAPHICS_BASE` / `GRAPHICS_BY_VENDOR` as reference data, not config.

`--graphics` also runs a second axis of checks — system-state probes from `primitives/graphics_probe.py` — after the package walk completes. These catch classes of graphics breakage that ABI/linkage walks cannot see: kernel-module parameters, NVIDIA driver version skew, session-type / compositor misconfiguration, missing Wayland explicit-sync protocol, Steam client config regressions. See `graphics_probe.py` below for the check inventory. Findings with severity `error` contribute to the exit code; `warn` and `info` do not.

Vendor detection for `--graphics` prefers the hardware profile (`/var/lib/sysforge/hardware_profile.toml` → `[gpu] vendors`); when that file is absent, falls back to `lspci -nnk` scraping, extracting `nvidia`/`amd`/`intel`/`radeon` from VGA-class device strings. The `lspci` fallback is used for both the package-expansion vendor list and the graphics-probe vendor-gating.

`--all` verifies every installed package (`pacman -Q`) — foreign and non-foreign. Slow but comprehensive: a one-shot "is anything broken anywhere" sweep. `--repo` narrows to non-foreign packages only (all of `pacman -Q` minus `pacman -Qm`), for when you want to scope a sweep to the distribution-provided side without walking every AUR/custom build.

`--suggest` (`-s`) reverse-looks up lookup-able findings via `pacman -Fq` and prints a kind-tagged candidate line under each issue. Findings are split into two kinds — the distinction matters because they imply different remediations:

- `install` — the soname is **not present** on disk. Installing (or reinstalling) the owning package fixes it. Rendered as `      → install candidate: repo/pkg, …`. Covers:
  - Depends issues whose text matches `soname not found in ldconfig: libfoo.so[=N]`.
  - ABI issues of the form `… NEEDED lib 'libfoo.so.N' not found in ldconfig cache`.
- `abi_drift` — the soname **is present** but one of its versioned symbols no longer resolves. The owning library needs to be **upgraded or rebuilt**, not reinstalled. Rendered as `      → ABI-drift candidate (rebuild/upgrade): repo/pkg, …`. Covers:
  - ABI issues of the form `<file>: undefined versioned symbol …` — the broken `.so`'s NEEDED sonames (from `abi_check.needed_sonames`) are enumerated and each is reverse-looked-up. One of the resulting NEEDED libs' owning packages is the ABI-drift culprit.

Keeping the two kinds distinct avoids the reinstall-loop failure mode where a user reinstalls the surfaced packages and the same findings reappear (reinstalling the same `.pkg.tar.zst` archive cannot change the versioned symbols on disk).

`lib32` context is inferred from the owning pkgname prefix (`lib32-*` → query `usr/lib32/<soname>`). Requires a synced files db: if `/var/lib/pacman/sync/*.files` is absent, the command emits one warning (`run sudo pacman -Fy`) and runs the rest of the report with lookups skipped — findings still show, exit code unchanged.

End-of-run summary (when any candidates were collected): `Suggestions:` header with one line per affected package (`  <pkg>: install: cand-a, cand-b` and/or `  <pkg>: abi-drift: cand-c`), followed by two deduped lists across the whole run — `Install candidates: …` and `ABI-drift candidates (rebuild or upgrade, not reinstall): …`. Useful for turning a long report into a separated install-vs-rebuild punch list.

All report output (headers, issue lines, summary) flows through `log.ui` (→ stderr + unified log file) so external callers that scrape the unified log see doctor findings.

Public API: `cmd_doctor(args)`. Positional `[PKG ...]` and flags `--graphics`, `--all`, `--repo`, `--shallow`, `--quiet` (suppress clean lines, show only issues), `--suggest` / `-s` (inline + end-of-run candidate lookup via files db).

Log tag: `[DOC]`. Primitive lookup helper lives in `sysforge/primitives/provides_lookup.py` — see the `provides_lookup.py` subsection for the public API. NEEDED-soname extraction reuses `abi_check.needed_sonames` (public since doctor calls it directly for ABI-issue suggestions). System-state probes live in `sysforge/primitives/graphics_probe.py` — log tag `[GFX]`, public API `check_system_graphics(config, *, gpu_vendors=None)`; invoked from `cmd_doctor` when `--graphics` is set.

### `setup_cmd.py`

Implements `sysforge setup` — one-shot pre-flight that stops `pacman -Syu` from silently clobbering sysforge-built packages with upstream repo binaries. It inspects `/etc/pacman.conf` for `IgnoreGroup = sf-build` and, if missing, offers to add it (interactive prompt). Packages built by sysforge carry the `sf-build` group, so the IgnoreGroup line gates the whole rebuild surface behind a single policy knob rather than requiring a per-package `IgnorePkg`.

Public API: `cmd_setup(args)`. Flag: `--pacman-conf PATH` (default `/etc/pacman.conf`) for VM or chroot runs where the file lives elsewhere. No effect if the line is already present. Intended to be run once after first installing sysforge; safe to re-run.

### `state_cmd.py`

Implements `sysforge state` — a small read/repair namespace for `build_state.toml` (the live install-state mirror). Separate from `sysforge packages` (which manages override rules in `packages.toml`); the split mirrors the rules-vs-state separation described in §Package Manifest.

- **`state list`** — tabulates `build_state.toml` entries: pkgbase, build_mode, last build, profile/flags. Read-only.
- **`state repair`** — re-parses PKGBUILDs for entries whose stored fields contain unexpanded shell variables (e.g. `${pkgver}` literals from a buggy parse) and rewrites those rows. `--dry-run` previews fixes without writing.

Public API: `cmd_state_list(args)`, `cmd_state_repair(args)`. Both accept `--state-dir` to target a non-default state directory.

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
| `explicit_sync_protocol` | `wayland-info` — look for `wp_linux_drm_syncobj_v1` in advertised globals | Wayland session + `nvidia` vendor | error |
| `steam_gpu_accel` | parse `~/.steam/root/config/config.vdf` for `GPUAccelerationEnabled "1"` | Steam installed | warn |

The explicit-sync check is the load-bearing one for NVIDIA-on-Wayland black-window breakage: when the compositor doesn't advertise `wp_linux_drm_syncobj_v1`, XWayland games on NVIDIA fall back to implicit sync which is known-broken on the NVIDIA explicit-sync driver path.

Log tag: `[GFX]`. No writes, no sudo, no network.

---

## Flag Profile System

### Profile structure

```toml
[profiles.bare]
# Fallback profile, no flags

[profiles.standard]
extends = "bare"
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

`--interactive` on `sysforge build` strips `--noconfirm` from the profile's `makepkg_flags` before invoking makepkg. Useful during development to review makepkg prompts without editing the profile. The flag has no effect if `--noconfirm` is not in `makepkg_flags`.

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

To make the pattern scan work for every build mode, `invoke_makepkg` always uses a single `Popen`-with-tee capture path: each line is matched against the patterns, then forwarded to stdout (or to `[DEBUG][MAKEPKG]` when verbosity ≥ 3). stdin remains inherited so sudo prompts still work.

### Build-failure auto-repair

> **Status: planned for v1.x.** Generalises the toolchain-mismatch auto-retry pattern above into a registry of narrow, deterministic repair primitives. Not implemented yet; documented here so the shape is fixed before code lands.

`invoke_makepkg`'s line-tee already supports stdout/stderr pattern matching. Auto-repair adds a registry of `(detection, repair_fn, retry_phase)` tuples. On match, `_run_build` invokes the repair function (idempotent), then re-runs makepkg with `-e` when sources are already laid down or from scratch when `prepare()` must repeat. Each repair class fires at most once per build, so a misdetected error cannot loop. Auto-repair runs before `_invoke_with_retry`'s interactive "correct manually" prompt; on repair success the prompt is skipped, on repair failure the build falls through to existing failure handling unchanged.

Configuration extends `[failure_handling]` with four scenario keys:

```toml
vendored_deps_missing = "auto_repair"
pgp_key_missing       = "auto_repair"
srcinfo_drift         = "auto_repair_with_warning"
checksum_mismatch     = "prompt_user"
```

Three new behaviour values join the existing `abort` / `warn_and_fallback` / `fallback` / `error` set: `auto_repair` (repair silently, retry, log at info), `auto_repair_with_warning` (repair, retry, log at `[WARN]`), `prompt_user` (surface the diff and require explicit consent before repairing).

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
| `--rebuild-profdata` | `run toolchain` | Force full 3-pass PGO build even if compatible profdata exists |

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

**Current (v1.0):** `argparse-manpage` generates `man/sysforge.1` from the argparse parser exposed via `_build_parser()` in `cli.py`. Generated during `make man` and during the PKGBUILD `build()` step (requires `python-argparse-manpage` makedepend). The man page is not checked into git — it is always generated from the parser at build/package time. Makefile target: `make man`.

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

**`hardware_profile.toml` layout:**
```toml
[hardware]
cpu_vendor  = "AuthenticAMD"
cpu_family  = 25
cpu_model   = 33
gpu_vendors = ["nvidia"]
nvme        = true

[kconfig]
CONFIG_MZEN3          = "y"
CONFIG_X86_AMD_PSTATE = "y"
CONFIG_DRM_NOUVEAU    = "n"
CONFIG_BLK_DEV_NVME   = "y"
```

Written atomically (write-then-rename) to `<state_dir>/hardware_profile.toml`. The kernel stage reads `[kconfig]` from this file; its absence is non-fatal (entries skipped with an INFO log).

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
- **v0.1.0** (shipped) — profiled AUR helper. Userspace commands stable under real use: `build`, `fetch`, `update`, `resolve`, `doctor`, `converge`, `setup`, `packages` (list/add/remove/sync), `run pipeline`, `run reconfigure`, `run packages`. The `run toolchain` and `run kernel` stages shipped in this release but have since been reclassified as **experimental** pending more testing — see v1.0 notes below. Marks the AUR publication milestone.
- **v0.2.0** (shipped) — follow-up release on the v0.1.0 surface: VM tooling (`tools/vm/`, `make vm-*` targets), install-path fixes for fresh Arch systems on Python 3.14, bulk-operation progress indicator, VCS detection and paging fixes, `doctor --graphics` scope refinement.
- **v1.0** (shipped, **current**) — system bootstrapper. Stages 1–4 fully implemented (partition, base_install, hardware, configure). Configure stage installs systemd-boot, enables NetworkManager/sshd, creates primary user with sudo, writes shell dotfiles, sets passwords, and sets the configured default login shell. Build-state persistence, `pacman -Q` superset coverage, and coloured CLI output all landed in 2026-04. **Experimental surface, deferred post-1.0:** `run toolchain`, `run kernel`, and the `sysforge update` PGO-toolchain profdata-reuse path (`build_mode = "pgo_llvm_toolchain"`) all remain shipped but emit a runtime `[WARN]` — 1.0 users should leave the stages disabled and use the system compiler + stock pacman kernel. Published to AUR via `tools/release.sh`.
- **v1.x:** `repo_mode = "profiled"` support in `sysforge update`; wrapping `pacman -Syu` inside `sysforge update` for a full AUR-helper experience; man page migration from `argparse-manpage` to a scdoc hybrid (hand-written narrative + auto-generated OPTIONS — see Man Pages section below); package groups (named DE sets for opt-in without enumerating every package); rule priority auto-calculation (CSS-specificity-style scoring from rule conditions); configure stage additions (btrfs snapshots, ccache/sccache init check, build time estimates); LLVM target filtering from hardware detection. Re-promotion of the toolchain and kernel stages from experimental happens here once their sharp edges are resolved.

### AUR publishing process

Release prep is driven by `tools/release.sh`, which reads the version from `pyproject.toml`, verifies the matching `vX.Y.Z` tag exists locally, fetches the GitHub tarball sha256, updates `PKGBUILD`, regenerates the man page, **validates both `PKGBUILD` and `PKGBUILD-git` in a clean chroot**, and writes `.SRCINFO` / `.SRCINFO-git`. After it succeeds, the script prints the `git clone`/`cp`/`commit`/`push` sequence for the `sysforge` and `sysforge-git` AUR repos.

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
- `--dry-run` — also skips the chroot build.

`makechrootpkg` bind-mounts require root; the script assumes passwordless sudo is configured for it and fails fast with a clear message otherwise.

---

## Re-converge

Two commands address drift in sysforge-managed packages:

**`sysforge update [PKG ...]`** (implemented) — handles **version drift**. After the `source_sync` scheduler refreshes each PKGBUILD dir (one batched AUR RPC call followed by per-package clone or shallow fetch as needed), it compares the new `pkgver`/`pkgrel`/`epoch` against the installed version via `vercmp`. Packages where the PKGBUILD is newer are rebuilt with the current profile. VCS packages (`-git`/`-svn`/`-hg`/`-bzr`) are reported as `DEVEL` and skipped by default; with `--devel`, each VCS package's `pkgver()` is resolved up-front via `vcs_pkgver.evaluate_vcs_pkgver` (one `makepkg --nobuild` pass per VCS pkgbase) and the resulting version is vercmp'd against installed — only genuinely-stale packages are rebuilt; up-to-date packages are reported as `UP_TO_DATE` and skipped. Resolution failures (broken `pkgver()`, transient network) report `DEVEL_EVAL_FAILED` and are also skipped. One or more package names may be given as positional arguments to restrict the run to a subset of sysforge-managed packages; unrecognised names are warned and skipped.

**PGO toolchain packages** (`build_mode = "pgo_llvm_toolchain"`, *experimental — deferred post-1.0*) are handled specially during update. `makepkg_wrapper.run()` reads `toolchain.toml → pgo_store`, checks for a saved `clang.profdata` and its `clang.profdata.version` sidecar, and compares the sidecar's LLVM major version against the PKGBUILD's `pkgver` major. If they match, `-fprofile-use=<profdata>` is injected and the build proceeds as a PGO-optimised build — a runtime `[WARN]` fires at this point so users of this path know it is not part of the 1.0 stable surface. If profdata is absent or version-mismatched (e.g. after a major LLVM bump), the user is prompted: **[p]lain build or [s]kip (default: skip)**. In non-interactive mode the build is skipped automatically. Skipped packages are counted separately in the update summary and do not count as failures. To rebuild profdata after a major version bump, run `sysforge run toolchain` (also experimental). The toolchain stage itself also reuses compatible profdata — see the **Profdata reuse** section under stage 6.

**Stale-profraw post-build check.** After every non-PGO-managed build, `makepkg_wrapper.run()` globs `pgo_store` for `*.profraw` files. Any file with `mtime >= build_start - 1s` is treated as **fresh** — it was written by the build just completed, which means an instrumented LLVM is still installed on the system and the build was leaking profile data. The wrapper fatals, telling the user to reinstall `llvm`/`llvm-libs` or run `sysforge run toolchain`. Files strictly older than `build_start` are **orphans** left behind by a prior failed or partial toolchain run whose instrumented binaries the user has since cleaned up; these are unlinked in place and an info line is logged. The split makes the safety net self-healing: once the system is clean, the next build purges the residue automatically instead of requiring manual cleanup of `pgo_store`.

**`sysforge converge`** (implemented) — handles **profile/flag drift**. Same package version but different compiler configuration — e.g. profile changed, new flag added, or build mode switched. At build time, `makepkg_wrapper.run()` stores the resolved flags string per package in `build_state.toml`. `converge` re-resolves the current profile for each package and diffs the result against the stored flags string; packages where the flags have changed are reported with a flag diff. Without `--apply` the command is read-only; `--apply` rebuilds all drifted packages.

`build_state.toml` is the shared source of truth for both commands. Written by `makepkg_wrapper.run()` after each successful build.

**`sysforge doctor`** is the third drift-surface command and completes the picture — it is read-only and catches the drift class neither of the above detects: **ABI / linkage drift** on already-installed packages, e.g. a partial graphics-stack rebuild leaving `steam` linked against a `libfoo.so.N` that the system no longer exposes. See the `doctor.py` subsection for the full algorithm. Together: `update` → version drift, `converge` → flag drift, `doctor` → ABI drift.

DAG stages are categorised as **bootstrap-only** (partition, base_install, configure) or **repeatable** (hardware, reconfigure, toolchain, packages, kernel). Only repeatable stages participate in re-converge runs. `hardware` is repeatable because re-detecting after a hardware change (e.g. GPU swap) is safe and needs no root.

---

## Known Gaps

Implemented behaviour that is incomplete or has known limitations. These are not deferred features — they are holes in currently active code.

**`sysforge update` does not iterate default-mode repo packages.** Under the rules-not-install model, `sysforge update` walks every installed AUR package (`pacman -Qm`) plus any repo packages selected by overrides. Default-mode repo packages — those with no override and not implicated by `[build].repo_mode` — are not iterated; `pacman -Syu` remains their upgrade path. Wiring `[build].repo_mode = "profiled"` through `update` (so all installed repo packages get rebuilt from source against the Arch packaging repo) is on the v1.x roadmap — see Release Plan and the next gap entry.

**`repo_mode = "profiled"` is wired in the packages stage only.** The `[build] repo_mode = "pacman" | "profiled"` setting in `packages.toml` is parsed and honoured by `run packages` and `run pipeline` — repo packages with `repo_mode = "profiled"` (or per-package `pkgbuild_patch = true`) are built from source via `_build_aur()` using `find_pkgbuild` (which calls `pkgctl_checkout` for repo packages). It has no effect on `sysforge build` or `sysforge update` — those commands operate on individual packages the user explicitly targets, not on manifest-wide policy. Wiring `repo_mode` into `sysforge update` (so tracked repo packages with `repo_mode = "profiled"` are rebuilt from source when the Arch packaging repo has a newer version) is on the v1.x roadmap — see Release Plan.

**`packages.toml [build] pkgbuild_src_dir` and `profiles.toml [paths] pkgbuild_src_dir` are separate.** The pipeline's `_resolve_pkgbuild` prefers `[build] pkgbuild_src_dir`; falls back to `[paths] pkgbuild_src_dir`. They can point to different directories or the same one — there's no enforcement that they match.

**`[env_precedence]` config table — design cancelled.** The original design proposed a priority stack (wrapper profile = 100, makepkg.conf = 80, shell passthrough = 20, PKGBUILD export = 10) and an `[env_precedence]` TOML table to configure it. This design is superseded. The current model is simpler and more predictable: build tool vars (`CC`, `CFLAGS`, `LDFLAGS`, etc.) are stripped from the inherited shell env in `invoke_makepkg` before makepkg runs — the temp conf is the sole authority for all makepkg-managed keys. Shell env bleed-through is not a configurable priority; it is prevented entirely. SysForge bootstrap vars (`SYSFORGE_STATE_DIR`, `SYSFORGE_CONFIG_DIR`) are exempt — they are SysForge's own interface, not build tool vars, and are not stripped. The `[env_precedence]` table will not be implemented.

**`[FLAG]` tag — partial coverage.** Emitted for: CLI toolchain overrides (`--cc`, `--cxx`, `--ld`), linker token replacement and injection, linker guard stripping, RUSTFLAGS linker reconciliation, GCC thin-LTO rewrite, GCC+lld LTO disabling, conflict group firing (logs group name, evicted tokens, inserted token), and prefix-match token replacement during `merge_extends`. Not emitted for: `apply_patch_pkgbuild` token changes (those use `[PATCH]`).

**`[CACHE]` ThinLTO probe is per-build, not per-run.** `emit_system_probes()` (ld.so mtime, pacman cache size) runs once at the start of each pipeline or build invocation. ThinLTO cache size is probed inside `_run_build()` because it requires the resolved profile's LDFLAGS — those are per-package, not available at run start. So ThinLTO appears in `[CACHE]` lines once per package that configures `--thinlto-cache-dir=` in its LDFLAGS.

---

## V1.x Roadmap

Post-v1.0 enhancements that build on existing infrastructure. Not required for the v1.0 release.

- **Package groups** — named DE sets (e.g. `[group.cosmic]`, `[group.gnome]`) so users can opt into a curated desktop environment without manually listing every component. Expands to constituent packages at build time.
- **Rule priority auto-calculation** — auto-calculate a baseline specificity score from rule conditions (mirrors CSS specificity: more AND'd conditions = higher weight), with manual `priority` override for ties. Deferred until enough real rules exist to validate whether auto-priority causes ordering problems in practice.
- **Configure stage additions** — btrfs snapshot before build runs, ccache/sccache initialisation check, estimated build time heuristic.
- **LLVM target filtering** — restrict `LLVM_TARGETS_TO_BUILD` based on detected hardware from the hardware stage (e.g. only X86 on x86_64 systems). Currently builds all targets.
- **Build-failure auto-repair** — detect narrow recurring failure modes (missing vendored deps, missing PGP keys, `.SRCINFO` drift, checksum mismatch) and run a deterministic repair before falling through to the manual-correction prompt. Extends the toolchain-mismatch auto-retry pattern documented under Makepkg Wrapper. Checksum repair is prompted, not silent — supply-chain safety.

---

## V2 Roadmap

V2 goal: advanced AUR helper features beyond the v1.0 scope.

V2 candidates:
- **PKGBUILD review** — present diffs to the user before building an AUR package
