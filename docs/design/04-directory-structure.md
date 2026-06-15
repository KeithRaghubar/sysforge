## Directory Structure

### Development (local repo)

```
sysforge/
├── sysforge/
│   ├── __init__.py
│   ├── cli.py                         # CLI entry: argv preprocessing + argparse parser construction + verb dispatch (verb classes live in their per-command modules)
│   ├── log.py                         # structured logging (stderr + optional file output)
│   ├── ui/
│   │   ├── __init__.py
│   │   ├── headers.py                 # shared visual primitives (welcome banner, stage banners, stage list, closing rule)
│   │   └── progress.py                # bottom-anchored batch progress indicator (TTY scroll region + plain fallback)
│   ├── resolve.py                     # sysforge resolve subcommand
│   ├── update.py                      # sysforge update subcommand
│   ├── build_core.py                  # shared build engine behind `build` + `update` (dep prep, build loop, install)
│   ├── build_cmd.py                   # sysforge build subcommand (BuildVerb; routes through build_core)
│   ├── run_cmd.py                     # sysforge run namespace verbs (pipeline/hardware/reconfigure/toolchain/packages/kernel)
│   ├── env_cmd.py                     # sysforge env subcommand (read-only env-chain inspector)
│   ├── completions_cmd.py             # sysforge completions data sink (consumed by _sysforge)
│   ├── doctor.py                      # sysforge doctor subcommand (ABI/linkage health check)
│   ├── fetch.py                       # sysforge fetch subcommand (download PKGBUILDs, no build)
│   ├── packages_cmd.py                # sysforge packages namespace (list/add/remove)
│   ├── state_cmd.py                   # sysforge state namespace (list/repair) — build_state.toml
│   ├── setup_cmd.py                   # sysforge setup subcommand (pacman IgnoreGroup = sf-build guard)
│   ├── verbs/
│   │   ├── base.py                    # Verb ABC + PreCheckResult/ExecResult result types
│   │   ├── runner.py                  # run_verb dispatch + sentinel wrapping
│   │   └── helpers.py                 # shared verb helpers (load_config_with_overrides)
│   └── primitives/
│       ├── paths.py                   # config path constants + resolve_packages_path()
│       ├── stage_ownership.py         # stage→package ownership registry (update skip bootstrap)
│       ├── config.py                  # TOML config loading, path constants, system conf parsing
│       ├── pacman.py                  # pacman queries, batch install, makedep helpers
│       ├── profile.py                 # profile resolution, rule matching, consumes
│       ├── pkgbuild_meta.py           # static PKGBUILD parser (read-only)
│       ├── pkgbuild_patcher.py        # PKGBUILD mutation + flag extraction
│       ├── prompt.py                  # shared interactive-prompt helpers (every stage uses these)
│       ├── makepkg_wrapper.py         # build execution: emit conf, invoke makepkg
│       ├── makepkg_flags.py           # makepkg flag-string transforms (owns [FLAG] tag)
│       ├── makepkg_artifacts.py       # built .pkg.tar* discovery + filename→version parse (pure, no tag)
│       ├── makepkg_pgo.py             # PGO profdata state resolution (pure, no logger)
│       ├── makepkg_env.py             # subprocess env resolution + effective build dir (owns [ENV] tag)
│       ├── makepkg_conf.py            # temp makepkg.conf emission ctx-mgr (owns [CONF])
│       ├── makepkg_invoke.py          # makepkg subprocess invocation + sudo-timeout retry (owns [MAKEPKG])
│       ├── pty_runner.py              # spawn subprocess on a pty (preserves cargo's live progress bar)
│       ├── aur_resolve.py             # recursive AUR dependency resolution + topo sort
│       ├── dep_analysis.py            # pre-build soname dependency checks
│       ├── abi_check.py               # post-build versioned-symbol ABI check (.so cross-ref)
│       ├── provides_lookup.py         # reverse-lookup soname → package (pacman -Fq)
│       ├── diagnostics.py             # unified Finding framework (severity, adapters, axis runner, renderer)
│       ├── graphics_probe.py          # system-state graphics checks (NVIDIA, Wayland, Steam)
│       ├── device_probe.py            # full PCI/USB inventory + driver-coverage (modalias→module→CONFIG)
│       ├── kernel_safety.py           # kernel boot-safety: .config audit, fallback/boot-artifact/DKMS checks
│       ├── system_probe.py            # doctor pacman axis: -Dk, db.lck, .pacnew, orphans (read-only)
│       ├── state_probe.py             # doctor state axis: build failures, stale sentinel, state drift
│       ├── runtime_probe.py           # doctor services axis: failed units, missing firmware
│       ├── failure.py                 # failure scenario handling (shared)
│       ├── resource_guard.py          # controller RLIMIT_AS cap + lift_for_child() for subprocesses
│       ├── cache_probe.py             # passive ccache/sccache monitoring ([CACHE] tag)
│       ├── aur.py                     # AUR RPC v5, git clone, pkgctl checkout, GPG key import
│       ├── rate_limit.py              # shared RPC + git fetch rate limiter (RateLimiter, RateLimited)
│       ├── source_meta.py             # per-package AUR RPC + git HEAD cache (source_meta.toml)
│       ├── source_sync.py             # process-wide SourceSyncScheduler (RPC-first, sequential)
│       ├── build_state.py             # per-package build metadata persistence (build_state.toml)
│       └── version.py                 # vercmp wrapper + version string formatting
│   └── pipeline/
│       ├── __init__.py
│       ├── runner.py                  # stage sequencing, checkpoint/resume
│       ├── state.py                   # pipeline_state.toml read/write
│       └── stages/
│           ├── __init__.py            # STAGES ordered list
│           ├── base.py                # Stage base class, RunOptions dataclass
│           ├── _bootstrap.py          # shared bootstrap config loader (BootstrapConfig dataclass)
│           ├── partition.py           # stage 1: GPT partitioning, mkfs, mount
│           ├── base_install.py        # stage 2: pacstrap + genfstab
│           ├── hardware.py            # stage 3: CPU/GPU/NVMe detection + PCI/USB inventory → hardware_profile.toml
│           ├── configure.py           # stage 4: hostname, locale, timezone, bootloader, user, services (arch-chroot)
│           ├── reconfigure.py         # stage 5: pre-build checkpoint
│           ├── toolchain.py           # stage 6: LLVM/GCC toolchain build (optional 4-pass PGO)
│           ├── packages.py            # stage 7: package builds
│           └── kernel.py              # stage 8: kernel build
├── tests/
│   ├── conftest.py
│   ├── data/
│   │   ├── PKGBUILDs/                 # htop, llvm, lib32-llvm, cosmic, vulkan-headers-git
│   │   │   ├── complex.PKGBUILD  →  vulkan-headers-git.PKGBUILD  (symlink alias)
│   │   │   ├── complex2.PKGBUILD →  lib32-llvm.PKGBUILD           (symlink alias)
│   │   │   └── simple.PKGBUILD   →  htop.PKGBUILD                 (symlink alias)
│   │   ├── test_profiles.toml
│   │   ├── etc/sysforge/
│   │   │   ├── profiles.toml
│   │   │   ├── packages.toml
│   │   │   ├── toolchain.toml
│   │   │   └── kernel.toml
│   │   └── user/.config/sysforge/
│   │       └── profiles.toml
│   ├── test_append_merge.py
│   ├── test_consumes.py
│   ├── test_dep_analysis.py
│   ├── test_env_pass.py
│   ├── test_aur.py
│   ├── test_cache_probe.py
│   ├── test_cli.py
│   ├── test_failure.py
│   ├── test_log.py
│   ├── test_parser.py
│   ├── test_patcher.py
│   ├── test_pipeline.py
│   ├── test_pipeline_runner.py
│   ├── test_pipeline_state.py
│   ├── test_reconfigure.py
│   ├── test_resolve.py
│   ├── test_stage_bootstrap.py
│   ├── test_stage_kernel.py
│   ├── test_stage_packages.py
│   ├── test_system_conf.py
│   ├── test_update.py
│   ├── test_build_state.py
│   ├── test_version.py
│   └── test_wrapper.py
├── completions/
│   └── _sysforge                      # zsh completion script
├── tools/
│   ├── iso-install.sh                 # bootstrap helper: installs sysforge on live ISO, writes bootstrap.toml
│   └── vm/
│       ├── boot.sh                    # launch QEMU VM (--iso, --snapshot modes)
│       └── bootstrap.toml             # VM-specific bootstrap.toml for testing
├── PKGBUILD
├── pyproject.toml
├── Makefile
└── DESIGN.md
```

### Installed

```
/etc/sysforge/
    profiles.toml                    # flag profiles, [[rules]], conflict groups, consumes inference
    packages.toml                    # default build-rule overrides
    bootstrap.toml                   # written by iso-install.sh (or hand-copied from the example)
/usr/share/sysforge/
    bootstrap.toml.example           # starter template for manual hand-edit setups
/usr/bin/sysforge
~/.config/sysforge/                  # $XDG_CONFIG_HOME/sysforge
    profiles.toml                    # user overrides (optional; merges with system via extends_system)
~/.cache/sysforge/                   # $XDG_CACHE_HOME/sysforge
    aur-packages.txt                 # AUR name cache (regenerable, refreshed every 24h)
~/.local/state/sysforge/             # $XDG_STATE_HOME/sysforge
    (state files)                    # fallback runtime state when /var/lib/sysforge is not writable
/var/cache/sysforge/                 # regenerable build cache (override via SYSFORGE_PGO_STORE); provisioned 0777 by tmpfiles.d, sudo-created at runtime if absent
    llvm-pgo/                        # LLVM PGO profraw/profdata store (written by unprivileged makepkg)
/var/lib/sysforge/
    pipeline_state.toml              # pipeline checkpoint state (created at runtime)
    build_state.toml                 # per-package build metadata (created at runtime, by sysforge build/update)
    source_meta.toml                 # per-package AUR RPC + git HEAD snapshot used by the source_sync scheduler
    sysforge.log                     # unified log (created at runtime, cleared on success)
```

#### makepkg-owned paths (not sysforge dirs)

The PKGBUILD source tree, build workspace, and package output directory are **makepkg's domain**, not sysforge XDG/FHS dirs, so they fall outside the `paths.py` discipline of §21-standards:

- **`pkgbuild_src_dir`** (`~/src` by default, `[paths] pkgbuild_src_dir` in `profiles.toml`) — production PKGBUILD checkouts. FHS-style user source code, deliberately *not* under `$XDG_*` (it is not regenerable cache).
- **`BUILDDIR`** (`$HOME/builds` by default, set in `[profiles.bare]`) — makepkg's build workspace. Kept as a top-level `~/builds` rather than `$XDG_CACHE_HOME/sysforge` on purpose: it is a **shared** workspace the user also builds in manually (sysforge does not own it), and `~/src`/`~/builds` form a matched pair of user working dirs. It is fully user-overridable via the `BUILDDIR` profile key or `/etc/makepkg.conf`; sysforge resolves the effective value through `pacman.get_builddir()` (env → system conf), never assuming the default.
- **`PKGDEST` / `SRCDEST` / `LOGDEST`** — left to makepkg/`/etc/makepkg.conf`; sysforge never writes a default and reads them via `pacman.get_pkgdest()` / `get_srcdest()` / `get_logdest()` when it needs to locate an artifact, source, or log.

The optional `[makepkg]` table in `bootstrap.toml` (`packager`, `makeflags`) lets an unattended install stamp `PACKAGER` / `MAKEFLAGS` into the target `/etc/makepkg.conf` during the configure stage; on a running system the `reconfigure` makepkg step offers the same interactively.

---

