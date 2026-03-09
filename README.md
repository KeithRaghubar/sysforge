# SysForge Design Document

> "Gentoo without the overhead."

SysForge is a personal system automation framework that produces a reproducible, performance-tuned Arch Linux install from declarative TOML configs. It is an installer and bootstrapper — not a package manager. Pacman owns the ongoing package lifecycle; SysForge gets you to a fully configured, optimized system from a vanilla Arch ISO.

---

## Table of Contents

1. [Philosophy](#philosophy)
2. [Distribution Model](#distribution-model)
3. [Architecture Overview](#architecture-overview)
4. [Directory Structure](#directory-structure)
5. [Config Layer](#config-layer)
6. [Pipeline Layer](#pipeline-layer)
7. [Primitives Layer](#primitives-layer)
8. [Flag Profile System](#flag-profile-system)
9. [Makepkg Wrapper](#makepkg-wrapper)
10. [Logging](#logging)
11. [Hardware Detection](#hardware-detection)
12. [Graphics Stack Build Order](#graphics-stack-build-order)
13. [Release Plan](#release-plan)
14. [Open Questions](#open-questions)

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
2. **base_install**
3. **hardware_detection** *(walks `lspci -k`, `lsmod`, `/sys/bus` → emits `hardware_profile.toml`)*
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

```python
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

```toml
[profiles.bare]
CFLAGS = "-O2 -pipe"
CXXFLAGS = "-O2 -pipe"
RUSTFLAGS = ""
MAKEFLAGS = "-j$(nproc)"

[profiles.safe_globals]
extends = "bare"

[profiles.safe_globals.append]
CFLAGS = "-march=znver3 -mtune=znver3 -fstack-protector-strong"
CXXFLAGS = "-march=znver3 -mtune=znver3 -fstack-protector-strong"
RUSTFLAGS = "-Ctarget-cpu=znver3 -Copt-level=3"
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

```toml
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

```toml
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

### Failure Handling

Each scenario has a configurable behaviour in `[failure_handling]`:

```toml
[failure_handling]
pkgbuild_unparseable  = "warn_and_fallback"  # fallback to bare
no_rule_matched       = "fallback"           # use default_profile silently
profile_missing       = "abort"             # config bug, always hard stop
profile_cycle         = "abort"             # extends loop, always hard stop
tempfile_write_failed = "abort"             # never silently use system conf
env_conflict          = "warn_and_fallback"  # wrapper value wins, logged

default_profile = "safe_globals"
```

**Behaviours:** `abort`, `warn_and_fallback`, `fallback`, `error`

New scenarios are added by registering a handler function and adding its key to `[failure_handling]`. `profile_missing` and `tempfile_write_failed` always abort regardless of config.

### Env Var Precedence

Env precedence is a first-class config table. Changing the hierarchy means changing these numbers — not tracing which file happened to win.

```toml
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

| Tag | Covers |
|---|---|
| `[PROFILE]` | Profile resolution, rule matching, extends chain |
| `[FLAG]` | makepkg.conf flag resolution and conflicts |
| `[ENV]` | Env var resolution, conflicts, overrides, precedence table |
| `[BUILD]` | makepkg invocation and exit codes |
| `[FAILURE]` | Any failure scenario firing |
| `[CONF]` | Config file loading, hierarchy, active consumes set |

Grepping a single tag gives the complete story for that concern across the full log.

**Example pre-build output:**
```
[ENV]    precedence: wrapper_profile=100 makepkg_conf=80 shell_passthrough=20 pkgbuild_export=10
[PROFILE] matched rule: groups=rust-lto-broken priority=10 → no_lto
[PROFILE] discarded: makedepends=cargo priority=5 → safe_globals (outprioritized)
[FLAG]   CFLAGS="-O2 -pipe -march=znver3 ..." (source: safe_globals via no_lto extends)
[ENV]    CARGO_PROFILE_RELEASE_LTO="false" (source: no_lto env override)
[CONF]   active conf files: makepkg, rust, env
[BUILD]  invoking makepkg -si via MAKEPKG_CONF=/tmp/sysforge_abc123.conf
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
   - PGO (64-bit): `llvm`, `llvm-libs`, `clang`, `lld`
   - Non-PGO (64-bit): `polly`, `compiler-rt`, `openmp`, `spirv-llvm-translator`
   - Non-PGO (lib32): `lib32-llvm`, `lib32-llvm-libs`, `lib32-clang`, `lib32-spirv-llvm-translator`
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

## Open Questions

- **`consumes` placement:** profiles vs rules — revisit after building and testing the wrapper
- **Re-converge mode:** post-install drift correction (pacman owns lifecycle, but a future `sysforge converge` command that re-applies profiles to a running system is an open possibility)
- **Rule match logic:** full implementation details marked for revisit before coding begins
