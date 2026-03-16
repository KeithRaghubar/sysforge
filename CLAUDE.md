# SysForge — Claude Code Context

## What This Is

SysForge is an **Arch Linux system automation framework** (installer/bootstrapper, **not** a package manager).
Repo: <https://github.com/KeithRaghubar/sysforge.git>

- Language: Python
- Config format: TOML
- Test suite: 545 pytest tests

## Dev Environment

- Ryzen 7 5800X3D, RTX 5070, Arch Linux, COSMIC on Wayland, nvidia-open-dkms
- `pkgbuild_dir = ~/builds`
- `/etc/sysforge/` is a live mirror of `tests/data/etc/sysforge/` — edit project files, then `sudo cp` to sync live

## Known Bugs & Gotchas

1. **`--config` flag (install)** — planned but **never added to the CLI**. `--profile-conf` exists and works as the config override. Scope for `--config` is undecided (whole dir override vs single file). Do not add it until the design is settled.
2. **AUR RPC lookup** — `manifest.py` uses `_stub_aur_fn`, which always returns `None`. Real AUR lookups are not wired up.
3. **`_pkgmeta_placeholder`** — wiring was fixed once; history may resurface. Verify if touching metadata paths.
4. **`test_pipeline.py`** — imports from both `config.py` and `profile.py`. Watch for breakage if module boundaries shift.
5. **`match_rules` and `pkgbase`** — split-package PKGBUILDs (e.g. kernels) set `pkgbase` to the canonical name; `pkgname` is an array of unexpanded sub-package names (`"$pkgbase"`). Rules using `pkgnames` now match against `pkgbase` too. Don't regress this.

## Implemented: Verbosity Levels

- **0** (default): `[ERROR]` only
- **1** (`-v`): adds `[WARN]`
- **2** (`-vv`): adds `[INFO]`
- **3** (`-vvv`): adds `[DEBUG]` — full body dumps of loaded configs, resolved profiles, conflict groups, inference map, and temp makepkg.conf

## Implemented: Dual Log Scheme

- **Unified log**: `state_dir/sysforge.log` (install subcommand only)
- **Per-package log**: written to source dir (both `build` and `install`)
- **CLI flags on `install`**: `--no-unified-log`, `--no-pkg-logs`, `--log-dir`, `--purge-log`, `--persist-log`
- **CLI flags on `build`**: `--persist-log`, `--no-pkg-log`, `--log-dir`

## Implemented: Cache Monitoring

- **`[CACHE]` tag** — per-build ccache/sccache hit/miss delta at `[INFO]` level; system probes (ld.so mtime, pacman cache size, ThinLTO cache size) once per run
- **`--cache-report` flag** — on both `build` and `install`; prints structured summary to stderr at end of run (always shown, bypasses verbosity gating)
- Module: `sysforge/primitives/cache_probe.py`

## Pending Work (Queued)

### Unimplemented features

- **`[FLAG]` tag** — ~~conflict group resolution and prefix-match token replacement logging~~ **done**. Remaining gap: `apply_patch_pkgbuild` (uses `[PATCH]` — intentional).

## Interaction Preferences

- Be direct. No coddling, no hedging on mistakes — own them and fix them.
- No uwuification. Ever.
- When a design decision is made during conversation, commit it to memory/docs **immediately in the same turn** — don't wait to be reminded.
