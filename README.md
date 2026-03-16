# SysForge

SysForge is a personal Arch Linux automation framework that produces a reproducible, performance-tuned system from declarative TOML configs. It is an installer and bootstrapper — not a package manager. Pacman owns the ongoing package lifecycle; SysForge gets you from a vanilla Arch ISO to a fully configured, compiler-optimized system.

**Current status:** Active development. The primitives layer and package build pipeline are functional and usable on a live system. Stages 1–4 (partition, base install, hardware detection, toolchain) are stubbed pending dedicated testing. Not ready for general use.

---

## What it does

- Declarative TOML profiles for per-package compiler flags (`-march=native`, LTO, PGO, etc.)
- Rule-based profile matching against PKGBUILD metadata — no manual annotation of individual packages
- Reproducible installs driven by a package manifest (`packages.toml`)
- Manifest generation from a list of package names (`sysforge manifest`)
- Checkpoint/resume across pipeline stages so a failed install can be continued, not restarted
- Pre-build soname dependency analysis to surface ABI mismatches before the build starts
- PKGBUILD flag extraction and patching — extracts compiler flags from PKGBUILD function bodies and manages them through the profile system instead

## What it is not

- A distro. Output is a standard Arch install; pacman owns it after bootstrap.
- A replacement for pacman. SysForge builds and installs; pacman manages the result.
- A fork of `archinstall`. It solves a different problem — compiler optimization, not just partitioning and package selection.

---

## Requirements

- Arch Linux (or Arch ISO for fresh installs)
- Python 3.11+
- `makepkg`, `pacman`, `sudo`
- An AUR helper is **not** required — SysForge handles AUR builds directly

---

## Installation

```bash
# Clone and build manually (until AUR publication)
git clone https://github.com/KeithRaghubar/sysforge.git
cd sysforge
makepkg -si
```

Installed paths:
- `/etc/sysforge/` — system defaults
- `~/.config/sysforge/` — user overrides (optional)
- `/usr/bin/sysforge` — CLI

---

## Quick start (live system, stages 5–7)

Stages 1–4 require a full install environment. To use SysForge on an existing Arch system for package builds:

```bash
# 1. Install your system config files
sudo mkdir -p /etc/sysforge
sudo cp /path/to/flag_profiles.toml /etc/sysforge/
sudo chmod 644 /etc/sysforge/*.toml /etc/sysforge/
sudo chmod 755 /etc/sysforge/

# 2. Generate a packages.toml from a list of names
sysforge manifest htop neovim mesa-git cosmic-comp-git > packages.toml

# 3. Run the packages stage directly
sysforge install --start-from packages --packages packages.toml --state-dir ~/sf-state
```

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

Profiles are named sets of compiler flags supporting inheritance via `extends`:

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

Use `[profiles.x.append]` for token-level flag merging (additive rather than full replacement):

```toml
[profiles.hardened.append]
CFLAGS = "-fstack-protector-strong"   # replaces -fstack-protector via conflict group
```

### Rules

Rules match packages to profiles based on PKGBUILD metadata. Conditions within a rule are AND'd; multiple rules are OR'd independently. The highest-priority matching rule wins.

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
append_groups = ["pgo"]
```

**Match fields:**

| Field | Matches when |
|---|---|
| `pkgnames` | any pattern matches any package name (glob supported) |
| `not_pkgnames` | all patterns absent from package names (glob supported) |
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

### Generate a package manifest

```bash
# From inline names (queries pacman to classify repo vs AUR)
sysforge manifest htop neovim mold pipewire > packages.toml

# From a text file (one name per line)
sysforge manifest --file my-packages.txt > packages.toml

# Combined
sysforge manifest htop --file extras.txt > packages.toml
```

Packages found in pacman sync DBs are marked `source = "repo"`. Others are confirmed via AUR RPC v5 batch query and marked `source = "aur"`. Packages not found anywhere are excluded with a warning.

### Build a single package

`sysforge build` accepts a PKGBUILD path, a package directory, or a bare package name. Bare names are resolved via `[paths] pkgbuild_dir` in `flag_profiles.toml`; if not found locally, repo packages are fetched via `pkgctl repo clone` and AUR packages via `git clone`.

```bash
# By path, directory, or bare name
sysforge build /path/to/PKGBUILD
sysforge build /path/to/htop/
sysforge build htop

# With additional makepkg flags (appended after profile makepkg_flags)
sysforge build htop -m "-si"
sysforge build htop -m "--noconfirm -f"

# Interactive — strips --noconfirm so makepkg prompts are visible
sysforge build htop --interactive
```

**Build flags:**

| Flag | Effect |
|---|---|
| `-m / --makepkg <flags>` | Append extra makepkg flags (combined flags like `-sfi` are expanded) |
| `--interactive` | Strip `--noconfirm` from makepkg_flags for this build |
| `--profile-conf <file>` | Use alternate `flag_profiles.toml` instead of default paths |
| `--cc <path>` | Override `CC` for this build |
| `--cxx <path>` | Override `CXX` for this build |
| `--ld <linker>` | Override linker (`lld`, `mold`, etc.) for this build |
| `--no-pkg-log` | Disable the per-package log for this build |
| `--log-dir <dir>` | Write per-package log to this directory instead of alongside the PKGBUILD |
| `--persist-log` | Keep per-package log after a successful build |

### Run the install pipeline

```bash
# Stages 1–4 are stubbed. Use --start-from to jump to packages:
sysforge install --start-from packages --packages packages.toml --state-dir ~/sf-state

# Resume after a failure
sysforge install --resume --state-dir ~/sf-state

# Preview without executing
sysforge install --start-from packages --dry-run --state-dir ~/sf-state

# Retry failed packages without prompting
sysforge install --resume --force-retry --state-dir ~/sf-state
```

**Install flags:**

| Flag | Effect |
|---|---|
| `--start-from <stage>` | Skip prior stages and begin from the named stage |
| `--resume` | Continue from the last checkpoint |
| `--force-retry` | On resume, retry failed packages without prompting |
| `--dry-run` | Log what would run without executing |
| `--packages <file>` | Override default `/etc/sysforge/packages.toml` path |
| `--state-dir <dir>` | Override state file location (also: `SYSFORGE_STATE_DIR` env var) |
| `--profile-conf <file>` | Use alternate `flag_profiles.toml` instead of default paths |
| `--no-unified-log` | Disable the unified log for this run |
| `--no-pkg-logs` | Disable per-package logs for this run |
| `--log-dir <dir>` | Override log file directory |
| `--purge-log` | Truncate unified log before the run starts |
| `--persist-log` | Keep log files after a successful run |

### Inspect profile matching

```bash
# Show which profile would apply and why
sysforge resolve htop
sysforge resolve htop --show-flags
```

### Tab completion (zsh)

```bash
# Permanent
sudo cp completions/_sysforge /usr/share/zsh/site-functions/

# Current session only (from repo root)
fpath=($(pwd)/completions $fpath) && compinit
```

Completes subcommands, all flags, and package names (local `pkgbuild_dir` + pacman sync DB).

---

## Logging

All output goes to stderr. Verbosity is controlled with `-v`/`-vv`:

```bash
sysforge build PKGBUILD           # errors only
sysforge -v build PKGBUILD        # + warnings (soname mismatches, skips, etc.)
sysforge -vv build PKGBUILD       # all messages
sysforge -vvv build PKGBUILD      # + debug: full config, profile, and conf file dumps
```

Every log line follows the format `[SYSFORGE][LEVEL][TAG] message`, making output greppable by level, tag, or package name independently:

```
[SYSFORGE][INFO][PROFILE] [htop] Matched profile 'optimized' (priority 0)
[SYSFORGE][INFO][CONF] [htop] consumes (inferred from makedepends ['git']): ['makepkg']
[SYSFORGE][INFO][DEP] [htop] soname ok: libcap.so → found
[SYSFORGE][INFO][BUILD] Running makepkg --noconfirm --syncdeps in /path/to/htop
[SYSFORGE][ERROR][BUILD] Build failed: Command 'makepkg' returned non-zero exit status 1
```

**Log levels:** `[ERROR]` always shown · `[WARN]` with `-v` · `[INFO]` with `-vv` · `[DEBUG]` with `-vvv`

**Tags:** `[PROFILE]` `[CONF]` `[ENV]` `[BUILD]` `[FAILURE]` `[DEP]` `[PATCH]` `[GROUPS]` `[CONFIG]` `[PACKAGES]` `[PIPELINE]` `[MANIFEST]` `[FLAG]`

---

## Project status and roadmap

| Milestone | Status |
|---|---|
| PKGBUILD parser | ✅ Done |
| Rule matching | ✅ Done |
| Profile extends + merge | ✅ Done |
| `[profiles.x.append]` token-level merge | ✅ Done |
| Conflict groups | ✅ Done |
| Consumes filtering (conf type routing) | ✅ Done |
| Env pass (`RUSTC_WRAPPER`, `CCACHE_DIR`, etc.) | ✅ Done |
| System makepkg.conf merge | ✅ Done |
| PKGBUILD flag extraction + patching | ✅ Done |
| Pre-build soname dep analysis | ✅ Done |
| Makepkg wrapper (end-to-end) | ✅ Done |
| Structured logging (`[SYSFORGE][LEVEL][TAG]`) | ✅ Done |
| Pipeline runner (checkpoint/resume) | ✅ Done |
| Packages stage (stage 5) | ✅ Done |
| Manifest generator (`sysforge manifest`) | ✅ Done |
| Pytest suite (561 tests) | ✅ Done |
| Kernel stage (stage 6) | ✅ Done |
| Configure stage (stage 7) | 🔧 Stub |
| Stages 1–4 (partition → toolchain) | 🔧 Stub |
| AUR RPC lookup in manifest | ✅ Done |
| Hardware detection stage | ⬜ Planned |
| `sysforge converge` | ⬜ Planned |
| `sysforge resolve` | ✅ Done |
| Bare package name resolution (`sysforge build htop`) | ✅ Done |
| AUR auto-clone on miss | ✅ Done |
| Repo package auto-checkout via pkgctl | ✅ Done |
| GPG key auto-import (`validpgpkeys` + bundled `keys/pgp/`) | ✅ Done |
| Zsh tab completion | ✅ Done |
| AUR publication | ⬜ Planned |
| Full yay replacement (V2) | ⬜ Long-term |

---

## Contributing

SysForge is a personal project and is not currently accepting contributions. Issues and feedback are welcome via GitHub.

---

## License

MIT. See [LICENSE](LICENSE).
