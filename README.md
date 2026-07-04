# SysForge

SysForge is an Arch Linux build and maintenance suite with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles — every AUR package is built with `-march=native`, LTO, or whatever profile matches its PKGBUILD metadata. Pacman owns the package database; SysForge owns the build configuration layer above it.

The default build profile uses the system gcc. LLVM (clang/lld) is fully supported but opt-in: install the LLVM `optdepends` (`clang`, `lld`, `llvm`, `compiler-rt`) and set `toolchain = "llvm"` in `[defaults]` or a profile, or use `sysforge run toolchain --compiler=llvm`.

**Commands:** `build` / `fetch` / `update` / `resolve` build and maintain profiled AUR & custom packages; `packages` / `state` manage the manifest and build state; `doctor` / `log` / `env` inspect system health and configuration; `setup` wires up pacman integration; `run <stage>` drives the bootstrap pipeline. See `sysforge --help` or the [man page](man/sysforge.1) for the full reference.

<sub><!--version-->v2.0.1<!--/version--></sub>

---

## About

SysForge started as a tool to streamline its author's own Arch Linux build and maintenance workflow on a daily-driver machine, and grew into a general-purpose suite. The codebase is developed essentially end-to-end with Claude Code, but it is deliberately structured to stay hand-editable: the modular design docs under `docs/design/` are the source of truth, every module has a documented owner and API, and the full test suite gates every change. Feedback and contributions are welcome — see [Contributing](#contributing).

---

## Requirements

- Arch Linux (or Arch ISO for fresh installs)
- Python 3.11+
- `makepkg`, `pacman`, `sudo`
- An AUR helper is **not** required — SysForge handles AUR builds directly

> SysForge's writable runtime directories (`/var/lib/sysforge`, `/var/cache/sysforge`)
> are owned by the `sysforge` group. The package install creates the group; the first
> privileged run also adds your user to it. Group membership only takes effect after you
> log out and back in — until then SysForge repairs the directory permissions per run, so
> nothing breaks, you may just be prompted for `sudo` once more than usual.

---

## Quick start

```bash
# 1. Install from the AUR
git clone https://aur.archlinux.org/sysforge.git
cd sysforge && makepkg -si

# 2. One-time bootstrap (sysforge reminds you until both complete)
sudo sysforge run reconfigure   # adopt system/toolchain defaults
sudo sysforge run hardware      # detect CPU/GPU, write the hardware profile

# 3. Edit the shipped config (self-documented)
sudo vim /etc/sysforge/profiles.toml

# 4. Build and install a package with your active profile
sysforge build neovim-git -m "-si"

# 5. Rebuild outdated installed AUR packages
sysforge update
sysforge update --devel       # VCS packages too
sysforge update --dry-run     # preview only
```

Anything you `build` is then *maintained*: `sysforge update` rebuilds it from source as upstream advances. This works for repo packages too — `sysforge build mesa` keeps your optimized mesa current. Stop maintaining one with `sysforge state forget <pkg>` (or `sudo pacman -S <pkg>`, which the next `update` auto-reverts to the repo binary).

Other common verbs:

```bash
sysforge packages list              # manage the package manifest
sysforge state list                 # inspect build state
sysforge doctor                     # fast full system health sweep
sysforge doctor mesa-git            # health-check one package's deps + linkage
sysforge log [PKG]                  # page sysforge logs
sysforge config merge               # adopt shipped config-default drift (.sfnew)
```

To override system defaults without touching `/etc/sysforge/`, create user copies in `~/.config/sysforge/`.

For the full flag reference — PGO/`--pgo`, `--cleansrc`, `--install-only`, throttling, profiling/`--timings`, the `doctor` axes, and profile/rule semantics — see `sysforge --help`, the [man page](man/sysforge.1), and [DESIGN.md](DESIGN.md) (`glow -p DESIGN.md` to render in-shell). Planned and abandoned features live in [ROADMAP.md](ROADMAP.md).

---

## Fresh install from the Arch ISO

This covers the full path from booting the Arch install ISO to a working system with all packages built and installed.

### 1. Connect to the internet

```bash
ip link                                   # wired — usually automatic
iwctl station wlan0 connect "SSID"        # wireless
```

### 2. Install SysForge and configure bootstrap.toml

Run the setup script directly from the repo:

```bash
# Latest stable AUR sysforge (add --git to track main via sysforge-git):
bash <(curl -sL https://raw.githubusercontent.com/KeithRaghubar/sysforge/main/tools/iso-install.sh)
```

The script checks connectivity, installs SysForge from the AUR, and prompts for all required bootstrap values (device, filesystem, hostname, locale, timezone, username, passwords). It writes a complete `bootstrap.toml` and prints the next command when done.

To configure manually instead, copy the documented starter template and edit it:

```bash
sudo install -Dm600 /usr/share/sysforge/bootstrap.toml.example /etc/sysforge/bootstrap.toml
sudo vim /etc/sysforge/bootstrap.toml
```

### 3. Configure packages.toml

Edit `/etc/sysforge/packages.toml` with the packages to install. Repo packages install via pacman; AUR packages are built with your compiler flag profile. Use `source = "local"` for hand-maintained PKGBUILDs. Include `sysforge` to install it in the target system:

```toml
[build]
pkgbuild_src_dir = "~/src"

[[package]]
name   = "sysforge"
source = "aur"

[[package]]
name   = "neovim-git"
source = "aur"
```

For a graphical desktop you don't have to enumerate every package: `sysforge packages add-group gnome` (also `kde`, `xfce`, `mate`, `cinnamon`, `lxqt`, `budgie`, `cosmic`) writes a curated desktop group — its session, display manager, and portals. Installing the group also enables the display manager so you boot straight to a graphical login. A fresh install offers the same choice interactively, or reads it from `bootstrap.toml [desktop] environment`.

A custom kernel goes in `kernel.toml` (not `packages.toml`) and a custom LLVM toolchain in `toolchain.toml` — the kernel and toolchain stages own those packages, and `sysforge update` skips stage-owned packages by default. Build/IO throttling, `PACKAGER`/`MAKEFLAGS` defaults, and per-profile knobs are documented inline in the shipped configs and in [DESIGN.md](DESIGN.md).

`packages.toml [build]` also carries per-flag defaults for `sysforge build` — `abi_check`, `cache_report`, `persist_log` — so you don't have to pass `--abi-check`/`--cache-report`/`--persist-log` every run; the CLI flag still wins if given. Similarly, `sysforge.toml [update]` carries `rebuild_on_drift` (+ the per-axis `rebuild_on_toolchain_drift`/`rebuild_on_flag_drift`) so `sysforge update` can default to rebuilding drifted packages without passing `--rebuild-on-drift` each time.

### 4. Run the bootstrap pipeline

```bash
# --state-dir writes into the target so checkpoint state survives the reboot
sysforge run pipeline --state-dir /mnt/var/lib/sysforge
```

This runs stages 1–4: partition the disk, `pacstrap` the base system, detect hardware, and configure the installed system (hostname, locale, timezone, mirrorlist, bootloader, services, primary user, and sysforge itself). The pipeline checkpoints after each stage; a failure resumes with `--resume`.

### 5. Reboot and continue

When the configure stage completes, the pipeline prints the resume command and exits cleanly. Reboot, log in, and reinstall SysForge on the new system:

```bash
reboot
# then, on the installed system:
pacman -Sy --needed git base-devel uv python-pip
git clone https://aur.archlinux.org/sysforge.git
cd sysforge && makepkg -si && cd ~
sysforge run pipeline --resume
```

This runs stages 5–8:

- **reconfigure** — pre-build checks: disk space, network, config review.
- **toolchain** — *(opt-in)* builds the LLVM toolchain via the PGO bootstrap, behind three safety gates that keep the live `/usr` toolchain from ever being left broken (pre-build preflight, pre-install ABI audit, post-install verify with auto-restore). Register-only on the default gcc path. See [DESIGN.md](DESIGN.md) §Toolchain stage.
- **packages** — builds and installs everything in `packages.toml` with profiled flags. Optionally trims mesa's GPU drivers to your hardware (`[mesa] filter_drivers`).
- **kernel** — *(opt-in)* builds a custom kernel with boot-safety gates (won't leave the system unbootable). Interactive by default; compiler independent of the toolchain stage. See [DESIGN.md](DESIGN.md) §Kernel stage.

If a stage 5–8 run is interrupted, resume it with `sysforge run pipeline --resume`.

---

## Tab completion

```bash
# zsh — permanent
sudo cp completions/_sysforge /usr/share/zsh/site-functions/

# bash — permanent
sudo cp completions/sysforge.bash /usr/share/bash-completion/completions/sysforge
```

For the current session only, from the repo root: `fpath=($(pwd)/completions $fpath) && compinit` (zsh) or `source completions/sysforge.bash` (bash). Both complete subcommands, flags, and package names.

---

## Verifying releases

Releases are GPG-signed end to end with the maintainer key: the release commit, the git tag, and the source tarball. The AUR `sysforge` package verifies the tarball signature automatically at install time via `validpgpkeys`.

To verify by hand, import the maintainer key (fingerprint in `keys/sysforge.asc`) and check the tag and tarball:

```bash
gpg --import keys/sysforge.asc
git tag -v vX.Y.Z
gpg --verify sysforge-X.Y.Z.tar.gz.asc sysforge-X.Y.Z.tar.gz
```

Signing applies from the first release cut after this feature landed; earlier releases were unsigned. See [SECURITY.md](SECURITY.md) for the disclosure policy.

---

## Contributing

Issues are the best channel for bug reports and feedback; pull requests are welcome too. See [CONTRIBUTING.md](CONTRIBUTING.md) for how the project is developed and what a good PR looks like.

---

## Standards & compliance

SysForge follows established Linux/Python ecosystem conventions: config/cache/state under the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/), system paths per the FHS, a POSIX/GNU CLI that honours `NO_COLOR`/`FORCE_COLOR` and splits stderr from stdout, [SemVer](https://semver.org/) releases with [Keep a Changelog](https://keepachangelog.com/) notes, and a [REUSE](https://reuse.software/)/SPDX-annotated tree (MIT).

The full list — each standard with its scope, status, and enforcement — is in [DESIGN.md](DESIGN.md) (§Standards & Specifications). A release gate (`make check-standards` plus behavioural tests) verifies the checkable subset before every release.

---

## License

MIT. See [LICENSE](LICENSE).
