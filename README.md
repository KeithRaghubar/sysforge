# SysForge

SysForge is an AUR helper for Arch Linux with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles — every AUR package is built with `-march=native`, LTO, or whatever profile matches its PKGBUILD metadata. Pacman owns the package database; SysForge owns the build configuration layer above it.

**Current status:** <!--version-->v1.1.0<!--/version-->. All commands implemented and usable. Userspace AUR management (`build`, `fetch`, `update`, `resolve`, `converge`, `doctor`, `setup`, `packages`, `run pipeline/reconfigure/packages`) plus full bootstrap pipeline (stages 1–4: partition, base install, hardware detection, configure) for fresh installs from the Arch ISO. `run toolchain` and `run kernel` are shipped but **experimental and deferred to post-1.0** — they emit a runtime `[WARN]` and default to disabled; 1.0 users should use the system compiler and a stock pacman kernel.

---

## Requirements

- Arch Linux (or Arch ISO for fresh installs)
- Python 3.11+
- `makepkg`, `pacman`, `sudo`
- An AUR helper is **not** required — SysForge handles AUR builds directly

---

## Quick start

```bash
# 1. Install from AUR
git clone https://aur.archlinux.org/sysforge.git
cd sysforge && makepkg -si

# 2. Edit the config files installed at /etc/sysforge/
#    (profiles.toml, packages.toml — both are self-documented)
sudo vim /etc/sysforge/profiles.toml

# 3. Build and install an AUR package with your active profile.
sysforge build neovim-git -m "-si"

# 4. Check for and rebuild any outdated installed AUR packages
#    (Source sync is RPC-first: one batched AUR `info` call, and a git
#     fetch per package only when the cached version/LastModified differs
#     from the local HEAD. Steady-state runs do zero git fetches.)
sysforge update

# 5. Rebuild VCS packages too. Each VCS package's pkgver() is resolved against
#    upstream once, then cached as `built_upstream_commit` in build_state.toml
#    on success; subsequent --devel runs short-circuit via `git ls-remote`
#    when the upstream tip hasn't moved.
sysforge update --devel

# 6. Preview what would be rebuilt without doing it
sysforge update --dry-run

# 6b. Default summary shows aggregated counts; -v expands each skipped or
#     up-to-date package to a per-line reason (rate-limited, devel, etc.).
sysforge update -v

# 7. Same as `update`, plus discard divergent local clones (force-pushed
#    upstream or local-only commits). --cleansrc refuses any clone that has
#    uncommitted changes, unpushed commits, or no upstream — those packages
#    are reported as failed and the run continues. --cleansrc also bypasses
#    the RPC short-circuit and re-clones every AUR package from scratch.
sysforge update --cleansrc

# 8. Install already-built artifacts from PKGDEST without re-running makepkg.
#    Useful when a previous update was interrupted between build and install,
#    or after a manual makepkg run. Implies --offline.
sysforge update --install-only

# 9. Manage packages.toml entries (install list during pipeline bootstrap;
#    build-rule overrides at steady-state — see DESIGN.md §Package Manifest)
sysforge packages list
sysforge packages list --orphans     # entries whose package isn't installed
sysforge packages add mesa-git --pkgbuild-patch
sysforge packages remove mesa-git

# 9b. Inspect / repair build_state.toml (the live install-state mirror)
sysforge state list                  # paginated by default (TTY); --no-pager
sysforge state list --no-pager       # raw output, no $PAGER pipe
sysforge state repair --dry-run      # preview fixes for legacy broken entries
sysforge state orphans               # superseded .pkg.tar* in PKGDEST (safe to delete)
sysforge state orphans --prune       # delete them after y/N confirmation

# 11. Health-check an installed package's depends + linkage (e.g. when Steam
#     launches as a black window and the graphics stack may be out of sync).
#     Walks the target's dep closure; --shallow restricts to direct depends.
sysforge doctor mesa-git
sysforge doctor --graphics            # curated stack + system-state probes
                                      # (nvidia-drm modeset, driver version skew,
                                      #  Wayland explicit-sync, Steam GPU accel, …)
sysforge doctor --all                 # every installed package: foreign + non-foreign
sysforge doctor --repo                # only non-foreign (native repo) packages
sysforge doctor steam --suggest       # reverse-lookup candidate packages for each
                                      # missing soname / broken ABI via pacman -Fq
                                      # (requires sudo pacman -Fy first); splits
                                      # findings into "install candidates" (missing
                                      # on disk and not yet installed), "rebuild
                                      # candidates" (installed; rebuild against
                                      # current system), and "ABI-drift candidates"
                                      # (present but out of sync → rebuild/upgrade)
```

To override system defaults without modifying `/etc/sysforge/`, create user copies in `~/.config/sysforge/`.

For full CLI reference, profile/rule semantics, and architecture details, see [DESIGN.md](DESIGN.md) (`glow -p DESIGN.md` for in-shell rendering).

---

## Fresh install from the Arch ISO

This covers the full path from booting the Arch install ISO to a working system with all packages built and installed.

### 1. Connect to the internet

```bash
# Wired — usually automatic
ip link

# Wireless
iwctl station wlan0 connect "SSID"
```

### 2. Install SysForge and configure bootstrap.toml

Run the setup script directly from the repo:

```bash
# Latest stable AUR sysforge:
bash <(curl -sL https://raw.githubusercontent.com/KeithRaghubar/sysforge/main/tools/iso-install.sh)

# Or, to track main with the AUR sysforge-git package:
bash <(curl -sL https://raw.githubusercontent.com/KeithRaghubar/sysforge/main/tools/iso-install.sh) --git
```

The script checks connectivity, installs SysForge from the AUR (config files at `/etc/sysforge/` are shipped by the package), and prompts for all required bootstrap values: device, filesystem, hostname, locale, timezone (validated against `/usr/share/zoneinfo/`), username, and passwords (entered silently with confirmation). It writes a complete `bootstrap.toml` and prints the next command when done.

To configure manually instead, copy the starter template into place and edit it — every field is documented inline:

```bash
sudo install -Dm600 /usr/share/sysforge/bootstrap.toml.example /etc/sysforge/bootstrap.toml
sudo vim /etc/sysforge/bootstrap.toml
```

### 3. Configure packages.toml

Edit `/etc/sysforge/packages.toml` with the packages to install. Repo packages install via pacman; AUR packages are built with your compiler flag profile. Include `sysforge` if you want it installed in the target system:

```toml
[build]
pkgbuild_src_dir = "~/src"

[[package]]
name   = "sysforge"
source = "aur"

[[package]]
name   = "neovim"
source = "repo"

[[package]]
name   = "neovim-git"
source = "aur"
```

### 4. Run the bootstrap pipeline

```bash
# --state-dir writes into the target so checkpoint state survives the reboot
sysforge run pipeline --state-dir /mnt/var/lib/sysforge
```

This runs stages 1–4: partition the disk, `pacstrap` the base system, detect hardware, and configure the installed system (hostname, locale, timezone, mirrorlist, systemd-boot bootloader, NetworkManager + sshd service enables, primary user with sudo, passwords, and sysforge itself — built from the ISO source with `makepkg` in the chroot and installed via pacman so the install is tracked from the start). The pipeline saves a checkpoint after each stage — a failure can be resumed with `--resume`.

### 5. Reboot into the installed system

When the configure stage completes, the pipeline prints the resume command and exits cleanly:

```
State saved. After rebooting, run:
  sysforge run pipeline --resume
```

```bash
reboot
```

Log in and install SysForge on the new system:

```bash
pacman -Sy --needed git base-devel uv python-pip python-argparse-manpage
git clone https://aur.archlinux.org/sysforge.git
cd sysforge && makepkg -si && cd ~
```

### 6. Continue the pipeline

```bash
sysforge run pipeline --resume
```

This runs stages 5–8:

- **reconfigure** — pre-build checks: disk space, network, config review
- **toolchain** — *(experimental — post-1.0)* builds LLVM/GCC toolchain with optional PGO (reuses existing profdata when compatible; `--rebuild-profdata` to force full 3-pass). Skipped cleanly if `toolchain.toml` has `enabled = false` (the default). Recommended for 1.0: leave disabled.
- **packages** — builds and installs everything in `packages.toml` with profiled flags
- **kernel** — *(experimental — post-1.0)* builds a custom kernel (skipped cleanly if `kernel.toml` is absent or `enabled = false`). Recommended for 1.0: leave disabled and use a stock pacman kernel.

If the stage 5–8 run is interrupted, resume it:

```bash
sysforge run pipeline --resume
```

---

## Tab completion (zsh)

```bash
# Permanent
sudo cp completions/_sysforge /usr/share/zsh/site-functions/

# Current session only (from repo root)
fpath=($(pwd)/completions $fpath) && compinit
```

Completes subcommands, all flags, and package names (local `pkgbuild_src_dir` + pacman sync DB + AUR cache if present at `~/.config/sysforge/cache/aur-packages.txt`).

---

## Contributing

SysForge is a personal project and is not currently accepting contributions. Issues and feedback are welcome via GitHub.

---

## License

MIT. See [LICENSE](LICENSE).
