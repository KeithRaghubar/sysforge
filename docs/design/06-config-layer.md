## Config Layer

### Config file hierarchy

- System default: `/etc/sysforge/profiles.toml`
- User override: `~/.config/sysforge/profiles.toml`

`profiles.toml` is a single file holding flag profiles, `[[rules]]`, `[append_conflict_groups]`, and `[consumes_inference]`. By default the user file **fully replaces** the system file. To layer on top instead, add `extends_system = true` at the top of the user file — user values take priority on all conflicts. User rule priorities are bumped by 100 on merge (range 100–199) to always outrank system rules (range 0–99).

### Adopting new shipped defaults

The non-`profiles.toml` configs (`packages.toml`, `toolchain.toml`, `kernel.toml`, `sysforge.toml`) are read from a single resolved path with no per-key fallback to shipped defaults, so a live config does **not** automatically gain keys/sections added by a new release. On an installed system pacman's `backup=()` + `.pacnew` reconciliation covers this (and `doctor --pacman` warns about unmerged `.pacnew`). In a from-repo dev setup — where `SYSFORGE_CONFIG_DIR` points at a working tree and pacman never touches the config — `make sync-config` (`tools/sync_config.py`) fills the gap.

**Config-dir resolution.** `SYSFORGE_CONFIG_DIR`, when set, is the directory that *directly contains* the TOML files (e.g. `~/sf-config/kernel.toml`) — it is **not** an FHS root prefix, mirroring how `SYSFORGE_STATE_DIR` holds state files directly. When unset, the config dir is the FHS system path `/etc/sysforge`. The single resolution home is `primitives/paths.py` (`CONFIG_DIR` + the `*_PATH` constants); `tools/sync_config.py` and `tests/conftest.py` mirror it. (The installed-system path is unchanged: with the env unset, everything still resolves under `/etc/sysforge`.)

`sync-config` is an **add-only**, comment-preserving merge from `etc/sysforge/*.toml` into the live config dir (`$SYSFORGE_CONFIG_DIR` itself, else `/etc/sysforge`, or `--target DIR`): it injects keys, tables, and their leading comment blocks the live file is missing, and never overwrites a value the live file already sets (even if the shipped default changed). Arrays-of-tables (`[[package]]`) are user content and are left untouched. Bare keys are spliced before the first table header (TOML adjacency rule); a new table is spliced **after its nearest shipped-order predecessor that the live file actually has** — falling back through the shipped order, and to EOF only when there is no such predecessor — so the live file keeps shipped section order. Order matters even though TOML ignores it (`3.1.0-B9`): appending at EOF reorders the live file a little further with every sync, and the `.sfnew` adoption step below is a line-based diff with no move detection, which renders a merely relocated section as a deletion in one hunk and an addition far away in another — the reading that invites an operator to hand-merge away a section that was never gone. A run of consecutive new tables anchors on each other, staying in shipped order; insertion lands immediately after the anchor's own entry, before any standalone comment run tomlkit holds for the following table, so those comments stay attached to what they document. Pre-existing order drift is **not** rewritten — the tool never does a wholesale rewrite of a live file it did not lay out. The merge is **key-anchored**, so it can only carry comments that lead an active key it injects — pure documentation comments and *commented-out example* settings (`# interactive = true`) have no key to anchor to. When a shipped file gains such content the live file lacks, the shipped file is written verbatim beside the target as `<name>.sfnew` (pacnew-style) for the operator to diff and adopt; a stale `.sfnew` is removed once the drift is resolved. Drift detection is line-level and value-aware: a commented example the live file has **already adopted** by uncommenting it into an *identical* active line (`# interactive = true` shipped vs `interactive = true` live) is not drift and does not spill a `.sfnew`, but a differing value or a trailing inline note still does. One drift class is **structural** rather than textual and blocks the merge outright: a section header that is active in the shipped file but only present *commented out* in the live file (a pre-`3.0.0-STD1` vintage still carrying `# [build]`). A commented header does not disable its section — it reassigns every key beneath it to the preceding table or to the top level, so the settings stay syntactically valid while being read from the wrong place. The add-only merge cannot flip an existing line from commented to active, and worse, it would see the table as absent and append a *second* copy holding the shipped defaults, superseding the operator's orphaned value on reparse. Header liveness is therefore compared in **both** directions on the pre-merge text (the comment-signature subtraction cannot see this, being one-directional — activating a header removes its commented form from shipped); on a hit the file reports status `needs merge`, the write is **skipped entirely** rather than merged into a structure the tool has misread, and the `.sfnew` companion is spilled pointing at `sysforge config merge`. A header commented out in the *shipped* file is an example block (`#[group.cosmic]`), not a live section, and is excluded. `tomlkit` is a **dev-only** dependency (ephemeral `uv run --no-sync --with tomlkit`), never added to `pyproject.toml`. `--dry-run` reports without writing. `bootstrap.toml` is excluded (per-host, no live counterpart).

**Adopting the `.sfnew` residue — `sysforge config merge`.** Hand-merging the `.sfnew` companions is the only remaining manual step, so `sysforge config merge` (verb `config-merge`, `config_cmd.py`) is a pacdiff-style interactive driver over them. It scans the resolved config dir (`--config-dir` override, else `paths.CONFIG_DIR`) for `*.sfnew` — plus pacman's own `*.pacnew`/`*.pacsave` for sysforge config files on a packaged install — and for each presents: `[v]iew` (a `difflib` unified diff through `$PAGER` via `maybe_pager`, prefixed by a **relocated-section banner** naming any header the diff shows as both removed and added — a header on both a `-` and a `+` line is present in both files, so the pair is a move, not a deletion; `difflib` has no move detection of its own), `[m]erge` (launch the resolved diff/merge tool with `live new`, then re-loop), `[s]kip`, `[r]emove` (drop the companion once the live file is satisfactory), `[o]verwrite` (copy the companion over the live file verbatim — guarded by a confirm and a "discards your local values" warning, never the default, intended for the `.pacnew` accept-maintainer case), and a[b]ort. Because a `.sfnew` is the *verbatim shipped file*, there is **no blind "accept theirs"** as a primary action — the safe path is merge-then-remove. The verb edits config files in place but never builds/installs, so it carries **no sentinel**. The diff/merge tool resolves through one home shared with the reconfigure editor chain — `primitives/editor.resolve_merge_tool` (`SYSFORGE_MERGE` > `sysforge.toml [ui].merge` > `$DIFFPROG` > `vimdiff`) launched via `run_tty_argv` (the `/dev/tty` passthrough, also used by `resolve_editor`'s callers, which additionally runs the child inside `ui.progress.suspended()` so an active progress bar's reserved bottom row is released before a full-screen editor takes the terminal). `--list`/`--dry-run` reports the companion→target pairs without prompting (scripting/CI).

### Dev fixtures vs. personal config

`tests/data/etc/sysforge/` is the **git-tracked test fixture set** wired in `tests/conftest.py` (which *forces* `SYSFORGE_CONFIG_DIR` to that dir directly, so a developer shell exporting its own value cannot leak into the suite). It is kept in shipped↔fixture parity by `make check-shipped`. A developer's **personal live config** is a separate, untracked dir (e.g. `~/sf-config`, holding the TOML files directly) that the shell's `SYSFORGE_CONFIG_DIR` points at and that `make sync-config` services — keeping personal config out of the tracked tree while leaving the fixtures deterministic.

#### Shipped-config comment style

Every key in `etc/sysforge/*.toml` is documented by the contiguous run of `#`
lines **immediately above** the line that assigns it — an active assignment or a
commented-out example, both anchored by the same `key =` shape:

    # key — one-line summary, then wrapped prose naming every accepted value
    #   form with an example for each.
    # key = "example"

A multi-line prose block is never placed *after* a key's line. `tools/sync_config.py`
is **key-anchored** — it can only carry a comment block that *leads* an active key it
injects — so trailing prose would be invisible to config adoption. The leading block
also gives `check_shipped`'s `config_comments` group an unambiguous, machine-readable
boundary: it walks upward from the key's own line (which it always includes) and stops
at the first non-comment line or at the previous key's own anchor line, whichever comes
first — so one key's block can never absorb a neighbour's paragraph.

Carve-outs: section-divider banners (`# ==== … ====`, `# ── [paths] ──`) lead a section
rather than a key, and a short unit/enum hint *may* trail a value on that same anchor
line, counting as part of that key's documentation because the anchor line is always
included (`nice = 19  # 0..19`, `ionice = "idle"  # IO class: "idle" | "best-effort"`).
Anything needing more than one line still gets a leading block.

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
| `[ui]` | `editor` | — | Editor for reconfigure stage (overridden by `SYSFORGE_EDITOR` env; one of three persistence targets offered by the reconfigure editor step — see §Pipeline Layer) |
| `[ui]` | `merge` | — | Diff/merge tool for `sysforge config merge` (`.sfnew` adoption). Resolved `SYSFORGE_MERGE` env > this > `$DIFFPROG` > `vimdiff`; accepts args (`"nvim -d"`, `"meld"`). Shares one home with the editor chain in `primitives/editor.py` (`resolve_merge_tool`/`resolve_editor`/`run_tty_argv`) |
| `[git]` | `fetch_timeout` | `30` | Seconds before a `git fetch` times out during source sync (0 = no limit). The `pull_timeout` alias was removed in 3.0.0; a stale key warns once and is ignored |
| `[git]` | `clone_timeout` | `60` | Seconds before `git clone` / `pkgctl repo clone` times out (0 = no limit) |
| `[build]` | `python` | `system` | Python interpreter for PKGBUILD `build()` steps, pinned ahead of any pyenv/asdf/conda shim on `PATH` so a bare `python` resolves to the interpreter its `python-*` makedepends were installed against. `system` / unset → `/usr/bin/python`; a bare version like `3.12` → `/usr/bin/python3.12`; or an absolute path. Resolved choice logged at DEBUG; an unusable value warns and falls back to the system python |
| `[build]` | `nice` | — | Build CPU-throttle: scheduling niceness `0..19` (out-of-range clamped). Applied as a `nice -n` front-end on the makepkg invocation — soft yield, full speed when idle. See §Flag/Profile System (build throttling) |
| `[build]` | `ionice` | — | Build IO-throttle class: `"idle"` or `"best-effort"`. Applied as `ionice -c {3,2}` |
| `[build]` | `cpu_quota` | — | Hard CPU ceiling, either `"N%"` (100% = one core) or a decimal fraction of total cores (`0.5` → half the host, translated against `os.cpu_count()` for portability). Enforced by wrapping makepkg in a transient `systemd-run --scope --user -p CPUQuota=N%` (folds in `Nice=`/`IOSchedulingClass=`); degrades to nice/ionice with a warning when `systemd-run` is absent or the user slice lacks CPU-controller delegation |
| `[build]` | `jobs` | — | Parallel build jobs; rewrites the `-jN` token in the emitted `MAKEFLAGS` (appends `-jN` if absent — make honours the last `-j`, so a `-j$(nproc)` baseline is still capped) |
| `[build]` | `mem_limit` | — | Per-build memory ceiling so a runaway build (OOM-prone link, sanitizer, LTO) can't take down the workstation. A byte count or binary-suffixed size (`"24G"`, `"512M"`). Off the `cpu_quota` path it's applied as an `RLIMIT_AS` clamp in the makepkg child's preexec; on the `cpu_quota` (`systemd-run --scope`) path it's delivered as a tighter cgroup `-p MemoryMax=` instead, so the two mechanisms never double-apply. Junk/zero/negative dropped with a warning. See §Flag/Profile System (build throttling) |
| `[build]` | `repo_track` | `"stable"` | Which sync-DB release a `source = "repo"` checkout tracks: `"stable"` pins to the release tag matching pacman's currently-resolvable repo version (`get_repo_candidate_version` — the sync DB cross-checked against a `checkupdates` side-copy refresh, so a stale local DB cannot pin a superseded release) so a source build matches what pacman would install; `"main"` leaves the checkout on the packaging repo's default branch (testing-track). Single read chokepoint `config.resolve_repo_track`; unrecognised values **warn and** fall back to `"stable"` via the shared `config.resolve_enum(raw, known, default, *, key)` seam (the one home for known-vocabulary string options — validate, warn on mismatch, fall back to the documented default). `resolve_repo_mode` routes through the same seam (falling back to `"pacman"`) — this lenient path serves the several defensive readers that only compare the resolved value. Its **authoritative** load point, `packages._load_packages`, is instead strict: it validates the raw `repo_mode` token against `config.REPO_MODE_ACCEPTED_INPUTS` and **hard-fails** on a typo, because silently falling back to `"pacman"` at that boundary would drop the source builds the user configured (strict-at-the-boundary, lenient-in-the-interior). The legacy `"profiled"` token was removed in 3.0.0 and is rejected by both. Distinct from packages.toml's `repo_mode` (source-vs-pacman install strategy) — this key only affects *which commit* a repo checkout sits on |
| `[aur]` | `min_fetch_interval_ms` | `500` | Minimum gap between consecutive git fetches against aur.archlinux.org (millisecond resolution) |
| `[aur]` | `rate_limit_abort_s` | `120` | If the accumulated AUR `Retry-After` penalty would exceed this many seconds, the remaining sync batch is aborted rather than waited out |
| `[mesa]` | `filter_drivers` | `false` | Opt-in master switch for hardware-filtering mesa's gallium/vulkan drivers (the meson analogue of LLVM target filtering). Off → mesa builds every upstream driver. On → `mesa_drivers.resolve_or_detect_mesa_drivers` trims `-D gallium-drivers=` / `-D vulkan-drivers=` to the detected GPU vendors, always keeping the mandatory software baseline (gallium `llvmpipe`/`softpipe`/`zink`, vulkan `swrast`/lavapipe) |
| `[mesa]` | `gallium` | — | Optional explicit gallium-driver override list (non-empty pins the axis, still baseline-enforced; absent → autodetect). Tokens must be valid meson gallium drivers |
| `[mesa]` | `vulkan` | — | Optional explicit vulkan-driver override list (same semantics) |
| `[security]` | `freeze_sources` | `false` | Master switch for the source freeze: when `true`, all new source egress (AUR clones, `pkgctl repo clone` checkouts of official-repo packages, source-sync fetches, both `vcs_pkgver` seams) is refused for the run — existing checkouts still build. Resolved by `net_policy.resolve_net_policy` via the shared `config.resolve_flag_default(args, "frozen", cfg, "freeze_sources")` seam, with precedence `--no-frozen` > `--frozen` > `[security] freeze_sources` > `false` (`--no-frozen` is an explicit-off wrapper on top of the shared seam, which has no "explicit false" concept). `--thaw PKG[,PKG...]` is a per-run *lift*, never a switch — it narrows an already-active freeze rather than enabling one. A refused package surfaces as the `STATUS_FROZEN` scheduler blocker (see §Primitives Layer → `source_sync.py`) |
| `[security]` | `sandbox_builds` | `false` | Master switch for the **build sandbox**: when `true`, every package build runs inside a clean container (devtools' `makechrootpkg`) instead of as the invoking user, so a PKGBUILD's `prepare()`/`build()`/`package()` cannot read `~/.ssh`, GPG material or browser profiles. The orthogonal half of `freeze_sources` — that gates code *ingress*, this gates blast *radius*. Resolved once at CLI entry by `build_sandbox.resolve_sandbox`, consulted at the makepkg invocation seam via `build_sandbox.get_policy()`, and overridable per profile with `sandbox_builds` in `profiles.toml` (`build_sandbox.for_profile`, two-way). Requires `devtools` and an existing `<sandbox_chroot_dir>/root`; a build that cannot be isolated raises `SandboxUnavailable` rather than silently running on the host. `run toolchain` / `run kernel` are exempt (`build_sandbox.suppressed`) — both build against, and install into, the host they are upgrading (see §Makepkg Wrapper → Build sandbox) |
| `[security]` | `sandbox_chroot_dir` | `~/chroot` | Where the clean chroot lives; `<dir>/root` must already exist (`mkarchroot <dir>/root base-devel`). Tilde-expanded |
| `[security]` | `sandbox_clean` | `true` | Sync a pristine copy of the chroot before each build (`makechrootpkg -c`). `false` reuses the working copy — faster, but a previous build's leftovers stay visible to the next one |
| `[security]` | `sandbox_update` | `true` | Update the working copy before building (`makechrootpkg -u`), so a build never links against a stale chroot |

### Toolchain drift detection (`toolchain.toml`)

`[toolchain] drift_detect` selects how `update` fingerprints the active toolchain to catch a **same-variant** rebuild (Phase 4.25; see §`update`). Resolved by `config.resolve_drift_detect()` — a missing file/key or an unrecognised value all fall back to the default. The value is the sole input to `build_fingerprint.toolchain_fingerprint(method, cc)`, whose opaque output is both stamped into `build_state.toml`'s `toolchain_fingerprint` at build time and recomputed for the active toolchain at update time; comparison is equality-only, so a method flip self-heals (old stamps stop matching → one fail-safe rebuild re-stamps).

| Key | Default | Description |
|-----|---------|-------------|
| `drift_detect` | `"fingerprint"` | `"fingerprint"` — `clang_identity`: path + size + nanosecond mtime + `--version` line. Fast, no hashing; a byte-identical reinstall flips mtime → one spurious (fail-safe) advisory. `"content_hash"` — sha256 of the resolved `libLLVM.so` (the real codegen carrier — the driver links it dynamically, so hashing the driver would miss a libLLVM-only rebuild) mixed with the `--version` line; precise but hashes a ~100 MB+ object each check. Falls back to `clang_identity` when no libLLVM resolves (e.g. a gcc variant) |

### Hardware overlays

The hardware detection stage emits `hardware_profile.toml` which feeds kconfig automation and gates hardware-specific packages in `packages.toml`. Key machine-specific caveats (Ryzen 7 5800X3D + RTX 5070):

- Explicit disable of `nouveau`
- CPU-specific flags: `CONFIG_MZEN3`, `CONFIG_X86_AMD_PSTATE`

---

