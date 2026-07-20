## Logging

All log output goes to stderr. Format: `[SYSFORGE][LEVEL][TAG] message`

Verbosity controlled by `-v`/`-vv`/`-vvv` on the CLI:
- Default: `[ERROR]` only (plus `ui()` primary output, which is verbosity-immune)
- `-v`: adds `[WARN]`
- `-vv`: adds `[INFO]`
- `-vvv`: adds `[DEBUG]` — full body dumps of every loaded config, resolved profile, conflict groups, inference map, and temp makepkg.conf

**The level rubric** (the audit authority — every call site is classified against it):

| Level | Fn | Gate | Reserved for |
|---|---|---|---|
| UI | `ui()` | always | The primary output the user ran the command to see — final summaries, `doctor` findings, `state`/`log`/`env` bodies, prompts, tables. **Not** progress narration. |
| ERROR | `error()`/`fatal()` | always | Failures that abort or degrade the run. |
| WARN | `warn()` | `-v` | Recoverable anomalies: skips, fallbacks, soname/ABI mismatches that don't block. |
| INFO | `info()` | `-vv` | Progress/status narration: "syncing X", "wrote temp conf", "building 3/7". |
| DEBUG | `debug()` | `-vvv` | Full body dumps: config/profile/conf contents, resolved argv, env snapshots. |

Decision test for each site: *is this the answer, or narration about producing the answer?* The answer → `ui()`; narration → `info()` (or `debug()` for full dumps). `ui()` is verbosity-immune and reserved for primary output only — it is **not** a "make this always show up" escape hatch. File logs are unaffected: every level is always written to file regardless of stderr gating, so a demotion never loses forensic detail.

**Configurable default verbosity.** The stderr level when no flag is passed is resolved once at CLI entry by `cli._resolve_verbosity(args)` (mirroring `_resolve_color_mode`), which calls the single `log.set_verbosity` seam — no resolution logic leaks into `log.py`. Precedence (highest first):

1. Global `--quiet` → level 0 (wins over everything; distinct `quiet_global` dest so it never clobbers `doctor`'s local `--quiet/-q`, and it is not hoisted ahead of the `doctor` subcommand).
2. Else `-v/-vv/-vvv` (argparse `count` > 0) → that level.
3. Else the `[log] verbosity` key in `sysforge.toml` (integer 0–3, clamped; non-int/unreadable ignored → never aborts startup).
4. Else 0.

The shipped default stays 0; the config key lets a user opt into a quieter or more verbose baseline without changing it. Golden-output regression tests assert that default-level (`verbosity=0`) output of a representative dry-run carries no `[INFO]`/`[WARN]` lines — the primary guard against future features re-leaking narration into `ui()`. Two stages anchor the guard: the day-to-day `packages` stage (`_load_packages`) and, extending the sweep to the interactive/bootstrap path, the `configure` stage's dry-run (both in their respective stage tests). The re-levelling audit covers every stage — `partition`/`install`, `hardware`, `configure`, `reconfigure`, `kernel`, `toolchain` — demoting progress narration ("Probing hardware…", "Building kernel…", "[PGO] 3/4 complete", per-step "wrote/synced/installed" confirmations) to `info()` while keeping prompts, plan tables, section headers, dry-run previews, and check results as `ui()`. Tests run at verbosity 2 (all messages visible).

### Colour

`log.py` is the single colour authority for the whole codebase. `log.use_color()` is the one gate every output site consults, and `log.bold()` / `dim()` / `red()` / `green()` / `yellow()` / `cyan()` are the shared helpers that wrap text only when the gate is on — no site hand-writes escape codes. `ui/headers.py` and `ui/progress.py` import these rather than carrying their own ANSI constants.

Resolution precedence in `use_color()`:

1. Colour **mode** (`log.set_color_mode`, set once at CLI entry): `"never"` → off; `"always"` → on (beats the environment, so colour survives being piped into a pager or colour-aware tool).
2. Mode `"auto"` (default): `NO_COLOR` (any non-empty value) disables; then `FORCE_COLOR` (any non-empty value) forces on; otherwise colour follows whether the active stream is a TTY.

The mode is resolved at startup as **`--color=auto|always|never` flag > `[ui] color` config (`sysforge.toml`) > `"auto"`** (`cli._resolve_color_mode`); a junk value degrades to `"auto"`. File logs are always written plain regardless of the gate. Because the decision is per-call, output piped through the pager is coloured up front (the review diff passes `git diff --color=always` when the gate is on, then `less -R` carries the ANSI through).

### Glyph downgrade

`log.use_unicode()` is the single capability gate for decorative non-ASCII glyphs (arrows, `✓`/`✗`, box-drawing, ellipsis, block-bar fills), parallel to `use_color()`. When it returns false, `log.downgrade_glyphs(text)` rewrites those code points to ASCII fallbacks (`→`→`->`, `✓`→`[OK]`, `─`→`-`, …) via a single `str.translate` table; when true it is a pass-through. This exists because a Linux framebuffer/VT console (`TERM=linux`) loads a console font that maps only a subset of code points, so the install-time pipeline rendered missing-glyph boxes on bare-metal/VM consoles.

Resolution precedence in `use_unicode()`:

1. Unicode **mode** (`log.set_unicode_mode`): `"never"` → off; `"always"` → on.
2. Mode `"auto"` (default): `SYSFORGE_ASCII` (any non-empty value) disables; a stream whose `encoding` is a known non-UTF value disables; `TERM=linux` disables; otherwise Unicode is allowed (an unknown/`None` encoding stays Unicode so test capture sinks aren't over-stripped).

Downgrade happens **only at the terminal-output chokepoints** — `log.ui()`, `log._format_line()` (error/warn/info/debug), `prompt.py`'s prompt strings, `ui/progress.py::_paint`, and the `partition` stage's plan-table `print()` — never at every call site. The UTF-8 file logs are written from the caller's original text and therefore always keep the real glyphs. In `progress._paint` the downgrade precedes the column clamp because ASCII fallbacks change string length.

### File logging

File logging runs at full verbosity regardless of the `-v` level — every `[INFO]`, `[WARN]`, and `[ERROR]` line is written to file even when the terminal shows only errors. Never let file I/O break a build: all file write errors are silently swallowed.

**Unified log** — one consolidated file for the entire run.

- Default path: `<state_dir>/sysforge.log` (i.e. `/var/lib/sysforge/sysforge.log`). Per verb: `sysforge update` → `<state_dir>/sysforge-update.log`; `sysforge build` → `<state_dir>/sysforge-build.log`. Every other substantial verb (`doctor`, `fetch`, `revert-to-stock`, `uninstall`, `setup`, `config merge`, `state repair/forget`) opts into the same pattern — `sysforge-<name>.log` — via the base-class flag `Verb.wants_run_log`, opened/closed by the verb runner (purged before `execute`, kept on success; a failed run is closed with a `FAILED` marker in the log). Trivial list/passthrough printers (`env`, `log`, `search`, `completions`, `packages list`, `state list/failed`) and read-only reports (`resolve`, `state orphans`) stay opted out; `doctor` opts back out for `--apply`, which delegates its rebuild to `sysforge update`'s own log. A standalone `run <stage>` invocation writes `sysforge-run-<stage>.log` and keeps it on success (an operator-inspection artifact); the full `run pipeline` keeps its own shared `sysforge.log`.
- `sysforge update` and `sysforge build` always truncate at run start and **keep** the log afterwards (so a multi-package run leaves one consolidated record next to the per-package logs). `sysforge run pipeline` appends across runs and clears on success; standalone `run <stage>` keeps its per-stage log on success instead.
- A `# log cleared after successful run` marker is left in the file after truncation.
- `--log-dir <path>` overrides the directory.
- `--purge-log` (`run pipeline` only) truncates before the run starts.
- `--persist-log` suppresses truncation on success. Use when you want to keep the log for post-run analysis.
- `--no-unified-log` (`run pipeline` only) disables the unified log for this run.

The lifecycle primitive (`log.open_unified_log` / `close_unified_log`) is the single home; its callers differ by verb shape. `run pipeline` opens it inside the pipeline runner (`run_pipeline`); standalone `run <stage>` opens it inside `run_stage_standalone`, deriving `sysforge-run-<stage>.log` and passing `persist=True` so it survives success; `update` opens it inside `cmd_update` (it owns a fine-grained success calc over failed/install/pacman state). Every other substantial verb declares only a basename via the base-class flag `Verb.wants_run_log` (or, for a conditional/non-derived basename, the `unified_log_basename(args)` override) and the **verb runner** (`verbs/runner.py`) opens it purged before `execute` and closes it kept afterwards — `build`, `doctor`, `fetch`, `revert-to-stock`, `uninstall`, `setup`, `config merge`, and `state repair/forget` all opt in this way (`build` routes through `build_core`, not the pipeline runner, so it would otherwise have no run-level log). `doctor` uses the `unified_log_basename(args)` override rather than the bare flag so it can opt back out for `--apply`, whose rebuild delegates to `cmd_update`'s own `sysforge-update.log` on the same process-global handle. Trivial list/passthrough printers and read-only reports (`resolve`, `state orphans`) leave `wants_run_log` at its `False` default to opt out.

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

### journald mirror (2.3.0-F6)

The unified run-log is the authoritative, user-facing capture. Complementing it,
every **sentinel-gated** (system-mutating) verb also emits one structured record
to the systemd journal via `primitives/journal.py`, so SysForge's changes appear
in `journalctl` alongside everything else that touched the system — where an
admin looks during incident review. This is additive and never load-bearing: on
a non-systemd host (no journal socket) it is a silent no-op.

Records carry queryable fields:

    journalctl -t sysforge                 # all SysForge mutations
    journalctl SYSFORGE_VERB=build         # just build invocations
    journalctl SYSFORGE_TARGET=mesa        # mutations touching a package
    journalctl -p err -t sysforge          # failed mutations (PRIORITY=3)

Fields are `SYSFORGE_`-prefixed to avoid colliding with journald's reserved
well-known names. Emission is keyed off `Verb.requires_sentinel` in
`verbs/runner.py`, so any future mutating verb is mirrored automatically.

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

