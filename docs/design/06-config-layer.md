## Config Layer

### Config file hierarchy

- System default: `/etc/sysforge/profiles.toml`
- User override: `~/.config/sysforge/profiles.toml`

`profiles.toml` is a single file holding flag profiles, `[[rules]]`, `[append_conflict_groups]`, and `[consumes_inference]`. By default the user file **fully replaces** the system file. To layer on top instead, add `extends_system = true` at the top of the user file — user values take priority on all conflicts. User rule priorities are bumped by 100 on merge (range 100–199) to always outrank system rules (range 0–99).

### Adopting new shipped defaults

The non-`profiles.toml` configs (`packages.toml`, `toolchain.toml`, `kernel.toml`, `sysforge.toml`) are read from a single resolved path with no per-key fallback to shipped defaults, so a live config does **not** automatically gain keys/sections added by a new release. On an installed system pacman's `backup=()` + `.pacnew` reconciliation covers this (and `doctor --pacman` warns about unmerged `.pacnew`). In a from-repo dev setup — where `SYSFORGE_CONFIG_DIR` points at a working tree and pacman never touches the config — `make sync-config` (`tools/sync_config.py`) fills the gap.

**Config-dir resolution.** `SYSFORGE_CONFIG_DIR`, when set, is the directory that *directly contains* the TOML files (e.g. `~/sf-config/kernel.toml`) — it is **not** an FHS root prefix, mirroring how `SYSFORGE_STATE_DIR` holds state files directly. When unset, the config dir is the FHS system path `/etc/sysforge`. The single resolution home is `primitives/paths.py` (`CONFIG_DIR` + the `*_PATH` constants); `tools/sync_config.py` and `tests/conftest.py` mirror it. (The installed-system path is unchanged: with the env unset, everything still resolves under `/etc/sysforge`.)

`sync-config` is an **add-only**, comment-preserving merge from `etc/sysforge/*.toml` into the live config dir (`$SYSFORGE_CONFIG_DIR` itself, else `/etc/sysforge`, or `--target DIR`): it injects keys, tables, and their leading comment blocks the live file is missing, and never overwrites a value the live file already sets (even if the shipped default changed). Arrays-of-tables (`[[package]]`) are user content and are left untouched. Bare keys are spliced before the first table header (TOML adjacency rule); new tables are appended. `tomlkit` is a **dev-only** dependency (ephemeral `uv run --no-sync --with tomlkit`), never added to `pyproject.toml`. `--dry-run` reports without writing. `bootstrap.toml` is excluded (per-host, no live counterpart).

### Dev fixtures vs. personal config

`tests/data/etc/sysforge/` is the **git-tracked test fixture set** wired in `tests/conftest.py` (which *forces* `SYSFORGE_CONFIG_DIR` to that dir directly, so a developer shell exporting its own value cannot leak into the suite). It is kept in shipped↔fixture parity by `make check-shipped`. A developer's **personal live config** is a separate, untracked dir (e.g. `~/sf-config`, holding the TOML files directly) that the shell's `SYSFORGE_CONFIG_DIR` points at and that `make sync-config` services — keeping personal config out of the tracked tree while leaving the fixtures deterministic.

### Directory layout

SysForge uses FHS-correct system roots and XDG Base Directory-correct user roots. The user side honours `$XDG_CONFIG_HOME`, `$XDG_CACHE_HOME`, and `$XDG_STATE_HOME` when set, falling back to their spec defaults.

| Location | Purpose |
|----------|---------|
| `/etc/sysforge/` | Shipped config (read-only, package-owned) |
| `/var/lib/sysforge/` | Runtime state (build_state, pipeline_state, source_meta, hardware_profile, sysforge.log) |
| `/var/cache/sysforge/` | Regenerable build cache (LLVM PGO profdata store; override via `SYSFORGE_PGO_STORE`) |
| `$XDG_CONFIG_HOME/sysforge/` (default `~/.config/sysforge/`) | User config overrides |
| `$XDG_CACHE_HOME/sysforge/` (default `~/.cache/sysforge/`) | AUR name cache (refreshed every 24h) |
| `$XDG_STATE_HOME/sysforge/` (default `~/.local/state/sysforge/`) | Fallback for runtime state when `/var/lib/sysforge` is not writable |

On first run, `sysforge` migrates the legacy consolidated dirs (`~/.config/sysforge/{cache,state}`) into their XDG-correct homes (`$XDG_CACHE_HOME/sysforge`, `$XDG_STATE_HOME/sysforge`). Migration is idempotent and best-effort — a failure logs a warning but does not block startup.

### State directory

Pipeline state is written to `/var/lib/sysforge/` by default. Override via the `SYSFORGE_STATE_DIR` environment variable or `--state-dir` CLI flag; CLI takes priority. Both are logged when present. `SYSFORGE_STATE_DIR` is a SysForge bootstrap var and is intentionally not subject to the build tool env isolation rule.

The configure stage creates a `sysforge` system group and sets the state directory to `root:sysforge` with mode `02775` (setgid: files written into the dir inherit the `sysforge` group). The recursive chown also normalises any state files written earlier in the same pipeline (`sysforge.log`, `pipeline_state.toml`) — without it, those files stay `root:root` and block the post-reboot `--resume` for the primary user. `open_unified_log` further `chmod 0o664`s the log on creation so future appends by other group members succeed. The builder user is added to the group during bootstrap; additional admin users can be added via `usermod -aG sysforge <user>`. If `/var/lib/sysforge` is not writable (e.g. standalone usage without bootstrap), the state dir falls back to the XDG state dir (`$XDG_STATE_HOME/sysforge`, default `~/.local/state/sysforge`).

### Profile conf override

Both `sysforge build` and `sysforge pipeline` accept `--profile-conf FILE` to substitute an alternate `profiles.toml` at runtime, bypassing the default user/system search paths. The override carries flag profiles, conflict groups, and consumes inference together (all sections live in the one file). If the specified file sets `extends_system = true`, the standard system config is still merged underneath it via the normal `extends_system` logic.

### Global settings (`sysforge.toml`)

`/etc/sysforge/sysforge.toml` holds global settings that don't belong in flag profiles or package manifests. Loaded by `load_sysforge_toml()` in `config.py`; returns `{}` if the file is missing.

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `[ui]` | `editor` | — | Editor for reconfigure stage (overridden by `SYSFORGE_EDITOR` env) |
| `[git]` | `fetch_timeout` | `30` | Seconds before a `git fetch` times out during source sync (0 = no limit). Legacy alias: `pull_timeout` |
| `[git]` | `clone_timeout` | `60` | Seconds before `git clone` / `pkgctl repo clone` times out (0 = no limit) |
| `[build]` | `python` | `system` | Python interpreter for PKGBUILD `build()` steps, pinned ahead of any pyenv/asdf/conda shim on `PATH` so a bare `python` resolves to the interpreter its `python-*` makedepends were installed against. `system` / unset → `/usr/bin/python`; a bare version like `3.12` → `/usr/bin/python3.12`; or an absolute path. Resolved choice logged at DEBUG; an unusable value warns and falls back to the system python |
| `[aur]` | `min_fetch_interval_ms` | `500` | Minimum gap between consecutive git fetches against aur.archlinux.org (millisecond resolution) |
| `[aur]` | `rate_limit_abort_s` | `300` | If AUR returns a `Retry-After` ≥ this many seconds, the remaining sync batch is aborted rather than waited out |

### Hardware overlays

The hardware detection stage emits `hardware_profile.toml` which feeds kconfig automation and gates hardware-specific packages in `packages.toml`. Key machine-specific caveats (Ryzen 7 5800X3D + RTX 5070):

- Explicit disable of `nouveau`
- CPU-specific flags: `CONFIG_MZEN3`, `CONFIG_X86_AMD_PSTATE`

---

