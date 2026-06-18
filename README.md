# SysForge

SysForge is an Arch Linux build and maintenance suite with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles — every AUR package is built with `-march=native`, LTO, or whatever profile matches its PKGBUILD metadata. Pacman owns the package database; SysForge owns the build configuration layer above it.

The default build profile uses the system gcc; LLVM (clang/lld) is fully supported but opt-in — install the LLVM `optdepends` (`clang`, `lld`, `llvm`, `compiler-rt`) and set `CC=clang`/`CXX=clang++` in a user profile, or use `sysforge run toolchain --compiler=llvm`.

**Commands:** `build` / `fetch` / `update` / `resolve` build and maintain profiled AUR & custom packages; `packages` / `state` manage the manifest and build state; `doctor` / `log` / `env` inspect system health and configuration; `setup` wires up pacman integration; `run <stage>` drives the bootstrap pipeline (plus the opt-in `toolchain` and `kernel` stages). See `sysforge --help` or the [man page](man/sysforge.1) for the full reference.

<sub><!--version-->v1.2.0<!--/version--></sub>

---

## About

SysForge started as a tool to streamline its author's own Arch Linux build and maintenance workflow on a daily-driver machine, and grew into a general-purpose suite. The codebase is developed essentially end-to-end with Claude Code, but it is deliberately structured to stay hand-editable: the modular design docs under `docs/design/` are the source of truth, every module has a documented owner and API, and the full test suite gates every change. Feedback of any kind is welcome via GitHub issues, and contributions are accepted — see [Contributing](#contributing).

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

# 3. Build and install an AUR package with your active profile. Dependencies
#    are handled the same way `sysforge update` does: missing repo deps are
#    installed up front and AUR/local deps are built first, so makepkg never
#    tries to `pacman -S` an AUR-only dependency.
sysforge build neovim-git -m "-si"

# 4. Check for and rebuild any outdated installed AUR packages
#    (Source sync is RPC-first: one batched AUR `info` call, and a git
#     fetch per package only when the cached version/LastModified differs
#     from the local HEAD. Steady-state runs do zero git fetches. A clean
#     clone is fast-forwarded — or, if upstream rewrote history, reset — to
#     track upstream automatically, so routine updates never need --cleansrc.)
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

# 6c. Performance introspection (global flags, position independent).
#     --timings prints a wall-clock phase report (source sync, version check,
#     drift detection, per-package builds, install, pacman -Syu) after build
#     and update runs. --py-profile runs the verb under Python's cProfile and
#     prints the top functions to stderr at exit; --py-profile-out FILE also
#     dumps raw stats for pstats/snakeviz.
sysforge --timings update
sysforge --py-profile update --dry-run

# 6d. Colour control (global flag, position independent). --color=auto (the
#     default) colours when writing to a terminal and honours NO_COLOR /
#     FORCE_COLOR; --color=always forces colour on (e.g. piping into a pager);
#     --color=never disables it. Persist a default with [ui] color in
#     sysforge.toml; the flag overrides it.
sysforge --color=always doctor | less -R

# 7. Heavy escape hatch: purge and re-clone every AUR package from scratch
#    (mainly for a corrupted local tree — routine updates auto-track upstream,
#    see step 4). --cleansrc refuses any clone that has real local work
#    (uncommitted non-pkgver edits, unpushed commits, or no upstream) — those
#    packages are reported as failed and the run continues. It also bypasses
#    the RPC short-circuit. makepkg's pkgver() auto-bump of PKGBUILD/.SRCINFO
#    on -git packages is treated as mechanical churn and does not block it.
sysforge update --cleansrc

# 8. Install already-built artifacts from PKGDEST without re-running makepkg.
#    Useful when a previous update was interrupted between build and install,
#    or after a manual makepkg run. Implies --offline.
sysforge update --install-only

# 8b. LLVM safety pre-flight. fetch / update / build surface
#     each LLVM-toolchain pkgbase in scope (variant, origin, dirty/diverged
#     state, install version, resolved build_mode) before acting; suppress
#     with --no-llvm-preflight or [safety] llvm_preflight = false in
#     sysforge.toml. `sysforge run toolchain` runs the same pre-flight in
#     strict mode and refuses on dirty or diverged trees; bypass per-run
#     with --allow-dirty-llvm. PGO profdata version mismatches are never
#     bypassable.
#
#     `run toolchain` auto-downloads any missing toolchain PKGBUILDs through
#     the SourceSyncScheduler and refreshes existing trees against upstream
#     (gated by --no-update). The PGO sub-flow is fragile, so it prompts at
#     four decision points (profdata reuse, staging/pgo_store purge, 4-pass
#     start, suspicious profdata size); pass --auto-pgo to bypass the prompts
#     for unattended runs.
sysforge run toolchain --allow-dirty-llvm

# 8c. Toolchain pre-flight. `sysforge update` probes rust / cmake / meson
#     availability before the build loop, and for lib32-* packages with
#     `rust` in consumes also probes the rustup i686-unknown-linux-gnu cross
#     target. When a probe fails interactively, the matching fix command
#     (e.g. `rustup target add --toolchain stable i686-unknown-linux-gnu`)
#     is offered for one-keystroke remediation; non-interactive runs print
#     the fix and abort. Suppress with --no-toolchain-preflight. On a build
#     failure, side-car logs (meson, cargo) are also scanned for known
#     signatures so the next-step fix is surfaced inline with the failure.

# 9. Manage packages.toml entries (install list during pipeline bootstrap;
#    build-rule overrides at steady-state — see DESIGN.md §Package Manifest)
sysforge packages list
sysforge packages list --orphans     # entries whose package isn't installed
sysforge packages add mesa-git --pkgbuild-patch
sysforge packages add-group gnome    # write a curated desktop group (gnome | kde)
sysforge packages remove mesa-git

# 9b. Inspect / repair build_state.toml (the live install-state mirror)
sysforge state list                  # paginated by default (TTY); --no-pager
sysforge state list --no-pager       # raw output, no $PAGER pipe
sysforge state repair --dry-run      # preview fixes for legacy broken entries
sysforge state orphans               # superseded .pkg.tar* in PKGDEST (safe to delete)
sysforge state orphans --prune       # delete them after y/N confirmation
sysforge state failed                # packages whose last build failed, with any diagnosed fix
sysforge state failed --clear PKG    # forget one failure (also: --clear-all)

# 10. Page sysforge logs through $PAGER (default less -RFX).
sysforge log                         # unified log: <state_dir>/sysforge.log
sysforge log linux-custom            # per-package log: <pkgbuild_src_dir>/linux-custom/sysforge_linux-custom.log
sysforge log --no-pager              # raw output, no $PAGER pipe

# 11. Health-check an installed package's depends + linkage (e.g. when Steam
#     launches as a black window and the graphics stack may be out of sync).
#     Walks the target's dep closure; --shallow restricts to direct depends.
sysforge doctor mesa-git
sysforge doctor --graphics            # curated stack + system-state probes
                                      # (nvidia-drm modeset, driver version skew,
                                      #  Wayland explicit-sync, Steam GPU accel, …)
sysforge doctor --hardware            # inventory all PCI/USB devices, flag any with
                                      # no driver bound, and audit the running kernel's
                                      # .config for missing-driver / boot-config gaps
                                      # (runnable on its own, no package needed)
sysforge doctor --toolchain           # flag when toolchain.toml asks for a custom LLVM
                                      # toolchain but stock repo LLVM is installed (or
                                      # PGO profdata is version-skewed); runs on its own
sysforge doctor                       # bare: run EVERY system axis (the fast full sweep) —
                                      # toolchain, hardware, graphics, pacman, state,
                                      # boot, services; no per-package walk
sysforge doctor --pacman              # local pacman-db consistency, stale lock, unmerged
                                      # .pacnew/.pacsave, orphans (read-only, never syncs)
sysforge doctor --state               # sysforge state: recorded build failures, an
                                      # interrupted stage sentinel, build_state drift
sysforge doctor --boot                # running-system boot readiness: kernel artifacts,
                                      # recovery fallback, /boot space, DKMS
sysforge doctor --services            # failed systemd units + firmware a driver could
                                      # not load this boot
sysforge doctor --all                 # every system axis PLUS every installed package
                                      # (foreign + non-foreign)
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

Edit `/etc/sysforge/packages.toml` with the packages to install. Repo packages install via pacman; AUR packages are built with your compiler flag profile. Use `source = "local"` for hand-maintained PKGBUILDs with no upstream remote to sync from. Include `sysforge` if you want it installed in the target system:

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

For a graphical desktop, you don't have to enumerate every package: `sysforge packages add-group gnome` (or `kde`) writes a curated desktop group. During a fresh install the configure stage offers the same choice interactively, or reads it from `bootstrap.toml [desktop] environment` for unattended installs.

A fresh `/etc/makepkg.conf` ships `PACKAGER="Unknown Packager"` and builds single-threaded. The `sysforge reconfigure` makepkg step offers to set `PACKAGER` (defaulting to your git identity) and `MAKEFLAGS=-j$(nproc)`; for unattended installs, set them in `bootstrap.toml [makepkg]` (`packager`, `makeflags`) and the configure stage writes them into the target `/etc/makepkg.conf`.

For a custom kernel like `linux-sysforge` (the shipped default name), configure it in `kernel.toml` instead of `packages.toml` — the kernel stage owns its lifecycle, and `sysforge update` will skip kernel-stage packages by default (use `--include-stage-owned` to override or just name them on the command line).

The same applies to a custom LLVM toolchain: when `toolchain.toml` is enabled with `compiler = "llvm"`, the toolchain stage owns the LLVM suite (`llvm`, `clang`, `lld`, `compiler-rt`, …), and `sysforge update` skips those packages by default — rebuild them with `sysforge run toolchain` (or override with `--include-stage-owned` / by naming the package).

### 4. Run the bootstrap pipeline

```bash
# --state-dir writes into the target so checkpoint state survives the reboot
sysforge run pipeline --state-dir /mnt/var/lib/sysforge
```

This runs stages 1–4: partition the disk, `pacstrap` the base system, detect hardware, and configure the installed system (hostname, locale, timezone, mirrorlist, pacman db refresh, systemd-boot bootloader, NetworkManager + sshd service enables, primary user with sudo, passwords, and sysforge itself — built from the ISO source with `makepkg` in the chroot and installed via pacman so the install is tracked from the start). The pipeline saves a checkpoint after each stage — a failure can be resumed with `--resume`.

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
pacman -Sy --needed git base-devel uv python-pip
git clone https://aur.archlinux.org/sysforge.git
cd sysforge && makepkg -si && cd ~
```

### 6. Continue the pipeline

```bash
sysforge run pipeline --resume
```

This runs stages 5–8:

- **reconfigure** — pre-build checks: disk space, network, config review
- **toolchain** — *(opt-in)* builds the LLVM toolchain via the 4-pass PGO bootstrap (reuses existing profdata when compatible; `--rebuild-profdata` forces a full 4-pass). When `compiler` is unset or `"gcc"` the stage is register-only — it writes the system `/usr/bin/gcc` paths into pipeline state without building, since stock gcc/gcc-libs from `base-devel` are already correct. Skipped cleanly if `toolchain.toml` has `enabled = false` (the default). The LLVM path runs three safety gates that keep the live `/usr` toolchain from ever being left broken: a pre-build preflight (Gate 1 — aborts before any build time on a PKGBUILD pkgver skew, a non-functional clang/lld, too little build-filesystem space, or `[multilib]` disabled while a `lib32-*` is in scope; override with `--allow-version-skew` / `--skip-build-space-check` / `require_multilib = false`, or `min_build_free_gb` to tune the space bar), a pre-install ABI-hazard audit of the built packages (Gate 2), and a post-install verify (Gate 3) that **auto-restores the previous toolchain from the pacman cache** if the freshly-built one fails to verify. The build is held back from `/usr` until Gate 2 passes, so an aborted or doomed build installs nothing. When building a libLLVM whose **version bumps the soname** (e.g. `libLLVM.so.22.1` → `23.0`), a pre-build soname gate warns up front and lists the installed packages that link the old soname (mesa's GPU drivers, etc.) — `--rebuild-soname-consumers` / `toolchain.toml rebuild_soname_consumers` (`prompt` default, `auto`, `off`) controls whether sysforge prompts and then **rebuilds those consumers automatically** after the toolchain install, so there's no post-install surprise that the new libLLVM broke other packages. A same-version PGO rebuild never changes the soname, so this stays silent in the common case.
- **packages** — builds and installs everything in `packages.toml` with profiled flags
- **kernel** — *(opt-in)* builds a custom kernel (skipped cleanly if `kernel.toml` is absent or `enabled = false`). `sysforge run kernel` is interactive by default (the PKGBUILD's `make nconfig`/`menuconfig` runs as written); pass `--non-interactive` for unattended runs. Compiler is independent of the toolchain stage — `kernel.toml compiler = "llvm"` or `--compiler llvm` builds the kernel with LLVM even on a gcc system. Bootloader is selectable via `kernel.toml bootloader` (`systemd-boot` default, `grub`, or `none`) or `--bootloader`. Source refresh routes through the source-sync scheduler (`--cleansrc` / `--cleansrc-force`); if the kernel source has diverged from upstream (local commits or a dirty tree), an interactive run prompts before building and an unattended run aborts. The build's base `.config` is selectable via `kernel.toml base_config` (`pkgbuild` default, `running` to seed from the running kernel, or a path); the `[[kconfig]]` fragment is overlaid on top. The resolved `.config` is installed (pacman-tracked) as `/boot/config-<release>` when the PKGBUILD doesn't already ship one. If the kernel `pkgname` collides with an official repo package, sysforge warns and prompts before overwriting it. The hardware stage automatically writes `# CONFIG_<other-arch> is not set` lines for every kernel architecture domain that isn't the host's (ARM/AArch64/RISC-V/PowerPC/MIPS/SPARC/LoongArch top-level keys plus curated SoC family umbrellas), culling unreachable subtrees from the kconfig menu — re-enable any specific key via `kernel.toml [[kconfig]]` if needed. It also enables modular drivers (`=m`) for the PCI/USB devices actually present on the machine; coverage widens automatically after the first kernel build (the build's own source tree is parsed and cached), and the behavior can be turned off with `kernel.toml device_kconfig = false`.

  **Boot safety:** the kernel stage will not leave the system unbootable. It refuses to install a custom kernel when no fallback kernel exists (override `--allow-no-fallback`), audits the built `.config` *before* installing and aborts if it dropped the root filesystem / storage controller / core boot infrastructure (override `--skip-boot-audit`), and after install verifies the kernel + initramfs landed and are referenced by a boot entry (and that DKMS modules like `nvidia-open-dkms` rebuilt). Tune via `kernel.toml` (`require_fallback_kernel`, `boot_audit`, `min_boot_free_mb`, `capture_lsmod_snapshot`).

If the stage 5–8 run is interrupted, resume it:

```bash
sysforge run pipeline --resume
```

---

## Tab completion

**zsh:**

```bash
# Permanent
sudo cp completions/_sysforge /usr/share/zsh/site-functions/

# Current session only (from repo root)
fpath=($(pwd)/completions $fpath) && compinit
```

**bash:**

```bash
# Permanent
sudo cp completions/sysforge.bash /usr/share/bash-completion/completions/sysforge

# Current session only (from repo root)
source completions/sysforge.bash
```

Both complete subcommands, all flags, and package names (local `pkgbuild_src_dir` + pacman sync DB + AUR cache if present at `~/.cache/sysforge/aur-packages.txt`).

---

## Contributing

Issues are the best channel for bug reports and feedback; pull requests are welcome too. See [CONTRIBUTING.md](CONTRIBUTING.md) for how the project is developed and what a good PR looks like.

---

## Standards & compliance

SysForge follows the established Linux/Python ecosystem conventions rather than inventing its own. Its own files live where the specs say they should: configuration under `$XDG_CONFIG_HOME` (default `~/.config/sysforge`), cache under `$XDG_CACHE_HOME` (`~/.cache/sysforge`), and state under `$XDG_STATE_HOME` (`~/.local/state/sysforge`), per the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/); system paths follow the FHS. The CLI follows POSIX/GNU conventions, honours `NO_COLOR`/`FORCE_COLOR`, splits diagnostics (stderr) from output (stdout), and emits RFC 3339 timestamps. Releases are [SemVer](https://semver.org/), notes follow [Keep a Changelog](https://keepachangelog.com/), and the tree is [REUSE](https://reuse.software/)/SPDX-annotated (MIT).

The full list — each standard with its scope, status, and how it's enforced — is in [DESIGN.md](DESIGN.md) (§Standards & Specifications). A release gate (`make check-standards` plus behavioural tests) verifies the checkable subset before every release.

---

## License

MIT. See [LICENSE](LICENSE).
