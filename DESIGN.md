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
18. [Open Questions](#open-questions)

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
│  thin Bash wrappers                     │
└─────────────────────────────────────────┘
```

---

## Directory Structure

### Development (local repo)

```
sysforge/
├── sysforge/
│   ├── __init__.py
│   ├── cli.py
│   └── primitives/
│       ├── pkgbuild_meta.py
│       └── makepkg_wrapper.py
├── configs/                    # planned — not yet in repo
│   ├── flag_profiles.toml
│   └── hardware/
│       └── zen3_rtx5070.toml
├── tests/
│   ├── data/
│   │   ├── PKGBUILDs/
│   │   │   ├── htop.PKGBUILD
│   │   │   ├── lib32-llvm.PKGBUILD
│   │   │   ├── llvm.PKGBUILD
│   │   │   ├── cosmic.PKGBUILD
│   │   │   └── vulkan-headers-git.PKGBUILD
│   │   ├── test_flag_profiles.toml
│   │   ├── etc/sysforge/
│   │   │   └── flag_profiles.toml
│   │   └── user/.config/sysforge/
│   │       └── flag_profiles.toml
│   ├── test_parser.py
│   ├── test_wrapper.py
│   └── test_pipeline.py
├── PKGBUILD
└── pyproject.toml
```

### Installed

```
/etc/sysforge/
    flag_profiles.toml
    consumes_inference.toml
    append_conflict_groups.toml
~/.config/sysforge/
    flag_profiles.toml        # user overrides
    append_conflict_groups.toml  # user conflict group overrides (optional)
/usr/bin/sysforge
```

---

## Package Manifest

`packages.toml` is the master list of packages SysForge installs. It is **separate from `flag_profiles.toml`** — package sourcing and build flag tuning are orthogonal concerns and must not be conflated.

Each entry declares:
- `source` — one of `repo` (pacman), `aur`, or `git` (direct PKGBUILD)
- `pkgbuild_patch` *(optional bool)* — if `true`, the PKGBUILD patching library runs on this package before build
- `requires_hardware` *(optional)* — hardware capability key that must be present in `hardware_profile.toml` for this package to be included; absent or false drops the package silently at pipeline time
- `cache` *(optional bool)* — overrides the profile-level cache default for this package. Set to `false` to unconditionally disable ccache/sccache for this build (required for all PGO stages)

```toml
[[package]]
name = "nvidia-open-dkms"
source = "repo"
requires_hardware = "nvidia_gpu"

[[package]]
name = "mesa-git"
source = "aur"
pkgbuild_patch = true

[[package]]
name = "linux-zen"
source = "aur"

[[package]]
name = "llvm-pgo-stage1"
source = "aur"
cache = false   # instrumented objects must never be cached
```

`requires_hardware` keys are matched against those emitted by the hardware detection stage into `hardware_profile.toml`. Example:

```toml
# hardware/zen3_rtx5070.toml
nvidia_gpu = true
amd_cpu    = true
```

`flag_profiles.toml` has no knowledge of package sources or hardware gates — those are exclusively a `packages.toml` concern.

### `-march=native` Strategy

SysForge uses `-march=native` rather than hardcoding CPU-specific flags like `znver3`. Optimization becomes a compile-time concern rather than a manifest or profile concern — it works across CPU families without separate logic and justifies a single `packages.toml` rather than per-CPU manifests. If a package is incompatible with native tuning (e.g. a portable binary), it matches the `bare` profile via a higher-priority rule, overriding `-march` for that package only.

---

## Config Layer

### Config File Hierarchy

- System default: `/etc/sysforge/flag_profiles.toml`
- User override: `~/.config/sysforge/flag_profiles.toml`

By default the user file **fully replaces** the system file. To layer the user file on top of the system file instead, add `extends_system = true` at the top of the user file — user values take priority on all conflicts.

### Hardware Overlays

Hardware-specific config lives in `configs/hardware/`. The hardware detection stage emits a `hardware_profile.toml` that feeds into kconfig automation. Key hardware caveats for the primary machine (Ryzen 7 5800X3D + RTX 5070):

- Explicit disable of `nouveau` for the RTX 5070
- CPU-specific flags: `CONFIG_MZEN3`, `CONFIG_X86_AMD_PSTATE`

---

## Pipeline Layer

Python DAG orchestrator with checkpoint/resume. Stages run in order:

1. **partition**
2. **base\_install**
3. **hardware\_detection** *(walks `lspci -k`, `lsmod`, `/sys/bus` → emits `hardware_profile.toml`)*
4. **toolchain**
5. **packages**
6. **kernel**
7. **configure**

### LLVM Bootstrap

Three-stage bootstrap to produce a fully PGO-optimized LLVM toolchain:

1. Build with system LLVM
2. Build instrumented PGO binary
3. Build final optimized LLVM — used for all subsequent package builds

---

## Primitives Layer

Independently testable before the pipeline exists. Start here.

### `pkgbuild_meta.py`

Static parser for PKGBUILD metadata. Does **not** source or execute the PKGBUILD.

**Reliably parseable fields:** `pkgname`, `pkgver`, `pkgrel`, `epoch`, `groups`, `depends`, `makedepends`, `provides`, and all standard scalar/array globals. Function bodies (`prepare`, `build`, `package`, `package_*`, and helper functions) are extracted and stored under `functions`.

**Not statically parseable:** computed values (`pkgver=$(...)`, conditional metadata, `depends+=()` inside functions). The wrapper falls back to `default_profile` when parsing fails.

**Implementation notes:**
- Comment stripping respects quoting — `#` inside single or double quotes is not treated as a comment
- Function extraction uses brace-depth tracking; `${}` expansions (including nested) are skipped to prevent their braces from affecting depth counts
- Functions are matched only at line boundaries to prevent mid-line matches (e.g. `package_lib32-llvm` will not spuriously match as `llvm`)
- Function names support hyphens (`[\w][\w-]*`) to handle split package functions like `package_lib32-llvm`
- Array parsing respects quoted strings with internal spaces
- Scalar regex handles single-quoted, double-quoted, and bare values as distinct branches
- Known limitation: heredocs containing bare `{` or `}` will confuse the depth tracker; rare in PKGBUILDs, deferred

Returns:
```python
{
    "globals": { "pkgname": "htop", "makedepends": ["git", "lm_sensors"], ... },
    "functions": { "build": "  cd ...\n  make", "prepare": "...", ... }
}
```

### `makepkg_wrapper.py`

See [Makepkg Wrapper](#makepkg-wrapper).

---

## Flag Profile System

### Profile Structure

Profiles are defined in `flag_profiles.toml`. Each profile is a named set of compiler flags and env vars.

```toml
[profiles.bare]
# Fallback profile, no flags

[profiles.standard]
extends = "bare"
CFLAGS = "-march=native -O2 -pipe"
CXXFLAGS = "$CFLAGS"
LDFLAGS = "-Wl,-O1,--sort-common,--as-needed,-z,relro,-z,now"

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
build_mode = "patch_linker"
```

### `extends` Semantics

Full inheritance with explicit override. The child starts as a complete copy of the parent's resolved values, then applies its own keys on top.

**Direct keys override** — a key set directly on a child profile fully replaces the parent's value. The child must restate the complete value.

**`[profiles.x.append]` subsection merges** — keys in the `append` subsection are merged into the parent's value using a token-level list merge rather than string concatenation. This handles both additive flags and conflicting ones cleanly. *(Not yet implemented — currently child keys always fully override parent.)*

#### Append Merge Algorithm *(planned)*

1. Tokenize parent and child values by whitespace
2. For each child token, resolve in this order:
   - **Explicit conflict group** — if the token belongs to a defined conflict group, remove all other group members from the accumulated token list, then insert the child token
   - **Prefix match** — extract the token's prefix (everything up to and including `=`, or up to a trailing digit run for flags like `-O2`); if a token with the same prefix already exists, replace it
   - **Append** — no match, add to end
3. Reconstruct as space-joined string

**Worked example:**
```
parent CFLAGS:        "-march=native -O2 -pipe -fstack-protector"
append CFLAGS:        "-O3 -fno-stack-protector --icf=all"

-O3                   prefix "-O" matches "-O2"            → replace
-fno-stack-protector  conflict group "stack"               → removes "-fstack-protector", inserts
--icf=all             no match                             → append

result: "-march=native -O3 -pipe -fno-stack-protector --icf=all"
```

#### Conflict Groups *(planned)*

Conflict groups define sets of mutually exclusive flags that don't share a detectable prefix. Defined in system config:

```toml
# /etc/sysforge/append_conflict_groups.toml
[conflict_groups]
pic   = ["-fPIC", "-fPIE", "-fpic", "-fpie", "-fno-pic", "-fno-pie"]
lto   = ["-flto", "-flto=thin", "-flto=full", "-fno-lto"]
stack = ["-fstack-protector", "-fstack-protector-strong", "-fno-stack-protector"]
```

User-defined groups live in `~/.config/sysforge/append_conflict_groups.toml` and follow the same `extends_system` merge model as profiles. Explicit conflict groups take precedence over prefix matching.

### Rules

Rules match packages to profiles based on package properties. All conditions within a rule are AND'd. Multiple rules are OR'd (each evaluated independently).

```toml
[[rules]]
pkgnames = ["htop"]
profile = "optimized"
priority = 0

[[rules]]
groups = ["cosmic-*"]
profile = "cosmic"
priority = 10
append_groups = ["cosmic-patched"]

[[rules]]
pkgnames = ["llvm", "clang", "lld"]
profile = "pgo_llvm_toolchain"
priority = 20
append_groups = ["pgo"]
```

### Rule Match Field Semantics

All match fields are optional. Omitting a field means it is not evaluated (passes unconditionally).

| Field | Semantics |
|---|---|
| `pkgnames` | ANY match + glob — rule matches if any pattern matches any package name |
| `not_pkgnames` | ALL absent + glob — rule skipped if any pattern matches any package name |
| `groups` | ALL match + glob — rule matches only if every pattern matches at least one group |
| `not_groups` | ALL absent, exact — rule skipped if any rule item appears in package groups |
| `depends_any` | ANY exact — rule matches if any rule item appears in depends |
| `depends_all` | ALL exact — rule matches only if every rule item appears in depends |
| `not_depends` | ALL absent, exact — rule skipped if any rule item appears in depends |
| `makedepends_any` | ANY exact — rule matches if any rule item appears in makedepends |
| `makedepends_all` | ALL exact — rule matches only if every rule item appears in makedepends |
| `not_makedepends` | ALL absent, exact — rule skipped if any rule item appears in makedepends |

`pkgnames` and `not_pkgnames` support fnmatch glob patterns (e.g. `lib32-*`). `groups` supports globs for matching but `not_groups` is exact. `depends_*` and `makedepends_*` are always exact.

The old singular `pkgname`, `depends`, and `makedepends` keys are not supported — use the typed variants above.

### Multi-Rule Merge and Priority

When multiple rules match a package, the **highest-priority rule wins outright** — its profile is resolved and used in full. The inheritance system (`extends`) is how profiles compose with each other; rules are not an additional composition layer.

- Highest priority rule → its profile is resolved via `extends` chain
- Equal priority → first occurrence (file order) wins
- All non-winning rules are logged with their priorities and discarded

The `priority` field is a required integer (range `0–99`). Higher = wins. User rules are bumped by `100` on merge, giving an effective range of `100–199` for user rules and `0–99` for system rules.

**`append_groups` is additive across all matched rules**, regardless of priority. Every matched rule's `append_groups` is collected and appended to the package's final group list, deduplicated in match order. This is asymmetric by design — flag resolution is winner-takes-all (one profile wins), groups are accumulative across all matches.

Note: `append_groups` on rules is unrelated to `[profiles.x.append]` — they are distinct mechanisms.

### `consumes` Field

Declares which conf files a package build requires (`makepkg`, `rust`, `meson`, `cmake`, `env`, etc.).

- Lives on profiles *(placement to revisit after build/test)*
- **Default:** auto-inferred from `makedepends` via a static inference map in system config
- **Override:** explicit `consumes` on a profile replaces inferred value

```toml
# /etc/sysforge/consumes_inference.toml
[consumes_inference]
cargo  = ["makepkg", "rust", "env"]
meson  = ["makepkg", "meson", "env"]
cmake  = ["makepkg", "cmake", "env"]
ninja  = ["makepkg", "env"]
```

The wrapper will generate only the conf files in the resolved `consumes` set, logging the active set under `[CONF]`. Missing conf files will cause a build failure — detailed logs are sufficient to diagnose the cause. *(Consumes filtering not yet implemented — currently a single makepkg conf is generated from all non-internal profile keys.)*

---

## Makepkg Wrapper

### High-Level Flow

1. Parse PKGBUILD statically via `pkgbuild_meta.py`
2. Evaluate all rules against package properties → collect matching profiles
3. Resolve `extends` chains on each matched profile into fully flat value sets
4. Merge across matched rules using priorities → one resolved value per key; log all discarded candidates
5. Log full resolved values with winning sources and discarded candidates
6. Generate temp conf files *(planned: only those in `consumes`; currently writes all non-internal keys)*
7. Run `makepkg` pointed at temp conf files via `MAKEPKG_CONF` *(planned: env vars from `[profiles.*.env]` passed as explicit env on invocation; currently not separated)*

Conf files only receive native keys (CFLAGS, LDFLAGS, RUSTFLAGS, etc.). Env vars from `[profiles.*.env]` will travel separately and never be written into conf files. *(Not yet implemented.)*

### Batch Builds

The `batch` profile key (`batch = true`) switches the wrapper into unattended mode:

- **Batch mode** — on build failure, abort immediately with a `[FAILURE]` log entry. No prompt.
- **Interactive mode** (default) — on build failure, prompt the user to manually correct the PKGBUILD and retry, or type `abort` to stop.

`batch = true` is the only mechanism needed to express this — it does not interact with `[failure_handling]`. The `failure_handling` key is not valid on individual profiles.

```toml
[profiles.batch]
extends = "standard"
batch = true
makepkg_flags = ["--noconfirm", "--syncdeps", "--rmdeps", "--install", "--noprogressbar", "--log", "--cleanbuild"]
clean_builddir = true
```

Build summary (packages built / failed / remaining) is deferred to V2 alongside the AUR wrapper. Per-failure logging under `[FAILURE]` is sufficient for v0.1.

### Pre-Build Dependency Analysis *(planned)*

Before invoking makepkg, the wrapper will run two checks against the resolved `depends` and `makedepends`:

**Soname inspection** — resolves the `.so` versions each declared dependency currently exposes on the system (via `ldconfig -p` and `/usr/lib`) and compares against what the package expects to link against. Any version mismatch is flagged under `[DEP]` before the build starts rather than surfacing as a cryptic mid-build linker error.

**Version constraint check** — parses version constraints from `depends` (e.g. `foo>=1.2`) and diffs against `pacman -Q` output. Unsatisfied or borderline constraints are flagged under `[DEP]` before makepkg attempts to resolve them itself.

Both checks are non-fatal by default and configurable via `abi_mismatch` and `dep_unsatisfied` in `[failure_handling]`. Results feed into the failure pattern library for human-readable diagnosis.

### Failure Handling

Each scenario has a configurable behaviour in `[failure_handling]`:

```toml
[defaults]
profile = "standard"        # fallback profile when no rule matches

[failure_handling]
pkgbuild_unparseable  = "warn_and_fallback"  # fallback to bare
no_rule_matched       = "fallback"           # use default_profile silently
profile_missing       = "abort"             # config bug, always hard stop
profile_cycle         = "abort"             # extends loop, always hard stop
tempfile_write_failed = "abort"             # never silently use system conf
env_conflict          = "warn_and_fallback"  # wrapper value wins, logged
abi_mismatch          = "warn_and_fallback"  # soname version mismatch detected pre-build
dep_unsatisfied       = "warn_and_fallback"  # version constraint not met pre-build
```

**Behaviours:** `abort`, `warn_and_fallback`, `fallback`, `error`

New scenarios are added by registering a handler function and adding its key to `[failure_handling]`. `profile_missing` and `tempfile_write_failed` always abort regardless of config.

### Env Var Precedence *(planned)*

Env precedence will be a first-class config table. Changing the hierarchy means changing these numbers — not tracing which file happened to win.

```toml
[env_precedence]
wrapper_profile   = 100  # TOML [profiles.*.env] overrides
makepkg_conf      = 80   # CFLAGS/LDFLAGS/RUSTFLAGS etc.
shell_passthrough = 20   # inherited calling env (allowlisted vars)
pkgbuild_export   = 10   # detected exports in PKGBUILD (best-effort)
```

The full precedence table will be logged at startup under `[ENV]`. *(Not yet implemented — `[env_precedence]` is not currently read.)*

When implemented, the wrapper will construct a clean environment dict rather than inheriting the calling shell's env wholesale — an explicit allowlist of vars (PATH, HOME, USER, etc.) will be passed through; everything else blocked unless the profile explicitly includes it. *(Not yet implemented — currently inherits full shell env.)*

---

## Logging

Every log line is prefixed with exactly one structured category tag. Every line also includes the current package name as a second field, e.g. `[PROFILE][mesa-git]`. This means the log can be filtered by either dimension independently — grep for a tag to see all activity of that type, or grep for a package name to see its full build story.

Tags marked *(planned)* are defined but not yet emitted in v0.1.

| Tag | Covers |
| --- | --- |
| `[PROFILE]` | Profile resolution, rule matching, extends chain |
| `[FLAG]` | makepkg.conf flag resolution and conflicts *(planned)* |
| `[ENV]` | Env var resolution, conflicts, overrides, precedence table *(planned)* |
| `[BUILD]` | makepkg invocation and exit codes |
| `[FAILURE]` | Any failure scenario firing |
| `[CONF]` | Temp makepkg conf file generation and cleanup, active consumes set |
| `[DEP]` | Pre-build dependency analysis: soname mismatches and version constraint checks *(planned)* |
| `[CACHE]` | Cache state snapshots: ccache/sccache activity, passive monitoring of external caches *(planned)* |
| `[CONFIG]` | Config file loading, hierarchy resolution, extends_system merge |
| `[GROUPS]` | Package group resolution: existing groups, defaults.append_groups, rule append_groups |

Grepping a single tag gives the complete story for that concern across the full log.

**Example pre-build output:**

```
[ENV]    [sysforge]    precedence: wrapper_profile=100 makepkg_conf=80 shell_passthrough=20 pkgbuild_export=10
[DEP]    [mesa-git]    soname ok: libLLVM-18.so → found
[DEP]    [mesa-git]    version ok: llvm-libs>=18.0 → 18.1.8-1
[PROFILE][mesa-git]    matched rule: makedepends_all=cmake,ninja priority=10 → optimized
[PROFILE][mesa-git]    discarded: pkgnames=mesa-git priority=5 → standard (outprioritized)
[FLAG]   [mesa-git]    CFLAGS="-march=native -O3 -pipe -fno-plt" (source: optimized)
[CONF]   [mesa-git]    active conf files: makepkg, env
[BUILD]  [mesa-git]    invoking makepkg -si via MAKEPKG_CONF=/tmp/sysforge_abc123.conf
```

### Dual Log Scheme

SysForge maintains two logs simultaneously by default:

- **Unified log** — all packages, all tags, single file for the full run
- **Per-package log** — one file per package, split from the unified log on the package tag

Both are enabled by default. Either can be disabled at invocation:

```
sysforge --no-unified-log  # per-package logs only
sysforge --no-pkg-logs     # unified log only
sysforge --log-dir <path>  # override output directory for per-package logs
```

---

## Cache Management

### ccache and sccache

SysForge configures build caches via a `[cache]` table in `flag_profiles.toml`:

```toml
[cache]
ccache                       = "auto"           # auto | enabled | disabled
sccache                      = "auto"           # auto | enabled | disabled
ccache_dir                   = "~/.cache/ccache"
sccache_dir                  = "~/.cache/sccache"
ccache_max_size              = "10G"
sccache_max_size             = "10G"
invalidate_on_toolchain_change = "warn"         # warn | purge | ignore
```

`auto` — use if installed, skip silently if not. `enabled` — require it; abort if missing (consistent with `[failure_handling]` philosophy).

**Compiler invocation** uses explicit absolute paths rather than ccache's symlink shim, which interacts badly with makepkg prepending `$srcdir` to `PATH`:

```
CC="ccache /usr/bin/clang"
CXX="ccache /usr/bin/clang++"
```

sccache wraps the Rust compiler via `RUSTC_WRAPPER=sccache` in the env block. The two caches own distinct domains and run simultaneously without conflict: **ccache owns C/C++, sccache owns Rust**.

`CARGO_INCREMENTAL=0` is set globally for all Rust package builds. Cargo's incremental cache uses fingerprinting that does not reliably invalidate on env var changes (e.g. `RUSTFLAGS`), making it unsafe in a flag-managed build environment.

### Per-Package Cache Override

The `cache = false` field in `packages.toml` disables both ccache and sccache for a specific package, overriding the profile-level default. This is required for all PGO build stages:

- **Instrumented stage** — cached instrumented objects must never be reused in a non-instrumented build; the failure would be silent and catastrophic
- **Optimized stage** — profdata is not reliably covered by sccache's cache key, so the full PGO sequence has cache disabled by default

### Toolchain Fingerprint Tracking

SysForge tracks a toolchain fingerprint (hash of compiler binary path + version string) in its state dir. On mismatch — e.g. after an LLVM version bump — the `invalidate_on_toolchain_change` behavior fires:

- `warn` — log a `[CACHE]` warning; leave caches intact
- `purge` — run `ccache -C` and wipe sccache dir automatically
- `ignore` — do nothing

`purge` is destructive and opt-in. Default is `warn` to surface the mismatch without data loss.

### Passive Cache Monitoring

SysForge tracks but does not manage several caches that can affect build behaviour. All observations are logged under `[CACHE]`. No automated intervention is performed; management may be added later if usage reveals a need.

| Cache | What SysForge tracks |
|---|---|
| ThinLTO cache dir | Existence, size, last-modified timestamp before each build |
| CMake/Meson build dirs | Presence of stale `CMakeCache.txt` / `build.ninja` in `$srcdir` before build starts |
| makepkg `SRCDEST` git cache | Git HEAD of cached VCS sources before and after fetch |
| `ld.so` cache | Mtime of `/etc/ld.so.cache` before and after any library package install |
| pacman package cache | Whether a cached `.pkg.tar.zst` in `/var/cache/pacman/pkg/` conflicts with a freshly built package |

The `--cache-report` flag dumps a structured summary of all passive cache observations at the end of any build run.

---

## Hardware Detection

Pipeline stage between `base_install` and `kernel`. Walks:

- `lspci -k`
- `lsmod`
- `/sys/bus`

Emits `hardware_profile.toml` which feeds kconfig automation (module → `CONFIG_*` mapping). Also wraps `make localmodconfig` with an lsmod snapshot for cross-machine reproducibility.

**Always-include list** for common drivers that may not be loaded at detection time. Key machine-specific caveats:

- Explicit disable of `nouveau` (RTX 5070)
- `CONFIG_MZEN3`, `CONFIG_X86_AMD_PSTATE` for Zen 3

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
- **AUR:** publish once the primitives layer is functional and the wrapper is runnable against real packages

### AUR Publishing Process

```bash
# One-time setup
# 1. Create account at aur.archlinux.org
# 2. Add SSH key to AUR account
# 3. Clone the (empty) AUR repo
git clone ssh://aur@aur.archlinux.org/sysforge.git

# Add PKGBUILD, generate .SRCINFO, push
makepkg --printsrcinfo > .SRCINFO
git add PKGBUILD .SRCINFO
git commit -m "Initial release"
git push
```

### Ongoing Maintenance

- Update `pkgver` and `sha256sums` on each new GitHub release
- Respond to AUR comments for reported breakage
- Orphan the package if abandoned so others can adopt it

---

## Re-converge

Re-converge is a first-class SysForge feature — not an afterthought. It makes SysForge a full lifecycle manager rather than a one-shot installer.

SysForge tracks build state in `/var/lib/sysforge/build_state.toml`:

```toml
[mesa-git]
profile   = "optimized"
pkgver    = "24.1.0"
built_at  = "2026-02-14T10:32:00Z"

[llvm]
profile   = "pgo_llvm_toolchain"
pkgver    = "18.1.8"
built_at  = "2026-02-10T08:15:00Z"
```

Running `sysforge converge` compares current installed state against the manifest and rebuild flags, then re-builds any package whose profile, flags, or version have drifted. A `--dry-run` flag shows what would be rebuilt without doing it.

DAG stages are categorised as **bootstrap-only** (partition, base_install, toolchain) or **repeatable** (packages, configure). Only repeatable stages participate in re-converge runs.

Requires root. No service user.

---

## V2 Roadmap

V0.1 scope is the primitives and bootstrap pipeline described in this document. The long-term goal is for SysForge to become a full `yay` replacement — an AUR helper with compiler optimization as a first-class concern rather than an afterthought.

V2 absorbs:

- **AUR fetch** — clone and update PKGBUILDs from AUR directly
- **PKGBUILD review** — present diffs to the user before build (standard AUR helper hygiene)
- **Recursive AUR dep resolution** — walk the full AUR dependency tree, not just declared pacman deps
- **Mixed pacman/AUR tree management** — unified view of repo and AUR packages
- **Upgrade management** — `sysforge upgrade` checks AUR for new versions and rebuilds with active profiles

The primitives layer built in v0.1 is the correct foundation for all of this — no teardown needed as scope expands.

### V1.5: Rule Priority Auto-Calculation

Currently `priority` is a manually assigned integer on each rule. A future improvement is to auto-calculate a baseline specificity score from the rule's conditions, with an optional manual `priority` override to break ties or force ordering where the auto value is wrong.

The logic mirrors CSS specificity: more conditions AND'd together = more specific = higher weight. Within a single condition type, `_all` is stricter than `_any` and should score higher. Item count within a condition also contributes — `makedepends_all = ["cmake", "ninja", "python"]` is more specific than `makedepends_all = ["cmake"]`.

Cross-field weighting (e.g. `pkgnames` vs `makedepends_all`) has no objectively correct answer and is where the manual override earns its keep. The auto-score provides a sensible default; the override handles intent that specificity counting can't capture.

This is deferred until there are enough real rules in production use to validate whether auto-priority actually causes ordering problems in practice. The manual `priority` field is sufficient for v1.0.

---

## Open Questions

- **`consumes` placement:** profiles vs rules — revisit after building and testing the wrapper
