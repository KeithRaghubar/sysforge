# SysForge

SysForge is an AUR helper for Arch Linux with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles — every AUR package is built with `-march=native`, LTO, or whatever profile matches its PKGBUILD metadata. Pacman owns the package database; SysForge owns the build configuration layer above it.

**Current status:** v0.1.0 complete. All userspace commands (`build`, `update`, `resolve`, `converge`, `packages`, `run pipeline/reconfigure/toolchain/packages/kernel`) are implemented and usable on a live system. Bootstrap stages 1–4 (partition, base install, hardware detection, configure) are deferred to v1.0.

---

## What it does

- Builds AUR and custom packages with system-tuned compiler flags (`-march=native`, LTO, PGO, etc.)
- Rule-based profile matching against PKGBUILD metadata — no manual annotation of individual packages
- `sysforge update` — checks all sysforge-managed AUR packages for new upstream versions and rebuilds outdated ones with active profiles; VCS packages (`-git` etc.) via `--devel`
- Reproducible installs driven by a package manifest (`packages.toml`)
- Checkpoint/resume across pipeline stages so a failed batch install can be continued, not restarted
- Pre-build soname dependency analysis to surface ABI mismatches before the build starts
- PKGBUILD flag extraction and patching — extracts compiler flags from PKGBUILD function bodies and manages them through the profile system instead

## What it is not

- A distro. Output is a standard Arch install; pacman owns the package database.
- A replacement for pacman. SysForge handles AUR build configuration and automation; pacman manages the installed package database.
- A system installer. Bootstrap functionality (partitioning, base install, hardware detection) is planned for v1.0; the v0.1.0 scope is the AUR management layer.

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

## Quick start

```bash
# 1. Install your system config files
sudo mkdir -p /etc/sysforge
sudo cp /path/to/flag_profiles.toml /etc/sysforge/
sudo chmod 644 /etc/sysforge/*.toml /etc/sysforge/
sudo chmod 755 /etc/sysforge/

# 2. Build and install an AUR package with your active profile
sysforge build neovim-git -m "-si"

# 3. Check for and rebuild any outdated sysforge-managed packages
sysforge update

# 4. Rebuild VCS packages too
sysforge update --devel

# 5. Preview what would be rebuilt without doing it
sysforge update --dry-run

# 6. Manage packages.toml
sysforge packages list
sysforge packages add htop neovim
sysforge packages remove htop
sysforge packages sync --dry-run
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

### Run the pipeline

```bash
# Stages 1–4 are stubbed. Start from reconfigure (pre-build checks) on a live system:
sysforge run pipeline --start-from reconfigure --packages packages.toml --state-dir ~/sf-state

# Or skip straight to builds:
sysforge run pipeline --start-from packages --packages packages.toml --state-dir ~/sf-state

# Resume after a failure
sysforge run pipeline --resume --state-dir ~/sf-state

# Preview without executing
sysforge run pipeline --start-from reconfigure --dry-run --state-dir ~/sf-state

# Retry failed packages without prompting
sysforge run pipeline --resume --force-retry --state-dir ~/sf-state
```

**`run pipeline` flags:**

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

Individual stages can also be run standalone outside the pipeline:

```bash
sysforge run reconfigure --state-dir ~/sf-state
sysforge run toolchain --dry-run
sysforge run packages --packages packages.toml --state-dir ~/sf-state
sysforge run kernel --state-dir ~/sf-state
```

### Check for and apply updates

`sysforge update` checks all sysforge-managed packages recorded in `/var/lib/sysforge/build_state.toml` against the latest PKGBUILD (after `git pull --rebase`) and rebuilds any package where the upstream version is newer than what is installed.

```bash
# Check and rebuild outdated packages
sysforge update

# Include VCS packages (-git, -svn, etc.)
sysforge update --devel

# Preview without rebuilding
sysforge update --dry-run

# Skip git pull (use cached PKGBUILD)
sysforge update --no-update

# Discover and add all foreign packages not yet tracked
sysforge update --all
```

**Update flags:**

| Flag | Effect |
|---|---|
| `--all` | Also discover foreign packages (`pacman -Qm`) not yet tracked; add to `packages.toml` and rebuild if outdated |
| `--packages <file>` | Path to `packages.toml` for `--all` discovery (default: `/etc/sysforge/packages.toml`) |
| `--dry-run` | Show what would be rebuilt without doing it |
| `--devel` | Include VCS packages (`-git`, `-svn`, `-hg`, `-bzr`) in the rebuild |
| `--no-update` | Skip `git pull --rebase` before checking versions |
| `--state-dir <dir>` | Override state directory |
| `--profile-conf <file>` | Use alternate `flag_profiles.toml` |
| `--cache-report` | Print cache summary after the run |
| `--no-pkg-log` | Disable per-package log files |
| `--persist-log` | Keep log files after successful completion |
| `--log-dir <dir>` | Override log file directory |

`sysforge update` is scoped to packages sysforge has built by default — it reads `build_state.toml` which is written by `sysforge build` and `sysforge run packages`. Use `--all` to also pick up foreign packages not yet tracked. Repo packages (installed via `pacman -S`) are out of scope; use `pacman -Syu` for those.

### Manage packages.toml

`sysforge packages` is a namespace for managing an existing `packages.toml`. Without a subcommand it defaults to `list`.

```bash
# Show all entries (name, source, optional fields)
sysforge packages list
sysforge packages list --packages ~/my-packages.toml

# Add a package: auto-classifies source (repo vs AUR), infers pkgbuild_patch
sysforge packages add htop neovim
sysforge packages add mesa-git --packages ~/my-packages.toml

# Remove a package entry
sysforge packages remove htop

# Re-validate inferable fields (source, pkgbuild_patch) for all entries
sysforge packages sync
sysforge packages sync --dry-run   # preview without writing
```

`packages add` classifies the package by querying pacman sync DBs and the AUR. For AUR packages, if the PKGBUILD is already cloned in `[build] pkgbuild_dir`, it runs flag extraction to infer `pkgbuild_patch = true` automatically.

`packages sync` re-validates `source` and `pkgbuild_patch` for all entries and rewrites the file. The `cache` field is preserved verbatim.

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

Completes subcommands, all flags, and package names (local `pkgbuild_dir` + pacman sync DB + AUR cache if present at `~/.cache/sysforge/aur-packages.txt`).

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
| Packages stage (stage 7) | ✅ Done |
| Pytest suite (688 tests) | ✅ Done |
| Kernel stage (stage 8) | ✅ Done |
| Reconfigure stage (stage 5) | ✅ Done |
| Toolchain stage (stage 6, LLVM/GCC + PGO) | ✅ Done |
| `sysforge update` (version drift detection + rebuild) | ✅ Done |
| Build state tracking (`build_state.toml`) | ✅ Done |
| `sysforge converge` (profile/flag drift detection) | ✅ Done |
| `sysforge update --all` (pacman -Qm foreign packages) | ✅ Done |
| AUR name cache (packages.gz → ~/.cache/sysforge/) | ✅ Done |
| `repo_mode` config (pacman passthrough vs profiled build) | 🔜 v1.0 — currently parsed by the bootstrap pipeline stages (deferred) but has no effect on `build`/`update` |
| `sysforge resolve` | ✅ Done |
| Bare package name resolution (`sysforge build htop`) | ✅ Done |
| AUR auto-clone on miss | ✅ Done |
| Repo package auto-checkout via pkgctl | ✅ Done |
| GPG key auto-import (`validpgpkeys` + bundled `keys/pgp/`) | ✅ Done |
| Zsh tab completion | ✅ Done |
| CLI restructure (`packages` namespace, `run` namespace) | ✅ Done |
| `packages list/add/remove/sync` | ✅ Done |
| Profiled AUR helper (v0.1.0) | ✅ Done |
| AUR publication | ⬜ After v0.1.0 |
| Bootstrap stages 1–4 (partition → configure) | ⬜ v1.0 |

---

## Contributing

SysForge is a personal project and is not currently accepting contributions. Issues and feedback are welcome via GitHub.

---

## License

MIT. See [LICENSE](LICENSE).
