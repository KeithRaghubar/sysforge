# SysForge Design Document

SysForge is an AUR helper for Arch Linux with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles, tracks build state for update detection, and automates the full build lifecycle — from fetching PKGBUILDs to installing profiled packages. Pacman owns the package database; SysForge owns the build configuration layer above it.

The v0.1.0 milestone is a functional yay replacement: install, update, and manage AUR and custom packages with system-tuned profiled builds. A full system bootstrapper (stages 1–4: partition, base install, hardware detection, configure) is scoped to v1.0.

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

SysForge was motivated by Gentoo's source-level control and performance tuning, without Portage's fragility and maintenance overhead. The core insight is that Gentoo conflates several concerns that are better separated:

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

The closest analogy is `archinstall` — a tool that lives in the Arch ecosystem and produces an Arch system.

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

---

## Directory Structure

### Development (local repo)

```
sysforge/
├── sysforge/
│   ├── __init__.py
│   ├── cli.py                         # CLI entry point and subcommand wiring
│   ├── log.py                         # structured logging (stderr + optional file output)
│   ├── manifest.py                    # packages.toml generator
│   ├── resolve.py                     # sysforge resolve subcommand
│   ├── update.py                      # sysforge update subcommand
│   └── primitives/
│       ├── config.py                  # TOML config loading, path constants, system conf parsing
│       ├── profile.py                 # profile resolution, rule matching, consumes
│       ├── pkgbuild_meta.py           # static PKGBUILD parser (read-only)
│       ├── pkgbuild_patcher.py        # PKGBUILD mutation + flag extraction
│       ├── makepkg_wrapper.py         # build execution: emit conf, invoke makepkg
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
│           ├── partition.py           # stage 1: stub
│           ├── base_install.py        # stage 2: stub
│           ├── hardware.py            # stage 3: stub
│           ├── configure.py           # stage 4: stub (bootstrap-only: hostname, locale, mirrorlist)
│           ├── reconfigure.py         # stage 5: pre-build checkpoint (implemented)
│           ├── toolchain.py           # stage 6: stub
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
│   │   │   └── packages.toml
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
│   ├── test_manifest.py
│   ├── test_parser.py
│   ├── test_patcher.py
│   ├── test_pipeline.py
│   ├── test_pipeline_runner.py
│   ├── test_pipeline_state.py
│   ├── test_resolve.py
│   ├── test_stage_kernel.py
│   ├── test_stage_packages.py
│   ├── test_system_conf.py
│   ├── test_update.py
│   ├── test_build_state.py
│   ├── test_version.py
│   └── test_wrapper.py
├── completions/
│   └── _sysforge                      # zsh completion script
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
- `requires_hardware` *(optional)* — hardware capability key that must be present in `hardware_profile.toml`; absent packages are excluded silently at pipeline time
- `cache` *(optional bool)* — `false` disables ccache/sccache for this package (required for PGO stages)

```toml
[build]
pkgbuild_dir = "~/builds"   # PKGBUILD root; auto-cloned if absent

[[package]]
name = "nvidia-open-dkms"
source = "repo"
requires_hardware = "nvidia_gpu"

[[package]]
name = "mesa-git"
source = "aur"
pkgbuild_patch = true

[[package]]
name = "llvm"
source = "aur"
cache = false   # PGO build — instrumented objects must never be cached
```

### Manifest generation

`sysforge manifest` generates a `packages.toml` stub from a list of package names. Classification: repo packages detected via `pacman -Si`; remaining names confirmed via AUR RPC v5 batch query (`aur.py`); anything not found in either is excluded with a warning.

```bash
sysforge manifest htop neovim mold > packages.toml
sysforge manifest --file pkglist.txt >> packages.toml
```

The packages stage resolves `packages.toml` from:
1. `--packages FILE` CLI flag
2. `/etc/sysforge/packages.toml` (system default)

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

1. **partition** — deferred to v1.0
2. **base_install** — deferred to v1.0
3. **hardware** — deferred to v1.0
4. **configure** — deferred to v1.0 (bootstrap-only: hostname, locale, mirrorlist)
5. **reconfigure** — fully implemented (pre-build checkpoint: config review, disk/network/gpg checks, build preview)
6. **toolchain** — fully implemented (LLVM/GCC, optional 3-pass PGO bootstrap, compiler propagation to packages/kernel)
7. **packages** — fully implemented
8. **kernel** — fully implemented

Stages 1–4 raise `NotImplementedError` with `--start-from` guidance. Use `--start-from reconfigure` to run the pre-build checkpoint on a live system; use `--start-from packages` to skip straight to builds.

### Runner

`run_pipeline(config, options, stages)` sequences stage execution:
- Validates `depends_on` references before running
- Reads checkpoint state to determine start index
- Calls `stage.run()`, marks done/failed, saves state after each stage
- On `NotImplementedError`: prints `--start-from` guidance and exits
- On `RuntimeError`: saves state and exits with resume instructions
- `--dry-run`: logs what would run without calling `stage.run()`

Guard against accidental state clobber: if a state file exists and neither `--resume` nor `--start-from` is passed, the runner exits with instructions rather than overwriting.

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

**Opt-in:** stage is a clean no-op if `/etc/sysforge/toolchain.toml` is absent. Systems that skip this stage use whatever compiler is already installed; packages and kernel stages proceed normally.

**`toolchain.toml` structure:**

```toml
compiler = "llvm"   # "llvm" or "gcc"
pgo = true          # only meaningful when compiler = "llvm"; ignored for gcc

# Package lists — all have sane defaults, override only if needed
[packages]
pgo     = ["llvm", "llvm-libs", "clang", "lld"]
non_pgo = ["polly", "compiler-rt", "openmp", "spirv-llvm-translator"]
lib32   = ["lib32-llvm", "lib32-llvm-libs", "lib32-clang", "lib32-spirv-llvm-translator"]

# Staging prefix for PGO stage 2 instrumented binary
pgo_staging = "/var/tmp/sysforge-llvm-stage2"
```

For `compiler = "gcc"`, the default package set is `["gcc", "gcc-libs"]`.

**PKGBUILD resolution:** follows `find_pkgbuild` lookup order (local `pkgbuild_dir` → `pkgctl repo clone`) for every package. At stage start, resolved paths are displayed in a table and the user is prompted to confirm or abort. On abort, the resume command is printed (`sysforge pipeline --resume --state-dir <dir>`) so they can make manual modifications and return.

**LLVM PGO bootstrap (three passes, only when `pgo = true`):**

1. **Pass 1** — build with system compiler. Uses the `pgo_llvm_toolchain` profile; `cache = false` on all packages (instrumented objects must not be cached).
2. **Pass 2** — rebuild with instrumented binary (`-fprofile-generate`). Installed to the staging prefix (`pgo_staging`) rather than the live system, keeping the system compiler clean.
3. **Pass 3** — final optimized build (`-fprofile-use`), with `CC`/`CXX` pointing at the staged binary from pass 2. Installs to the system. Staging prefix is removed on success.

**`pgo = false` path:** single build pass with the `pgo_llvm_toolchain` profile. No staging prefix. Useful when custom flags (`-march=native`) are wanted without the overhead of a full PGO cycle.

**GCC path (`compiler = "gcc"`):** single build pass. `pgo` field is ignored. Produces `/usr/bin/gcc` and `/usr/bin/g++`.

**Compiler propagation:** on completion the toolchain stage writes the resolved compiler paths into pipeline state:

```toml
[stages.toolchain.result]
cc  = "/usr/bin/clang"   # or "/usr/bin/gcc"
cxx = "/usr/bin/clang++" # or "/usr/bin/g++"
ld  = "lld"              # llvm path only; absent for gcc
```

The packages and kernel stages read these values and inject them into the build environment, overriding any profile-level `CC`/`CXX` defaults. If the toolchain stage was skipped, these keys are absent and stages fall back to the profile.

---

## Primitives Layer

All modules independently testable. 648 pytest tests (`pytest` from repo root).

### `log.py`

Structured logging module. Output goes to stderr (verbosity-gated) and optionally to log files (always full verbosity). File handles are module-level globals managed by `open_unified_log`/`close_unified_log` and `open_pkg_log`/`close_pkg_log`. All file write errors are silently swallowed so file I/O can never interrupt a build.

```
[SYSFORGE][LEVEL][TAG] message
```

Four levels: `error` (always shown), `warn` (`-v`), `info` (`-vv`), `debug` (`-vvv`). Set once at CLI entry with `log.set_verbosity(args.verbose)`.

### `config.py`

TOML config loading and path resolution. Public API:
- `load_config(config_paths=None)` — loads `flag_profiles.toml`, merges user onto system via `extends_system`, validates rule priorities
- `load_conflict_groups(paths=None)` — loads `append_conflict_groups.toml`
- `load_consumes_inference(paths=None)` — loads `consumes_inference.toml`
- `find_pkgbuild(pkg, config=None)` — resolves a bare package name, directory path, or PKGBUILD path to an absolute PKGBUILD path. Search order: (1) direct path or directory (resolves `dir/PKGBUILD`), (2) `<cwd>/<name>/PKGBUILD`, (3) `<config [paths] pkgbuild_dir>/<name>/PKGBUILD`, (4) auto-clone if not found locally — repo packages via `pkgctl repo clone --protocol=https`, AUR packages via `aur_clone`. Used by `sysforge build`, `sysforge resolve`, and the packages stage.

`[paths] pkgbuild_dir` in `flag_profiles.toml` is the user-configured root for local PKGBUILDs (`~/src` by default). Auto-clone also targets this directory.
- `parse_system_makepkg_conf(path=None)` — parses `/etc/makepkg.conf` into `{key: raw_value_string}` for use in temp conf generation. Handles multiline bash array values (e.g. `VCSCLIENTS=(...)` spanning multiple lines) by tracking paren depth across lines.

### `profile.py`

Profile resolution and rule matching. Public API:
- `merge_extends` — resolves the full `extends` chain into a flat profile dict, applying `[profiles.x.append]` token-level merges with conflict groups
- `match_rules` — evaluates all match fields against a parsed PKGBUILD. `pkgnames` rules match against both `pkgname` and `pkgbase` — split packages (e.g. kernels) set `pkgbase` to the canonical name and `pkgname` to an array of unexpanded sub-package names; matching on `pkgbase` ensures rules like `pkgnames = ["linux-custom"]` work correctly.
- `resolve_profile` — selects the winning rule by priority; optionally injects `pkgbuild_extracted` as the chain root
- `resolve_groups` — accumulates package groups from PKGBUILD, defaults, and all matched rules
- `resolve_consumes` — determines which conf types the build needs

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

### `failure.py`

Cross-cutting failure scenario handler. Imported by `makepkg_wrapper` and `dep_analysis` to avoid circular imports.

`handle_failure(scenario, message, config, fallback=None)` dispatches to `abort`, `error`, `warn_and_fallback`, or `fallback` based on `[failure_handling]` config. `profile_missing` and `tempfile_write_failed` always abort regardless of config.

### `makepkg_wrapper.py`

Build execution. Public API: `run(pkgbuild_path, extra_flags=None, interactive=False, ...)`

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

- `is_repo_package(name)` — `pacman -Si <name>`; returns `True` if found in any sync DB. Used by `find_pkgbuild` to route auto-clone: repo packages → `pkgctl_checkout`, AUR → `aur_clone`. Also used by `manifest.py` for source classification.
- `aur_info(names)` — single batch `GET https://aur.archlinux.org/rpc/v5/info?arg[]=…` for all names; returns `{name: result_dict}`. Silent on network/JSON errors (returns `{}`).
- `aur_clone(name, dest)` — `git clone https://aur.archlinux.org/<name>.git <dest>`; raises `RuntimeError` on failure.
- `pkgctl_checkout(name, dest)` — `pkgctl repo clone --protocol=https <name>` run in `dest.parent`; fetches official Arch packaging repo. Raises `RuntimeError` on failure.
- `import_pgp_keys(pkgmeta, pkgbuild_path)` — ensures all `validpgpkeys` listed in the PKGBUILD are in the GPG keyring before `makepkg` runs. Strategy: (1) import any bundled `.asc` files from `keys/pgp/` alongside the PKGBUILD, (2) check which keys are still missing via `gpg --list-keys`, (3) fetch remaining via `gpg --recv-keys`. Import failures are logged as warnings — makepkg surfaces a clearer error if a key is still absent at verification time.

### `build_state.py`

Build state persistence. Writes `/var/lib/sysforge/build_state.toml` after each successful build, recording `pkgver`, `pkgrel`, `epoch`, `pkgbase`, and `pkgbuild_dir` per package name. Read by `sysforge update` to determine which packages to check. Follows the same atomic write-then-rename pattern as `pipeline/state.py`. Split packages (multiple `pkgname` from one `pkgbase`) each get their own entry, all pointing at the same `pkgbuild_dir`.

### `version.py`

Version comparison utilities. `vercmp(a, b)` wraps the system `vercmp` binary and returns -1/0/1 (negative/zero/positive output from vercmp is clamped). `format_version(globals_)` assembles an `[epoch:]pkgver-pkgrel` string from parsed PKGBUILD globals, omitting the epoch prefix when it is `"0"` or absent.

### `resolve.py`

Implements `sysforge resolve` — inspect profile matching for a PKGBUILD without building it. Output goes to stdout (same pattern as `manifest.py`).

Public API: `cmd_resolve(args)`. Uses `find_pkgbuild` from `config.py` for PKGBUILD lookup (same search order as `sysforge build`). Internal helpers:
- `_get_profile_chain(profile_name, profiles)` — walks the `extends` chain and returns it root-last; stops on cycle or missing parent
- `_find_winner(matched_rules)` — returns the highest-priority rule that specifies a `profile` key
- `_format_conditions(rule)` — compact single-line summary of match conditions, omitting `profile`/`priority`
- `_print_resolve(...)` — formats and prints the resolve summary: package name, PKGBUILD path, all matched rules with winner marker, profile chain (`→` separated), build mode (if set), consumes, groups; with `--show-flags` expands the full resolved flag set with sysforge-internal keys separated under a comment

### `update.py`

Implements `sysforge update` — the update manager. Algorithm:

1. Load `build_state.toml` to get the set of sysforge-managed packages and their last-built versions and PKGBUILD dirs.
2. Group by `pkgbase` to deduplicate split packages.
3. For each `pkgbase`: `git pull --rebase` the PKGBUILD dir (unless `--no-update`), parse the updated PKGBUILD, get the installed version via `pacman -Q`, compare with `vercmp`.
4. VCS packages (`-git`, `-svn`, `-hg`, `-bzr`) are flagged as `DEVEL` — their `pkgver` is only meaningful after running `pkgver()`, so static comparison is not possible. They are rebuilt only when `--devel` is passed.
5. Print a summary table: `NEEDS_REBUILD`, `UP_TO_DATE`, `DEVEL`, `NOT_INSTALLED`, `DOWNGRADE`, `PULL_FAILED`.
6. Rebuild `NEEDS_REBUILD` packages (and `DEVEL` if `--devel`) via `makepkg_wrapper.run()` with `update=False` (pull already done).

Flags: `--dry-run`, `--devel`, `--no-update`, `--state-dir`, `--profile-conf`, `--cache-report`, `--no-pkg-log`, `--persist-log`, `--log-dir`.

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

Unclassified keys (not in any `_CONF_KEY_MAP` type and not in `_SYSFORGE_KEYS`) travel via env pass and are logged as `[WARN][ENV]`.

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
| `--no-unified-log` | `pipeline` | Disable unified log for this run |
| `--no-pkg-logs` | `pipeline` | Disable per-package logs for this run |
| `--no-pkg-log` | `build` | Disable the per-package log for this build |
| `--log-dir <path>` | `pipeline`, `build` | Override log file directory |
| `--purge-log` | `pipeline` | Truncate unified log before run |
| `--persist-log` | `pipeline`, `build` | Keep log files after success |

### Tags in use

| Tag | Covers |
|---|---|
| `[PROFILE]` | Profile resolution, rule matching, extends chain |
| `[CONF]` | Temp conf generation, active consumes set |
| `[ENV]` | Env var routing; per-key shell strip with old value (INFO); skipped env-type keys when not in active_consumes (INFO); override of non-stripped shell var by profile (WARN); unclassified profile key warnings (WARN) |
| `[BUILD]` | makepkg invocation, exit codes, patched PKGBUILD lifecycle |
| `[FAILURE]` | Failure scenario dispatch |
| `[DEP]` | Soname checks |
| `[PATCH]` | PKGBUILD flag extraction, patching, artifact lifecycle; noninteractive kconfig target replacement |
| `[GROUPS]` | Package group resolution |
| `[CONFIG]` | Config file loading, state dir resolution |
| `[KERNEL]` | Kernel stage: lsmod snapshot, kconfig fragment, build, post-install |
| `[PACKAGES]` | Packages stage progress |
| `[PIPELINE]` | Stage sequencing, checkpoint events |
| `[MANIFEST]` | Manifest generation |
| `[FLAG]` | CLI toolchain overrides (--cc/--cxx/--ld), linker guard: stripped lld-specific flags when declared linker not on PATH |
| `[CACHE]` | ccache/sccache passive monitoring (per-build hit/miss delta, system probes) |

---

## Hardware Detection

Pipeline stage 3 (stub). When implemented, walks `lspci -k`, `lsmod`, `/sys/bus`, emits `hardware_profile.toml` feeding kconfig automation and `packages.toml` hardware gates. Wraps `make localmodconfig` with an lsmod snapshot for cross-machine reproducibility.

Key machine-specific caveats (Ryzen 7 5800X3D + RTX 5070):
- Explicit disable of `nouveau`
- `CONFIG_MZEN3`, `CONFIG_X86_AMD_PSTATE`

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
- **v0.1.0:** functional yay replacement — all userspace commands stable under real use: `build`, `update`, `packages`, `toolchain`, `kernel`, `reconfigure`, `resolve`, `manifest`. Target milestone for AUR publication.
- **v1.0:** system bootstrapper — stages 1–4 implemented (partition, base_install, hardware, configure).

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

**`sysforge update`** (implemented) — handles **version drift**. After `git pull --rebase` on each PKGBUILD dir, it compares the new `pkgver`/`pkgrel`/`epoch` against the installed version via `vercmp`. Packages where the PKGBUILD is newer are rebuilt with the current profile. VCS packages (`-git`, etc.) require `--devel` to rebuild since their version is only known after running `pkgver()` during the build.

**`sysforge converge`** (planned) — handles **profile/flag drift**. Same package version but different compiler configuration — e.g. profile changed, new flag added, or build mode switched. Compares the profile hash at last build (from `build_state.toml`) against the currently resolved profile. Not yet implemented.

`build_state.toml` is the shared source of truth for both commands. Written by `makepkg_wrapper.run()` after each successful build.

DAG stages are categorised as **bootstrap-only** (partition, base_install, hardware, configure) or **repeatable** (reconfigure, toolchain, packages, kernel). Only repeatable stages participate in re-converge runs.

---

## Known Gaps

Implemented behaviour that is incomplete or has known limitations. These are not deferred features — they are holes in currently active code.

**`sysforge install` — not yet implemented.** The design question of how to handle repo packages is unresolved: pure pacman passthrough (`pacman -S`) vs. a unified dispatch that routes repo packages to pacman and AUR packages to a profiled build. Until resolved, repo packages require `pacman -S` directly and AUR packages use `sysforge build`.

**`sysforge update` is scoped to sysforge-managed packages only.** `build_state.toml` records only packages that sysforge built. Packages installed via pacman from repos are not tracked; `pacman -Syu` remains the update path for those. A future `sysforge update --all` could use `pacman -Qm` (foreign packages not in any sync DB) to discover AUR packages installed outside sysforge, but this is not yet implemented.

**`packages.toml [build] pkgbuild_dir` and `flag_profiles [paths] pkgbuild_dir` are separate.** The pipeline's `_resolve_pkgbuild` prefers `[build] pkgbuild_dir`; falls back to `[paths] pkgbuild_dir`. They can point to different directories or the same one — there's no enforcement that they match.

**`[env_precedence]` config table — design cancelled.** The original design proposed a priority stack (wrapper profile = 100, makepkg.conf = 80, shell passthrough = 20, PKGBUILD export = 10) and an `[env_precedence]` TOML table to configure it. This design is superseded. The current model is simpler and more predictable: build tool vars (`CC`, `CFLAGS`, `LDFLAGS`, etc.) are stripped from the inherited shell env in `invoke_makepkg` before makepkg runs — the temp conf is the sole authority for all makepkg-managed keys. Shell env bleed-through is not a configurable priority; it is prevented entirely. SysForge bootstrap vars (`SYSFORGE_STATE_DIR`, `SYSFORGE_CONFIG_DIR`) are exempt — they are SysForge's own interface, not build tool vars, and are not stripped. The `[env_precedence]` table will not be implemented.

**`[FLAG]` tag — partial coverage.** Emitted for: CLI toolchain overrides (`--cc`, `--cxx`, `--ld`), linker token replacement and injection, linker guard stripping, conflict group firing (logs group name, evicted tokens, inserted token), and prefix-match token replacement during `merge_extends`. Not emitted for: `apply_patch_pkgbuild` token changes (those use `[PATCH]`).

**`[CACHE]` ThinLTO probe is per-build, not per-run.** `emit_system_probes()` (ld.so mtime, pacman cache size) runs once at the start of each pipeline or build invocation. ThinLTO cache size is probed inside `_run_build()` because it requires the resolved profile's LDFLAGS — those are per-package, not available at run start. So ThinLTO appears in `[CACHE]` lines once per package that configures `--thinlto-cache-dir=` in its LDFLAGS.

---

## V2 Roadmap

V2 goal: advanced AUR helper features beyond the v0.1.0 yay-replacement baseline.

V2 candidates:
- **`sysforge install`** — unified install command routing repo packages to `pacman -S` and AUR packages to a profiled build. Design question (dispatch model) must be resolved first.
- **PKGBUILD review** — present diffs to the user before building an AUR package, analogous to `yay`'s PKGBUILD diff prompt
- **Recursive AUR dep resolution** — walk the full AUR dependency tree; currently AUR deps on other AUR packages require manual ordering
- **`sysforge update --all` via `pacman -Qm`** — discover and update foreign packages installed outside sysforge, not just those in `build_state.toml`
- **AUR package name cache** — fetch `packages.gz` (~80k names) into `~/.cache/sysforge/` for full tab-completion of AUR package names; refresh via `sysforge sync` or a systemd timer

### V1.5: Rule priority auto-calculation

Currently `priority` is manually assigned. A future improvement is to auto-calculate a baseline specificity score from the rule's conditions (mirrors CSS specificity: more AND'd conditions = higher weight), with a manual `priority` override for ties. Deferred until enough real rules exist to validate whether auto-priority causes ordering problems in practice.
