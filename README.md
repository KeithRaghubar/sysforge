# SysForge Design Document

> "Gentoo without the overhead."

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
13. [Graphics Stack Build Order](#graphics-stack-build-order)
14. [Release Plan](#release-plan)
15. [Re-converge](#re-converge)
16. [V2 Roadmap](#v2-roadmap)
17. [Open Questions](#open-questions)

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
~/sysforge/
├── sysforge/
│   ├── __init__.py
│   ├── cli.py
│   ├── dag.py
│   └── primitives/
│   │   ├── pkgbuild_meta.py
│   │   └── makepkg_wrapper.py
│   └── config/
│       ├── loader.py
│       └── profiles.py
├── configs/
│   ├── packages.toml
│   ├── flag_profiles.toml
│   └── hardware/
│       └── zen3_rtx5070.toml
├── tests/
│   └── pkgbuild_samples/
├── PKGBUILD
└── pyproject.toml
```

### Installed

```
/etc/sysforge/
    flag_profiles.toml
    consumes_inference.toml
~/.config/sysforge/
    flag_profiles.toml        # user overrides
/usr/bin/sysforge
```

---

## Package Manifest

`packages.toml` is the master list of packages SysForge installs. It is **separate from `flag_profiles.toml`** — package sourcing and build flag tuning are orthogonal concerns and must not be conflated.

Each entry declares:

- `source` — one of `repo` (pacman), `aur`, or `git` (direct PKGBUILD)
- `pkgbuild_patch` *(optional bool)* — if `true`, the PKGBUILD patching library runs on this package before build
- `requires_hardware` *(optional)* — hardware capability key that must be present in `hardware_profile.toml` for this package to be included; absent or false drops the package silently at pipeline time

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

Static regex parser for PKGBUILD metadata. Does **not** source or execute the PKGBUILD.

**Reliably parseable fields:** `pkgname`, `pkgver`, `pkgrel`, `epoch`, `groups`, `depends`, `makedepends`, `provides`

**Not statically parseable:** computed values (`pkgver=$(...)`, conditional metadata, `depends+=()` inside functions). The wrapper falls back to `default_profile` when parsing fails.

```
import re

def parse_pkgbuild(path):
    text = open(path).read()
    result = {}

    # Scalars
    for m in re.finditer(r'^(\w+)=["\']?([^()\n"\']+)["\']?', text, re.MULTILINE):
        result[m.group(1)] = m.group(2).strip()

    # Arrays
    for m in re.finditer(r'^(\w+)=\(([^)]*)\)', text, re.MULTILINE | re.DOTALL):
        items = re.findall(r'["\']?([^\s"\']+)["\']?', m.group(2))
        result[m.group(1)] = [i for i in items if i]

    return result
```

### `makepkg_wrapper.py`

See [Makepkg Wrapper](#makepkg-wrapper).

---

## Flag Profile System

### Profile Structure

Profiles are defined in `flag_profiles.toml`. Each profile is a named set of compiler flags and env vars.

```
[profiles.bare]
CFLAGS = "-O2 -pipe"
CXXFLAGS = "-O2 -pipe"
RUSTFLAGS = ""
MAKEFLAGS = "-j$(nproc)"

[profiles.safe_globals]
extends = "bare"

[profiles.safe_globals.append]
CFLAGS = "-march=native -fstack-protector-strong"
CXXFLAGS = "-march=native -fstack-protector-strong"
RUSTFLAGS = "-Ctarget-cpu=native -Copt-level=3"
LDFLAGS = "-Wl,-O1,--sort-common,--as-needed -fuse-ld=mold"

[profiles.lto]
extends = "safe_globals"

[profiles.lto.append]
CFLAGS = "-flto=thin"
CXXFLAGS = "-flto=thin"
RUSTFLAGS = "-Clinker-plugin-lto -Clto=thin"

[profiles.no_lto]
extends = "safe_globals"

[profiles.no_lto.env]
CARGO_PROFILE_RELEASE_LTO = "false"
```

### `extends` Semantics

Full inheritance with explicit override. The child starts as a complete copy of the parent's resolved values, then applies its own keys on top. The `append` subsection concatenates onto the parent's already-resolved value rather than replacing it — this means a child's appends always build correctly on top of whatever the parent resolved to, even if the parent's value changes.

### Rules

Rules match packages to profiles based on package properties. All conditions within a rule are AND'd. Multiple rules are OR'd (each evaluated independently).

```
[[rules]]
priority = 5
match.makedepends = ["cargo"]
profile = "safe_globals"

[[rules]]
priority = 10
match.groups = ["rust-lto-broken"]
profile = "no_lto"

[[rules]]
priority = 15
match.pkgname = "firefox"
profile = "lto"

[[rules]]
priority = 10
match.pkgname_regex = "^lib32-.*"
match.makedepends = ["cmake"]
not.groups = ["lto-safe"]
profile = "safe_globals"
```

**Match fields:**

- `pkgname` — exact string match
- `pkgname_regex` — full regex; mutually exclusive with `pkgname` per rule
- `groups`, `depends`, `makedepends` — any-overlap (package lists any of these)
- `not.groups`, `not.makedepends` — negative match; rule skipped if package matches any

> **Note:** Rule match logic is marked for revisit before implementation.

### Multi-Rule Merge and Priority

When multiple rules match a package, all matching profiles are collected and merged:

- Each flag key takes the value from the **highest priority rule**
- Equal priority → first occurrence (file order) wins
- Losers are logged with their priorities but discarded — output is always a single resolved value per key, no cross-rule concatenation

The `priority` field is an integer (default `0`). Higher = wins.

**`append` across rules:** when two matching rules both have `append` entries for the same key, this is treated as a conflict — highest priority rule's append wins, others are logged and discarded. There is no cross-rule concatenation. `append` only concatenates within a single profile's own `extends` chain.

### `consumes` Field

Declares which conf files a package build requires (`makepkg`, `rust`, `meson`, `cmake`, `env`, etc.).

- Lives on profiles *(placement to revisit after build/test)*
- **Default:** auto-inferred from `makedepends` via a static inference map in system config
- **Override:** explicit `consumes` on a profile replaces inferred value

```
# /etc/sysforge/consumes_inference.toml
[consumes_inference]
cargo  = ["makepkg", "rust", "env"]
meson  = ["makepkg", "meson", "env"]
cmake  = ["makepkg", "cmake", "env"]
ninja  = ["makepkg", "env"]
```

The wrapper generates only the conf files in the resolved `consumes` set, logs the active set under `[CONF]`. Missing conf files cause a build failure — detailed logs are sufficient to diagnose the cause.

---

## Makepkg Wrapper

### High-Level Flow

1. Parse PKGBUILD statically via `pkgbuild_meta.py`
2. Evaluate all rules against package properties → collect matching profiles
3. Resolve `extends` chains on each matched profile into fully flat value sets
4. Merge across matched rules using priorities → one resolved value per key; log all discarded candidates
5. Log full resolved values with winning sources and discarded candidates
6. Generate temp conf files (only those in `consumes`); env overrides kept separate
7. Run `makepkg` pointed at temp conf files via `MAKEPKG_CONF`; env vars passed as explicit env on invocation

Conf files only receive native keys (CFLAGS, LDFLAGS, RUSTFLAGS, etc.). Env vars from `[profiles.*.env]` travel separately and are never written into conf files.

### Pre-Build Dependency Analysis

Before invoking makepkg, the wrapper runs two checks against the resolved `depends` and `makedepends`:

**Soname inspection** — resolves the `.so` versions each declared dependency currently exposes on the system (via `ldconfig -p` and `/usr/lib`) and compares against what the package expects to link against. Any version mismatch is flagged under `[DEP]` before the build starts rather than surfacing as a cryptic mid-build linker error.

**Version constraint check** — parses version constraints from `depends` (e.g. `foo>=1.2`) and diffs against `pacman -Q` output. Unsatisfied or borderline constraints are flagged under `[DEP]` before makepkg attempts to resolve them itself.

Both checks are non-fatal by default and configurable via `abi_mismatch` and `dep_unsatisfied` in `[failure_handling]`. Results feed into the failure pattern library for human-readable diagnosis.

### Failure Handling

Each scenario has a configurable behaviour in `[failure_handling]`:

```
[failure_handling]
pkgbuild_unparseable  = "warn_and_fallback"  # fallback to bare
no_rule_matched       = "fallback"           # use default_profile silently
profile_missing       = "abort"             # config bug, always hard stop
profile_cycle         = "abort"             # extends loop, always hard stop
tempfile_write_failed = "abort"             # never silently use system conf
env_conflict          = "warn_and_fallback"  # wrapper value wins, logged
abi_mismatch          = "warn_and_fallback"  # soname version mismatch detected pre-build
dep_unsatisfied       = "warn_and_fallback"  # version constraint not met pre-build

default_profile = "safe_globals"
```

**Behaviours:** `abort`, `warn_and_fallback`, `fallback`, `error`

New scenarios are added by registering a handler function and adding its key to `[failure_handling]`. `profile_missing` and `tempfile_write_failed` always abort regardless of config.

### Env Var Precedence

Env precedence is a first-class config table. Changing the hierarchy means changing these numbers — not tracing which file happened to win.

```
[env_precedence]
wrapper_profile   = 100  # TOML [profiles.*.env] overrides
makepkg_conf      = 80   # CFLAGS/LDFLAGS/RUSTFLAGS etc.
shell_passthrough = 20   # inherited calling env (allowlisted vars)
pkgbuild_export   = 10   # detected exports in PKGBUILD (best-effort)
```

The full precedence table is logged at startup under `[ENV]`. The wrapper constructs a clean environment dict rather than inheriting the calling shell's env wholesale — an explicit allowlist of vars (PATH, HOME, USER, etc.) is passed through; everything else is blocked unless the profile explicitly includes it.

---

## Logging

Every log line is prefixed with exactly one structured category tag:

Every line also includes the current package name as a second field, e.g. `[PROFILE][mesa-git]`. This means the log can be filtered by either dimension independently — grep for a tag to see all activity of that type, or grep for a package name to see its full build story.

| Tag | Covers |
| --- | --- |
| `[PROFILE]` | Profile resolution, rule matching, extends chain |
| `[FLAG]` | makepkg.conf flag resolution and conflicts |
| `[ENV]` | Env var resolution, conflicts, overrides, precedence table |
| `[BUILD]` | makepkg invocation and exit codes |
| `[FAILURE]` | Any failure scenario firing |
| `[CONF]` | Config file loading, hierarchy, active consumes set |
| `[DEP]` | Pre-build dependency analysis: soname mismatches and version constraint checks |

Grepping a single tag gives the complete story for that concern across the full log.

**Example pre-build output:**

```
[ENV]    [sysforge]    precedence: wrapper_profile=100 makepkg_conf=80 shell_passthrough=20 pkgbuild_export=10
[DEP]    [mesa-git]    soname ok: libLLVM-18.so → found
[DEP]    [mesa-git]    version ok: llvm-libs>=18.0 → 18.1.8-1
[PROFILE][mesa-git]    matched rule: groups=rust-lto-broken priority=10 → no_lto
[PROFILE][mesa-git]    discarded: makedepends=cargo priority=5 → safe_globals (outprioritized)
[FLAG]   [mesa-git]    CFLAGS="-O2 -pipe -march=znver3 ..." (source: safe_globals via no_lto extends)
[ENV]    [mesa-git]    CARGO_PROFILE_RELEASE_LTO="false" (source: no_lto env override)
[CONF]   [mesa-git]    active conf files: makepkg, rust, env
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

```
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
profile   = "no_lto"
pkgver    = "24.1.0"
built_at  = "2026-02-14T10:32:00Z"

[llvm]
profile   = "lto"
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

---

## Open Questions

- **`consumes` placement:** profiles vs rules — revisit after building and testing the wrapper
- **Rule match logic:** full implementation details marked for revisit before coding begins
