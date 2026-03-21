# SysForge — Claude Code Context

## What This Is

SysForge is an **Arch Linux AUR helper with profiled builds** — v0.1.0 target is a fully usable AUR helper with system-tuned builds. Bootstrap stages 1–4 (partition, base install, hardware, configure) are deferred to v1.0.
Repo: <https://github.com/KeithRaghubar/sysforge.git>

- Language: Python
- Config format: TOML
- Test suite: 769 pytest tests

## Dev Environment

- Ryzen 7 5800X3D, RTX 5070, Arch Linux, COSMIC on Wayland, nvidia-open-dkms
- `pkgbuild_dir = ~/src` (sources); builds at `~/builds`
- `etc/sysforge/` — canonical shipped defaults (installed by PKGBUILD to `/etc/sysforge/`); live `/etc/sysforge/` mirrors this
- `tests/data/etc/sysforge/` — test fixtures; may differ (personal packages, test-specific rules)

## Known Bugs & Gotchas

1. **`_pkgmeta_placeholder`** — wiring was fixed once; history may resurface. Verify if touching metadata paths.
2. **`test_pipeline.py`** — imports from both `config.py` and `profile.py`. Watch for breakage if module boundaries shift.
3. **`match_rules` and `pkgbase`** — split-package PKGBUILDs (e.g. kernels) set `pkgbase` to the canonical name; `pkgname` is an array of unexpanded sub-package names (`"$pkgbase"`). Rules using `pkgnames` now match against `pkgbase` too. Don't regress this.

## Implemented: Verbosity Levels

- **0** (default): `[ERROR]` only
- **1** (`-v`): adds `[WARN]`
- **2** (`-vv`): adds `[INFO]`
- **3** (`-vvv`): adds `[DEBUG]` — full body dumps of loaded configs, resolved profiles, conflict groups, inference map, and temp makepkg.conf

## CLI Structure

**Top-level commands:** `build`, `update`, `resolve`, `converge`

**`packages` namespace** — manifest management:
- `packages list` (default when no subcommand)
- `packages add <pkg> [<pkg>...]` — classify, infer pkgbuild_patch, append entries; accepts multiple args
- `packages remove <pkg>`
- `packages sync` — re-validate source + pkgbuild_patch; in-place field edits (comments preserved)

**`run` namespace** — stage execution:
- `run pipeline` — stages 1–8; `--start-from`, `--resume`, `--force-retry`
- `run reconfigure` — stage 5, standalone
- `run toolchain` — stage 6, standalone
- `run packages` — stage 7, standalone
- `run kernel` — stage 8, standalone

Stages 1–4 (bootstrap) only accessible via `run pipeline`, NOT as individual `run` targets.

## packages.toml Fields

Valid per-entry fields: `name`, `source` (`"repo"` | `"aur"`), `pkgbuild_patch` (bool), `cache` (bool).
`profile` and `requires_hardware` are **removed** — do not add them.

## v0.1.0 Status: Complete

All userspace components are implemented. Bootstrap stages 1–4 deferred to v1.0.

## Implemented: Dual Log Scheme

- **Unified log**: `state_dir/sysforge.log` (`run pipeline` only)
- **Per-package log**: written to source dir (both `build` and `run pipeline/packages/kernel`)
- **CLI flags on `run pipeline`**: `--no-unified-log`, `--no-pkg-logs`, `--log-dir`, `--purge-log`, `--persist-log`
- **CLI flags on `build`**: `--persist-log`, `--no-pkg-log`, `--log-dir`

## Implemented: Cache Monitoring

- **`[CACHE]` tag** — per-build ccache/sccache hit/miss delta at `[INFO]` level; system probes (ld.so mtime, pacman cache size, ThinLTO cache size) once per run
- **`--cache-report` flag** — on `build`, `update`, `run pipeline/toolchain/packages/kernel`; prints structured summary to stderr at end of run (always shown, bypasses verbosity gating)
- Module: `sysforge/primitives/cache_probe.py`

## Implemented (notable features)

- **Bare package name resolution** — `sysforge build htop` / `sysforge resolve htop`. `find_pkgbuild` in `config.py` searches: direct path/dir → cwd → `[paths] pkgbuild_dir` → auto-clone. Repo packages: `pkgctl repo clone --protocol=https`. AUR packages: `git clone` from AUR.
- **GPG key auto-import** — before each build: imports bundled `keys/pgp/*.asc` first, then `gpg --recv-keys` for any still missing `validpgpkeys`.
- **Zsh completion** — `completions/_sysforge`. Install to `/usr/share/zsh/site-functions/`. `sysforge completions packages` outputs local pkgbuild_dir packages + pacman sync DB names + AUR cache if present. Completes `packages` and `run` subcommands and their flags. `build` and `resolve` complete both package names and local file paths simultaneously via `_alternative`.
- **`sysforge update`** — `sysforge/update.py`. Loads `build_state.toml`, `git pull --rebase` each PKGBUILD dir, compares versions via `vercmp`, rebuilds outdated packages. VCS packages (`-git`, `-svn`, `-hg`, `-bzr`) require `--devel`. `--dry-run` shows what would rebuild. `--all` discovers foreign packages via `pacman -Qm`, adds them to `packages.toml`, rebuilds if outdated. Side effect: refreshes `~/.cache/sysforge/aur-packages.txt` (packages.gz).
- **`sysforge converge`** — `sysforge/converge.py`. Re-resolves the current compiler flag profile for each profiled package and diffs against the `flags_string` stored in `build_state.toml`. Reports `DRIFTED`/`IN_SYNC`/`NO_FLAGS`/`NO_PKGBUILD` per package. `--apply` rebuilds all drifted packages.
- **Build state tracking** — `sysforge/primitives/build_state.py`. `makepkg_wrapper.run()` writes per-package metadata to `/var/lib/sysforge/build_state.toml` after each successful build. Fields: `pkgver`, `pkgrel`, `epoch`, `pkgbase`, `pkgbuild_dir`, `build_mode` (`"pacman"` | `"profiled"`), `flags_string` (serialized resolved flags), `built_at`.
- **`sysforge/primitives/version.py`** — `vercmp(a, b)` wraps the system binary; `format_version(globals_)` assembles `[epoch:]pkgver-pkgrel`.
- **`packages list/add/remove/sync`** — `sysforge/packages_cmd.py`. `add` accepts multiple args, classifies via pacman/AUR, infers `pkgbuild_patch`. `remove` is line-level (preserves comments). `sync` does in-place field edits (comments preserved).

## Interaction Preferences

- Be direct. No coddling, no hedging on mistakes — own them and fix them.
- No uwuification. Ever.
- When a design decision is made during conversation, commit it to memory/docs **immediately in the same turn** — don't wait to be reminded.
