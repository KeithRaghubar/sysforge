# SysForge

SysForge is an AUR helper for Arch Linux with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles — every AUR package is built with `-march=native`, LTO, or whatever profile matches its PKGBUILD metadata. Pacman owns the package database; SysForge owns the build configuration layer above it.

**Current status:** v0.2.0. All commands implemented and usable. Userspace AUR management (`build`, `update`, `resolve`, `converge`, `packages`, `run pipeline/reconfigure/toolchain/packages/kernel`) plus full bootstrap pipeline (stages 1–4: partition, base install, hardware detection, configure) for fresh installs from the Arch ISO.

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
#    (flag_profiles.toml, packages.toml — both are self-documented)
sudo vim /etc/sysforge/flag_profiles.toml

# 3. Build and install an AUR package with your active profile
sysforge build neovim-git -m "-si"

# 4. Check for and rebuild any outdated sysforge-managed packages
sysforge update

# 5. Rebuild VCS packages too
sysforge update --devel

# 6. Preview what would be rebuilt without doing it
sysforge update --dry-run

# 7. Manage packages.toml
sysforge packages list
sysforge packages list --state        # show build_state.toml entries instead
sysforge packages repair-state --dry-run  # preview fixes for legacy broken entries
sysforge packages add htop neovim
sysforge packages remove htop
sysforge packages sync --dry-run
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
bash <(curl -sL https://raw.githubusercontent.com/KeithRaghubar/sysforge/main/tools/iso-install.sh)
```

The script clones the repo itself — no prior download needed.

The script checks connectivity, installs SysForge (lightweight — no build tools needed), copies config files to `/etc/sysforge/`, and prompts for all required bootstrap values: device, filesystem, hostname, locale, timezone (validated against `/usr/share/zoneinfo/`), username, and passwords (entered silently with confirmation). It writes a complete `bootstrap.toml` and prints the next command when done.

To configure manually instead, edit `/etc/sysforge/bootstrap.toml` directly — every field is documented inline.

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

This runs stages 1–4: partition the disk, `pacstrap` the base system, detect hardware, and configure the installed system (hostname, locale, timezone, mirrorlist, systemd-boot bootloader, NetworkManager + sshd service enables, primary user with sudo, and passwords). The pipeline saves a checkpoint after each stage — a failure can be resumed with `--resume`.

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
- **toolchain** — builds LLVM/GCC toolchain with optional PGO (reuses existing profdata when compatible; `--rebuild-profdata` to force full 3-pass)
- **packages** — builds and installs everything in `packages.toml` with profiled flags
- **kernel** — builds a custom kernel (skipped cleanly if `kernel.toml` is absent)

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

Completes subcommands, all flags, and package names (local `pkgbuild_src_dir` + pacman sync DB + AUR cache if present at `~/.cache/sysforge/aur-packages.txt`).

---

## Contributing

SysForge is a personal project and is not currently accepting contributions. Issues and feedback are welcome via GitHub.

---

## License

MIT. See [LICENSE](LICENSE).
