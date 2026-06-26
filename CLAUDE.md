# SysForge — Claude Code Context

**DESIGN.md is the source of truth** for module layout, public APIs, CLI structure,
feature status, and known gaps. Read it before proposing architecture/API changes.
It is **generated** from the modular sources under `docs/design/` (assembled by
`make design` per `docs/design/_manifest`, guarded by `make check-design`). Edit the
`docs/design/*.md` sources, not `DESIGN.md` directly.

This file carries only the always-on guardrails: each rule + where the code lives + a
pointer to the DESIGN.md section that holds the rationale. **Do not re-inline design-doc
detail here** — extend the design source instead.

Repo: <https://github.com/KeithRaghubar/sysforge.git> — Python, TOML config. The Makefile
is the canonical entry point (`make test` / `test-x` / `lint` / `release-{major,minor,patch}`
/ `vm-*`); don't invoke `pytest` directly.

## Commands (canonical: the Makefile)

```bash
make test          # full suite          make test-x   # stop on first failure
make lint          # ruff
make design        # regenerate DESIGN.md from docs/design/* (run after editing sources)
make check-design  # guard: DESIGN.md in sync with its sources
make check-shipped # guard: etc/sysforge, PKGBUILD*, hooks, completions, manpage parity
make check-standards
make sync-config   # adopt new shipped defaults into the untracked live config (add-only)
make release-{major,minor,patch}
```

Don't invoke `pytest` directly. Shipped-file edits must pass `make check-shipped`; doc/design
edits must pass `make check-design` after `make design`.

## Dev Environment

- Ryzen 7 5800X3D, RTX 5070, Arch Linux, COSMIC/Wayland, nvidia-open-dkms.
- `pkgbuild_src_dir = ~/src` (sources); builds at `~/builds`.
- `etc/sysforge/` = shipped defaults (installed by PKGBUILD). `tests/data/etc/sysforge/` =
  git-tracked test fixtures, kept in shipped↔fixture parity by `make check-shipped`
  (`conftest.py` forces `SYSFORGE_CONFIG_DIR` to it). Personal live config is a separate
  untracked dir; adopt new shipped defaults into it with `make sync-config` (add-only,
  comment-preserving). A change touching both shipped defaults and fixtures updates both.
  See DESIGN.md §Config Layer.

## Known Bugs & Gotchas

1. **`tests/test_pipeline.py`** imports from both `sysforge.primitives.config` and
   `…profile` — moving symbols across those modules breaks it; update the test in the
   same change.
2. **`match_rules` matches against `pkgbase`** too (split packages) — don't regress.
3. **Source sync goes through the scheduler, never `git pull --rebase`.** Any new
   fetch/update/build path needing a fresh PKGBUILD calls
   `source_sync.get_scheduler().request(SyncRequest(...))`. Fetch is **full-history**
   (`git_fetch_and_compare`), never `--depth=1` (a shallow graft makes every advance look
   `diverged`). `STATUS_DIVERGED` auto-resolves (hard-reset) for non-local sources when
   clean per `git_is_dirty(is_vcs=…)`; survives as a warning only when genuinely dirty.
   `git_is_dirty(is_vcs=True)` ignores `.SRCINFO` and PKGBUILD pkgver/pkgrel auto-bumps —
   don't re-narrow to a line-level `.SRCINFO` filter. `STATUS_FAILED`/`RATE_LIMITED`/
   `PURGE_REFUSED` are blockers. See DESIGN.md §`source_sync.py` / §`git_ops.py`.

## Project Conventions

- **Doc update order**: `docs/design/*.md` source first (then `make design`), then
  README.md, then CLAUDE.md. Never edit generated DESIGN.md directly.
- **DESIGN = implemented only; `/ROADMAP.md` = planned + abandoned.** DESIGN.md (and its
  `docs/design/*` sources) describes only shipped/implemented design. Planned features,
  candidates, and the rationale for purposely-excluded/abandoned ideas live in the
  top-level hand-maintained `/ROADMAP.md` (not generated, not gitignored — unlike the old
  `docs/plans/backlog.md`). Roadmap IDs are version-prefixed `<version>-<TYPE><n>` (e.g.
  `1.2.0-F1`), per-release/per-type counters reset on each `release.sh` bump, and appear
  **only** in ROADMAP + `docs/release-notes/` — never in DESIGN. Triage `notes.txt` into
  ROADMAP.md.
- **Completions stay in lockstep with the CLI**: update `completions/_sysforge` (and the
  bash completion) in the same change as any CLI-surface change, not as a follow-up.
- **Releases are GPG-signed; the stable PKGBUILD verifies the maintainer signature**:
  `tools/release.sh` signs the commit + annotated tag (`git tag -s`/`-v`) and the release
  tarball (detached `.asc`, uploaded with the GitHub release), gated by a signing preflight
  and a sentinel-fingerprint publish gate. The stable `PKGBUILD` carries `validpgpkeys` + a
  `.asc` source (paired with `SKIP`) so `makepkg` verifies at install; `-git` is stable-only
  exempt. `check_shipped` `pkgbuild`/`pkgbuild_parity` permit signature `SKIP` + stable-only
  `validpgpkeys` (sentinel `REPLACE_WITH_MAINTAINER_KEY_FINGERPRINT` tolerated in dev). Don't
  add a parallel signer/gate. See DESIGN.md §Release Process / §Standards (row 16).
- **PKGBUILD parsing/detection/patching**: cross-check `PKGBUILD(5)` before changing
  (array vs string fields, escape rules). Arch array families (`makedepends_x86_64`, …)
  merge into the canonical key in `parse_pkgbuild` — extend `_ARCH_ARRAY_FAMILIES` in
  `pkgbuild_meta.py` for a new family; downstream never reads `_<arch>` keys. The static
  parser resolves brace expansion and array-param splices (`_expand_array_refs`) but never
  sources the PKGBUILD; unresolved `${…}`/`$(…)` tokens are caught by `_looks_unresolved`
  and rescued via AUR RPC `.SRCINFO` — don't hand unparsed tokens to pacman or add a
  parallel discovery path. cmake-arg injection (`patch_llvm_targets`/`patch_llvm_dir`)
  anchors on the cmake **configure** invocation (`_find_cmake_configure_anchor`) and
  appends at the statement's true end (`_cmake_statement_end`); every injection is gated
  by `validate_patched_pkgbuild` (G1 identity/deps unchanged, G2 managed `-D` rides a
  cmake line) in `makepkg_wrapper._run_build`. Don't add a parallel injector/validator.
  See DESIGN.md §`pkgbuild_meta.py` / §`pkgbuild_patcher.py`.
- **lib32-\* flag scrubs live at conf emit, not the profile**: `emit_makepkg_conf(is_lib32=True)`
  strips `-march` ISA levels, lld `--icf=*` (unconditional — 32-bit ICF breaks links), and
  PGO flags (`makepkg_flags._strip_pgo_flags`, after `compiler_flags_extra` injection).
  Reuse `_strip_lld_flags`/`_strip_pgo_flags`; don't add an i686 per-profile rule. See
  DESIGN.md §Flag/Profile System.
- **musl-static flag scrubs live at the same conf-emit home**: `emit_makepkg_conf(is_musl_static=True)`
  forces the bfd linker (`-fuse-ld=lld`→`bfd`), strips lld-only tokens, and scrubs PGO flags for
  static-musl bootstraps (e.g. `pacman-static`) — lld+`-static`+musl segfaults at startup and
  musl-gcc can't read a clang `.profdata`. Detection has one home: `pkgbuild_meta.is_musl_static_build`
  (musl makedepend + build-time `CC=musl-gcc`/`-static`). Reuse the same strip helpers; don't add a
  parallel musl rule. See DESIGN.md §Flag/Profile System.
- **Build CPU/IO throttling has one home**: `primitives/build_throttle.py`
  (`resolve_throttle` — `sysforge.toml [build]` defaults + per-profile override; the four keys
  `nice`/`ionice`/`cpu_quota`/`jobs` are in `profile.SYSFORGE_KEYS`, never conf/env). Two
  channels: `wrapper_argv` prepends a `nice`/`ionice`/`systemd-run --scope CPUQuota` prefix to
  `cmd` at the `makepkg_invoke` chokepoint (best-effort — `shutil.which`-guarded, never fails a
  build); `apply_jobs_to_makeflags` rewrites the `-j` token via `emit_makepkg_conf(jobs=…)`.
  Don't add a parallel `nice`/`systemd-run`/`-j` path. See DESIGN.md §Flag/Profile System.
- **`build` is a strict subset of `update`; both go through `build_core.py`**
  (`build_and_install` → `prepare_deps` + per-package loop + `install_built`). makepkg
  always runs with `BATCH_STRIP_FLAGS` + `force_batch` (sysforge pre-installs repo
  build deps via `collect_builddeps`/`batch_install_makedeps` — depends+makedepends+
  checkdepends — and builds AUR/local deps up front). Batch members are topo-ordered
  (`_order_targets_by_intra_deps`, matched on pkgnames **and** provides) and JIT-installed
  before dependents. The only intended caller difference is `sync_source`. Don't add a
  second dep-handling/build loop — extend `build_core`. Tests monkeypatch
  `sysforge.build_core.X`. See DESIGN.md §CLI Verb Framework.
- **makepkg path resolution has one home**: `pacman.get_pkgdest()`/`get_builddir()`/
  `get_srcdest()`/`get_logdest()` (shared `_resolve_makepkg_path`, env-first then layered
  `parse_system_makepkg_conf()`). Anything locating an artifact/build tree/source/log uses
  these — never read `os.environ["BUILDDIR"]` or assume `~/builds`/the PKGBUILD dir.
  Writing makepkg.conf keys has one home too: `config.set_makepkg_conf_keys(path, mapping,
  dest=None)`. See DESIGN.md §`primitives-layer`.
- **The consolidated run-log lifecycle has one home**: `log.open_unified_log`/
  `close_unified_log`. Callers differ by verb shape — `run pipeline`/`run <stage>` open it in
  `pipeline/runner.py` (`sysforge.log`); `update` opens it in `cmd_update`
  (`sysforge-update.log`, own success calc). Every other verb only declares a basename via
  the opt-in `Verb.unified_log_basename(args)` hook and `verbs/runner.py` opens (purge) /
  closes (kept) it around `execute`; `build` is the sole user (`sysforge-build.log` — it
  routes through `build_core`, not the pipeline runner). Don't add a parallel open/close path;
  a verb that manages its own lifecycle returns `None` to opt out. See DESIGN.md §Logging.
- **Flag-drift detection has one home**: `primitives/flag_drift.resolve_flag_drift`
  (pure, never logs); sole consumer is `update` Phase 4.3 (report by default, rebuild via
  `--rebuild-on-flag-drift`). Don't re-implement the re-resolve+diff. See DESIGN.md §update.
- **`toolchain` profile-field expansion has one home**: `profile._expand_toolchain` (pure,
  run after `merge_extends` in `resolve_profile`). `toolchain = "gcc"|"llvm"` `setdefault`s
  the compiler/binutils bundle (+lld into LDFLAGS for llvm when no `-fuse-ld=`); explicit
  CC/CXX/AR/… win. The package-compiler knob — **distinct** from `toolchain.toml compiler`
  and from `toolchain_variant`; don't conflate. Keeping `[defaults] toolchain` in sync with
  `toolchain.toml` has one home: `config.set_default_toolchain`, called only by the toolchain
  stage (`_propagate_default_toolchain`) on success. See DESIGN.md §Flag/Profile System.
- **packages.toml `[group.*]` expansion has one home**: `config.expand_package_groups(data)`
  — every manifest consumer routes raw TOML through it; never iterate `data.get("package")`
  directly. Writing groups/desktop catalog has one home: `primitives/pkg_catalog.py`
  (`DESKTOP_CATALOG`, `select_desktop`, `write_desktop_group`). Add a DE by extending
  `DESKTOP_CATALOG` (+ `bootstrap.toml [desktop]` validation + both completions). See
  DESIGN.md §Package Manifest.
- **`build_state.toml` is the steady-state tracking authority** (not packages.toml).
  `update`'s walk (`update_assemble._assemble_package_set`) includes every installed package
  sysforge source-built (`build_mode != "pacman"`), so `sysforge build mesa` is durable —
  rebuilt from source on every `update`. A source-built repo package is classified
  `repo_class = "source"` (rebuild) not `"pacman"` (a deferred `pacman -Syu` no-ops behind
  `IgnoreGroup = sf-build`). packages.toml stays the *declarative* layer: bootstrap set +
  groups + per-package overrides; `repo_mode = "build_from_source"` is bulk repo-drift
  surfacing, not the source-tracking mechanism. `build` stamps the resolved `source`
  (`is_repo_package`) so the registry is self-describing. Stop tracking via `sysforge state
  forget <pkg>` (`BuildState.delete`, pkgbase-expanded), or automatically: a `source_built`
  package reinstalled via `pacman -S` is demoted to a `pacman` marker at the next `update`
  (`BuildState.reconcile_external_installs`, fed by `primitives/install_reconcile`'s
  buildstate−self-install sentinel diff; stage-owned exempt). Don't re-add a
  packages.toml-entry-gates-tracking path. See DESIGN.md §Package Manifest / §update.
- **Vocabulary (renamed; legacy aliases honored on read)**: build_state `build_mode` value is
  `"source_built"` (legacy `"profiled"` normalized in `BuildState.__init__`); packages.toml
  `[build] repo_mode` value is `"build_from_source"` (legacy `"profiled"` via
  `config.resolve_repo_mode`); per-package key is `enable_build_from_source` (legacy
  `pkgbuild_patch` via `config.normalize_package_entry` in `expand_package_groups`; the
  packages_cmd auto-prune is legacy-aware so it never drops a pre-rename entry). One read
  chokepoint per surface — don't scatter `or "profiled"` checks.
- **`build` gates repo packages on opt-in**: `build_cmd` source-builds AUR/git/local
  unconditionally but a `source="repo"` target not already opted in (global
  `repo_mode="build_from_source"` or per-package `enable_build_from_source`) prompts on a TTY
  (yes → build + write the key via `packages_cmd`; no → skip) or aborts with a hint when
  non-interactive. `--force` builds every arg this run only and never prompts/writes
  packages.toml. Don't add a second packages.toml writer. See DESIGN.md §CLI Verb Framework.
- **build()-hardcoded `-fuse-ld=` reconciliation has one home**: detection
  `pkgbuild_meta.hardcoded_build_linker`, effective-linker `makepkg_flags.resolve_effective_linker`
  (shared with the conf layer), rewrite `pkgbuild_patcher.patch_build_linker`, wired via
  `makepkg_wrapper._maybe_patch_build_linker` (gate: hardcoded != effective; validated by the
  existing `validate_patched_pkgbuild`). Linker only — compiler stays with `has_hardcoded_gcc`.
  See DESIGN.md §`pkgbuild_patcher.py` / §Flag-Profile.
- **Optimization-build naming/store has one home each**: the `-sysforge` rename gate is
  `profile.is_optimized_build_mode(build_mode)` (the membership set — add a new optimization
  `build_mode` there, don't scatter checks); the rename itself is
  `pkgbuild_patcher.patch_package_suffix(path, "sysforge", mode="conflict"|"coexist")`, applied
  once in `makepkg_wrapper._run_build` (gated on that predicate; **mode chosen by
  `profile.rename_mode_for_build_mode`** — kernel FDO modes coexist, all else conflict, one home
  in `_COEXIST_BUILD_MODES`) and validated by
  `validate_patched_pkgbuild(..., rename=…)` (G3: every renamed split member keeps a renamed
  `package_<name>()` — `patch_package_suffix` renames the functions too, or makepkg bricks at
  packaging time). The renamed build records its renamed pkgnames + sticky `origin_pkgbase` so
  `update` still source-syncs the upstream tree. Profile stores route through
  `makepkg_pgo.resolve_method_store(method)` (`pgo-mesa`/`autofdo`/`propeller`/`bolt`; `instr-pgo`
  aliases the legacy clang store). Don't add a parallel renamer/store-resolver. See DESIGN.md
  §CLI Verb Framework / §Flag-Profile.
- **Instrumentation PGO has one home**: `primitives/mesa_pgo.py` (`resolve_store(pkgbase)`,
  `merge_profraw` → `llvm-profdata`, `generate_flag`/`use_flags`, `reuse_profdata(pkgbase)`,
  `build_mode_for(pkgbase)` → `pgo_mesa` for mesa / generic `pgo` else, both in
  `_OPTIMIZED_BUILD_MODES`). PGO is "just a build flag", so **`--pgo` works on any package** (F5),
  not only mesa — every function takes a `pkgbase`. The `build --pgo=record|use` flow injects
  `-fprofile-generate=<store>` / `-fprofile-use=<profdata>` via the **same `compiler_flags_extra`
  seam** the toolchain PGO uses (no `-Db_pgo` patch, no second injector); `record` keeps the stock
  name, `use` earns `-sysforge`. **Stores are per-package**: mesa-family keeps the back-compat
  `<root>/pgo-mesa` (method `pgo-mesa`); any other target gets `<root>/pgo/<pkgbase>` (method `pgo`,
  `target=pkgbase`) — don't collapse mesa into the generic path (orphans collected profiles).
  **Reuse is durable**: a no-`--pgo` rebuild (`update`/plain `build <pkg>`) re-applies an existing
  merged `<pkgbase>.profdata` via `reuse_profdata(pkgbase)` (profdata-exists is the signal) through
  the same seam and re-stamps `build_mode_for(pkgbase)` — bare `.profraw` (record-only) is not
  reused; opt out by removing the store profdata or `state forget <pkg>`. That reuse branch in
  `_run_build` is `is_llvm_toolchain`-guarded (it bypasses `pre_check`) so a clang `.profdata` never
  feeds a gcc build. LLVM-only — gated in `BuildVerb.pre_check` via `profile.is_llvm_toolchain` +
  `LLVM_REQUIRED_HINT`. **Warn-list (one home: `config.pgo_warns_for` over `sysforge.toml [pgo]
  allow`, seeded with mesa)**: `pre_check` warns (not blocks) on `--pgo` for any target that is
  neither mesa-family nor allow-listed. Distinct from Phase-2's compiler training corpus (that
  enriches `clang.profdata`; this profiles the package itself). See DESIGN.md §CLI Verb Framework.
- **Kernel sample-based FDO has one home**: `primitives/kernel_fdo.py` (`resolve_store`,
  `fdo_kconfig`, `use_env`, `require_profile`, `detect_branch_sampling`, `resolve_vmlinux`,
  `capture_commands`, `build_mode`). `run kernel --autofdo=record|capture|use` (+`--propeller`)
  is **sample**-based, not instrumentation: `record` merges `CONFIG_AUTOFDO_CLANG` (+`PROPELLER`)
  into the kconfig fragment (new `fdo` source in `_write_kconfig_fragment`, precedence
  device<hardware<fdo<manual); `capture` is **read-only** (no build/install/sentinel) — prints the
  host-tailored `perf -b` + `create_llvm_prof` commands; `use` injects `CLANG_AUTOFDO_PROFILE`/
  `CLANG_PROPELLER_PROFILE_PREFIX` via the **`extra_env` make-variable seam** (same channel as
  `LLVM=1`, not `compiler_flags_extra`), stamps `build_mode=autofdo_kernel`/`propeller_kernel` →
  `-sysforge` **coexist** rename. Stage threads an effective `<pkgname>-sysforge` to Gate-3/
  sentinel/collision-check (the use build is renamed in makepkg_wrapper). LLVM-only: hard gate
  `_gate_fdo_llvm`/`_fdo_is_llvm` (compiler==llvm or `cc`/`$CC` basename `clang*`). BRS is Zen3+
  only (experimental); `detect_branch_sampling` reads `/proc/cpuinfo`. Don't add a parallel
  perf/profile injector. See DESIGN.md §Kernel stage (Sample-based kernel FDO).
- **BOLT post-link optimization has one home**: `primitives/bolt.py` (`resolve_store` method
  `bolt`, `emit_relocs_ldflag`, `collect_profile` → `perf record`+`perf2bolt`, `bolt_binary` →
  `llvm-bolt`, `tools_available`, `BUILD_MODE="bolt_llvm"`, **and the generated `llvm-bolt`
  PKGBUILD** `render_pkgbuild`/`materialize_pkgbuild`). The only **post-link** method (no compiler
  flag). **EXPERIMENTAL — and currently BLOCKED.** BOLT is not in the Arch repos and stock `llvm`
  doesn't build it, so sysforge builds the tools itself: `materialize_pkgbuild` writes a
  version-locked `llvm-bolt` PKGBUILD (modeled on the `clang` component; the `bolt/` subtree rides
  in the same monorepo tarball; `sha256sums=SKIP`) and `_build_bolt_tools` (Pass 4a) builds it
  standalone against the installed PGO libLLVM. **BLOCKER: the standalone build doesn't link.**
  Every BOLT tool forces `DISABLE_LLVM_LINK_LLVM_DYLIB`, so it links the per-component static LLVM
  archives (`libLLVMObject.a`/`libLLVMMC.a`/X86/…) — which a dylib-only LLVM (the PGO toolchain,
  and stock Arch `llvm`) does NOT ship, so `ld.lld` can't find `-lLLVMObject`. The real fix is an
  in-tree BOLT build (not yet done); until then `_build_bolt_tools` guards on
  `bolt.standalone_build_viable()` (probes `libLLVMObject.a`/`libLLVMMC.a`) and skips Pass 4 with a
  clear WARN on a dylib-only host. **Keep `[bolt] enabled = false`.** Wired as **toolchain Pass 4**
  (`run toolchain`, opt-in `toolchain.toml
  [bolt] enabled`, `_bolt_config` reader): Pass 3a/3b link with `-Wl,--emit-relocs` (threaded as
  `linker_flags_extra` through `_build_llvm_pgo_inner`, gated on `bolt_relocs`); `_run_bolt_pass4`
  runs **after Gate 3, inside the sentinel** — 4a builds the tools, 4b BOLTs the installed
  `/usr/bin/clang` **in place, stock name (not `-sysforge`** — the toolchain stage is the in-place
  system replacement), smoke-tests before replacing. Best-effort end to end (failed tool build /
  missing `perf` / BOLT / smoke failure warns, leaves the PGO clang). Post-link rewrite →
  `pacman -Qkk clang` reports modified (expected, not corruption). `rename_mode_for_build_mode("bolt_llvm")`
  is `conflict` (for a future `build`-driven BOLT), but Pass-4 does no rename. `[bolt]` is in
  `check_shipped` `_KNOWN_SECTIONS`. Don't add a parallel perf2bolt/llvm-bolt path or a second
  PKGBUILD generator. See DESIGN.md §Toolchain stage (BOLT Pass 4).
- **PKGBUILD review gate has one home**: `build_core.build_and_install(review=…)` →
  `pkgbuild_review.review_target` (`build` defaults `"prompt"`, `update` `"auto"`); AUR
  dep builds gated via `review_deps` in `prepare_deps`. Baseline is sticky `reviewed_commit`
  in `build_state.toml`. Abort is a clean `BuildOutcome.aborted`, not an exception. New
  serialized `build_state` fields go in `BuildState._serialize`'s key tuple. See DESIGN.md
  §`primitives-layer`.
- **Interactive build-failure recovery has one home**: `makepkg_invoke._run_recovery_menu`
  (menu + editor + cc/ld swap, returns `RecoveryOutcome`); the wrapper supplies the
  `reemit_conf` closure and persists a successful swap via
  `profile_writer.write_package_compiler_override` (the sole `profiles.toml` writer) into
  `[package_compiler_overrides]`, applied last in `profile.resolve_profile`. Don't add a
  second profiles.toml writer or a parallel recovery loop. See DESIGN.md §Flag/Profile
  System / §makepkg-wrapper.
- **First-install notice has one home**: `primitives/init_notice.py`
  (`maybe_emit_init_notice` — called once per invocation from `cli.main()` after the
  stale-sentinel gate, skipped for `completions`). The marker `<state_dir>/.sysforge-init-notice`
  is **created only** by the PKGBUILD `post_install` scriptlet (`sysforge.install`, `install=` in
  both PKGBUILDs); sysforge only reads/deletes it. Advises the still-pending `reconfigure`/`hardware`
  stages (via `PipelineState.stage_status`) until both `done`, then self-deletes. Best-effort, never
  blocks/raises. Don't add a parallel notice or recreate the marker. See DESIGN.md §`init_notice.py`.
- **LLVM source-state inspection**: `primitives/llvm_state.collect_llvm_state` is the only
  entry point — don't call `git_is_dirty` + URL parsing directly.
- **Dual-toolchain test parity**: any logic branching on resolved compiler (gcc vs llvm)
  ships with both a gcc-path and an llvm-path test in the same change.
- **CLI verbs go through the Verb framework**: every top-level command is a `Verb` subclass
  dispatched by `verbs.runner.run_verb` (`pre_check`/`execute`/`post_validate`;
  `requires_sentinel=True` if it mutates the system). Wire via `set_defaults(verb_cls=…)`,
  not `func=` callbacks. See DESIGN.md §CLI Verb Framework.
- **Install-bearing scopes share one sentinel primitive**: `primitives.stage_sentinel.
  sentinel_scope()`. Don't roll a separate install/clear path. See DESIGN.md §Toolchain stage.
- **Editor/merge-tool launch has one home**: `primitives/editor.py` —
  `resolve_editor`/`editor_usable` (reconfigure), `resolve_merge_tool` (`config merge`:
  `SYSFORGE_MERGE` > `[ui].merge` > `$DIFFPROG` > `vimdiff`), and `run_tty_argv` (the
  `/dev/tty` passthrough both use). Don't add a second `/dev/tty` launcher or resolution
  chain. `.sfnew` adoption lives in `config_cmd.ConfigMergeVerb` (no sentinel; no blind
  "accept theirs"). See DESIGN.md §Config Layer / §CLI Verb Framework.
- **Runtime directory/group provisioning has one home**: `primitives/fs_provision.py`
  (`ensure_writable_dir` provisions sysforge's writable FHS dirs `root:sysforge` setgid
  `2775` — `groupadd`/`usermod` + per-run `chgrp`/`chmod` repair; `FsProvisionError` →
  caller XDG-fallback; `empty_dir_contents` for the PGO purge; `build_user()` for
  `SUDO_USER`>`USER`). `/etc/sysforge` stays root-owned. Don't add a parallel
  `mkdir`/`chown`/`sudo` path; the shipped `tmpfiles.d`/`sysusers.d` (both PKGBUILDs) and
  bootstrap `configure.py` reuse the same `SYSFORGE_GROUP`/`SYSFORGE_DIR_MODE`, gated by
  `check_shipped` `provisioning`. See DESIGN.md §07 (Directory provisioning).
- **libalpm-hook install/refresh has one home**: `primitives/pacman_hooks.py`
  (`shipped_sources` — repo checkout > wheel `_data` `force-include`; `diff_status` →
  `ok`/`missing`/`stale` pure read; `provision` writes via `fs_provision._run_priv`, no second
  sudo path). Two consumers: `setup_cmd` (provision after the IgnoreGroup step) and
  `doctor._collect_hook_findings` (read-only `--pacman` warnings). The PKGBUILD still installs the
  live copies; this is the runtime/dev-checkout refresh. A new hook updates `HOOK_NAMES`, the
  shipped `.hook` files, the `pyproject.toml` `force-include`, and the helper's documented kinds in
  lockstep (gated by `check_shipped` `hooks`). Don't re-implement the compare/install or read
  `/usr/share/libalpm/hooks` directly. See DESIGN.md §`pacman_hooks.py` / §setup.
- **`doctor` axes share one Finding framework and stay read-only**: each axis is a producer
  returning `list[diagnostics.Finding]` (never import `pipeline` from `diagnostics`).
  Register in `doctor.py` (`_SYSTEM_AXIS_ORDER`/`_AXIS_FLAGS`/`_system_axes` +
  `_collect_<axis>_findings`), add the flag to `cli.py` + both completions + regenerate the
  manpage in the same change, and extend `_patch_axes_clean` in the test. Probe primitives
  and the boot axis do no `pacman -Sy`/`BuildState.save()`. See DESIGN.md §doctor.
- **ABI checker (`abi_check.py`) is symbol-version precise** — `sym@@VER`/`sym@VER` capture,
  Verneed soname for the skip decision but satisfaction vs the union of NEEDED exports,
  absent optional LLVM target-init demoted to `benign_sink`. Don't regress. See DESIGN.md
  §`abi_check.py`.
- **Toolchain preflight assumes rustup**: the rust cross probe in
  `toolchain_preflight.py` reads `$RUSTUP_TOOLCHAIN`; system-rust downgrades to a pacman
  hint. New cross targets plug into `collect_required_toolchains` + the `rust:cross:<target>`
  grammar — no parallel probe.
- **Shipped-file changes must pass `make check-shipped`** (`etc/sysforge/*.toml`,
  `PKGBUILD*`, hooks, completions, CLI parser→manpage). Schema allowlists live in
  `_KNOWN_SECTIONS`/`_KNOWN_TOP_KEYS` in `tools/check_shipped.py` — extend in the same
  change. See DESIGN.md §Shipped-file pre-release checks.
- **Standards adherence has one home**: `docs/design/21-standards.md`. On any change to
  paths/CLI/output/versioning/packaging, cross-check the relevant row. Enforcement splits:
  static subset `tools/check_standards.py` (`make check-standards`), behavioural subset
  `tests/test_standards_compliance.py`, SemVer in `check_shipped.check_versions`. User paths
  route through `primitives/paths.py`; colour through `log.use_color()`. Don't add a parallel
  list/path-constructor/colour gate.
- **Stage-owned packages stamp `owner_stage` on `BuildState.record()`** (kernel/toolchain)
  so `update` skips them by default; ownership policy is `stage_ownership.owner_of`
  (toolchain owns in-scope LLVM only when `enabled`+`compiler="llvm"`). **`lib32-*` is
  excluded** from implicit prefix-ownership (and from the `LLVM_TARGETS_TO_BUILD` reduction
  in `makepkg_wrapper._maybe_patch_llvm_targets`). `--include-stage-owned` / naming the pkg
  overrides. Both fields sticky in `build_state.toml`. See DESIGN.md §Toolchain stage.
- **Build failures live in a reserved `[failures]` namespace**, isolated by
  `BuildState.__init__`/`_serialize` — never leak into `all_packages()`/`sync_with_installed()`.
  Written by `update`'s `_record_build_failure`, auto-cleared by a successful `record()`.
  New failure diagnostics go in `primitives/build_diag.py` (`_match_*` added to `_MATCHERS`),
  riding up via `.diagnosis`. All matching runs on `pty_runner.strip_ansi`-cleaned lines.
  See DESIGN.md §`build_diag` / §`makepkg-wrapper`.

### Toolchain & kernel deep invariants (rule + one home; rationale in DESIGN.md §07)

- **Toolchain health: exactly two checkers** — `toolchain.py::_verify_llvm_install`
  (authoritative, `run toolchain` only) + `toolchain_preflight._probe_cc` (`update` path),
  both over `LLVM_LOCKSTEP_SUITE`. `toolchain_safety.py` (facts) and
  `llvm_state.detect_toolchain_config_mismatch` (provenance) are **not** a third — don't add one.
- **Toolchain/kernel stages: 3 gates, build split from install, snapshot+auto-restore undo.**
  Facts in `primitives/{toolchain,kernel}_safety.py` (pure); policy in the stage. Build is
  `install=False`; Gate 2 runs **outside** `sentinel_scope`, install→Gate-3→rollback inside.
  GCC path is register-only — **the stage never builds GCC**. LLVM with `pgo=false` +
  packages.toml `[build] repo_mode="pacman"` is a third no-build branch: install the stock
  suite from the repos (`pacman.install_repo_pkgs`, inside `sentinel_scope`) instead of
  building. **PGO always builds from source regardless of `repo_mode`** (no repo artifact for a
  profiled toolchain). `repo_mode` read through `config.resolve_repo_mode` only.
- **Pass-3 builds non-pgo against the libLLVM it ships** (3a→`_extract_pass2_to_staging`→3b/3c
  with `CMAKE_PREFIX_PATH` **and** forced `-DLLVM_DIR` via `patch_llvm_dir` — prefix-path alone
  is NOT enough). staging3 needs both `llvm-libs` and `llvm`; don't collapse Pass 3 into one pass.
- **Pass-2 training corpus has one home**: `_resolve_training_corpus(tcfg)` (reads `[packages]
  training_corpus`, default `["llvm"]`; strips the implicit `"llvm"` base, warns+drops unknowns)
  + the `corpus_map` second `_build_pass` inside Pass 2. Extras (mesa) compile with the **same**
  instrumented stage1 clang + `LLVM_PROFILE_FILE` so their profraw merges into the one
  `clang.profdata` — **never installed, never `-fprofile-use` targets** (that's Phase-3 mesa-PGO,
  a distinct cycle). `staged_deps=True` keeps the no-pacman-mutation invariant; the build is
  **best-effort** (a corpus failure warns and the run continues with LLVM-only profraw). PGO path
  only. Don't add a parallel corpus builder or make it a profile consumer.
- **libLLVM soname-bump → consumer rebuild**: facts `assess_libllvm_soname_impact`; policy
  `_gate_soname_consumers`/`_rebuild_soname_consumers` (rebuild after Gate 3, outside the
  sentinel). No parallel reverse-dep scanner.
- **System libLLVM must keep `AMDGPU` (mesa survives a target reduction)** — system mesa's
  `libgallium` links `LLVMInitializeAMDGPU*`/llvmpipe **unconditionally**, so a reduced
  `LLVM_TARGETS_TO_BUILD` that drops AMDGPU bricks the whole desktop. Enforced at **resolution**
  (one home: `llvm_targets._ensure_system_consumer_targets`, applied in `resolve_llvm_targets`/
  `resolve_or_detect_llvm_targets` — not only `hardware.derive_llvm_targets`, which a cached/
  edited `hardware_profile.toml` bypasses). Verified by a **graphics-consumer symbol gate** (one
  home: `toolchain_safety.check_system_consumer_symbols` pre-install in `_gate2_audit`,
  `check_installed_consumer_symbols` post-install in the Gate-3 path; shared core
  `_diff_consumers_against_libllvm` over abi_check seams — no parallel differ). Gate-3
  `expected_targets` comes from `resolve_or_detect_llvm_targets`, **not** `toolchain.toml [llvm]
  targets` (else check #3 silently skips on autodetect hosts). Same fact surfaced read-only by
  `doctor --graphics` (`graphics_probe._check_mesa_llvm_symbols`). Opt-out is `[llvm] targets = []`
  (build all). See DESIGN.md §Hardware detection / §`graphics-stack` / §07.
- **Mesa driver filtering is the meson analogue of LLVM target filtering, inverted** — trims
  mesa's `-D gallium-drivers=all` / `-D vulkan-drivers=…` to detected GPU vendors. One home each:
  derivation `hardware.derive_mesa_drivers`, resolution `mesa_drivers.resolve_or_detect_mesa_drivers`
  (mirrors the llvm_targets precedence), meson rewrite `pkgbuild_patcher.patch_mesa_drivers` (+ the
  only meson injector/validator — `validate_patched_meson_pkgbuild`; don't add a parallel one;
  the same function hardens packaging via `_harden_mesa_packaging` so a filtered driver's missing
  `libvulkan_*.so` doesn't abort `package_*()` — `_pick` skips unbuilt sources, split `mv` is
  `compgen`-guarded; don't map driver→pkgname),
  wired via `makepkg_wrapper._maybe_patch_mesa_drivers` gated by `profile.is_mesa_pkgbase`. The
  invariant is **inverted** vs LLVM: the mandatory *software* baseline (`_MESA_MANDATORY_GALLIUM`
  llvmpipe/softpipe/zink, `_MESA_MANDATORY_VULKAN` swrast) is always kept (`_ensure_mesa_software_baseline`)
  — reducing too MUCH bricks headless/VM/recovery. A gallium reduction also intersects
  `gallium-rusticl-enable-drivers` (rusticl ⊆ built gallium). **Opt-in** (`sysforge.toml [mesa]
  filter_drivers`, default off) — unlike LLVM filtering; lib32-mesa **is** filtered (vendor- not
  arch-coupled, unlike lib32-llvm). See DESIGN.md §Hardware detection / §Config Layer.
- **Pass-3 build reuse has one home**: `primitives/build_fingerprint.py` (opt-in `--reuse-built`,
  fail-safe to rebuild). New `compute_fingerprint` inputs bump `_SCHEMA`.
- **Kernel stage**: compiler independent of toolchain (`_compiler_paths`, don't hardcode);
  **interactive by default** (only verb where unattended is opt-in); base `.config` via
  `_resolve_base_config`. Boot-safety tables in `kernel_safety.py`/`device_probe`, not inline;
  `device_probe.enumerate_devices` + `kernel_safety.parse_kconfig_text` are the single entry points.
  Headers/docs toggles resolve in `_resolve_subpackages` (CLI `--headers`/`--docs` >
  `kernel.toml build_headers`/`build_docs` > headers-on/docs-off default); dropping a
  subpackage has one home — `pkgbuild_patcher.patch_kernel_subpackages` edits the
  `pkgname=(...)` array (no parallel pkgname editor) **and**, when docs are off, comments out
  the standalone doc-build make line via `_neutralize_kernel_doc_build` (stock `linux` runs
  `make htmldocs` in `build()`, not only `_package-docs()`; a mixed `make all htmldocs` line is
  left alone). Disabling headers must keep the Gate-1 DKMS/out-of-tree warning. See DESIGN.md
  §Kernel stage.

`run toolchain` (stage 6), `run kernel` (stage 8), and the PGO profdata-reuse path
(`build_mode = "pgo_llvm_toolchain"`) are stable but default `enabled = false` (opt-in —
don't flip). `run toolchain` defaults `compiler = "gcc"`; LLVM is opt-in only.

## Interaction Preferences

- Be direct. Own mistakes and fix them immediately.
- When a design decision is made, update the relevant `docs/design/*.md` source (and run
  `make design`) immediately in the same turn — don't wait to be reminded.
