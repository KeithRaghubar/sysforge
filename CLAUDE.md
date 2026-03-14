# SysForge — Claude Code Context

## What This Is

SysForge is an **Arch Linux system automation framework** (installer/bootstrapper, **not** a package manager).
Repo: <https://github.com/KeithRaghubar/sysforge.git>

- Language: Python
- Config format: TOML
- Test suite: 292 pytest tests

## Dev Environment

- Ryzen 7 5800X3D, RTX 5070, Arch Linux, COSMIC on Wayland, nvidia-open-dkms
- `pkgbuild_dir = ~/builds`
- `/etc/sysforge/` has test data configs

## Known Bugs & Gotchas

1. **`--config` flag (install)** — accepted by the CLI but **silently ignored**. Scope is undecided: whole dir override vs single file. Needs a design decision before implementing.
2. **AUR RPC lookup** — `manifest.py` uses `_stub_aur_fn`, which always returns `None`. Real AUR lookups are not wired up.
3. **`_pkgmeta_placeholder`** — wiring was fixed once; history may resurface. Verify if touching metadata paths.
4. **`test_pipeline.py`** — imports from both `config.py` and `profile.py`. Watch for breakage if module boundaries shift.

## Implemented: Dual Log Scheme

- **Unified log**: `state_dir/sysforge.log` (install subcommand only)
- **Per-package log**: written to source dir (both `build` and `install`)
- **CLI flags on `install`**: `--no-unified-log`, `--no-pkg-logs`, `--log-dir`, `--purge-log`, `--persist-log`
- **CLI flags on `build`**: `--persist-log`, `--no-pkg-log`, `--log-dir`

## Pending Work (Queued)

### Unimplemented features

- **`[FLAG]` tag** — makepkg.conf flag resolution/conflict logging
- **`[CACHE]` tag** — ccache/sccache passive monitoring, ThinLTO, CMake/Meson build dirs, ld.so cache mtime, pacman cache
- **`--cache-report` flag** — structured end-of-run summary
- **`[env_precedence]` config table** — designed but not read yet. Priority order: wrapper=100, makepkg conf=80, shell=20, pkgbuild export=10

## Interaction Preferences

- Be direct. No coddling, no hedging on mistakes — own them and fix them.
- No uwuification. Ever.
- When a design decision is made during conversation, commit it to memory/docs **immediately in the same turn** — don't wait to be reminded.
