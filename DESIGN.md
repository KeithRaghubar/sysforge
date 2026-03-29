# SysForge Design Document

SysForge is an AUR helper for Arch Linux with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles, tracks build state for update detection, and automates the full build lifecycle — from fetching PKGBUILDs to installing profiled packages. Pacman owns the package database; SysForge owns the build configuration layer above it.

The v0.1.0 milestone is a profiled AUR helper: install, update, and manage AUR and custom packages with system-tuned profiled builds. The full bootstrap pipeline (stages 1–4: partition, base install, hardware detection, configure) is implemented — a fresh Arch install is fully automated from the ISO.

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
12. [Hardware Detection](#hardware-detection)
13. [Cache Management](#cache-management)
14. [Graphics Stack Build Order](#graphics-stack-build-order)
15. [Release Plan](#release-plan)
16. [Re-converge](#re-converge)
17. [Known Gaps](#known-gaps)
18. [V2 Roadmap](#v2-roadmap)

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
│   ├── resolve.py                     # sysforge resolve subcommand
│   ├── update.py                      # sysforge update subcommand
│   ├── converge.py                    # sysforge converge subcommand (flag drift detection)
│   ├── packages_cmd.py                # sysforge packages namespace (list/add/remove/sync)
│   └── primitives/
│       ├── config.py                  # TOML config loading, path constants, system conf parsing
│       ├── profile.py                 # profile resolution, rule matching, consumes
│       ├── pkgbuild_meta.py           # static PKGBUILD parser (read-only)
│       ├── pkgbuild_patcher.py        # PKGBUILD mutation + flag extraction
│       ├── makepkg_wrapper.py         # build execution: emit conf, invoke makepkg
│       ├── aur_resolve.py             # recursive AUR dependency resolution + topo sort
│       ├── dep_analysis.py            # pre-build soname dependency checks
│       ├── failure.py                 # failure scenario handling (shared)
│       ├── cache_probe.py             # passive ccache/sccache monitoring ([CACHE] tag)
│       ├── aur.py                     # AUR RPC v5, git clone, pkgctl checkout, GPG key import
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
│           ├── reconfigure.py         # stage 5: pre-build checkpoint (implemented)
│           ├── toolchain.py           # stage 6: LLVM/GCC toolchain build (optional 3-pass PGO)
│           ├── packages.py            # stage 7: real implementation
│           └── kernel.py              # stage 8: full implementation
├── tests/
│   ├── conftest.py
│   ├── data/
│   │   ├── PKGBUILDs/                 # htop, llvm, lib32-llvm, cosmic, vulkan-headers-git
│   │   │   ├── complex.PKGBUILD  →  vulkan-headers-git.PKGBUILD  (symlink alias)
│   │   │   ├── complex2.PKGBUILD →  lib32-llvm.PKGBUILD           (symlink alias)
│   │   │   └── simple.PKGBUILD   →  htop.PKGBUILD                 (symlink alias)
│   │   ├── test_flag_profiles.toml
│   │   ├── etc/sysforge/
│   │   │   ├── flag_profiles.toml
│   │   │   ├── consumes_inference.toml
│   │   │   ├── append_conflict_groups.toml
│   │   │   ├── packages.toml
│   │   │   ├── toolchain.toml
│   │   │   └── kernel.toml
│   │   └── user/.config/sysforge/
│   │       └── flag_profiles.toml
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
    flag_profiles.toml
    consumes_inference.toml
    append_conflict_groups.toml
    packages.toml                    # default package manifest
~/.config/sysforge/
    flag_profiles.toml               # user overrides
    append_conflict_groups.toml      # user conflict group overrides (optional)
/usr/bin/sysforge
/var/lib/sysforge/
    pipeline_state.toml              # pipeline checkpoint state (created at runtime)
    build_state.toml                 # per-package build metadata (created at runtime, by sysforge build/update)
    sysforge.log                     # unified log (created at runtime, cleared on success)
```

---

## Package Manifest

`packages.toml` is the master list of packages SysForge installs. It is **separate from `flag_profiles.toml`** — package sourcing and build flag tuning are orthogonal concerns and must not be conflated.

Each entry declares:
- `source` — one of `repo` (pacman), `aur`, or `git` (direct PKGBUILD)
- `pkgbuild_patch` *(optional bool)* — if `true`, the PKGBUILD patching library runs on this package before build
- `cache` *(optional bool)* — `false` disables ccache/sccache for this package (required for PGO stages)

```toml
[build]
pkgbuild_dir = "~/builds"   # PKGBUILD root; auto-cloned if absent

[[package]]
name = "mesa-git"
source = "aur"
pkgbuild_patch = true

[[package]]
name = "llvm"
source = "aur"
cache = false   # PGO build — instrumented objects must never be cached
```

### Manifest lifecycle commands

`sysforge packages` is a namespace for managing an existing `packages.toml`:

- **`packages list`** (default when no subcommand) — tabulates all entries: name, source, and any optional fields set.
- **`packages add <pkg> [<pkg>...]`** — classifies each package (repo vs AUR via pacman/AUR RPC), infers `pkgbuild_patch` by running `extract_pkgbuild_profile()` on the local PKGBUILD if one exists, and appends the entry. Uses `[build] pkgbuild_dir` from the existing file first, falls back to `[paths] pkgbuild_dir` from `flag_profiles.toml`.
- **`packages remove <pkg>`** — removes the `[[package]]` block for the named entry using line-level manipulation; preserves all surrounding comments and section headers.
- **`packages sync`** — re-classifies each entry's `source` and re-checks `pkgbuild_patch` (if the local PKGBUILD is available). Non-destructive: manual fields (`cache`) are preserved verbatim. Comments are preserved. `--dry-run` shows what would change without writing.

All subcommands accept `--packages FILE` to target a specific file (default: `/etc/sysforge/packages.toml`).

Valid per-entry fields: `name`, `source`, `pkgbuild_patch`, `cache`, `reason`. Fields `profile` and `requires_hardware` are **removed** from the schema — do not add them.

`reason` *(optional string)* — `"explicit"` (default, may be omitted) or `"dependency"`. Tracks whether the entry was added directly by the user or auto-added as a transitive AUR dependency during a build with `--track-deps`. Used for display purposes (e.g. `packages list`) and to distinguish user intent; has no effect on build behaviour.

### `-march=native` strategy

SysForge uses `-march=native` rather than hardcoding CPU-specific flags. Optimization becomes a compile-time concern — it works across CPU families without separate logic. If a package is incompatible with native tuning, a higher-priority rule pointing to the `bare` profile overrides `-march` for that package only.

---

## Config Layer

### Config file hierarchy

- System default: `/etc/sysforge/flag_profiles.toml`
- User override: `~/.config/sysforge/flag_profiles.toml`

By default the user file **fully replaces** the system file. To layer on top instead, add `extends_system = true` at the top of the user file — user values take priority on all conflicts. User rule priorities are bumped by 100 on merge (range 100–199) to always outrank system rules (range 0–99).

### State directory

Pipeline state is written to `/var/lib/sysforge/` by default. Override via the `SYSFORGE_STATE_DIR` environment variable or `--state-dir` CLI flag; CLI takes priority. Both are logged when present. `SYSFORGE_STATE_DIR` is a SysForge bootstrap var and is intentionally not subject to the build tool env isolation rule.

### Profile conf override

Both `sysforge build` and `sysforge pipeline` accept `--profile-conf FILE` to substitute an alternate `flag_profiles.toml` at runtime, bypassing the default user/system search paths. Scope is intentionally limited to flag profiles — conflict groups and consumes inference are not affected (edit those files directly if needed). If the specified file sets `extends_system = true`, the standard system config is still merged underneath it via the normal `extends_system` logic.

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
6. **toolchain** — fully implemented (LLVM/GCC, optional 3-pass PGO bootstrap, compiler propagation to packages/kernel)
7. **packages** — fully implemented
8. **kernel** — fully implemented

Stages 1–4 are **bootstrap-only** — they run once from a live install environment. Stages 5–8 are **repeatable** and run on the installed system. Use `sysforge run pipeline --start-from reconfigure` to run the pre-build checkpoint on a live system; use `--start-from packages` to skip straight to builds. Stages 5–8 are also available as standalone `sysforge run <stage>` commands for repeated, out-of-pipeline use (e.g. `sysforge run toolchain`, `sysforge run packages`).

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

**`iso-install.sh`** (`tools/iso-install.sh`) automates the live-ISO setup steps: checks connectivity, installs sysforge (lightweight pip install, no build tools), copies `/etc/sysforge/` defaults, and prompts for all required bootstrap values with validation (timezone checked against `/usr/share/zoneinfo/`, passwords entered silently with confirmation). Writes a complete `bootstrap.toml` and prints the pipeline command when done.

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

Builds a custom kernel from a PKGBUILD. The stage is a clean no-op if `/etc/sysforge/kernel.toml` is absent, so systems using a stock pacman kernel skip it without needing `--start-from`.

**`kernel.toml` structure:**

```toml
pkgname      = "linux-custom"
pkgbuild_dir = "~/builds"        # parent dir; PKGBUILD is at <pkgbuild_dir>/<srcdir>/PKGBUILD
srcdir       = "linux"           # source directory name if different from pkgname (optional)
bootloader   = "systemd-boot"    # systemd-boot | grub | none  (default: systemd-boot)

[[kconfig]]                      # manual kconfig overrides (optional, repeatable)
option = "CONFIG_HZ_1000"        # must match CONFIG_[A-Z0-9_]+
value  = "y"                     # y | m | n | non-empty string
```

`srcdir` is needed when the PKGBUILD directory name differs from `pkgname` (e.g. `pkgname = "linux-custom"` but the repo is cloned as `~/builds/linux`). Defaults to `pkgname` if omitted.

**kconfig fragment:**

Hardware-driven kconfig entries come from `hardware_profile.toml [kconfig]` (emitted by the hardware stage). Manual overrides from `kernel.toml [[kconfig]]` are merged on top — manual wins on conflict with a `[WARN]`. The combined result is written to `<pkgbuild_dir>/<srcdir>/sysforge.config` before `makepkg` runs. The PKGBUILD must merge this into its `.config`; a compatible PKGBUILD calls `scripts/kconfig/merge_config.sh` in `prepare()`.

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
- `source = "aur"` / `"git"` → `_resolve_pkgbuild()` → `makepkg_wrapper.run()`. PKGBUILD lookup order: `packages.toml [build] pkgbuild_dir` → `flag_profiles [paths] pkgbuild_dir` → AUR clone.
- Hardware-gated packages skipped if `hardware_profile.toml` is absent or key is missing
- Non-fatal per-package failures: build continues, failures recorded in state
- Summary at end: `Total | Built | Failed | Skipped`

### Toolchain stage (stage 6)

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

**PKGBUILD resolution:** follows `find_pkgbuild` lookup order (local `pkgbuild_dir` → `pkgctl repo clone`) for every package. At stage start, resolved paths are displayed in a table and the user is prompted to confirm or abort. On abort, the resume command is printed (`sysforge pipeline --resume --state-dir <dir>`) so they can make manual modifications and return.

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

All modules independently testable. 898 pytest tests (`pytest` from repo root).

### `log.py`

Structured logging module. Output goes to stderr (verbosity-gated) and optionally to log files (always full verbosity). File handles are module-level globals managed by `open_unified_log`/`close_unified_log` and `open_pkg_log`/`close_pkg_log`. All file write errors are silently swallowed so file I/O can never interrupt a build.

```
[SYSFORGE][LEVEL][TAG] message
```

Four levels: `error` (always shown), `warn` (`-v`), `info` (`-vv`), `debug` (`-vvv`). Set once at CLI entry with `log.set_verbosity(args.verbose)`.

Modules obtain a bound `Logger` instance via `log.get_logger("TAG")`, which stores the tag and exposes the same `ui`/`error`/`warn`/`info`/`debug`/`newline`/`prompt_prefix` interface as the module-level functions. Modules with multiple logging subsystems (e.g. `makepkg_wrapper.py`, `profile.py`, `aur.py`) create multiple named loggers at module level (`_conf_log`, `_build_log`, etc.). Module-level helpers (`open_unified_log`, `close_unified_log`, `open_pkg_log`, `close_pkg_log`, `set_verbosity`, `set_dry_run_mode`) are called directly on the `log` module.

### `config.py`

TOML config loading and path resolution. Public API:
- `load_config(config_paths=None)` — loads `flag_profiles.toml`, merges user onto system via `extends_system`, validates rule priorities
- `load_conflict_groups(paths=None)` — loads `append_conflict_groups.toml`
- `load_consumes_inference(paths=None)` — loads `consumes_inference.toml`
- `find_pkgbuild(pkg, config=None)` — resolves a bare package name, directory path, or PKGBUILD path to an absolute PKGBUILD path. Search order: (1) direct path or directory (resolves `dir/PKGBUILD`), (2) `<cwd>/<name>/PKGBUILD`, (3) `<config [paths] pkgbuild_dir>/<name>/PKGBUILD`, (4) auto-clone if not found locally — repo packages via `pkgctl repo clone --protocol=https`, AUR packages via `aur_clone`. Used by `sysforge build`, `sysforge resolve`, and the packages stage.

`[paths] pkgbuild_dir` in `flag_profiles.toml` is the user-configured root for local PKGBUILDs (`~/src` by default). Auto-clone also targets this directory.
- `parse_system_makepkg_conf(path=None)` — parses `/etc/makepkg.conf` into `{key: raw_value_string}` for use in temp conf generation. Handles backslash line continuation (e.g. `CFLAGS="... \\\n  -flag"`) and multiline bash array values (e.g. `VCSCLIENTS=(...)` spanning multiple lines) by tracking paren depth across lines. Merges user conf (`$XDG_CONFIG_HOME/pacman/makepkg.conf`, `~/.makepkg.conf`) on top of system conf.

### `pacman.py`

All pacman and batch-install shared operations. Public API:
- `get_pkgdest()` — resolves the `PKGDEST` directory from makepkg.conf
- `snapshot_pkg_dir(pkgdest)` — records the set of `.pkg.tar.*` files currently in pkgdest before a build
- `batch_install_pkgs(pkgdest, pre_snapshot, ...)` — diffs the post-build pkgdest against the snapshot and installs all new packages in a single `sudo pacman -U`
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

### `pkgbuild_patcher.py`

All PKGBUILD mutation. Active when `build_mode = "patched_pkgbuild"` or `"kernel"` on the resolved profile.

**Flag extraction** (`extract_pkgbuild_profile`) scans all function bodies and extracts bare, `export`, and `+=` assignments to known flag variables. Strips self-references (`$CFLAGS` in CFLAGS), skips complex bash expressions (e.g. `${CFLAGS/-g /-g1 }`), expands packed `-Wl,a,b,c` tokens into individual sub-tokens. Returns a synthetic profile dict used as the implicit chain root in `merge_extends` — forming the chain: `pkgbuild_extracted → bare → standard → optimized`.

**Conditional block handling** (`_extract_conditional_blocks`) finds `if...fi` blocks containing extractable key assignments using depth-tracked scanning. Entire blocks are removed from the patched PKGBUILD, never partially.

**Patching** (`apply_patch_pkgbuild`) writes `PKGBUILD.sysforge` with all managed flag assignments and conditional blocks removed. The original is untouched. Artifacts persist on build failure for diagnosis; `cleanup_patch_artifacts` removes them on success. On failure, the warning only mentions `pkgbuild_extracted_profile.toml` if it was actually written (non-empty extraction).

Inline `make VAR=val` and `cmake -DKEY=val` lines are only removed when the key is in `_EXTRACTABLE_KEYS` — keys that sysforge manages. This prevents accidental removal of kernel build commands like `make LOCALVERSION=...` or `make INSTALL_MOD_PATH=...` which are real build invocations, not flag assignments.

**Noninteractive kconfig patching** (`patch_noninteractive_kconfig`) replaces interactive kconfig targets (`oldconfig`, `nconfig`, `menuconfig`, `xconfig`, `gconfig`) with `make olddefconfig` in an already-patched PKGBUILD file. Called by the kernel stage after normal patching; modifies `PKGBUILD.sysforge` in place. Preserves `VAR=val` arguments before the target and trailing comments.

### `dep_analysis.py`

Pre-build soname dependency checks. Runs before `_run_build` in `makepkg_wrapper.run()`.

`check_soname_deps` filters `.so` and `.so=N` entries from `depends`, parses ldconfig -p output, and checks presence and major version. `libcap.so=2` means libcap.so.2 must be present in ldconfig's cache.

Version constraint checking (pacman -Q / vercmp) was intentionally omitted — makepkg already does this and any pre-check adds false-positive risk without meaningful value.

Both functions accept injectable callables for testing. Non-fatal by default; configurable via `abi_mismatch` in `[failure_handling]`.

**Recursive AUR dependency resolution (v1.0):**

`makepkg --syncdeps` installs missing `depends`/`makedepends` via `pacman -S`. Pacman has no AUR visibility, so any dep that is AUR-only and not already installed will cause the build to fail. Sysforge does not currently detect or pre-build AUR deps.

New module: `primitives/aur_resolve.py`. Public API:

- `resolve_aur_deps(pkgbuild_path, config) -> list[ResolvedDep]` — full recursive resolution for a single package
- `resolve_aur_deps_batch(pkgbuild_paths, config) -> list[ResolvedDep]` — batch resolution for multiple packages (de-duplicated, single topo-sorted build order)

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
- **`sysforge build`** — resolve before building. `--track-deps` auto-adds resolved AUR deps to `packages.toml` with `reason = "dependency"`.
- **`run packages` stage** — resolve before building each AUR/profiled package. `--track-deps` behaves the same.
- **Batch builds (`update`, `converge`)** — resolve after `collect_makedeps()`, before `batch_install_makedeps()`. AUR deps are built and installed first, then the main batch proceeds. No `--track-deps` for update/converge (operates on already-tracked packages only).
- **`sysforge resolve --deps <pkg>`** — standalone dry-run inspection. Shows the full dep tree with build order, AUR vs repo classification, and which deps are already installed.

The `dep_analysis.py` soname check is orthogonal — it validates shared-library ABI for packages that *are* installed, not whether they can be installed in the first place.

### `failure.py`

Cross-cutting failure scenario handler. Imported by `makepkg_wrapper` and `dep_analysis` to avoid circular imports.

`handle_failure(scenario, message, config, fallback=None)` dispatches to `abort`, `error`, `warn_and_fallback`, or `fallback` based on `[failure_handling]` config. `profile_missing` and `tempfile_write_failed` always abort regardless of config.

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

**Makepkg flag passthrough:** `extra_flags` from the CLI (`-m "-sfci"`) are appended after profile `makepkg_flags`. Combined short flags are expanded: `-sfci` → `[-s, -f, -c, -i]`.

### `aur.py`

AUR RPC queries, package source detection, git/pkgctl clone helpers, and GPG key import.

- `repo_packages(names)` — single `pacman -Si name1 name2 ...` invocation; returns the subset of names present in any sync DB. Use for batch classification (O(1) subprocesses). Parses stdout for `Name : <pkg>` lines; packages not found produce errors to stderr only.
- `is_repo_package(name)` — single-name wrapper around pacman -Si; returns `True` if found in any sync DB. Used by `find_pkgbuild` to route auto-clone: repo packages → `pkgctl_checkout`, AUR → `aur_clone`.
- `aur_info(names)` — single batch `GET https://aur.archlinux.org/rpc/v5/info?arg[]=…` for all names; returns `{name: result_dict}`. Silent on network/JSON errors (returns `{}`).
- `aur_clone(name, dest)` — `git clone https://aur.archlinux.org/<name>.git <dest>`; raises `RuntimeError` on failure.
- `pkgctl_checkout(name, dest)` — `pkgctl repo clone --protocol=https <name>` run in `dest.parent`; fetches official Arch packaging repo. Raises `RuntimeError` on failure.
- `import_pgp_keys(pkgmeta, pkgbuild_path)` — ensures all `validpgpkeys` listed in the PKGBUILD are in the GPG keyring before `makepkg` runs. Strategy: (1) import any bundled `.asc` files from `keys/pgp/` alongside the PKGBUILD, (2) check which keys are still missing via `gpg --list-keys`, (3) fetch remaining via `gpg --recv-keys`. Import failures are logged as warnings — makepkg surfaces a clearer error if a key is still absent at verification time.
- `fetch_aur_name_cache(force=False)` — downloads `https://aur.archlinux.org/packages.gz` and extracts it to `~/.cache/sysforge/aur-packages.txt`. Skips the download if the cache is less than 24 hours old unless `force=True`. Called as a side effect of `sysforge update`; read by `sysforge completions packages` to provide AUR package name completion.

`sysforge completions packages` — outputs local pkgbuild_dir packages + pacman sync DB names + AUR cache. Used by zsh completion for `build`, `packages add`. Caps output via `grep -m N "^$PREFIX"` in the completion script to avoid rendering thousands of entries; shows `zle -M` message when limit exceeded.

`sysforge completions local` — outputs only locally-cloned packages from `pkgbuild_dir` (no network). Used by zsh completion for `resolve` (only packages with a local PKGBUILD can be resolved without triggering a download).

`sysforge completions manifest` — outputs only names from the active `packages.toml`. Used by zsh completion for `packages remove` (only valid to remove what's already there).

### `build_state.py`

Build state persistence. Writes `/var/lib/sysforge/build_state.toml` after each successful build. Per-package fields: `pkgver`, `pkgrel`, `epoch`, `pkgbase`, `pkgbuild_dir`, `build_mode` (`"pacman"` | `"profiled"`), `flags_string` (serialized resolved compiler flags, newline-separated `KEY=value` lines), `built_at` (ISO 8601 UTC timestamp). Read by `sysforge update` for version drift detection and by `sysforge converge` for flag drift detection. Follows the same atomic write-then-rename pattern as `pipeline/state.py`. Split packages (multiple `pkgname` from one `pkgbase`) each get their own entry, all pointing at the same `pkgbuild_dir`.

### `version.py`

Version comparison utilities. `vercmp(a, b)` wraps the system `vercmp` binary and returns -1/0/1 (negative/zero/positive output from vercmp is clamped). `format_version(globals_)` assembles an `[epoch:]pkgver-pkgrel` string from parsed PKGBUILD globals, omitting the epoch prefix when it is `"0"` or absent.

### `fetch.py`

Implements `sysforge fetch` — download one or more PKGBUILDs into `pkgbuild_dir` without building. Uses `find_pkgbuild` (auto-clones via `pkgctl_checkout` or `aur_clone` if not already present), then runs `git_pull_rebase` for packages that were already cloned (skipped with `--no-update`). Prints the resulting PKGBUILD directory path for each package. Exits 1 if any package failed.

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

Implements `sysforge update` — the update manager. Algorithm:

1. Refresh the AUR name cache as a side effect (`fetch_aur_name_cache()`).
2. If `--all`: run `_discover_and_add()` — two phases:
   - **Phase 1:** find foreign packages (`pacman -Qm`) not in `build_state.toml` or `packages.toml`; AUR-verify, append to `packages.toml`, compare versions against AUR RPC, queue outdated ones for rebuild.
   - **Phase 2:** find packages in `packages.toml` that are installed but have no `build_state` record. For those with a local PKGBUILD clone: `git pull --rebase` then compare PKGBUILD version against installed. For those without a local clone: check `pacman -Si` (source=repo) or AUR RPC (source=aur, batched) to get the current version — no auto-clone during discovery. If outdated, clone on demand at build time via `find_pkgbuild`.
3. Load `build_state.toml` to get the set of sysforge-managed packages. If positional `PKG` names were given, filter to that subset (unrecognised names are warned and skipped).
4. Group by `pkgbase` to deduplicate split packages.
5. For each `pkgbase`: `git pull --rebase` the PKGBUILD dir (unless `--no-update`), parse the updated PKGBUILD, get the installed version via `pacman -Q`, compare with `vercmp`.
6. VCS packages (`-git`, `-svn`, `-hg`, `-bzr`) are flagged as `DEVEL` — their `pkgver` is only meaningful after running `pkgver()`, so static comparison is not possible. They are rebuilt only when `--devel` is passed.
7. Print a summary table: `NEEDS_REBUILD`, `UP_TO_DATE`, `DEVEL`, `NOT_INSTALLED`, `DOWNGRADE`, `PULL_FAILED`.
8. Rebuild `NEEDS_REBUILD` and discovered `OUTDATED` packages (and `DEVEL` if `--devel`) via `makepkg_wrapper.run()`. All update builds default to `--cleanbuild` (`-C`) to prevent stale `$srcdir` from a previous failed run causing patch-already-applied errors in `prepare()`. `--syncdeps`/`-s` and `--install`/`-i` are stripped; packages are installed in a single `sudo pacman -U` call at the end. Without `--interactive`, build failures are logged and skipped (batch mode); with `--interactive`, failures pause for user input.

Positional: `[PKG ...]` — optional package names to restrict the run to a subset of sysforge-managed packages.

Flags: `--all`, `--interactive`, `--packages`, `--dry-run`, `--devel`, `--no-update`, `--state-dir`, `--profile-conf`, `--cache-report`, `--no-pkg-log`, `--persist-log`, `--log-dir`, `--makepkg`.

### `converge.py`

Implements `sysforge converge` — the flag drift detector. Algorithm:

1. Load `build_state.toml`. Group by `pkgbase`.
2. For each profiled package (`build_mode = "profiled"`): re-resolve the current profile via `parse_pkgbuild` → `match_rules` → `resolve_profile` → `serialize_flags`.
3. Diff the stored `flags_string` against the freshly resolved flags. Packages where the flags differ are `DRIFTED`; identical are `IN_SYNC`. Packages without a stored `flags_string` (built before this feature) are `NO_FLAGS`. Missing PKGBUILD → `NO_PKGBUILD`. Non-profiled packages are silently omitted.
4. Print a per-package summary with flag diffs for `DRIFTED` entries.
5. With `--apply`: rebuild all `DRIFTED` packages via `makepkg_wrapper.run()` with `update=False`.

Without `--apply`, the command is read-only — it reports drift but does not rebuild. Flags: `--apply`, `--state-dir`, `--profile-conf`, `--no-pkg-log`, `--persist-log`, `--log-dir`, `--cache-report`.

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

Defined in `/etc/sysforge/append_conflict_groups.toml`:

```toml
[conflict_groups]
pic   = ["-fPIC", "-fPIE", "-fpic", "-fpie", "-fno-pic", "-fno-pie"]
lto   = ["-flto", "-flto=thin", "-flto=full", "-fno-lto"]
stack = ["-fstack-protector", "-fstack-protector-strong", "-fno-stack-protector"]
```

User-defined groups in `~/.config/sysforge/append_conflict_groups.toml` follow the same `extends_system` merge model. Explicit conflict groups take precedence over prefix matching.

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

- **Default:** auto-inferred from `makedepends` via `consumes_inference.toml`
- **Override:** explicit `consumes` on a profile replaces the inferred value

```toml
# /etc/sysforge/consumes_inference.toml
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

3. **GCC thin-LTO rewrite** — `-flto=thin` is clang-only. When the effective CC is GCC-based, rewrites `-flto=thin` → `-flto` in LTOFLAGS, CFLAGS, CXXFLAGS, and LDFLAGS. Falls back to system conf values when the profile doesn't override a key.

4. **GCC + lld LTO disabling** — GCC LTO produces `.gnu.lto_*` bitcode that only GNU ld/gold can process; lld cannot read it. When CC is GCC-based and the effective linker is lld, LTO is disabled entirely: LTOFLAGS cleared, `-flto*` stripped from flag keys, and `lto` flipped to `!lto` in OPTIONS (prevents makepkg's `${LTOFLAGS:--flto}` fallback).

5. **Full LTO stripping** (PGO only) — strips `-flto`/`-flto=full` from CFLAGS/CXXFLAGS/LDFLAGS and clears LTOFLAGS during PGO passes.

Guards 3–4 determine the effective CC from: `cc_override` (CLI `--cc`) > `resolved_profile["CC"]`. The effective linker is determined by guard 1 and shared with subsequent guards.

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

**Unified log** — one file for the entire `sysforge pipeline` run.

- Default path: `<state_dir>/sysforge.log` (i.e. `/var/lib/sysforge/sysforge.log`)
- Appends across runs. Cleared (truncated, not deleted) on successful pipeline completion. Consecutive failures accumulate the full log from first failure until success.
- A `# log cleared after successful run` marker is left in the file after truncation.
- `--log-dir <path>` overrides the directory.
- `--purge-log` truncates before the run starts, regardless of outcome. Use when you want a clean log for a fresh attempt.
- `--persist-log` suppresses truncation on success. Use when you want to keep the log for post-run analysis.
- `--no-unified-log` disables the unified log entirely for this run.

**Per-package log** — one file per package build, written alongside the PKGBUILD.

- Path: `<pkgbuild_dir>/<pkgname>/sysforge_<pkgname>.log`
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
| `[CONFIG]` | Config file loading (`flag_profiles.toml`, conflict groups, consumes inference) |
| `[GROUPS]` | Package group resolution |
| `[PROFILE]` | Profile resolution, rule matching, extends chain |
| `[STATE]` | Pipeline state directory resolution |

**AUR / package management:**

| Tag | Covers |
|---|---|
| `[AUR]` | AUR name cache lifecycle, clone operations |
| `[DEP]` | Soname dependency graph checks |
| `[FAILURE]` | Failure scenario dispatch |
| `[MANIFEST]` | AUR RPC queries |
| `[PACMAN]` | pacman database and install operations |
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

**v0.1.0 (current):** `argparse-manpage` generates `man/sysforge.1` from the argparse parser exposed via `_build_parser()` in `cli.py`. Generated during `make man` and during the PKGBUILD `build()` step (requires `python-argparse-manpage` makedepend). The man page is not checked into git — it is always generated from the parser at build/package time. Makefile target: `make man`.

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

Configured via a `[cache]` table in `flag_profiles.toml`:

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
- **v0.1.0:** profiled AUR helper — all userspace commands stable under real use: `build`, `fetch`, `update`, `resolve`, `packages` (list/add/remove/sync), `run pipeline`, `run reconfigure`, `run toolchain`, `run packages`, `run kernel`. Target milestone for AUR publication.
- **v1.0:** system bootstrapper — stages 1–4 fully implemented (partition, base_install, hardware, configure). Configure stage installs systemd-boot, enables NetworkManager/sshd, creates primary user with sudo, writes shell dotfiles, sets passwords, and sets the configured default login shell. Remaining v1.0 work: recursive AUR dependency resolution (see `dep_analysis.py` section); `repo_mode = "profiled"` support in `sysforge update`; man page migration from `argparse-manpage` to a scdoc hybrid (hand-written narrative + auto-generated OPTIONS — see Man Pages section below); direct makepkg flag passthrough via `parse_known_args` (completions done, implementation pending).
- **v1.x:** package groups (named DE sets for opt-in without enumerating every package); rule priority auto-calculation (CSS-specificity-style scoring from rule conditions); configure stage additions (btrfs snapshots, ccache/sccache init check, build time estimates); LLVM target filtering from hardware detection.

### AUR publishing process

```bash
git clone ssh://aur@aur.archlinux.org/sysforge.git
makepkg --printsrcinfo > .SRCINFO
git add PKGBUILD .SRCINFO
git commit -m "Initial release"
git push
```

---

## Re-converge

Two commands address drift in sysforge-managed packages:

**`sysforge update [PKG ...]`** (implemented) — handles **version drift**. After `git pull --rebase` on each PKGBUILD dir, it compares the new `pkgver`/`pkgrel`/`epoch` against the installed version via `vercmp`. Packages where the PKGBUILD is newer are rebuilt with the current profile. VCS packages (`-git`, etc.) require `--devel` to rebuild since their version is only known after running `pkgver()` during the build. One or more package names may be given as positional arguments to restrict the run to a subset of sysforge-managed packages; unrecognised names are warned and skipped.

**PGO toolchain packages** (`build_mode = "pgo_llvm_toolchain"`) are handled specially during update. `makepkg_wrapper.run()` reads `toolchain.toml → pgo_store`, checks for a saved `clang.profdata` and its `clang.profdata.version` sidecar, and compares the sidecar's LLVM major version against the PKGBUILD's `pkgver` major. If they match, `-fprofile-use=<profdata>` is injected and the build proceeds as a PGO-optimised build. If profdata is absent or version-mismatched (e.g. after a major LLVM bump), the user is prompted: **[p]lain build or [s]kip (default: skip)**. In non-interactive mode the build is skipped automatically. Skipped packages are counted separately in the update summary and do not count as failures. To rebuild profdata after a major version bump, run `sysforge run toolchain`. The toolchain stage itself also reuses compatible profdata — see the **Profdata reuse** section under stage 6.

**`sysforge converge`** (implemented) — handles **profile/flag drift**. Same package version but different compiler configuration — e.g. profile changed, new flag added, or build mode switched. At build time, `makepkg_wrapper.run()` stores the resolved flags string per package in `build_state.toml`. `converge` re-resolves the current profile for each package and diffs the result against the stored flags string; packages where the flags have changed are reported with a flag diff. Without `--apply` the command is read-only; `--apply` rebuilds all drifted packages.

`build_state.toml` is the shared source of truth for both commands. Written by `makepkg_wrapper.run()` after each successful build.

DAG stages are categorised as **bootstrap-only** (partition, base_install, configure) or **repeatable** (hardware, reconfigure, toolchain, packages, kernel). Only repeatable stages participate in re-converge runs. `hardware` is repeatable because re-detecting after a hardware change (e.g. GPU swap) is safe and needs no root.

---

## Known Gaps

Implemented behaviour that is incomplete or has known limitations. These are not deferred features — they are holes in currently active code.

**`sysforge update` is scoped to sysforge-managed packages by default.** `build_state.toml` records only packages that sysforge built. Packages installed via pacman from repos are not tracked; `pacman -Syu` remains the update path for those. `sysforge update --all` extends this: it discovers all foreign (non-repo) packages via `pacman -Qm` that are not yet in `build_state.toml` or `packages.toml`, and checks packages in `packages.toml` with no build record — using git pull for those with local PKGBUILD clones, and `pacman -Si` / AUR RPC for those without.

**`repo_mode = "profiled"` is wired in the packages stage only.** The `[build] repo_mode = "pacman" | "profiled"` setting in `packages.toml` is parsed and honoured by `run packages` and `run pipeline` — repo packages with `repo_mode = "profiled"` (or per-package `pkgbuild_patch = true`) are built from source via `_build_aur()` using `find_pkgbuild` (which calls `pkgctl_checkout` for repo packages). It has no effect on `sysforge build` or `sysforge update` — those commands operate on individual packages the user explicitly targets, not on manifest-wide policy. v1.0 target: wire `repo_mode` into `sysforge update` so tracked repo packages with `repo_mode = "profiled"` are rebuilt from source when the Arch packaging repo has a newer version.

**`packages.toml [build] pkgbuild_dir` and `flag_profiles [paths] pkgbuild_dir` are separate.** The pipeline's `_resolve_pkgbuild` prefers `[build] pkgbuild_dir`; falls back to `[paths] pkgbuild_dir`. They can point to different directories or the same one — there's no enforcement that they match.

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

---

## V2 Roadmap

V2 goal: advanced AUR helper features beyond the v0.1.0 scope.

V2 candidates:
- **PKGBUILD review** — present diffs to the user before building an AUR package
