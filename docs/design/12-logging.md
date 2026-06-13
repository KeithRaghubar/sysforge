## Logging

All log output goes to stderr. Format: `[SYSFORGE][LEVEL][TAG] message`

Verbosity controlled by `-v`/`-vv`/`-vvv` on the CLI:
- Default: `[ERROR]` only
- `-v`: adds `[WARN]`
- `-vv`: adds `[INFO]`
- `-vvv`: adds `[DEBUG]` — full body dumps of every loaded config, resolved profile, conflict groups, inference map, and temp makepkg.conf

Set once at CLI entry via `log.set_verbosity(args.verbose)`. Tests run at verbosity 2 (all messages visible).

### Colour

`log.py` is the single colour authority for the whole codebase. `log.use_color()` is the one gate every output site consults, and `log.bold()` / `dim()` / `red()` / `green()` / `yellow()` / `cyan()` are the shared helpers that wrap text only when the gate is on — no site hand-writes escape codes. `ui/headers.py` and `ui/progress.py` import these rather than carrying their own ANSI constants.

Resolution precedence in `use_color()`:

1. Colour **mode** (`log.set_color_mode`, set once at CLI entry): `"never"` → off; `"always"` → on (beats the environment, so colour survives being piped into a pager or colour-aware tool).
2. Mode `"auto"` (default): `NO_COLOR` (any non-empty value) disables; then `FORCE_COLOR` (any non-empty value) forces on; otherwise colour follows whether the active stream is a TTY.

The mode is resolved at startup as **`--color=auto|always|never` flag > `[ui] color` config (`sysforge.toml`) > `"auto"`** (`cli._resolve_color_mode`); a junk value degrades to `"auto"`. File logs are always written plain regardless of the gate. Because the decision is per-call, output piped through the pager is coloured up front (the review diff passes `git diff --color=always` when the gate is on, then `less -R` carries the ANSI through).

### File logging

File logging runs at full verbosity regardless of the `-v` level — every `[INFO]`, `[WARN]`, and `[ERROR]` line is written to file even when the terminal shows only errors. Never let file I/O break a build: all file write errors are silently swallowed.

**Unified log** — one file for the entire run.

- Default path: `<state_dir>/sysforge.log` (i.e. `/var/lib/sysforge/sysforge.log`). For `sysforge update`: `<state_dir>/sysforge-update.log`.
- `sysforge update` always truncates at run start. `sysforge run pipeline` appends across runs and clears on success.
- A `# log cleared after successful run` marker is left in the file after truncation.
- `--log-dir <path>` overrides the directory.
- `--purge-log` (`run pipeline` only) truncates before the run starts.
- `--persist-log` suppresses truncation on success. Use when you want to keep the log for post-run analysis.
- `--no-unified-log` (`run pipeline` only) disables the unified log for this run.

**Per-package log** — one file per package build, written alongside the PKGBUILD.

- Path: `<pkgbuild_src_dir>/<pkgname>/sysforge_<pkgname>.log`
- Same lifecycle as the unified log: appends across builds, cleared on success unless `--persist-log`.
- Written by both `sysforge pipeline` (via the packages stage) and `sysforge build`.
- `--no-pkg-logs` disables per-package logs for `sysforge pipeline`.

**CLI flag summary:**

| Flag | Command | Effect |
|---|---|---|
| `--no-unified-log` | `run pipeline` | Disable unified log for this run |
| `--no-pkg-logs` | `run pipeline`, `run packages`, `run kernel` | Disable per-package logs for this run |
| `--no-pkg-log` | `build` | Disable the per-package log for this build |
| `--log-dir <path>` | `run pipeline`, `run packages`, `run kernel`, `build` | Override log file directory |
| `--purge-log` | `run pipeline` | Truncate unified log before run |
| `--persist-log` | `run pipeline`, `run toolchain`, `run packages`, `run kernel`, `build` | Keep log files after success |
| `--rebuild-profdata` | `run toolchain` | Force full 4-pass PGO build even if compatible profdata exists |
| `--auto-pgo` | `run toolchain` | Bypass all PGO confirmation prompts (required for non-interactive PGO runs) |
| `--cleansrc` | `build`, `update`, `fetch`, `run toolchain` | Purge each package's src dir and re-clone before fetching/building. Refuses (per package) on uncommitted changes, ahead-of-upstream commits, or `diverged_user` state |
| `--cleansrc-force` | `build`, `update`, `fetch`, `run toolchain` | Like `--cleansrc` but bypasses the dirty/diverged guard. Use when the upstream rewrote history (e.g. Arch packaging repos force-push every release) and the local commits have no value to preserve |

### Tags in use

**Core build subsystem** (`makepkg_wrapper.py` and related):

| Tag | Covers |
|---|---|
| `[ABI]` | ABI compatibility checks — soname comparison before and after build |
| `[BUILD]` | makepkg invocation, exit codes, patched PKGBUILD lifecycle |
| `[CACHE]` | ccache/sccache passive monitoring (per-build hit/miss delta, system probes) |
| `[CONF]` | Temp makepkg.conf generation, active consumes set |
| `[ENV]` | Env var routing; per-key shell strip (INFO); skipped keys not in active_consumes (INFO); unclassified profile key warnings (WARN) |
| `[FLAG]` | Flag-string transforms in `makepkg_flags` (linker detect/inject/replace, full-LTO & lld-flag strips, lib32 `-march` scrub). Conf-time flag adjustments (CLI `--cc`/`--cxx`/`--ld`, linker guard) log under `[CONF]` (P2b.6a); profile append-merge flag logging (conflict-group firing, token replacement) moved to `[PROFILE]` (P3.1) |
| `[GIT]` | Local git plumbing in `git_ops` — fetch/compare, dirty detection, safe purge (split out of `aur` in P2e). The build orchestrator's own source-sync result logs under `[BUILD]` (P2b.6c) |
| `[BUILD_PREP]` | Pre-build source acquisition — pkgctl checkout + validpgpkey import (`build_prep`; split from `[BUILD]` in P3.2) |
| `[KERNEL]` | Kernel stage: lsmod snapshot, kconfig fragment, build, post-install. The build orchestrator's kernel `LLVM=1` injection logs under `[BUILD]` (P2b.6c) |
| `[MAKEPKG]` | makepkg subprocess invocation + sudo-timeout retry: build status, inherited-shell-env scrub, toolchain-mismatch note, retry prompts (P2b.6b) |
| `[PATCH]` | PKGBUILD flag extraction, patching, artifact lifecycle; noninteractive kconfig target replacement |

**`[FLAG]` coverage (by design, partial).** Emitted for: CLI toolchain overrides (`--cc`, `--cxx`, `--ld`), linker token replacement and injection, linker guard stripping, RUSTFLAGS linker reconciliation, GCC thin-LTO rewrite, GCC+lld LTO disabling. Profile-side append-merge logging — conflict-group firing (group name, evicted tokens, inserted token) and prefix-match token replacement during `merge_extends` — moved to `[PROFILE]` in P3.1. Not emitted for: `apply_patch_pkgbuild` token changes (those use `[PATCH]`).

`[PGO]` was retired in P2b.6c: PGO build narration now logs under `[BUILD]` (build path) and `[TOOLCHAIN]` (toolchain stage), with "PGO"/"profdata" carried in the message text rather than a dedicated tag.

**Profile / config subsystem:**

| Tag | Covers |
|---|---|
| `[CONFIG]` | Config file loading (`profiles.toml`: flag profiles, conflict groups, consumes inference) |
| `[PROFILE]` | Profile resolution, rule matching, extends chain, group resolution, consumes inference, and append-merge — the per-facet `[CONF]`/`[FLAG]`/`[GROUPS]` loggers collapsed into one `[PROFILE]` in P3.1 |
| `[STATE]` | Pipeline state directory resolution |

**AUR / package management:**

| Tag | Covers |
|---|---|
| `[AUR]` | AUR name cache lifecycle, clone operations, and RPC queries (the separate `[MANIFEST]` tag collapsed into `[AUR]` in P3.5) |
| `[AUR_RESOLVE]` | Transitive AUR dependency-graph resolution / build order (`aur_resolve`; split from the `resolve` verb's `[RESOLVE]` in P3.3) |
| `[DEP]` | Soname dependency graph checks |
| `[DOCTOR]` | `sysforge doctor` — installed-package depends + linkage health check (was `[DOC]` before P3.4) |
| `[FAILURE]` | Failure scenario dispatch |
| `[GFX]` | `graphics_probe` — system-state graphics checks (kernel params, compositor protocols, driver skew) |
| `[PACMAN]` | pacman database and install operations |
| `[PROV]` | `provides_lookup` — reverse soname → package via `pacman -Fq` |
| `[VERSION]` | Package version comparison |

**Pipeline stages:**

| Tag | Covers |
|---|---|
| `[BASE_INSTALL]` | Stage 2: pacstrap, genfstab |
| `[CONFIGURE]` | Stage 4: hostname, locale, bootloader, user, services |
| `[HARDWARE]` | Stage 3: CPU/GPU/NVMe detection |
| `[PACKAGES]` | Stage 7: package build progress |
| `[PARTITION]` | Stage 1: GPT partitioning, mkfs, mount |
| `[PIPELINE]` | Stage sequencing, checkpoint events |
| `[RECONFIGURE]` | Stage 5: pre-build checkpoint and config review |
| `[TOOLCHAIN]` | Stage 6: LLVM/GCC build, PGO pass orchestration |

**Commands:**

| Tag | Covers |
|---|---|
| `[CLI]` | CLI entry point (invocation logging). The `build` verb logs under `[BUILD]` like the rest of the build subsystem (P3.3) |
| `[ENV_CHAIN]` | `sysforge env` — OS environment-inheritance chain snapshot (distinct from `[ENV]` build-env routing; P3.3) |
| `[FETCH]` | `sysforge fetch` — PKGBUILD download/update |
| `[REVIEW]` | PKGBUILD review gate (`primitives/pkgbuild_review.py`) — source-change diff prompt before building |
| `[UPDATE]` | `sysforge update` — version check, toolchain-variant + flag drift (Phase 4.3, via the `flag_drift` primitive), and rebuild |

---

