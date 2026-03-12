# SysForge

SysForge is a personal Arch Linux automation framework that produces a reproducible, performance-tuned system from declarative TOML configs. It is an installer and bootstrapper — not a package manager. Pacman owns the ongoing package lifecycle; SysForge gets you from a vanilla Arch ISO to a fully configured, compiler-optimized system.

**Current status:** Early development. Primitives layer is functional; full pipeline is not yet complete. Not ready for general use.

---

## What it does

- Declarative TOML profiles for per-package compiler flags (`-march=native`, LTO, PGO, etc.)
- Rule-based profile matching against PKGBUILD metadata — no manual annotation of individual packages
- Reproducible installs driven by a package manifest
- Hardware detection stage that profiles your machine and feeds kernel config automation
- Checkpoint/resume across pipeline stages so a failed install can be continued, not restarted

## What it is not

- A distro. Output is a standard Arch install; pacman owns it after bootstrap.
- A replacement for pacman. SysForge builds and installs; pacman manages the result.
- A fork of `archinstall`. It solves a different problem — compiler optimization, not just partitioning and package selection.

---

## Requirements

- Arch Linux (or Arch ISO for fresh installs)
- Python 3.11+
- `makepkg`, `pacman`
- An AUR helper is **not** required — SysForge handles AUR builds directly

---

## Installation

SysForge is distributed as an AUR package.

```bash
# Clone and build manually (until AUR publication)
git clone https://github.com/KeithRaghubar/sysforge.git
cd sysforge
makepkg -si
```

Once published to AUR, installation will be:

```bash
yay -S sysforge
# or
paru -S sysforge
```

Installed paths:
- `/etc/sysforge/` — system defaults
- `~/.config/sysforge/` — user overrides (optional)
- `/usr/bin/sysforge` — CLI

---

## Configuration

SysForge is configured through two files:

- **`flag_profiles.toml`** — compiler flag profiles and package matching rules
- **`packages.toml`** — the package manifest (what to install and from where)

The system defaults live in `/etc/sysforge/`. To override without touching system files, create `~/.config/sysforge/flag_profiles.toml`. By default the user file fully replaces the system one. To layer it on top instead:

```toml
# ~/.config/sysforge/flag_profiles.toml
extends_system = true

[profiles.my_custom_profile]
extends = "optimized"
CFLAGS = "-march=native -O3 -pipe -fno-plt -fno-stack-protector"
```

### Flag profiles

Profiles are named sets of compiler flags. They support inheritance via `extends`:

```toml
[profiles.bare]
# Fallback — no flags

[profiles.standard]
extends = "bare"
CFLAGS = "-march=native -O2 -pipe"
CXXFLAGS = "$CFLAGS"
LDFLAGS = "-Wl,-O1,--sort-common,--as-needed,-z,relro,-z,now"

[profiles.optimized]
extends = "standard"
CFLAGS = "-march=native -O3 -pipe -fno-plt"
LDFLAGS = "-Wl,-O1,--sort-common,--as-needed,-z,relro,-z,now,--icf=all"
```

### Rules

Rules match packages to profiles based on PKGBUILD metadata. Conditions within a rule are AND'd; multiple rules are OR'd independently. The highest-priority matching rule wins each flag key.

```toml
[[rules]]
pkgnames = ["htop"]
profile = "optimized"
priority = 0

[[rules]]
makedepends_all = ["cmake", "ninja"]
profile = "optimized"
priority = 5

[[rules]]
pkgnames = ["llvm", "clang", "lld"]
profile = "pgo_llvm_toolchain"
priority = 20
```

**Match fields:**

| Field | Matches when |
|---|---|
| `pkgnames` | any pattern matches any package name (glob supported) |
| `not_pkgnames` | all patterns are absent from package names (glob supported) |
| `groups` | all patterns match at least one package group (glob supported) |
| `not_groups` | all items absent from package groups (exact) |
| `depends_any` | any item present in depends |
| `depends_all` | all items present in depends |
| `not_depends` | all items absent from depends |
| `makedepends_any` | any item present in makedepends |
| `makedepends_all` | all items present in makedepends |
| `not_makedepends` | all items absent from makedepends |

---

## Usage

> **Note:** Full pipeline commands are not yet implemented. The following documents the intended interface.

### Fresh install (from Arch ISO)

```bash
# Boot Arch ISO, install SysForge, then:
sysforge install --config ~/.config/sysforge/
```

This runs the full pipeline: partition → base install → hardware detection → toolchain → packages → kernel → configure.

### Re-converge an existing system

```bash
# Rebuild any package whose profile, flags, or version has drifted
sysforge converge

# Preview what would be rebuilt without doing it
sysforge converge --dry-run
```

### Build a single package

```bash
# Build a package using its matched profile
sysforge build htop

# Build against a specific profile, bypassing rule matching
sysforge build htop --profile optimized
```

### Inspect profile resolution

```bash
# Show which profile would be applied to a package and why
sysforge resolve htop

# Show the full resolved flag set
sysforge resolve htop --show-flags
```

---

## Logging

Every log line is tagged by category and package name, making logs greppable by either dimension:

```
[PROFILE][mesa-git]   matched rule: groups=modified priority=15 → optimized
[FLAG]   [mesa-git]   CFLAGS="-march=native -O3 -pipe -fno-plt" (source: optimized)
[BUILD]  [mesa-git]   invoking makepkg via MAKEPKG_CONF=/tmp/sysforge_abc123.conf
```

SysForge writes a unified log (all packages) and per-package logs simultaneously. Override with:

```bash
sysforge build htop --no-unified-log   # per-package logs only
sysforge build htop --no-pkg-logs      # unified log only
sysforge build htop --log-dir /tmp/sf  # custom log directory
```

---

## Project status and roadmap

| Milestone | Status |
|---|---|
| PKGBUILD parser (`pkgbuild_meta.py`) | ✅ Done |
| Rule matching (`match_rules`) | ✅ Done |
| Profile extends + merge (`merge_extends`) | ✅ Done |
| Makepkg wrapper (end-to-end) | 🔧 In progress |
| Pipeline DAG | ⬜ Planned |
| Hardware detection stage | ⬜ Planned |
| AUR publication | ⬜ Blocked on wrapper completion |
| `sysforge converge` | ⬜ Planned |
| Full yay replacement (V2) | ⬜ Long-term |

---

## Contributing

SysForge is a personal project and is not currently accepting contributions. Issues and feedback are welcome via GitHub.

---

## License

MIT. See [LICENSE](LICENSE).
