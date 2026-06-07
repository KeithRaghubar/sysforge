## Pipeline Layer

Python DAG orchestrator with checkpoint/resume. Stages run in order:

1. **partition** — fully implemented (GPT, ESP + root, mkfs, mount)
2. **base_install** — fully implemented (pacstrap minimal base, genfstab)
3. **hardware** — fully implemented (CPU/GPU/NVMe detection → hardware_profile.toml)
4. **configure** — fully implemented (hostname, locale, timezone, mirrorlist, systemd-boot, user creation + sudo, sshd config, shell dotfiles, passwords via arch-chroot)
5. **reconfigure** — fully implemented (pre-build checkpoint: config review, disk/network/gpg checks, build preview)
6. **toolchain** — fully implemented (LLVM/GCC, optional 4-pass PGO bootstrap, compiler propagation to packages/kernel)
7. **packages** — fully implemented
8. **kernel** — fully implemented

Stages 1–4 are **bootstrap-only** — they run once from a live install environment. Stages 5–8 are **repeatable** and run on the installed system. Use `sysforge run pipeline --start-from reconfigure` to run the pre-build checkpoint on a live system; use `--start-from packages` to skip straight to builds. Stages 5–8 are also available as standalone `sysforge run <stage>` commands for repeated, out-of-pipeline use (e.g. `sysforge run packages`). The toolchain (6) and kernel (8) stages default to `enabled = false` because building a custom toolchain or kernel is an opt-in decision; users who want the stock system compiler and pacman kernel leave them disabled.

### Bootstrap workflow (stages 1–4)

Stages 1–4 run from a live Arch install environment (booted from the install ISO). The state dir must be set to the target system so pipeline state persists across the reboot:

```bash
# From the live environment — iso-install.sh sets this up automatically
sysforge run pipeline --state-dir /mnt/var/lib/sysforge
```

When stage 4 (configure) completes, the reconfigure stage detects it is running on the live ISO (via `/run/archiso`) and raises `BootstrapRebootRequired`. The runner catches this as a clean stop (exit 0), saves state, and prints the resume command. After rebooting into the installed system:

```bash
sysforge run pipeline --resume
```

**`iso-install.sh`** (`tools/iso-install.sh`) automates the live-ISO setup steps: checks connectivity, installs sysforge from the AUR (`sysforge` by default; pass `--git` to install `sysforge-git` instead), and prompts for all required bootstrap values with validation (timezone checked against `/usr/share/zoneinfo/`, passwords entered silently with confirmation). Writes a complete `bootstrap.toml` and prints the pipeline command when done. Builds the AUR package as a temporary unprivileged user (`aurbuild`) since `makepkg` refuses to run as root; the user and its sudoers drop-in are removed on exit.

**`bootstrap.toml`** (`/etc/sysforge/bootstrap.toml`) configures stages 1–4. The package does not install this file directly — it ships a starter template at `/usr/share/sysforge/bootstrap.toml.example`. `iso-install.sh` writes the live file from interactive prompts; for hand-edit setups, copy the example to `/etc/sysforge/` first.

```toml
target = "/mnt"          # mount point for the new system

[partition]
device       = "/dev/sda"   # required — block device to wipe and partition
esp_size_mib = 512          # EFI System Partition size in MiB (default: 512)
root_fs      = "ext4"       # "ext4" | "btrfs" (default: "ext4")

[system]
hostname           = "archlinux"    # required
locale             = "en_US.UTF-8" # required
timezone           = "UTC"          # required
keymap             = "us"           # optional (default: "us")
parallel_downloads = 5              # pacman ParallelDownloads (default: 5)
root_password      = "secret"       # optional — set via chpasswd; warn if absent
username           = "builder"      # optional — primary user (default: "builder")
user_password      = "secret"       # optional — user password; warn if absent

[mirror]
countries = ["Canada"]  # reflector --country (optional)
protocol  = "https"
age       = 12                 # reflector --latest N hours
```

**Configure stage (stage 4)** runs all one-time system identity steps inside `arch-chroot`:
- Hostname (`/etc/hostname`), locale (`locale-gen`), timezone (`ln -sf /usr/share/zoneinfo/...`), keymap (`/etc/vconsole.conf`), `ParallelDownloads` in `pacman.conf`
- Reflector mirrorlist (skipped gracefully if `reflector` absent in chroot)
- systemd-boot: `bootctl install`, `loader.conf`, `entries/arch.conf` (uses `root=LABEL=root`)
- `systemctl enable NetworkManager` + `systemctl enable sshd`
- `PermitRootLogin yes` in `/etc/ssh/sshd_config`
- `useradd -m -G wheel <username>` + `/etc/sudoers.d/wheel` drop-in
- Shell dotfiles: `.bashrc` + `.zshrc` for root (red prompt) and primary user (green prompt)
- Root and user passwords via `chpasswd` (warns if absent from bootstrap.toml)
- sysforge install in target via `makepkg -si` from the source tree's PKGBUILD, run as the build user with a temporary `NOPASSWD` sudoers drop-in (removed after install). The configure stage stages the source as `sysforge-$pkgver.tar.gz` so makepkg uses the local copy instead of fetching, runs with `--skipchecksums --skipinteg` since the tarball is locally produced, and ends with sysforge owned by pacman (`pacman -Q sysforge`). This replaces the earlier `uv pip install --system` path, which left files unowned and forced `pacman -U --overwrite='*'` on the first AUR-driven update.

The hardware stage (stage 3) needs no config — it auto-detects and writes `hardware_profile.toml` to `state_dir`. After reboot the file is at its natural path (`/var/lib/sysforge/hardware_profile.toml`) and the kernel stage picks it up automatically.

**Full device inventory.** Beyond the scalar CPU/GPU/NVMe summary, the stage enumerates every PCI and USB device via `primitives/device_probe.enumerate_devices()` and appends a `[[devices]]` array-of-tables to `hardware_profile.toml` (bus, address, modalias, class, description, bound driver, expected modules, suggested `CONFIG_*`). The device→module link is resolved against a complete **reference kernel**'s `modules.alias` (newest installed stock kernel, excluding any `custom` modules dir) — a custom kernel that omitted a driver can't resolve the modalias it lacks, so resolving against the reference surfaces the gap. Any present, functional device with no driver bound is WARNed at the stage and pointed at `sysforge doctor --hardware`. The `[[devices]]` block is emitted after the scalar `[hardware]`/`[kconfig]` tables; existing readers (`tomllib`) are unaffected.

### Runner

`run_pipeline(config, options, stages)` sequences stage execution:
- Validates `depends_on` references before running
- Reads checkpoint state to determine start index
- Calls `stage.run()`, marks done/failed, saves state after each stage
- On `NotImplementedError`: prints `--start-from` guidance and exits
- On `BootstrapRebootRequired`: saves state, prints reboot + resume instructions, exits 0 (clean stop, not failure)
- On `RuntimeError`: saves state and exits with resume instructions
- `--dry-run`: logs what would run without calling `stage.run()`

Guard against accidental state clobber: if a state file exists and neither `--resume` nor `--start-from` is passed, the runner exits with instructions rather than overwriting. Both flags are supported on `sysforge run pipeline`.

**User-facing output.** The runner emits a welcome banner (sysforge version + ordered stage chain) and a status snapshot (`✓ done`, `▸ running`, `· pending`, `↳ skipped_to`) before the loop, a stage banner before each stage (`[N/M] name` between two `═` rules), a `✓ name complete` line after each stage, and a closing rule on success. All of this routes through `log.ui` so it reaches both stderr and the unified log regardless of `-v` level. Visual primitives live in `sysforge/ui/headers.py` and share the `═` rule + bold-cyan style with `tools/iso-install.sh` (parallel `_double_rule` / `_step` / `_field` helpers in shell). Step counters are 1-based against the full stage list, so `--start-from configure` shows `[4/8]`, not `[1/…]`.

### Checkpoint state

`pipeline_state.toml` is the authoritative checkpoint record. Written atomically (write-then-rename) after every state transition. Human-readable TOML for manual recovery.

Per-stage status: `pending` → `running` → `done` / `failed` / `skipped_to`

Intra-stage package progress (packages stage only):

```toml
[stages.packages.progress]
built     = ["llvm", "clang", "lld"]
failed    = ["mesa-git"]
skipped   = []
remaining = ["cosmic-comp-git", "cosmic-panel-git"]
```

On resume with failed packages, the user is prompted to retry or skip each (or `--force-retry` bypasses the prompt).

### Kernel stage (stage 8)

Builds a custom kernel from a PKGBUILD. The stage is a clean no-op if `/etc/sysforge/kernel.toml` is absent or has `enabled = false`, so systems using a stock pacman kernel skip it without needing `--start-from`. Opt-in by design — users who want a stock kernel leave the stage disabled.

**`kernel.toml` structure:**

```toml
pkgname          = "linux-sysforge"  # shipped default; must match the PKGBUILD pkgbase
pkgbuild_src_dir = "~/src"       # parent dir; PKGBUILD is at <pkgbuild_src_dir>/<srcdir>/PKGBUILD
srcdir           = "linux"       # source directory name if different from pkgname (optional)
bootloader       = "systemd-boot"    # systemd-boot | grub | none  (default: systemd-boot)
interactive      = true              # default: true — interactive kconfig (make nconfig)
compiler         = "llvm"            # "gcc" | "llvm" — kernel-stage compiler (optional)
base_config      = "pkgbuild"        # "pkgbuild" (default) | "running" | <path> — base .config source
source           = "local"           # "local" (default) | "aur" | "git"
                                     # "local" = hand-maintained PKGBUILD, no remote sync.
                                     # "aur"/"git" = PKGBUILD is a clone of an AUR/git remote.

# Boot safety (defaults shown; see §Kernel stage boot-safety):
require_fallback_kernel = true       # refuse to install a custom kernel as the only kernel
boot_audit              = true       # run /boot-space + pre-install resolved-.config audit
min_boot_free_mb        = 200        # minimum free MiB on /boot before building
capture_lsmod_snapshot  = true       # capture lsmod for `make localmodconfig`

[[kconfig]]                      # manual kconfig overrides (optional, repeatable)
option = "CONFIG_HZ_1000"        # must match CONFIG_[A-Z0-9_]+
value  = "y"                     # y | m | n | non-empty string
```

`srcdir` is needed when the PKGBUILD directory name differs from `pkgname` (e.g. `pkgname = "linux-sysforge"` but the repo is cloned as `~/builds/linux`). Defaults to `pkgname` if omitted.

**Kernel-stage compiler override:** `compiler = "gcc" | "llvm"` is independent of the toolchain stage. A system that keeps gcc system-wide can still build the kernel with LLVM (or vice versa). Resolution order: `--compiler` CLI flag > `kernel.toml compiler` > toolchain-stage pipeline state (cc/cxx set by stage 6) > profile defaults. When set to LLVM, the standard `LLVM=1 LLVM_IAS=1` env vars are injected by `makepkg_wrapper` automatically — no extra PKGBUILD changes needed. Note: `compiler = "llvm"` builds the kernel *with* clang but does **not** apply PGO profdata — the profdata trains the clang binary, not the linux target, so there is no kernel-PGO path here.

**Resolution summary.** After resolving compiler (+ its origin), variant, bootloader (+ whether the chosen one is detected installed), source, interactive mode, kconfig counts, and the boot-safety gate settings, the stage emits a single labelled "Kernel build plan:" block (`_log_resolution_summary`). It prints on every run (useful before a multi-hour build) and is the readable core of `--dry-run`, replacing decisions previously scattered across the log. The standalone interactive default also emits a one-line nudge pointing at `--non-interactive` for unattended runs.

**Variant-inheritance nudge.** When `compiler` is unset (neither CLI nor `kernel.toml`) and the toolchain-stage variant is `pgo_llvm`, the stage emits a WARN naming the inherited variant and recommending that the operator persist `compiler = "llvm"` in `kernel.toml` so the choice survives a future toolchain-stage disable (which clears `[stages.toolchain.result]`). `stock_llvm` gets the same nudge at INFO level. `gcc` and `system` variants are silent — gcc is the safe default and `system` means there's no opinion to project.

**Configured-vs-installed toolchain mismatch.** The variant nudge above reflects what the toolchain *stage* registered in pipeline state; this check reflects on-disk reality. When `toolchain.toml` requests a custom LLVM toolchain (`enabled = true`, `compiler = "llvm"`) but the installed LLVM is stock repo (`install_origin == "repo"` — a custom build is never in a sync DB) or its PGO profdata is version-skewed, the stage emits a WARN before the build. It uses `llvm_state.detect_toolchain_config_mismatch`, which is built strictly on `collect_llvm_state` (the sanctioned LLVM-inspection entry point) — this is **provenance reporting**, deliberately *not* a third toolchain *health* probe (those remain `_verify_llvm_install` and `toolchain_preflight._probe_cc`). The same detector backs `sysforge doctor --toolchain`. There is intentionally no persisted "toolchain is correct" flag: it would go stale the moment pacman replaces LLVM out-of-band, so the mismatch is computed on read from current install state.

**Per-kernel toolchain-drift check.** Stage entry compares the installed kernel's recorded `toolchain_variant` (from `build_state.toml`) against the active variant. On mismatch (e.g. installed kernel was built under `stock_llvm`, active is `pgo_llvm`), the stage emits a WARN before the build runs. This mirrors `sysforge update`'s drift sweep but covers the kernel package, which `update` excludes via the stage-ownership skip. Back-compat: no recorded variant → silent (older builds preceded the field).

**Bootloader-installed preflight.** Stage entry probes for systemd-boot (`/boot/loader/loader.conf`) and grub (`/boot/grub/grub.cfg`); falls back to `pacman -Qq systemd grub` when neither marker is present. When the resolved `bootloader` (≠ `none`) isn't in the detected set, a single non-fatal WARN surfaces the mismatch *before* the build runs — so a user on a grub-only system who left the default `systemd-boot` configured gets an early signal instead of a post-install `bootctl update` failure. False negatives on exotic setups (UKI, custom loaders) don't block the build; the post-install branch still tolerates the bootloader-update failure.

**Pkgname/pkgbase consistency check.** After the source sync, the stage static-parses the PKGBUILD via `parse_pkgbuild` and confirms the parsed `pkgbase` (or `pkgname` for non-split packages) matches `kernel.toml pkgname`. A typo or a cloned PKGBUILD whose `pkgbase` has drifted from the directory name raises a clear `RuntimeError` at stage entry instead of failing late at `makepkg --install` after a multi-hour build.

**Pkgname repo-collision check.** Immediately after the consistency check, the stage tests `kernel.toml pkgname` against the pacman sync DBs via `aur.is_repo_package` (one `pacman -Si`). A custom kernel should carry a unique name; if the name matches an official package (e.g. `linux`, `linux-lts`), building and installing it would overwrite the stock package on `pacman -U`. Interactive runs prompt for confirmation (`prompt_choice`, default no); unattended runs (`--non-interactive` or no TTY) abort; `--dry-run` warns without prompting.

**kconfig fragment:**

Hardware-driven kconfig entries come from `hardware_profile.toml [kconfig]` (emitted by the hardware stage). These include both positive `=y` enables (CPU/GPU/NVMe-driven) and architecture-disable `=n` umbrellas — when the host is x86_64, the hardware stage writes `# CONFIG_ARM64 is not set`, the same for RISC-V/PowerPC/MIPS top-level keys and a curated set of ARM64 SoC families, culling unreachable subtrees from `make nconfig`. See §Hardware Detection → *Architecture-aware kconfig disable* for the registry. Manual overrides from `kernel.toml [[kconfig]]` are merged on top — manual wins on conflict with a `[WARN]`, including for arch-disable entries (a cross-compile use case can re-enable `CONFIG_ARM64=y` per the override path). The combined result is written to `<pkgbuild_src_dir>/<srcdir>/sysforge.config` before `makepkg` runs. The PKGBUILD must merge this into its `.config`; a compatible PKGBUILD calls `scripts/kconfig/merge_config.sh` in `prepare()`.

Manual override validation: `option` must match `CONFIG_[A-Z0-9_]+`; `value` must be non-empty (`n` to disable); duplicates within `kernel.toml` are an error.

If neither source provides any kconfig entries, no fragment is written. The fragment is written *after* the source sync (so a `--cleansrc` re-clone doesn't wipe it) and *after* compiler resolution, so its banner carries a toolchain-provenance line (`# toolchain variant: <variant>  cc: <path>`) giving a `.config` diff between two builds a trail of which toolchain produced it.

**Base config (`base_config`):**

The fragment is an *overlay* — it does not define the build's starting `.config`. `base_config` selects that base: `"pkgbuild"` (default, no-op — the PKGBUILD provides its own base), `"running"` (the running kernel's config, read via `dep_analysis.read_running_kconfig_text` from `/proc/config.gz` then `/boot/config-$(uname -r)`), or a path to a `.config` file. For `"running"`/`<path>`, sysforge writes the resolved config to `<pkgbuild_src_dir>/<srcdir>/sysforge.base.config` before the build (dry-run aware). The cooperation contract mirrors the fragment: a compatible PKGBUILD's `prepare()` copies `sysforge.base.config` to `.config` (then runs `make olddefconfig`) **before** merging `sysforge.config`. sysforge never mutates tracked source files. A `"running"` source that resolves to nothing (no `/proc/config.gz`, no `/boot/config-*`) warns and falls back to the PKGBUILD base; an unknown non-path value raises. The resolved source appears in the "Kernel build plan:" summary (`base cfg:` line).

**lsmod snapshot:**

Before the build, `lsmod` output is captured to `<state_dir>/lsmod.snapshot` (unless `capture_lsmod_snapshot = false`). This lets the PKGBUILD run `make localmodconfig` reproducibly using a fixed module set from the running system rather than whatever is loaded at build time. `localmodconfig` strips drivers for hardware *not loaded at snapshot time* — Gate 1 warns about this and Gate 2 (below) is the backstop that catches a dropped root-path driver before install.

**Interactive kconfig (kernel-stage default):**

`sysforge run kernel` is interactive by default — the kernel stage passes `interactive=True` into `BuildOptions`, so `patch_noninteractive_kconfig` is skipped and the PKGBUILD's kconfig target (`make nconfig`/`menuconfig`/etc.) runs as written. The user reviews and edits the resolved config before the build proceeds. The default can be flipped via `kernel.toml interactive = false` or the `--non-interactive` CLI flag; both routes patch interactive targets (`oldconfig`, `nconfig`, `menuconfig`, `xconfig`, `gconfig`) to `make olddefconfig` for unattended runs. `olddefconfig` applies defaults for all new symbols without terminal interaction; VAR=val arguments before the target (e.g. `ARCH=x86_64`) and trailing comments are preserved. `--noconfirm` only controls makepkg's own prompts and has no effect on interactive make targets inside the PKGBUILD.

Note: when other verbs (`sysforge build`, `sysforge update`) build a kernel PKGBUILD with `build_mode = "kernel"` on the resolved profile, those paths still default to *non-interactive* — interactive-by-default is a kernel-stage-only contract because the stage is the user-driven kernel build entry point.

**Source sync via the scheduler:**

The kernel stage routes its source refresh through `source_sync.get_scheduler().request(SyncRequest(..., source=<kernel.toml source>))` ahead of the build, the same path as the toolchain stage. With the default `source = "local"`, the scheduler short-circuits (no RPC, no clone, no fetch) — only `--cleansrc` / `--cleansrc-force` would attempt a purge, but a hand-maintained tree has no remote to re-clone from, so users on the `local` path leave cleansrc unset. For `source = "aur"` / `"git"`, the normal sync runs: `--cleansrc` purges and re-clones (refusing on dirty/ahead/no-upstream clones); `--cleansrc-force` overrides that guard; cleansrc forces a sync even when `--no-update` is also set. `STATUS_FAILED` / `STATUS_RATE_LIMITED` / `STATUS_PURGE_REFUSED` raise.

`STATUS_DIVERGED` (upstream advanced but the local tree can't fast-forward — local commits or a dirty tree) gets stronger handling in the *kernel* stage than the plain warning the other verbs use, because building a kernel off stale or hand-edited source is exactly the easy-to-miss footgun. `_warn_and_confirm_diverged` enriches the WARN with ahead/behind counts (`classify_head_vs_upstream`) so the "upstream has new commits but the local repo is dirty" case is spelled out, then **gates the build**: an interactive run must confirm (`prompt_choice`, default no), and an unattended run (`--non-interactive` or no TTY) aborts. Either decline raises, leaving nothing built (the sync runs before the sentinel). `--cleansrc` to discard local edits is the suggested escape hatch.

The source sync (including the `--cleansrc` purge, which `purge_src` does as a non-atomic `shutil.rmtree`) runs **outside** the boot sentinel by design: it mutates only the src tree, nothing boot-critical, so wrapping it in the sentinel — whose `recovery_cmd` is `sudo mkinitcpio -P` — would be semantically wrong. The atomicity contract is "purge, then clone"; an interrupted purge leaves a missing/partial PKGBUILD that fails **loudly** at `_pkgbuild_path` on the next run (with a hint to re-run `--cleansrc` to re-clone), not a silent brick. No sentinel is needed because the running kernel was never touched.

**Stage-ownership stamp:**

After a successful build, the kernel stage stamps `owner_stage = "kernel"` and `source = "local"` (or the configured value) into `build_state.toml` via `BuildOptions`. `sysforge update` honours that marker and skips the kernel package by default — the canonical update path is `sysforge run kernel`, not a sweep through `update`. Before the first kernel-stage build has written that stamp, the config bootstrap fallback in `primitives/stage_ownership.py` (consulted by `update.py`) reads `kernel.toml`'s `pkgname` and applies the same skip; split sub-packages collapse to the kernel `pkgbase` via the same `get_pkgbase()` lookup that handles other custom-built split packages. `--include-stage-owned` overrides the skip; naming the package explicitly on the `sysforge update` command line is treated as an opt-in for that run.

**Kernel stage boot-safety.**

The kernel stage must never leave the machine unbootable. Three gates wrap the build/install, backed by `primitives/kernel_safety.py` (the policy — what aborts vs warns — lives in the stage; the facts live in the primitive). Brick-class findings (`is_brick=True`) hard-fail; everything else warns.

To make a *pre-install* hard-fail possible, the build is **split from the install**: the stage builds with `BuildOptions.no_install=True` (the profile's `-i`/`--install` flags are stripped via `INSTALL_FLAGS`), audits the resolved `.config`, then installs the produced artifact via `makepkg_wrapper.install_built_packages()` (a `sudo pacman -U` of the built `.pkg.tar*`). Without the split, Arch's pacman hooks (`kernel-install`/mkinitcpio) would build the initramfs and boot entry *at install time* — before any audit could run. The build mutates nothing and runs **outside** the sentinel, so a Gate 2 abort leaves the system completely untouched (nothing installed, no sentinel set).

- **Gate 1 — preflight (before the build).** Cheap, read-only. Hard-fails on a missing **fallback kernel** (no stock `linux`/`linux-lts` with a boot image — installing a custom kernel as the only kernel has no recovery path; override with `--allow-no-fallback` / `require_fallback_kernel = false`) and on a **missing/too-full `/boot`** (`min_boot_free_mb`; part of `boot_audit`). Captures the root topology (FS / storage transport / crypt-LVM-RAID from `/proc/mounts` + `lsblk -s` + `/etc/crypttab` + `/proc/mdstat`) for Gate 2. Advisory warnings: localmodconfig strip, DKMS rebuild reminder, mkinitcpio `HOOKS` vs root topology. In `--dry-run` the hard-fails downgrade to warnings.
- **Gate 2 — resolved-`.config` audit (after build, before install).** Reads the resolved `.config` from the build tree and runs `kernel_safety.audit_resolved_config(config, topology, devices)`. This is the only placement that sees post-merge / post-`olddefconfig` / post-`nconfig` state, so it's the single catch for a **Kconfig dependency cascade** (e.g. `CONFIG_SND_PCI=n` silently dropping `CONFIG_SND_HDA_INTEL`). Brick-class drops — root filesystem, root storage controller, core boot infra (`CONFIG_MODULES`/`BLK_DEV_INITRD`/`DEVTMPFS`/…), systemd prerequisites, crypt/LVM/RAID stacking — **abort before install** (override: `--skip-boot-audit` / `boot_audit = false`). Device-driver gaps (present PCI/USB device with no enabled driver, from `device_probe`) and console/framebuffer drops are advisory.
- **Gate 3 — boot-readiness (after install + mkinitcpio + bootloader).** `verify_boot_artifacts` confirms `vmlinuz-<pkg>` + `initramfs-<pkg>.img` are present, non-trivial, and referenced by ≥1 boot entry (systemd-boot loader entry or `grub.cfg`) — a missing entry means the kernel installed but cannot be selected (the `bootctl update` ≠ boot-entry trap). `check_dkms_for_kernel` flags DKMS modules not rebuilt for the new release (nvidia → black screen, zfs root → unbootable). Brick findings raise; running inside the sentinel, that leaves the sentinel set so the next run is prompted to recover.

**CLI surface (`sysforge run kernel`):**

`--dry-run`, `--no-update`, `--cleansrc`, `--cleansrc-force`, `--non-interactive`, `--compiler {gcc,llvm}`, `--bootloader {systemd-boot,grub,none}`, `--allow-no-fallback`, `--skip-boot-audit`, `--no-pkg-logs`, `--persist-log`, `--log-dir`, `--cache-report`, `--abi-check`, `--state-dir`, `--profile-conf`.

**Post-install steps** (run after the artifact is installed):
1. `sudo mkinitcpio -P`
2. Bootloader update: `bootctl update` (systemd-boot, default), `grub-mkconfig -o /boot/grub/grub.cfg` (grub), or skipped (`none`). The selection comes from `kernel.toml bootloader`, overridable per-invocation via `--bootloader`.
3. Gate 3 boot-readiness verification (above).

**Interrupted-install protection.** The artifact install (`pacman -U`), the post-install `mkinitcpio -P`, the bootloader regen, and Gate 3 are wrapped in `sentinel_scope(state_name="kernel", recovery_cmd="sudo mkinitcpio -P", …)`. (The build runs *before* this scope.) An interruption anywhere in that window leaves the sentinel in place so the next sysforge invocation blocks at the CLI-entry recovery prompt and offers to regenerate the initramfs — the step whose absence makes the system unbootable. See §Toolchain stage → *Interrupted-install protection* for the shared primitive.

**Concurrency lock.** The build → Gate 2 → install window is additionally wrapped in `primitives.build_lock.build_lock(state_dir / "kernel-build.lock", label="kernel")` so two concurrent `sysforge run kernel` runs sharing a state dir can't clobber `~/builds/<pkgbase>` (the second `nconfig`/makepkg would step on the first's `.config`). This is the **same shared primitive** the toolchain stage's PGO lock (`_pgo_lock`) delegates to — distinct from the sentinel: the *lock* is transient mutual exclusion held only for the run, while the *sentinel* persists an interrupted boot-critical mutation across runs. The kernel lock lives under `state_dir` (not `/var/tmp` like the PGO lock, whose staging dirs are genuinely global), so per-state-dir test runs stay isolated. Skipped in `--dry-run` (nothing is built).

### Packages stage (stage 7)

Walks `packages.toml` in order:
- `source = "repo"` → `sudo pacman -S --needed --noconfirm`
- `source = "aur"` / `"git"` → `_resolve_pkgbuild()` → `makepkg_wrapper.run()`. PKGBUILD lookup order: `packages.toml [build] pkgbuild_src_dir` → `profiles.toml [paths] pkgbuild_src_dir` → AUR clone.
- Hardware-gated packages skipped if `hardware_profile.toml` is absent or key is missing
- Non-fatal per-package failures: build continues, failures recorded in state
- Summary at end: `Total | Built | Failed | Skipped`

The AUR-dep build and per-package install loop are wrapped in `sentinel_scope(state_name="packages", …)` (no `recovery_cmd` — there's no single shell command that restores a partially-installed package set; the operator verifies with `pacman -Dk` and re-runs `sysforge run packages`). Per-package `RuntimeError` is caught and reported via the state machine; only an interruption or unexpected exception inside the scope preserves the sentinel.

### Toolchain stage (stage 6)

**Opt-in:** stage is a clean no-op if `/etc/sysforge/toolchain.toml` is absent or has `enabled = false`. Systems that skip this stage use whatever compiler is already installed; packages and kernel stages proceed normally.

**`toolchain.toml` structure:**

```toml
enabled     = true   # must be true to activate the stage
compiler    = "gcc"  # "gcc" (default when key absent) or "llvm"; LLVM is opt-in
pgo         = true   # only meaningful when compiler = "llvm"; ignored for gcc
skip_build  = false  # skip build; just register compiler paths in pipeline state

# Staging prefixes. Pass 1 outputs land in stage1 (system /usr never touched);
# Pass 2 outputs land in stage2 and are used as CC/CXX in Pass 3.
pgo_staging1 = "/var/tmp/sysforge-llvm-stage1"
pgo_staging  = "/var/tmp/sysforge-llvm-stage2"

# PGO data dir: profraw files written here during Pass 2, merged to clang.profdata
pgo_store   = "/var/tmp/sysforge-llvm-pgo"

# Build-safety Gate 1 (LLVM path only; see Build-safety gates below)
min_build_free_gb = 40    # min free GiB per build filesystem (override: --skip-build-space-check)
require_multilib  = true  # require [multilib] enabled when any lib32-* is in scope

# Package lists — all have sane defaults, override only if needed
[packages]
pgo     = ["llvm", "llvm-libs", "clang", "lld"]
non_pgo = ["polly", "compiler-rt", "openmp", "spirv-llvm-translator"]
lib32   = ["lib32-llvm", "lib32-llvm-libs", "lib32-clang", "lib32-spirv-llvm-translator"]
```

When `compiler` is unset (or set to `"gcc"`), the toolchain stage is **register-only**: it writes the system `/usr/bin/gcc` and `/usr/bin/g++` paths into pipeline state and returns without building anything. Stock `gcc-libs` from pacman's `base-devel` provides the runtime. The 4-pass PGO architecture below only kicks in for the explicit `compiler = "llvm"` path. Building GCC from source has no meaningful performance gains and is error-prone, so the stage doesn't own that path — use `pacman -S gcc gcc-libs` (already in `base-devel`) if you need to (re)install it.

**`skip_build = true`:** registers the system compiler paths in pipeline state without building anything. Downstream stages (packages, kernel) will use the system compiler. Useful when the system compiler is already optimized and no rebuild is needed.

**Build-safety gates + build/install split (kernel-parity).** The LLVM path mirrors the kernel stage's three-gate / build-install-split structure so a broken or doomed build can never leave the live `/usr` toolchain inconsistent. The pure, unit-testable facts live in `primitives/toolchain_safety.py` (`ToolchainFinding(severity, check_id, message, remediation, is_brick)`); the toolchain stage owns the abort/warn *policy*. `toolchain_safety` imports `LLVM_LOCKSTEP_SUITE` from `toolchain_preflight` (both primitives — no layering issue) and is **not** a third health-check entry point: `_verify_llvm_install` (pipeline, post-install) and `toolchain_preflight._probe_cc` (primitives, update path) remain the only two, and `_verify_llvm_install`'s skew arm now draws from `toolchain_safety.detect_suite_skew`.

- **Gate 1 — pre-build preflight** (`_gate1_preflight`, outside the sentinel, runs for both PGO and non-PGO). Brick-class aborts *before any build time is spent*: PKGBUILD pkgver skew across the lockstep suite (`check_pkgver_lockstep`; `spirv-llvm-translator` + `lib32-*` are excluded so their legitimately-different versions don't false-positive — the bug the old whole-set `_check_pkgver_consistency` had); a non-functional clang / missing lld (`smoke_test_compilers`, now run on the non-PGO path too); insufficient build-filesystem headroom (`check_build_space`, deduped by `st_dev`); `[multilib]` disabled while a `lib32-*` is in scope (`check_multilib_enabled`). Each brick is overridable (`--allow-version-skew`, `--skip-build-space-check`, `require_multilib = false`) and **downgraded to a warning in `--dry-run`**. Advisory (warn-only): residual `-fprofile-generate` instrumentation; an incomplete rollback snapshot.
- **Build (outside the sentinel).** All passes build with `install=False`; the build functions return the built package map. A build-pass failure therefore mutates nothing and leaves **no sentinel** (matches kernel).
- **Gate 2 — pre-install ABI audit** (`_gate2_audit`, outside the sentinel, both paths). Scans the built `.pkg.tar*` for the `_ZNSt*@LLVM_*` hazard via `toolchain_safety.scan_abi_hazards`. A brick aborts before any `pacman -U` — nothing installed, no sentinel.
- **Snapshot.** Right before the install, `cached_pkg_files_for(lockstep suite ∪ built names)` (in `primitives/pacman.py`) locates each currently-installed member's `.pkg.tar*` in the pacman cache. This is the offline-undo source. Gate 1 warns up front when any member's archive is missing (auto-undo will fall back to `pacman -S`).
- **Gate 3 — post-install verify, inside the sentinel** (`_verify_llvm_install`). On failure, the stage **auto-restores** the prior-good toolchain from the snapshot in one `pacman -U` transaction (`_rollback_to_snapshot` → `batch_install_pkgs`): if restore succeeds the live `/usr` is whole again, so the sentinel is **cleared** and a `RuntimeError` is raised telling the user to investigate; if restore fails or the snapshot was incomplete, the sentinel is **kept** with `recovery_cmd` set to the snapshot restore (offline `pacman -U <cached>`, falling back to `pacman -S <suite>`).

The mutation window is therefore exactly install → Gate 3 → (rollback). The concurrent-run lock (`_pgo_lock`) wraps the whole build → audit → snapshot → install window, like the kernel stage's `kernel-build.lock`. A consolidated resolution summary (`_log_toolchain_resolution_summary`) prints the compiler/pgo/variant, package counts, staging paths, gate settings, and snapshot availability — the readable core of `--dry-run`.

**PKGBUILD resolution:** follows `find_pkgbuild` lookup order (local `pkgbuild_src_dir` → `pkgctl repo clone`) for every package. After path resolution, the stage routes each unique resolved `pkgbuild_dir` through `SourceSyncScheduler.sync_many()` so missing trees are cloned and pre-existing trees are refreshed against AUR/repo upstream — same RPC short-circuit, rate-limit, and dirty-tree handling as `sysforge update`. Pass `--no-update` to skip the sync step (use whatever is on disk verbatim). Blocker statuses (`STATUS_FAILED`, `STATUS_RATE_LIMITED`, `STATUS_PURGE_REFUSED`) abort the stage; `STATUS_DIVERGED` is a warning. Resolved paths are then displayed in a table and the user is prompted to confirm or abort. On abort, the resume command is printed (`sysforge pipeline --resume --state-dir <dir>`) so they can make manual modifications and return.

**LLVM PGO bootstrap (only when `pgo = true`):**

Every PGO pass runs makepkg with `--cleanbuild --force` so the prior pass's `.pkg.tar.zst` in PKGDEST never short-circuits the next build (each pass produces a different artifact at the same pkgver). `makepkg` runs without `--install` — sysforge controls when (and where) Pass outputs land. **Only the final Pass 3 install touches `/usr`.** Earlier passes go to staging prefixes; the live system is never made ABI-incoherent mid-run. A sudo keepalive thread refreshes credentials every 60 seconds throughout the sequence (the final `pacman -U` still needs root). `llvm-profdata` is invoked with `RLIMIT_AS` lifted (`resource_guard.lift_for_child`) so it is not constrained by the sysforge controller's 2 GiB virtual address space cap.

The sequence is **four builds** (Pass 1a, Pass 1b, Pass 2, Pass 3) that produce two on-disk staging prefixes (`pgo_staging1` → `pgo_staging`) before the final install:

1. **Pass 1a** — instrumented build of the pgo packages (`llvm`, `llvm-libs`) with the system compiler + `-fprofile-generate=<pgo_store>/`. Every output is **extracted to `pgo_staging1`** (no `pacman -U`, no live-root mutation), including the cmake-config / static-lib `llvm` package — Pass 1b's `find_package(LLVM)` needs those configs. The instrumented `.a` archives that land alongside surface `__llvm_profile_*` link errors for anything that consumes LLVM component targets; Pass 1b and Pass 2 work around that by injecting the clang profile runtime into LDFLAGS (see below). Spurious profraw from CMake feature probes is purged before later passes begin.

2. **Pass 1b** — **non-instrumented** build of the non_pgo packages (`clang`, `lld`, `compiler-rt`, `polly`, `openmp`, `spirv-llvm-translator`) against stage1. The Pass 1b environment sets `CMAKE_PREFIX_PATH=<staging1>/usr` so `find_package(LLVM)` finds stage1's headers and configs; the resulting binaries link against stage1's `libLLVM.so` and are ABI-coherent with it. **LD_LIBRARY_PATH is deliberately NOT set** for Pass 1b — that would force the host `/usr/bin/clang` to load stage1's libLLVM and recreate the version-skew failure mode this design exists to prevent. `linker_flags_extra = _profile_runtime_ldflag()` adds `-L<runtime_dir> -lclang_rt.profile-x86_64` so the instrumented static archives' `__llvm_profile_*` references resolve at link time. Pass 1b outputs are extracted into the same `pgo_staging1`, making it **self-sufficient**: stage1 now has a working clang and a working libLLVM, both built from the in-tree LLVM source, both ABI-coherent.

3. **Pass 2** — training run. CC is `<staging1>/usr/bin/clang` (built in Pass 1b), and the Pass-2 environment redirects dyld / cmake at stage1 via `LD_LIBRARY_PATH=<staging1>/usr/lib:…`, `CMAKE_PREFIX_PATH=<staging1>/usr:…`, `PATH=<staging1>/usr/bin:…`. The running clang and the libLLVM it loads are guaranteed coherent because they were built together — no possibility of version drift against `/usr`. Pass 2 builds pgo + non_pgo packages; the act of running stage1's clang against stage1's instrumented libLLVM generates profraw as a side effect. `LLVM_PROFILE_FILE` uses `%m_%p` (per-module-hash + per-PID) so parallel `make -j` clang processes each write their own `.profraw` file rather than contending on one; `CCACHE_DISABLE` and `SCCACHE_DISABLE` are set so neither cache tool bypasses the instrumented compiler. `linker_flags_extra` carries the same profile-runtime LDFLAGS so Pass 2's non_pgo `find_package(LLVM)` builds against stage1's instrumented `.a` archives still link cleanly. A background daemon merges profraw into `clang.profdata` every 15 seconds using adaptive batch sizing (starts at 128 files; halves on OOM; minimum batch 8). No install. After the build, Pass 2 binaries are extracted to `pgo_staging` (stage2). The stage2 outputs are **non-instrumented** since Pass 2 doesn't apply `-fprofile-generate`.

4. **Pass 3** — final optimized build of pgo + non_pgo + lib32 with `-fprofile-use=<clang.profdata>`. CC selection is conditional on whether `<pgo_staging>/usr/bin/clang` exists: when the PGO package set includes `clang` (so Pass 2 produces a staged clang), Pass 3 uses `CC=<pgo_staging>/usr/bin/clang` and the env redirects dyld / cmake at stage2 (`LD_LIBRARY_PATH=<pgo_staging>/usr/lib:…`, `CMAKE_PREFIX_PATH=<pgo_staging>/usr:…`, `PATH=<pgo_staging>/usr/bin:…`) — the staged clang's NEEDED libLLVM is stage2's libLLVM, so the redirect is ABI-coherent. The shipped default config only PGO-builds `llvm`/`llvm-libs`; clang is in `non_pgo` and lives in stage1, never stage2. In that **system-clang fallback** path Pass 3 uses `CC=/usr/bin/clang` (Arch's stock clang, linked against the full-target live `/usr/lib/libLLVM.so.22.1`) and the stage2 dyld/cmake redirect is **suppressed** — pointing system clang at stage2's `LLVM_TARGETS_TO_BUILD`-restricted libLLVM via `LD_LIBRARY_PATH` would recreate the Pass 1b version-skew failure (symbol lookup errors for absent target init functions like `LLVMInitializeBPFTarget`). Either way, `LLVM_PROFILE_FILE` is cleared so any inherited Pass-2 training env can't leak. Because stage2's LLVM is non-instrumented, **no profile-runtime LDFLAGS injection is needed** here — `find_package(LLVM)` sees no `__llvm_profile_*` references, and leaving `linker_flags_extra` unset prevents the Pass 1b/Pass 2 residual flag from leaking into the final optimized binaries. This is the **only** pass that runs `sudo pacman -U` against `/usr`. Staging prefixes are removed only **after** the post-install verify passes (see below), so a failed verify still has the stage2 prefix on disk for diagnostic inspection. Profdata is **preserved** at `<pgo_store>/clang.profdata`; a version sidecar `clang.profdata.version` (LLVM major integer, e.g. `22`) is written alongside it so `sysforge update` can check compatibility before reusing the profdata.

**Pre-install ABI hazard check (Gate 2).** Between the Pass-3 build and the final `sudo pacman -U`, `_gate2_audit` extracts each built `.pkg.tar*`'s shared libraries and scans their `nm -D` output via `toolchain_safety.scan_abi_hazards` (which uses `abi_check._undefined_versioned`). Any UND versioned symbol whose mangled name is in the C++ stdlib namespace (`_ZNSt*`) and whose version starts with `LLVM_` is a hard block: it means Pass 2's instrumented stage2 `libLLVM` leaked `std::string` (or similar) exports under the `LLVM_X.Y` version namespace and Pass 3's linker bound libclang-cpp's references to them. Installing those binaries would leave the live toolchain unable to resolve `std::string` methods at runtime (`symbol lookup error: libclang-cpp.so: undefined symbol _ZNSt..., version LLVM_22.1`). Gate 2 runs *outside* the sentinel, so a hazard aborts with the live `/usr` intact and **no sentinel**; the user is told to restart with `--rebuild-profdata`. (This scan moved out of `_pgo_install` — which now only installs — and is shared with the non-PGO path.)

**ABI-safety invariant (Path B).** The live `/usr` is observably coherent before and after every step except the single final `pacman -U`, and even that is now reversible: Gate 3 verifies the result and auto-rolls-back to the snapshot on failure. A run that aborts before install (build failure, Gate 1, or Gate 2) leaves the system exactly as `sysforge run toolchain` found it — nothing installed, no sentinel. A run whose install verifies-bad restores the prior-good suite from the pacman cache. No half-installed instrumented `libLLVM.so`, no orphaned `/usr/bin/clang` that can't resolve `LLVMInitializeBPFTarget@LLVM_22.1`. The only role `/usr/bin/clang` plays in the run is **as a bootstrap host compiler in Pass 1b** (compiling source into objects, never loading a different-version libLLVM); version drift between the in-tree LLVM source and the installed system packages is therefore no longer a failure mode.

**Stage ownership (`sysforge update` skip).** The install-bearing final pass (Pass 3, or the single pass when `pgo = false`) stamps `owner_stage = "toolchain"` into `build_state.toml` via `BuildOptions` — mirroring how the kernel stage stamps `owner_stage = "kernel"`. `sysforge update` honours that marker and skips the LLVM suite by default, pointing the user at `sysforge run toolchain` instead of rebuilding `llvm`/`clang`/`lld`/`compiler-rt` mid-sweep. Intermediate PGO passes (1a/1b/2) leave the marker unset so their transient, soon-overwritten staging writes don't claim ownership. Before the first toolchain-stage build has written that stamp — and for build_state entries written by older sysforge versions that predate the field — the config bootstrap fallback in `primitives/stage_ownership.py` (consulted by `update.py` via `load_stage_ownership()`) reads `toolchain.toml` and applies the same skip, but **only when** the stage is `enabled` *and* `compiler = "llvm"` (the default/unset `gcc` path is register-only and owns no LLVM, so stock pacman LLVM stays pacman-class and is left alone). Ownership is the **union** of `is_llvm_pkgbase` (prefix match: `llvm`/`clang`/`compiler-rt`/`lld` + `lib32-`) and the explicit `toolchain.toml [packages]` lists captured in the same snapshot. The configured set is what catches members `is_llvm_pkgbase` doesn't match by prefix — notably `spirv-llvm-translator` (and any custom-listed package) — so they're skipped too, not just the prefix set. `--include-stage-owned` overrides the skip; naming an LLVM package explicitly on the `sysforge update` command line is an opt-in for that run. This is the exact analogue of the `kernel.toml` bootstrap fallback (see the kernel stage's stage-ownership note).

**Pass 1b skipped when `non_pgo` is empty.** Minimal configs (tests, intentionally-narrow rebuilds) can set `[packages] non_pgo = []`. In that case stage1 has no clang, and Pass 2 falls back to `/usr/bin/clang` — recreating the bootstrap-host-clang behaviour, where the user is responsible for keeping system clang ABI-coherent with the in-tree LLVM source. The non-empty default (clang/lld/compiler-rt/...) is the supported path.

**Dep resolution for staged passes.** Pass 1a builds against the live `/usr` and keeps the profile-supplied `--syncdeps`, so missing build tools (cmake, ninja, python, z3, libffi, …) are pacman-installed normally. Pass 1b, Pass 2, and Pass 3 build against a stage prefix; `CMAKE_PREFIX_PATH=<staging>/usr` makes `find_package(LLVM)` see the staged headers and cmake configs, but pacman has no knowledge of those staged packages. `_build_pass(staged_deps=True)` therefore strips `--syncdeps`/`-s` (via the shared `SYNC_FLAGS` constant from `makepkg_wrapper.py`, the same set `pacman.BATCH_STRIP_FLAGS` removes for batch builds) from the resolved profile's makepkg flags and appends `--nodeps` for those three passes. Without that, makepkg's pre-build dep check would invoke `sudo pacman -S llvm=<pkgver>` and fail with "target not found" (the just-built version is not in any repo). The non-llvm build deps stay required — they're expected to already be on the system from Pass 1a's `--syncdeps` install.

**Concurrent-run lock.** `ToolchainStage.run` acquires an advisory `flock(2)` (`_pgo_lock`, the shared `build_lock` primitive) on `_pgo_lock_path(staging1)` = `<pgo_staging1>.parent/sysforge-pgo.lock` (typically `/var/tmp/sysforge-pgo.lock`) around the whole build → audit → snapshot → install window — not just the PGO passes, so the non-PGO path is guarded too (mirroring the kernel stage's `kernel-build.lock`). The sentinel scope guards re-entry on the state-dir but not the `/var/tmp` staging dirs or `~/pgo`, both of which two concurrent runs would corrupt. The lock file holds the owner's PID, so the loser surfaces "another sysforge PGO build is running (pid N)" rather than a confusing mid-flow failure. The path is in `staging1.parent` rather than inside `pgo_store` so the Pass-1 purge cannot delete it. Skipped in `--dry-run` (the lock file would be a side effect).

**Post-install libLLVM resolution check.** `_verify_llvm_install` runs `ldd /usr/bin/clang` and `ldd /usr/bin/lld` and asserts that any `libLLVM*.so` lines resolve under `/usr/lib`. A `/var/tmp/sysforge-llvm-stage*` path appearing in `ldd` of an installed binary means Pass 3 packaged a bad RPATH or the install is incomplete — `/usr` looks consistent until `/var/tmp` gets cleaned, at which point the live toolchain silently breaks. The verify-stage check catches that before the sentinel clears.

**Verify-failure diagnostic dump.** On a `_verify_llvm_install` failure, `ToolchainStage.run` calls `_dump_stage_dynsym_evidence(staging, state_dir)` before the recovery prompt. It writes `nm -D --defined-only` of stage2's `libLLVM.so.*` to `<state_dir>/llvm_abi_hazard.log`, with a filtered "suspicious symbols" header listing every line matching `_ZNSt` — direct evidence of which exports leaked into stage2's libLLVM under the LLVM version namespace. Staging removal is deferred until verify passes, so the evidence directory survives the failure path. The log path is surfaced in the WARN block alongside the suggested recovery command.

**Profdata reuse:** before purging `pgo_store`, the stage checks for an existing `clang.profdata` + version sidecar. The sidecar's LLVM major version is compared against the `pkgver` in the pgo PKGBUILDs (not the installed version — the toolchain stage builds a *new* version). If compatible (same major), passes 1a–2 are skipped entirely and only the optimized build (Pass 3) runs, using system clang as CC (which, after a prior successful run, is already PGO-optimized). Staging is not needed in this path. `--rebuild-profdata` forces a full 4-pass build regardless, e.g. after upstream codegen changes within the same major version.

**Sidecar write timing.** The version sidecar is written **right after Pass 2 completes** (after the final profraw merge produces `clang.profdata`, before Pass 3 starts) — not after a successful Pass 3 install. The sidecar's only invariant is "this profdata is for LLVM major N", which is determined entirely by what Pass 2 instrumented; Pass 3 success has no bearing on it. Writing it post-Pass-2 means a Pass-3 failure (e.g. a transient toolchain bug, an aborted run) still leaves recoverable profdata that the next invocation can reuse via `_check_existing_profdata` rather than being forced into a full 4-pass rebuild. The major itself is derived from the in-tree PGO PKGBUILD `pkgver` (`_pgo_target_major`), matching the value `_check_existing_profdata` will later compare against — symmetric with the reuse check, and correct across major bumps where `pacman -Q llvm` would report a stale value.

**Confirmation gating (PGO).** Unlike the rest of sysforge (which is automation-focused), the LLVM PGO sub-flow is fragile enough that wrong profdata silently mis-optimises the resulting compiler. Four decision points in `_build_llvm_pgo` therefore prompt the user before destructive or long-running work, all sharing a single `_pgo_confirm` helper:

1. **Reuse vs rebuild** — when compatible profdata is found, prompt `[Y/n]` to reuse; declining triggers a full 4-pass rebuild (and continues into prompts 2–3).
2. **Purge `staging/` and `pgo_store/`** — prompt `[y/N]` before `rmtree`; declining aborts PGO.
3. **4-pass start** — prompt `[y/N]` before launching the ~2–3 hour 4-pass sequence; declining aborts PGO.
4. **Suspicious Pass-2 profdata size** (`< 10 MiB`) — prompt `[y/N]` to continue into Pass 3; declining aborts before Pass 3 so the user can investigate instrumentation.

`--auto-pgo` (added on `run toolchain`) bypasses all four prompts and falls through to the prior automated behaviour. **Non-interactive without `--auto-pgo`** — the prompt's `eof_default="n"` fires and the PGO path aborts with a clear error directing the user to pass `--auto-pgo` or run with a TTY. The other existing prompts (residual-instrumentation, GCC-anyway, main "Proceed with toolchain build?", LLVM blockers) are unchanged.

**`pgo = false` path:** single build pass, all packages built and installed together. No profdata, no staging, no daemon.

**GCC path (`compiler = "gcc"`):** no build. Registers `/usr/bin/gcc` and `/usr/bin/g++` in pipeline state and returns. `pgo`, `skip_build`, `[packages]`, and the LLVM safety preflight are all skipped — none of them apply.

**Compiler propagation:** on completion the toolchain stage writes the resolved compiler paths into pipeline state:

```toml
[stages.toolchain.result]
cc      = "/usr/bin/clang"   # or "/usr/bin/gcc"
cxx     = "/usr/bin/clang++" # or "/usr/bin/g++"
ld      = "lld"              # llvm only; absent for gcc
variant = "pgo_llvm"         # gcc | stock_llvm | pgo_llvm
```

The packages and kernel stages read these values and inject them into the build environment, overriding any profile-level `CC`/`CXX` defaults. If the toolchain stage was skipped, these keys are absent and stages fall back to the profile.

**`variant` is the canonical toolchain-identity signal** for downstream conditional behaviour. Consumers should read it via `pipeline.state.get_toolchain_variant(state)` — do not derive it from the `cc` path or by re-parsing `toolchain.toml`. The fallback `"system"` is returned when the toolchain stage has never run on this state dir. The `skip_build = true` path reflects on-disk reality: if `pgo_store/clang.profdata` and its version sidecar exist, the installed clang is the result of a prior PGO build and `variant = "pgo_llvm"`; otherwise `"stock_llvm"`. Variants flow into `build_state.toml` per-build via `BuildOptions.toolchain_variant`, where `sysforge update` reads them back to detect toolchain drift (see §Toolchain-variant drift detection below).

**Variant-driven per-package env overlay.** `profile.variant_env_overlay(pkgbase, variant) -> dict[str, str]` returns extra env vars to inject for specific pkgbases when sysforge owns the LLVM toolchain. `_run_build` applies the overlay AFTER profile-derived env vars and AFTER `injected_env`, but only fills keys that aren't already set — overlays are defaults, not overrides, and a stage explicitly setting `MESA_WHICH_LLVM` (or any other overlay key) still wins. Today the only entry is `mesa` / `mesa-git` / `lib32-mesa` / `lib32-mesa-git` → `MESA_WHICH_LLVM=4` under `stock_llvm`/`pgo_llvm`. Reason: those PKGBUILDs use a case selector to pick which LLVM tree they link against, defaulting to `4` (`extra/llvm`) — which is the same package name sysforge installs after the toolchain stage. Setting it explicitly makes the build reproducible even if the user's shell exports a different value (e.g. `MESA_WHICH_LLVM=1` pointing at `llvm-minimal-git`). Skipped for `gcc` and `system` variants so sysforge doesn't override the user's shell preference when it has no LLVM opinion.

**Variant-driven linker soft default.** `emit_makepkg_conf` injects `-fuse-ld=lld` into `LDFLAGS` when (a) `toolchain_variant in {"stock_llvm", "pgo_llvm"}`, (b) no explicit `ld_override` was passed, (c) the resolved `LDFLAGS` (profile first, then system conf) declares no `-fuse-ld=` linker, (d) `lld` is on `PATH`, and (e) this is not a kernel build. The defaults-not-overrides rule is the key invariant: a profile that already declares `LDFLAGS="… -fuse-ld=mold"` keeps mold, and an explicit `BuildOptions.ld_override` still wins (hard override beats soft default). Effect: any build path that flows through `BuildOptions.toolchain_variant` — `packages` stage, `sysforge update`, `sysforge build` — picks up the toolchain's linker without each caller having to repeat the propagation. The kernel build path is opt-out because kernel linker selection is controlled by `LLVM=1`, not LDFLAGS. `gcc` and `system` variants skip the injection so sysforge doesn't override the user's makepkg.conf when it has no LLVM opinion.

**Stale-state wipe (disabled / absent stage).** When `toolchain.toml` is absent or has `enabled = false`, the stage clears any prior `[stages.toolchain.result]` from pipeline state before returning. This prevents the failure mode where a user runs `compiler = "llvm"`, disables the stage, and subsequent `packages`/`kernel` stages keep using the stale `cc=/usr/bin/clang`/`ld=lld` overrides — the disable opts out of all downstream LLVM propagation, not just the build.

**Interrupted-install protection.** Three layers wired into the LLVM build path (the GCC path is register-only and skips all three). Note the sentinel now wraps only the install → Gate-3 → rollback window (the build runs before it, outside the sentinel — see *Build-safety gates* above):

1. **Stage sentinel** (`primitives/stage_sentinel.py`) — writes `<state_dir>/stage_in_progress.toml` just before the `sudo pacman -U` of the run and clears it after Gate-3 verification passes (or after a successful auto-rollback, since the system is then whole again). Schema records `stage`, `started_at`, `compiler`, `pgo`, and a `recovery_cmd` string. The recovery command is **snapshot-aware** (`_snapshot_recovery_cmd`): when every suite member's prior `.pkg.tar*` is cached it's an offline `sudo pacman -U <cached files>`; otherwise it falls back to the online `sudo pacman -S <suite>`. On every subsequent sysforge invocation, `cli.main()` calls `check_and_recover_stale_sentinel()` before dispatching install-bearing commands (`build`, `update`, `converge`, `run *`, `setup`) — the gate is centralised in `cli._gate_sentinel_check(args)`, which also skips read-only invocations (`--dry-run`) so users can inspect the system without first running recovery. If a sentinel is found, the operator is prompted to auto-run the recovery command (`[y/N]`). **TTY-only prompt:** when stdin is not a TTY (background sessions, scripts, IDE wrappers), the prompt would silently auto-decline; `check_and_recover_stale_sentinel` instead emits an explicit error naming the sentinel file path and the recovery command, then returns False. **Verify-after-clear:** after `sentinel.clear()` runs, the recovery path checks that `sentinel.path.exists()` is False before printing "Recovery completed". A still-present file means the recovery cleared a different path (state-dir mismatch, namespace/chroot surprise) — the path is logged loudly so the operator can investigate instead of trusting a false-positive cleared message. Refusing recovery exits with status 2 and leaves the sentinel in place; success clears the sentinel and proceeds.

2. **Post-install verification (Gate 3)** — after the `pacman -U` of the LLVM run, `_verify_llvm_install()` checks: (a) `pacman -Q` versions across `_LLVM_VERSION_MATCH_SET` (which *is* `LLVM_LOCKSTEP_SUITE` from `toolchain_preflight` — `llvm`/`llvm-libs`/`clang`/`lld`/`compiler-rt`/`polly`/`openmp`) all agree, via `toolchain_safety.detect_suite_skew` (the canonical interrupted-install symptom — a mismatched `llvm-libs` is the exact failure mode that produces a broken GUI), (b) `clang --version` and `lld --version` invoke cleanly without missing-symbol errors, (c) `ldd` of installed clang/lld resolves libLLVM under `/usr/lib` (`_check_llvm_link_resolution`), (d) when `[llvm] targets` is configured, `llvm-config --targets-built` is a superset. On failure the stage **auto-rolls-back** to the pre-install snapshot rather than prompting (the kernel-parity overhaul replaced the old interactive `_prompt_llvm_recovery`): a successful restore clears the sentinel and raises "prior toolchain was restored"; a failed/incomplete restore keeps the sentinel with the snapshot recovery command. This verification is comprehensive and fatal, but only runs inside `run toolchain`; if a toolchain run is interrupted before it (or a later partial pacman transaction reintroduces a skew), the broken state can still reach an everyday `sysforge update`. That gap is closed by the `cc:<name>` compiler-health probe in `toolchain_preflight` (see §`toolchain_preflight.py`), which re-detects the suite-wide pkgver skew / non-runnable clang before any package builds — deliberately a lightweight independent check sharing the `LLVM_LOCKSTEP_SUITE` constant rather than importing this pipeline-layer verifier into the primitives layer.

3. **Clean-exit SIGINT scope** (`primitives/interrupt.py`) — wraps the LLVM build dispatch in an `InterruptScope` context. The first Ctrl-C flips a flag without raising; the build code checks the flag at safe boundaries (between `makepkg` runs, between PGO passes) and raises `CleanExitRequested` to exit at the next safe point, sentinel intact. A second Ctrl-C falls through to default `SIGINT` handling (immediate termination) — the operator explicitly chose the unsafe path. `CleanExitRequested` subclasses `BaseException` so it propagates through `except Exception:` blocks without being silently swallowed.

The sentinel-installation and clean-exit machinery is exposed as a shared `sentinel_scope()` context manager in `primitives/stage_sentinel.py` (see Verb Framework below) so the same install-bearing protection used by the toolchain stage is also available to other stages and standalone CLI verbs.

**Sentinel coverage map.** The primitive is now used at every install-bearing stage entry, not just the LLVM toolchain. Current callers:

| Caller | `stage_name` | `recovery_cmd` | Notes |
|---|---|---|---|
| `pipeline/stages/toolchain.py` (LLVM path) | `toolchain` | snapshot-aware: offline `sudo pacman -U <cached suite>`, else `sudo pacman -S <suite>` | Sentinel scoped to install → Gate-3 verify → auto-rollback (build + Gates 1–2 run outside it). Full three-layer protection (sentinel + verify + clean-exit). |
| `pipeline/stages/kernel.py` | `kernel` | `sudo mkinitcpio -P` (regenerates initramfs — the boot-critical step) | Wraps `makepkg --install`, `mkinitcpio -P`, and the bootloader regen. |
| `pipeline/stages/packages.py` | `packages` | _none_ (no single command restores a partially-installed package set) | Wraps AUR-dep build + per-package install loop. Per-package failures are state-tracked and don't preserve the sentinel; only an interruption / unexpected exception does. |
| `pipeline/stages/reconfigure.py` (`_try_install_editor`) | `reconfigure-editor` | _none_ | Single-package install; sentinel is cheap consistency with the larger stages. |
| `verbs/runner.py` (any verb with `requires_sentinel=True`) | _verb name_ | per-verb (`verb.sentinel_recovery_cmd(args, pre)`) | Currently `build`, `update`, `converge`, `state repair`, `state orphans --prune`, `state failed --clear`/`--clear-all`. |

The kernel and packages stage sentinels close the audit gap where an interrupted `pacman -U linux-custom` followed by an unfinished `mkinitcpio -P` could leave the system unbootable: the sentinel now persists across the makepkg → mkinitcpio → bootloader window, and the next sysforge invocation blocks at the CLI-entry recovery prompt with `sudo mkinitcpio -P` queued for auto-execution.

---

