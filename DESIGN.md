# SysForge Design Document

SysForge is a personal system automation framework that produces a reproducible, performance-tuned Arch Linux install from declarative TOML configs. It is an installer and bootstrapper — not a package manager. Pacman owns the ongoing package lifecycle; SysForge gets you to a fully configured, optimized system from a vanilla Arch ISO.

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
17. [V2 Roadmap](#v2-roadmap)

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
│   ├── log.py                         # structured logging (all output to stderr)
│   ├── manifest.py                    # packages.toml generator
│   └── primitives/
│       ├── config.py                  # TOML config loading, path constants, system conf parsing
│       ├── profile.py                 # profile resolution, rule matching, consumes
│       ├── pkgbuild_meta.py           # static PKGBUILD parser (read-only)
│       ├── pkgbuild_patcher.py        # PKGBUILD mutation + flag extraction
│       ├── makepkg_wrapper.py         # build execution: emit conf, invoke makepkg
│       ├── dep_analysis.py            # pre-build soname dependency checks
│       └── failure.py                 # failure scenario handling (shared)
│   └── pipeline/
│       ├── __init__.py
│       ├── runner.py                  # stage sequencing, checkpoint/resume
│       ├── state.py                   # pipeline_state.toml read/write
│       └── stages/
│           ├── __init__.py            # STAGES ordered list
│           ├── base.py                # Stage base class, RunOptions dataclass
│           ├── packages.py            # stage 5: real implementation
│           ├── kernel.py              # stage 6: stub
│           ├── configure.py           # stage 7: stub
│           ├── partition.py           # stage 1: stub
│           ├── base_install.py        # stage 2: stub
│           ├── hardware.py            # stage 3: stub
│           └── toolchain.py           # stage 4: stub
├── configs/
│   └── packages.toml                  # starter package manifest
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
│   │   │   └── append_conflict_groups.toml
│   │   └── user/.config/sysforge/
│   │       └── flag_profiles.toml
│   ├── test_append_merge.py
│   ├── test_consumes.py
│   ├── test_dep_analysis.py
│   ├── test_env_pass.py
│   ├── test_manifest.py
│   ├── test_parser.py
│   ├── test_patcher.py
│   ├── test_pipeline.py
│   ├── test_pipeline_runner.py
│   ├── test_pipeline_state.py
│   ├── test_stage_packages.py
│   ├── test_system_conf.py
│   └── test_wrapper.py
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
~/.config/sysforge/
    flag_profiles.toml               # user overrides
    append_conflict_groups.toml      # user conflict group overrides (optional)
/usr/bin/sysforge
/var/lib/sysforge/
    pipeline_state.toml              # pipeline checkpoint state (created at runtime)
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
pkgbuild_dir = "~/builds"   # pre-cloned AUR PKGBUILDs live here

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

`sysforge manifest` generates a `packages.toml` stub from a list of package names, classifying each as `repo` or `aur` by querying pacman sync DBs. AUR RPC lookup is currently stubbed — packages not found in repos are excluded with a warning.

```bash
sysforge manifest htop neovim mold > packages.toml
sysforge manifest --file pkglist.txt >> packages.toml
```

### `-march=native` strategy

SysForge uses `-march=native` rather than hardcoding CPU-specific flags. Optimization becomes a compile-time concern — it works across CPU families without separate logic. If a package is incompatible with native tuning, a higher-priority rule pointing to the `bare` profile overrides `-march` for that package only.

---

## Config Layer

### Config file hierarchy

- System default: `/etc/sysforge/flag_profiles.toml`
- User override: `~/.config/sysforge/flag_profiles.toml`

By default the user file **fully replaces** the system file. To layer on top instead, add `extends_system = true` at the top of the user file — user values take priority on all conflicts. User rule priorities are bumped by 100 on merge (range 100–199) to always outrank system rules (range 0–99).

### State directory

Pipeline state is written to `/var/lib/sysforge/` by default. Override via `SYSFORGE_STATE_DIR` environment variable or `--state-dir` CLI flag. CLI takes priority over the env var; both are logged when present.

### Hardware overlays

The hardware detection stage emits `hardware_profile.toml` which feeds kconfig automation and gates hardware-specific packages in `packages.toml`. Key machine-specific caveats (Ryzen 7 5800X3D + RTX 5070):

- Explicit disable of `nouveau`
- CPU-specific flags: `CONFIG_MZEN3`, `CONFIG_X86_AMD_PSTATE`

---

## Pipeline Layer

Python DAG orchestrator with checkpoint/resume. Stages run in order:

1. **partition** — stub
2. **base_install** — stub
3. **hardware** — stub
4. **toolchain** — stub
5. **packages** — fully implemented
6. **kernel** — stub
7. **configure** — stub

Stubs raise `NotImplementedError` with `--start-from` guidance. Stages 5–7 are usable on a live system via `--start-from packages`.

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

### Packages stage (stage 5)

Walks `packages.toml` in order:
- `source = "repo"` → `sudo pacman -S --needed --noconfirm`
- `source = "aur"` / `"git"` → `makepkg_wrapper.run()` against the pre-cloned PKGBUILD
- Hardware-gated packages skipped if `hardware_profile.toml` is absent or key is missing
- Non-fatal per-package failures: build continues, failures recorded in state
- Summary at end: `Total | Built | Failed | Skipped`

### LLVM bootstrap

Three-stage bootstrap to produce a fully PGO-optimized LLVM toolchain:

1. Build with system LLVM
2. Build instrumented PGO binary
3. Build final optimized LLVM — used for all subsequent package builds

---

## Primitives Layer

All modules independently testable. 290 pytest tests (`pytest` from repo root).

### `log.py`

Structured logging module. All output goes to stderr.

```
[SYSFORGE][LEVEL][TAG] message
```

Three levels: `error` (always shown), `warn` (`-v`), `info` (`-vv`). Set once at CLI entry with `log.set_verbosity(args.verbose)`.

### `config.py`

TOML config loading and path resolution. Public API:
- `load_config(config_paths=None)` — loads `flag_profiles.toml`, merges user onto system via `extends_system`, validates rule priorities
- `load_conflict_groups(paths=None)` — loads `append_conflict_groups.toml`
- `load_consumes_inference(paths=None)` — loads `consumes_inference.toml`
- `parse_system_makepkg_conf(path=None)` — parses `/etc/makepkg.conf` into `{key: raw_value_string}` for use in temp conf generation

### `profile.py`

Profile resolution and rule matching. Public API:
- `merge_extends` — resolves the full `extends` chain into a flat profile dict, applying `[profiles.x.append]` token-level merges with conflict groups
- `match_rules` — evaluates all match fields against a parsed PKGBUILD
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

All PKGBUILD mutation. Active when `build_mode = "patch_pkgbuild"` on the resolved profile.

**Flag extraction** (`extract_pkgbuild_profile`) scans all function bodies and extracts bare, `export`, and `+=` assignments to known flag variables. Strips self-references (`$CFLAGS` in CFLAGS), skips complex bash expressions (e.g. `${CFLAGS/-g /-g1 }`), expands packed `-Wl,a,b,c` tokens into individual sub-tokens. Returns a synthetic profile dict used as the implicit chain root in `merge_extends` — forming the chain: `pkgbuild_extracted → bare → standard → optimized`.

**Conditional block handling** (`_extract_conditional_blocks`) finds `if...fi` blocks containing extractable key assignments using depth-tracked scanning. Entire blocks are removed from the patched PKGBUILD, never partially.

**Patching** (`apply_patch_pkgbuild`) writes `PKGBUILD.sysforge` with all managed flag assignments and conditional blocks removed. The original is untouched. Artifacts persist on build failure for diagnosis; `cleanup_patch_artifacts` removes them on success.

### `dep_analysis.py`

Pre-build soname dependency checks. Runs before `_run_build` in `makepkg_wrapper.run()`.

`check_soname_deps` filters `.so` and `.so=N` entries from `depends`, parses ldconfig -p output, and checks presence and major version. `libcap.so=2` means libcap.so.2 must be present in ldconfig's cache.

Version constraint checking (pacman -Q / vercmp) was intentionally omitted — makepkg already does this and any pre-check adds false-positive risk without meaningful value.

Both functions accept injectable callables for testing. Non-fatal by default; configurable via `abi_mismatch` in `[failure_handling]`.

### `failure.py`

Cross-cutting failure scenario handler. Imported by `makepkg_wrapper` and `dep_analysis` to avoid circular imports.

`handle_failure(scenario, message, config, fallback=None)` dispatches to `abort`, `error`, `warn_and_fallback`, or `fallback` based on `[failure_handling]` config. `profile_missing` and `tempfile_write_failed` always abort regardless of config.

### `makepkg_wrapper.py`

Build execution. Public API: `run(pkgbuild_path, extra_flags=None)`

High-level flow:
1. Parse PKGBUILD via `pkgbuild_meta.py`
2. Match rules, resolve profile (injecting `pkgbuild_extracted` root if patching)
3. Resolve consumes and groups
4. Run pre-build soname dep analysis
5. If `patch_pkgbuild` mode: extract PKGBUILD flags, write extracted profile, apply patch
6. Emit complete temp `makepkg.conf` (merged system conf + profile overrides)
7. Resolve env vars for subprocess injection
8. Invoke `makepkg` with temp conf and injected env

**System conf merge:** `emit_makepkg_conf` reads `/etc/makepkg.conf` as a baseline and writes a complete self-contained temp conf — system keys pass through verbatim, profile keys override their counterparts inline, new profile keys are appended. No `. /etc/makepkg.conf` sourcing at runtime.

**Makepkg flag passthrough:** `extra_flags` from the CLI (`-m "-sfci"`) are appended after profile `makepkg_flags`. Combined short flags are expanded: `-sfci` → `[-s, -f, -c, -i]`.

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

[profiles.cosmic]
extends = "optimized"
build_mode = "patch_pkgbuild"
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

Profile keys are routed to one of two delivery channels:

- **Conf file** (`makepkg`, `rust`, `cmake`, `meson` types) — written into the temp `makepkg.conf`. Only types in `active_consumes` are written.
- **Subprocess env** (`env` type, or any unclassified key) — injected via `subprocess.run(env=...)`. Used for `RUSTC_WRAPPER`, `CCACHE_DIR`, `SCCACHE_DIR`, etc.

Unclassified keys travel via env pass and are logged as `[WARN][ENV]`.

---

## Makepkg Wrapper

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

Verbosity controlled by `-v`/`-vv` on the CLI:
- Default: `[ERROR]` only
- `-v`: adds `[WARN]`
- `-vv`: adds `[INFO]`

Set once at CLI entry via `log.set_verbosity(args.verbose)`. Tests run at verbosity 2 (all messages visible).

**Tags in use:**

| Tag | Covers |
|---|---|
| `[PROFILE]` | Profile resolution, rule matching, extends chain |
| `[CONF]` | Temp conf generation, active consumes set |
| `[ENV]` | Env var routing, unclassified key warnings |
| `[BUILD]` | makepkg invocation, exit codes, patched PKGBUILD lifecycle |
| `[FAILURE]` | Failure scenario dispatch |
| `[DEP]` | Soname checks |
| `[PATCH]` | PKGBUILD flag extraction, patching, artifact lifecycle |
| `[GROUPS]` | Package group resolution |
| `[CONFIG]` | Config file loading, state dir resolution |
| `[PACKAGES]` | Packages stage progress |
| `[PIPELINE]` | Stage sequencing, checkpoint events |
| `[MANIFEST]` | Manifest generation |

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
- **AUR:** publish once stages 5–7 are stable under real use

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

`sysforge converge` compares current installed state in `/var/lib/sysforge/build_state.toml` against the manifest and flag profiles, then rebuilds any package whose profile, flags, or version have drifted. `--dry-run` shows what would be rebuilt.

DAG stages are categorised as **bootstrap-only** (partition, base_install, toolchain) or **repeatable** (packages, configure). Only repeatable stages participate in re-converge runs.

Not yet implemented.

---

## V2 Roadmap

V2 goal: full `yay` replacement — an AUR helper with compiler optimization as a first-class concern.

V2 absorbs:
- **AUR fetch** — clone and update PKGBUILDs from AUR directly (AUR RPC lookup already stubbed in `manifest.py`)
- **PKGBUILD review** — present diffs to the user before build
- **Recursive AUR dep resolution** — walk the full AUR dependency tree
- **Mixed pacman/AUR tree management** — unified view of repo and AUR packages
- **Upgrade management** — `sysforge upgrade` checks AUR for new versions and rebuilds with active profiles

### V1.5: Rule priority auto-calculation

Currently `priority` is manually assigned. A future improvement is to auto-calculate a baseline specificity score from the rule's conditions (mirrors CSS specificity: more AND'd conditions = higher weight), with a manual `priority` override for ties. Deferred until enough real rules exist to validate whether auto-priority causes ordering problems in practice.
