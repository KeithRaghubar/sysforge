# SysForge — Claude Code Context

## What This Is

SysForge is an **Arch Linux system automation framework** (installer/bootstrapper, **not** a package manager).
Repo: <https://github.com/KeithRaghubar/sysforge.git>

- Language: Python
- Config format: TOML
- Test suite: 561 pytest tests

## Dev Environment

- Ryzen 7 5800X3D, RTX 5070, Arch Linux, COSMIC on Wayland, nvidia-open-dkms
- `pkgbuild_dir = ~/src` (sources); builds at `~/builds`
- `/etc/sysforge/` is a live mirror of `tests/data/etc/sysforge/` — edit project files, then `sudo cp` to sync live

## Known Bugs & Gotchas

1. **`_pkgmeta_placeholder`** — wiring was fixed once; history may resurface. Verify if touching metadata paths.
2. **`test_pipeline.py`** — imports from both `config.py` and `profile.py`. Watch for breakage if module boundaries shift.
3. **`match_rules` and `pkgbase`** — split-package PKGBUILDs (e.g. kernels) set `pkgbase` to the canonical name; `pkgname` is an array of unexpanded sub-package names (`"$pkgbase"`). Rules using `pkgnames` now match against `pkgbase` too. Don't regress this.

## Implemented: Verbosity Levels

- **0** (default): `[ERROR]` only
- **1** (`-v`): adds `[WARN]`
- **2** (`-vv`): adds `[INFO]`
- **3** (`-vvv`): adds `[DEBUG]` — full body dumps of loaded configs, resolved profiles, conflict groups, inference map, and temp makepkg.conf

## Implemented: Dual Log Scheme

- **Unified log**: `state_dir/sysforge.log` (pipeline subcommand only)
- **Per-package log**: written to source dir (both `build` and `pipeline`)
- **CLI flags on `pipeline`**: `--no-unified-log`, `--no-pkg-logs`, `--log-dir`, `--purge-log`, `--persist-log`
- **CLI flags on `build`**: `--persist-log`, `--no-pkg-log`, `--log-dir`

## Implemented: Cache Monitoring

- **`[CACHE]` tag** — per-build ccache/sccache hit/miss delta at `[INFO]` level; system probes (ld.so mtime, pacman cache size, ThinLTO cache size) once per run
- **`--cache-report` flag** — on both `build` and `pipeline`; prints structured summary to stderr at end of run (always shown, bypasses verbosity gating)
- Module: `sysforge/primitives/cache_probe.py`

## Implemented (notable recent additions)

- **Bare package name resolution** — `sysforge build htop` / `sysforge resolve htop`. `find_pkgbuild` in `config.py` searches: direct path/dir → cwd → `[paths] pkgbuild_dir` → auto-clone. Repo packages: `pkgctl repo clone --protocol=https`. AUR packages: `git clone` from AUR.
- **GPG key auto-import** — before each build: imports bundled `keys/pgp/*.asc` first, then `gpg --recv-keys` for any still missing `validpgpkeys`.
- **Zsh completion** — `completions/_sysforge`. Install to `/usr/share/zsh/site-functions/`. `sysforge completions packages` subcommand outputs local pkgbuild_dir packages + pacman sync DB names.

## Interaction Preferences

- Be direct. No coddling, no hedging on mistakes — own them and fix them.
- No uwuification. Ever.
- When a design decision is made during conversation, commit it to memory/docs **immediately in the same turn** — don't wait to be reminded.
