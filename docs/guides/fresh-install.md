# Fresh Install from the Arch ISO

The full path from booting the Arch install ISO to a working system with all packages built and
installed. On a system that is already running, [Getting started](getting-started.md) is all you
need.

---

## 1. Connect to the internet

```bash
ip link                                   # wired: usually automatic
iwctl station wlan0 connect "SSID"        # wireless
```

## 2. Install SysForge and configure bootstrap.toml

Run the setup script directly from the repo:

```bash
# Latest stable AUR sysforge (add --git to track main via sysforge-git):
bash <(curl -sL https://raw.githubusercontent.com/KeithRaghubar/sysforge/main/tools/iso-install.sh)
```

The script checks connectivity, installs SysForge from the AUR, and prompts for all required
bootstrap values (device, filesystem, hostname, locale, timezone, username, passwords). It writes
a complete `bootstrap.toml` and prints the next command when done.

To configure manually instead, copy the documented starter template and edit it:

```bash
sudo install -Dm600 /usr/share/sysforge/bootstrap.toml.example /etc/sysforge/bootstrap.toml
sudo vim /etc/sysforge/bootstrap.toml
```

## 3. Configure packages.toml

Edit `/etc/sysforge/packages.toml` with the packages to install. Repo packages install via
pacman; AUR packages are built with your compiler flag profile. Use `source = "local"` for
hand-maintained PKGBUILDs. Include `sysforge` to install it in the target system:

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

For a graphical desktop you don't have to enumerate every package: `sysforge packages add-group
gnome` (also `kde`, `xfce`, `mate`, `cinnamon`, `lxqt`, `budgie`, `cosmic`) writes a curated
desktop group: its session, display manager, and portals. Installing the group also enables the
display manager so you boot straight to a graphical login. A fresh install offers the same choice
interactively, or reads it from `bootstrap.toml [desktop] environment`.

A custom kernel goes in `kernel.toml` (not `packages.toml`) and a custom LLVM toolchain in
`toolchain.toml`, because the kernel and toolchain stages own those packages, and
`sysforge update` skips stage-owned packages by default. Build/IO/memory throttling (including a
per-build `mem_limit` ceiling), `PACKAGER`/`MAKEFLAGS` defaults, and per-profile knobs are
documented inline in the shipped configs and in [DESIGN.md](../../DESIGN.md).

`packages.toml [build]` also carries per-flag defaults for `sysforge build` (`abi_check`,
`cache_report`, `persist_log`), so you don't have to pass `--abi-check`/`--cache-report`/
`--persist-log` every run; the CLI flag still wins if given. Similarly, `sysforge.toml [update]`
carries `rebuild_on_drift` (+ the per-axis `rebuild_on_toolchain_drift`/`rebuild_on_flag_drift`)
so `sysforge update` can default to rebuilding drifted packages without passing
`--rebuild-on-drift` each time. `packages.toml [build] system_upgrade = true` does the same for
`--sysupgrade`, the trailing `pacman -Syu` that refreshes your repo packages after the source
builds are installed (`--no-sysupgrade` opts out for one run). It runs unattended with
`--noconfirm` (a package conflict still cancels it rather than removing anything unasked); add
`--interactive` to answer pacman's prompts yourself. That upgrade is summarized
package-by-package at the end of the run. The version changes are read back off the local
package DB, so the summary reports what pacman actually did; the list is capped by default and
printed in full under `-v`, and `--no-sysupgrade-report` drops it.

## 4. Run the bootstrap pipeline

```bash
# --state-dir writes into the target so checkpoint state survives the reboot
sysforge run pipeline --state-dir /mnt/var/lib/sysforge
```

This runs stages 1–3: **install** (disk partition/format/mount, base install, bootloader, users,
services, and system identity, all via archinstall from a generated config), detect hardware, and
**configure** the installed system with sysforge-specific tuning (mirrorlist, `makepkg.conf`,
desktop group, and sysforge itself). The pipeline checkpoints after each stage; a failure resumes
with `--resume`.

## 5. Reboot and continue

When the configure stage completes, the pipeline prints the resume command and exits cleanly.
Reboot, log in, and reinstall SysForge on the new system:

```bash
reboot
# then, on the installed system:
pacman -Sy --needed git base-devel uv python-pip
git clone https://aur.archlinux.org/sysforge.git
cd sysforge && makepkg -si && cd ~
sysforge run pipeline --resume
```

This runs stages 4–7:

- **reconfigure:** pre-build checks (disk space, network, config review).
- **toolchain:** *(opt-in)* builds the LLVM toolchain via the PGO bootstrap, behind three safety
  gates that keep the live `/usr` toolchain from ever being left broken (pre-build preflight,
  pre-install ABI audit, post-install verify with auto-restore). Register-only on the default gcc
  path. See [DESIGN.md](../../DESIGN.md) §Toolchain stage.
- **packages:** builds and installs everything in `packages.toml` with profiled flags. Optionally
  trims mesa's GPU drivers to your hardware (`[mesa] filter_drivers`).
- **kernel:** *(opt-in)* builds a custom kernel with boot-safety gates (won't leave the system
  unbootable). Interactive by default; compiler independent of the toolchain stage. On
  systemd-boot it also writes and maintains the kernel's loader entry (your own entries are never
  modified; `sysforge state boot-entries` lists them). See [DESIGN.md](../../DESIGN.md) §Kernel
  stage, the [kernel guide](kernel.md) and the [boot entries guide](boot-entries.md).

If a stage 4–7 run is interrupted, resume it with `sysforge run pipeline --resume`.
