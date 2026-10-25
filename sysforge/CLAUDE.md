# SysForge package — code-seam guardrails

Loaded lazily when working under `sysforge/`. Process conventions live in the root `CLAUDE.md`;
design detail in DESIGN.md (§ pointers below). Rules cite real paths/symbols — `make
check-standards` (group `claude_md`) verifies they still resolve.

## Gotchas

- **`tests/test_pipeline.py`** imports from both `primitives.config` and `…profile` — moving
  symbols across them breaks it; update the test in the same change.
- **`primitives/` must not import from `pipeline/`, `ui/` or `build/`** (the orchestration layer
  `makepkg_wrapper` moved into, `3.2.0-F4`) — it is the leaf layer, and since
  `3.2.0-F1` the guard covers both in full, not just `pipeline.stages`. A function-level import to
  dodge the cycle is not a fix: `pipeline/stages/__init__.py` instantiates every stage at import, so
  one constant drags all eleven stage modules in. Two ways down: **relocate** (shared tables →
  `primitives/hardware_tables.py`, detection → `primitives/hardware_probe.py`, state-dir resolution →
  `resolve_state_dir` → `primitives/paths.py`, `BootstrapConfig` → `primitives/archinstall_config.py`),
  leaving a re-export behind so import paths and test patch points still resolve; or **invert** —
  `primitives/progress_hooks.py` declares the progress surface as a protocol with a no-op default and
  `ui/progress.py` registers itself, so call `progress_hooks.hooks().<name>()` per use and never
  import `ui.progress` from a primitive. `tests/test_module_layering.py` enforces this via a
  **shrinking** allowlist — never add a name to it.
- **`match_rules` matches against `pkgbase`** too (split packages) — don't regress.
- **Source sync goes through the scheduler** (`source_sync.get_scheduler().request(...)`), never
  `git pull --rebase`. Fetch is full-history (`git_fetch_and_compare`), never `--depth=1`.
  `STATUS_DIVERGED` auto-resolves (hard-reset) when clean per `git_is_dirty(is_vcs=…)` (ignores
  `.SRCINFO`/pkgver/pkgrel bumps — don't re-narrow). `FAILED`/`RATE_LIMITED`/`PURGE_REFUSED` are
  blockers. §`source_sync.py` / §`git_ops.py`.
- **PKGBUILD parsing/patching**: cross-check `PKGBUILD(5)` (array vs string, escapes). Arch array
  families merge into the canonical key (`_ARCH_ARRAY_FAMILIES` in `pkgbuild_meta.py`); never read
  `_<arch>` keys. Static parser never sources the PKGBUILD — unresolved `${…}`/`$(…)` caught by
  `_looks_unresolved`, rescued via AUR RPC `.SRCINFO`. cmake-arg injection anchors the cmake
  configure invocation, gated by `validate_patched_pkgbuild`. §`pkgbuild_meta.py`/§`pkgbuild_patcher.py`.

## One-home invariants (don't add parallel paths)

Each is the sole home for its concern — reuse the seam, never add a second writer/injector/loop.
Mechanism lives in the cited §DESIGN section.

- **Flag scrubs** (lib32/musl-static/PGO): `emit_makepkg_conf(is_lib32=|is_musl_static=)`, reuse
  `_strip_lld_flags`/`_strip_pgo_flags`; musl via `pkgbuild_meta.is_musl_static_build`. §Flag/Profile.
- **Build throttle** (nice/ionice/cpu_quota/jobs/mem_limit): `build_throttle.resolve_throttle`;
  channels `wrapper_argv` + `apply_jobs_to_makeflags`. `mem_limit` is dual-mechanism: off the
  cpu_quota path it's an `RLIMIT_AS` clamp via the shared `resource_guard.make_child_preexec(cap)`
  preexec; with cpu_quota it becomes `-p MemoryMax=` on the systemd-run scope.
  `resolve_child_mem_cap` arbitrates so the two never double-apply (returns `None` when the scope
  owns the cap). Under the build sandbox neither reaches the container (it gets its own scope
  under `devtools-<user>.slice`): `wrapper_argv(scope=False)` + `build_sandbox.slice_limits`
  carry `cgroup_properties` on that slice instead (`3.5.0-B131`). §Flag/Profile.
- **Controller rlimit lift**: every child runs without the controller's 2 GiB `RLIMIT_AS`, or
  `sudo pacman` carries it into DKMS (`3.4.0-B11`). The `run`/`pty_runner` seams default
  `preexec_fn=lift_for_child`; a raw `subprocess` site passes `preexec_fn=` itself (`run_seam`
  fails one that doesn't). §primitives-layer `resource_guard.py`.
- **Replacing an existing config file**: `atomic_write.replace_file` (symlink-, mode- and
  owner-preserving temp-in-dest-dir + rename; escalates via `run_privileged`). Never
  `write_text`/`sudo cp` over a user's or root's config. Creating files with a fixed mode
  (`artifacts.write_live`, `pacman_hooks`) is a different shape and stays out. sysforge's **own
  state files** go through `atomic_write.write_state` (unique temp + fsync + rename, never
  escalates, `3.5.0-B81`) — never a fixed `<file>.tmp` + rename. §primitives-layer.
- **Runtime TOML key writes**: `config.set_toml_key(path, section, key, value)` (bool/str only,
  comment-preserving, via `atomic_write.replace_file`); `set_default_toolchain` and `setup` are its
  callers. Never `write_text` a config or pull tomlkit in at runtime.
- **makepkg path resolution**: `pacman.get_pkgdest()`/`get_builddir()`/`get_srcdest()`/
  `get_logdest()` — never read `os.environ["BUILDDIR"]` or assume `~/builds`. Write conf keys via
  `config.set_makepkg_conf_keys`. §primitives-layer.
- **build ⊂ update, both via `build_core.build_and_install`** (`prepare_deps` + per-pkg loop +
  `install_built`); makepkg runs with `BATCH_STRIP_FLAGS`+`force_batch`, batch members topo-ordered
  + JIT-installed. The JIT install passes `allow_break_deps` (pkgnames of batch members not yet
  built, **including the current target** — it is the usual pin holder) so an exact-version
  sibling pin (`lib32-*` VCS) can be broken; `pacman.batch_install_pkgs` re-verifies via
  `deps_broken_by_install` and adds a **single** `--nodeps`, never `-dd`. No second dep/build
  loop. File-conflict isolation (retry without pacman's named culprits) lives only in
  `install_built`; `batch_install_pkgs` just reports them via `conflicts_out` and never retries
  (`3.2.0-B33`). Tests monkeypatch `sysforge.build_core.X`. §CLI Verb.
- **PKGBUILD review gate**: `build_and_install(review=…, review_trusted=…)` →
  `pkgbuild_review.review_target`; both verbs resolve via `resolve_review_policy`; a refusal drops
  the target (never aborts). The baseline is saved **on accept** by
  `build_core._save_review_baseline` into `[reviewed]`, never at the record site (`3.5.0-B8`).
  New build_state fields go in `_ENTRY_KEYS` (`primitives/build_state.py`), which
  `BuildState._serialize` writes. §primitives-layer.
- **Unified run-log**: `log.open_unified_log`/`close_unified_log`; verbs opt in via
  `Verb.unified_log_basename`. §Logging.
- **Machine-readable output** (`--json`): `primitives/json_out.py` only — `reserved_stdout()` (entered by
  `cli.main` and `verbs.runner.run_verb`; re-entrant) + one `emit(kind, body)`; never `print(json.dumps…)`.
  Build the body from the renderer's own result objects; a new field updates its
  `tests/data/json-schema/` schema in the same change (standards row 32). §Standards.
- **Flag-drift**: `flag_drift.resolve_flag_drift` (pure); sole consumer `update` Phase 4.3. §update.
  Out-of-profile inputs (python/mesa/llvm/PGO profile) go through `build_inputs.resolve_build_inputs`
  on **both** sides (`_record_build_state` and the drift sub-axis): a new input is added there,
  never recorded from what a patcher reported applying (`3.2.0-F18`). §Drift detection.
- **Config→flag precedence**: `config.resolve_flag_default(args, attr, cfg, key)` is the sole seam
  for `store_true` CLI flags falling back to a config default (CLI wins). §primitives / §`update.py`.
- **`toolchain` profile-field expansion**: `profile._expand_toolchain`; `[defaults] toolchain` sync
  via `config.set_default_toolchain`. Distinct from `toolchain.toml compiler`/`toolchain_variant`.
- **packages.toml `[group.*]` expansion**: `config.expand_package_groups` — never iterate
  `data.get("package")`. Desktop catalog: `primitives/pkg_catalog.py` (add a DE via `DESKTOP_CATALOG`
  + `bootstrap.toml [desktop]` + both completions). §Package Manifest.
- **`build_state.toml` = steady-state tracking authority** (not packages.toml): `update` rebuilds
  every source-built pkg (`build_mode != "pacman"`), so `build mesa` is durable. Demotion on external
  `pacman -S` via `BuildState.reconcile_external_installs`, which resolves each external name through
  `install_reconcile.resolve_installed_name` (the `-sysforge` rename) and then sweeps its `pkgbase`
  siblings, so a split set demotes as a unit (`3.2.0-B15`) — an exact-pkgname lookup silently misses
  both. A member displaced by a *differently named* stock package is surfaced, never demoted
  (`update_split.annotate_split_members`, `3.2.0-B31`). Stop via `state forget`. §update.
  The *decision* to stop is the opt-out record (`opt_out.OptOutList`, `3.4.0-F4`): written only via
  `verbs.shared.forget_packages(opt_out=…)` (revert/forget, never uninstall), subtracted only in
  `update_assemble._assemble_package_set`, cleared only by an explicit adoption.
- **`revert-to-stock` branch = rename mode, not a suffix test**: `revert_cmd.plan_revert` classifies
  via `profile.is_optimized_build_mode` then `rename_mode_for_build_mode` — plain `source_built`→
  `reinstall` (`pacman -S <name>`), `conflict` optimized→`replace` (**one** `pacman -S` of every
  *installed* split member's stock name via `_conflict_members`, with `reinstall_repo_pkgs(replace=True)`
  → `--ask=4`, since `--noconfirm` answers the conflict prompt N; no pre-`-R` — it breaks reverse deps,
  pacman does the atomic swap; `3.3.0-B6`), `coexist` (kernel FDO only)→`derename` (`-R` renamed then `-S` stock). Never collapse
  conflict+coexist. Demotion reuses `cmd_state_forget` + `reconcile_external_installs`. §CLI Verb.
- **`build` repo-pkg opt-in gate**: `build_cmd` prompts on TTY (writes via `packages_cmd`) or aborts
  non-interactive; `--force` builds this run only, never writes. One packages.toml writer. §CLI Verb.
- **Vocabulary** (renamed): build_mode `source_built` (legacy `profiled` build_state token removed
  in 3.0.0 — a pre-rename file now reads `profiled` verbatim until `sysforge state repair`, which
  also normalizes it via `build_state.LEGACY_BUILD_MODE_TOKENS`, as it does mesa's retired `pgo_mesa`; the profile-layer token
  `patched_pkgbuild` was removed in 4.0.0 and raises in `profile.normalize_build_mode`);
  repo_mode `build_from_source` (via `config.resolve_repo_mode`; legacy `profiled` token removed in
  3.0.0 — rejected by `REPO_MODE_ACCEPTED_INPUTS`, not aliased);
  per-pkg `enable_build_from_source` (legacy `pkgbuild_patch` — write-side only as of 3.0.0;
  `normalize_package_entry` no longer reads it). One read chokepoint per surface.
- **Hardcoded `-fuse-ld=` reconcile** (linker only; compiler stays `has_hardcoded_gcc`): detect
  `pkgbuild_meta.hardcoded_build_linker`, effective `makepkg_flags.resolve_effective_linker`, patch
  `pkgbuild_patcher.patch_build_linker`. §`pkgbuild_patcher.py`.
- **Optimization rename/store + PGO/FDO/BOLT** (all coexist via `-sysforge` rename): gate
  `profile.is_optimized_build_mode`; rename `pkgbuild_patcher.patch_package_suffix(mode=conflict|
  coexist)` — a thin wrapper over `patch_pkgbase_rename` (kernel local-rename via
  `BuildOptions.rename_pkgbase_to`, applied first so layers stack), validated G3; stores via
  `makepkg_pgo.resolve_method_store`.
  - **Instrumentation PGO** (`primitives/mesa_pgo.py`): `--pgo` on any package via the
    `compiler_flags_extra` seam (no `-Db_pgo` patch); `record` keeps stock name, `use` earns
    `-sysforge`. Per-package stores `pgo/<pkgbase>` (mesa too since 4.0.0; `state repair` migrates the old `pgo-mesa`); durable reuse via
    `reuse_profdata`. LLVM-only; warn-list `config.pgo_warns_for`.
  - **Kernel sample-based FDO** (`primitives/kernel_fdo.py`): `run kernel --autofdo=record|capture|
    use`, run twice for Propeller (round 2 pins round 1's profile). One decision:
    `fdo.plan_fdo_build` → `FdoPlan`, threaded through the stage. Role names only via
    `kernel_fdo.record_pkgname`/`optimized_pkgname` (`strip_role` inverts); `round.toml` only via
    `kernel_fdo.write_round`/`read_round`, `applied.toml` only via `kernel_fdo.write_applied`/
    `read_applied` (a `use` forces `-f` when the profile changed; every `record` forces).
    `capture` read-only; profiles go in via the `extra_env` make-var seam. LLVM-only. §Kernel stage.
  - **BOLT post-link** (`primitives/bolt.py`, orchestration `toolchain/bolt.py::optimize_in_build`):
    EXPERIMENTAL, default off. Runs on the Pass-4a **build tree** (unstripped, `--emit-relocs`),
    then `makepkg --repackage`; never rewrites an installed file. Replace tree files only via
    `bolt.replace_keep_mtime` (a changed mtime makes ninja relink and undo it). Targets
    `libclang-cpp` (+ `libLLVM` behind `[bolt] libllvm`), never the clang driver.
  - §CLI Verb / §Flag-Profile / §`pkgbuild_patcher.py`.
- **Bench engine**: `bench_core.run_toolchain_bench` is the one home, called by `bench_cmd` (the
  verb), `state_cmd` (GAIN, via `bench_core.current_fingerprint`) and the toolchain stage
  (`toolchain/measure.py::after_install`, `[bench] after_build`). Never import `bench_cmd` from
  another verb or the pipeline. `bench.toml` only via `primitives/bench_state.py`. §CLI Verb.
- **Stage step progress** (`3.5.0-F6`): a long stage counts its own steps with `progress.steps`
  (outside the tracker, one-tracker rule untouched), advanced by `progress.step(name)` — which
  drops skipped planned steps and inserts unplanned ones, so never pre-tick to fix a count.
  Step durations only via `primitives/stage_timings.py` (`stage.step_recorder`; nothing under
  `--dry-run`). §`ui/progress.py`.
- **Build-failure recovery**: `makepkg_invoke._run_recovery_menu`; persists a cc/ld swap via
  `profile_writer.write_package_compiler_override` (sole `profiles.toml` writer, with
  `delete_package_compiler_override`; `build --save-/--reset-toolchain` use the same pair).
  A CLI toolchain swaps through `profile.swap_toolchain` like a row. §makepkg-wrapper.
- **Sandbox host-only set**: `build_sandbox.host_only_packages` (source-built ∪ `pacman -Qm`) feeds
  both container channels — the `-I` closure and `local_repo.sync`; the ABI gate keeps the narrower
  `source_built_packages`. A `rust`/`cargo` target swaps `llvm_linked` for the repo LLVM via `-I` only
  (`fetch_repo_packages`), never by dropping LLVM from the local repo (2026-09-11 mesa). §makepkg-wrapper.
- **Build failures**: reserved `[failures]` namespace in `build_state.toml`; diagnostics in
  `build_diag.py` (`_MATCHERS`, on `strip_ansi`-cleaned lines). §Build failures.
- **Refused-install revert/restore**: `build_core._revert_uninstalled_state` stashes the undone entry
  in the reserved `[reverted]` namespace (keyed by artifact filename); `install_built` restores it via
  `_restore_reverted_state` when that file installs. That is the only record a reused (`AlreadyBuilt`)
  artifact gets, so every `install_built` caller passes `state_dir` (`3.3.0-B2`).
- **First-install notice**: `init_notice.maybe_emit_init_notice` (reads/deletes only; marker created
  solely by the PKGBUILD `post_install` scriptlet). §`init_notice.py`.
- **Report-block presentation**: `primitives/render.py` (`arrow`, `version_pair`, `tag_header`) —
  the one home for the `[TAG]` gutter and `old → new` / `ver (=)` vocabulary shared by
  `update_summary` and both `render_preflight`s. **Never hardcode `→` or re-inline the gutter
  literal**; glyphs resolve at format time (idempotent with `log.ui`'s `downgrade_glyphs`).
  **Every pre-flight block emits via `log.ui`, never bare `print()`** — that is what mirrors it
  into the unified run-log; enforced by `tests/test_preflight_logging.py`.
  A pair's jump-size tint is `version_pair(..., classify=True)` (`3.4.0-F8`, order via the pure
  `version.vercmp_local`); only on a `ui()`/`print()` line, never in text that can reach `error()`.
- **LLVM source-state**: `llvm_state.collect_llvm_state` — don't call `git_is_dirty`+URL-parse.
- **Live-config schema and repair**: `primitives/config_schema.py` (`KNOWN_SECTIONS`/`KNOWN_TOP_KEYS`,
  `OPEN_SECTIONS`, `RETIRED_KEYS`/`RETIRED_VALUES`, `shipped_text`, `inspect`, `clean_text`,
  `reset_keys_text`); `tools/check_shipped.py` imports its schema. A retired config key or value
  gets a row there in the same change; a new key is valid once the shipped default documents it.
- **Editor/merge-tool launch**: `primitives/editor.py` (`resolve_editor`, `resolve_merge_tool`,
  `run_tty_argv`); `.sfnew` adoption in `config_cmd.ConfigMergeVerb`. §Config Layer.
- **Dir/group provisioning**: `primitives/fs_provision.py` (`root:sysforge` setgid 2775); `/etc/
  sysforge` stays root-owned; shipped tmpfiles.d/sysusers.d reuse `SYSFORGE_GROUP`/`SYSFORGE_DIR_MODE`.
  §07.
- **libalpm-hook refresh**: `primitives/pacman_hooks.py` (consumers: `setup_cmd`,
  `doctor._collect_hook_findings`); a new hook updates `HOOK_NAMES` + shipped `.hook` +
  `pyproject.toml` force-include in lockstep. §`pacman_hooks.py`.
- **Install sentinel**: `stage_sentinel.sentinel_scope()` — record *and* its liveness lock
  (`stage_in_progress.lock`, via `build_lock`; never roll a second flock path or a PID field).
  Scopes are sequential, never nested. Contention is strict (refuse, no override, no prompt);
  `OSError` stays lenient. The verb runner's scope (`arm_on_mutation=True`, `3.5.0-B138`) takes
  the lock on entry but writes the record at the first mutating `privileged_argv`, announced via
  `privilege.on_mutation`, so a pre-install failure leaves none; stages arm on entry. §Toolchain stage.
- **`doctor` axes**: each a producer → `list[diagnostics.Finding]`, read-only (no `pacman -Sy` /
  `BuildState.save()`; never import `pipeline`). Register in `doctor.py` (the producer plus its
  flag in `_add_system_axis_flags`) + both completions + manpage + each axis's `clean_msg` in the
  same change. §doctor.
- **ABI checker** (`abi_check.py`): symbol-version precise (`sym@@VER`/`sym@VER`) — don't regress.
- **Toolchain preflight assumes rustup**: `toolchain_preflight._probe_cc` reads `$RUSTUP_TOOLCHAIN`;
  new cross targets plug into `collect_required_toolchains` + `rust:cross:<target>`.
- **Stage-owned packages** stamp `owner_stage` (kernel/toolchain) so `update` skips them; policy
  `stage_ownership.owner_of`; `lib32-*` excluded from prefix-ownership. §Toolchain stage.
- **Privilege escalation**: `primitives/privilege.py` (`privileged_argv`/
  `run_privileged`) — never hand-roll `["sudo", …]`; auth probes (`-v`/`-n true`)
  and drop-priv (`-u`) are the only exceptions. Every escalation arms the verb sentinel
  unless it passes `mutates=False`, which is only for a command an interruption cannot leave
  inconsistent (download, runtime setting, atomic/idempotent write, the sandbox chroot) or an
  argv only displayed, with the reason beside it. §22.
- **Sudo credential *lifetime*** (orthogonal to escalation): `primitives/sudo_session.py`
  — `authenticate()` (returns usability, so "not authorized, nothing ran" is
  distinguishable from "ran and failed") + `keepalive(tag=…, enabled=…)` context
  manager. The two long-building stages, `build_core.build_and_install`, and `update`'s
  `-Syu` + end-of-run prune window (`3.4.0-B2`) use it;
  never re-roll the daemon. **makepkg never escalates** (`3.4.0-F1`): strip `-s`/`-i`
  and install deps (`build_core.install_missing_repo_deps`) and artifacts
  (`install_built_packages`) through the privilege seam — sudo's prompt goes to
  `/dev/tty`, where a forwarded child's prompt can't be seen or kept off the bar.
  **Always `authenticate()` before entering `keepalive()`, and stop on `False`** — the
  refresh is `sudo -v -n` and cannot prompt, so without credentials it only warns while
  a build that cannot install runs (`3.5.0-B55`). The kernel stage also
  probes *before* `sentinel_scope` so a stale prompt can't strand a recovery
  sentinel for a mutation that never began. §22.
- **Source freeze**: `primitives/net_policy.py` (`get_policy().check(...)`) is consulted at eight
  seams — AUR clone, `build_prep.pkgctl_checkout`, source-sync fetch, both `vcs_pkgver.py`
  probes, `build_prep.fetch_pgp_keys` (`KIND_KEY_FETCH`, the one keyserver home — consent-gated,
  `auto_repair` routes through it), `reconfigure._step_gpg`'s whole-keyring refresh (same kind,
  `None` pkgbase, `3.5.0-B119`), and `auto_repair`'s subproject/submodule download
  (`KIND_SUBPROJECT_FETCH`) — the two `vcs_pkgver` seams must stay gated together. Every seam keys the `--thaw` lift
  on the **authoritative pkgbase**, threaded explicitly (never the checkout dir name — a rename
  breaks it). A new code-ingress seam gets its own `KIND_*`, never a reuse. `update.py`'s single
  `_raise_if_frozen` is the one home for the non-zero exit. `[aur] enabled = false` is a `NetPolicy`
  field (`KIND_AUR_CLONE`/`KIND_AUR_RPC`), enforced in `aur.py`'s four network functions plus the
  scheduler's `is_aur_checkout` gate; never by checking a `source` label alone. §Config Layer / §primitives-layer.

## Toolchain & kernel deep invariants (rationale: DESIGN.md §07)

- **Both stages are packages, not modules** (`pipeline/stages/{toolchain,kernel}/`). Two rules keep
  the split honest: modules call each other **module-qualified** (`profdata.pgo_install(...)`, never
  a bare imported name) so a test patch on the owning module reaches every caller; and a name that
  **crosses a module boundary is public**. Each `__init__` re-exports the public names, so the old
  flat import paths still resolve. §07.
- **Stage config is parsed once at entry** into a frozen `ToolchainConfig`/`KernelConfig`
  (`<stage>/config.py::from_toml`) — **never re-read a TOML key at a use site**, and never default
  one there. `raw` is kept only for the store resolvers (`resolve_pgo_store`,
  `bolt.resolve_store`), `resolve_pkgbuild_src_dir` and the Pass-4 `config_digest` (which must
  keep hashing the raw table, via `reuse_digest_raw` minus the build-inert `[bench]`, or every reuse
  cache key changes).
  `KernelConfig.compiler` is `str | None`: unset means "inherit from pipeline state". §Config Layer.

- **Health = exactly two checkers**: `toolchain/verify.py::verify_llvm_install` (`run toolchain`) +
  `toolchain_preflight._probe_cc` (`update`), over `LLVM_LOCKSTEP_SUITE`. `toolchain_safety.py` /
  `llvm_state.detect_toolchain_config_mismatch` are facts, **not** a third checker.
- **Stages = 3 gates, build split from install, snapshot+auto-restore.** Build is `install=False`;
  Gate 2 outside `sentinel_scope`, install→Gate-3→rollback inside. **GCC path never builds GCC**
  (register-only). LLVM `pgo=false` + repo_mode=`pacman` → install stock from repos; **PGO always
  builds from source**. `repo_mode` read via `config.resolve_repo_mode` only.
- **Pass-4 builds non-pgo against the libLLVM it ships** (`CMAKE_PREFIX_PATH` **and** forced
  `-DLLVM_DIR` via `patch_llvm_dir`; staging3 needs both `llvm-libs`+`llvm`). Build reuse opt-in via
  `build_fingerprint.py` (`--reuse-built`, fail-safe to rebuild; bump `_SCHEMA` on new inputs).
- **Pass-3 training corpus**: `resolve_training_corpus`; extras (mesa) compile with the instrumented
  stage1 clang so profraw merges into the one `clang.profdata` — never installed, never
  `-fprofile-use` (`pgo_reuse=False`), no profile-runtime force-load. Best-effort, but
  `BuildAborted` (user abort) re-raises. PGO path only.
- **libLLVM soname-bump → consumer rebuild**: `assess_libllvm_soname_impact` / `_gate_soname_consumers`
  (after Gate 3, outside sentinel). No parallel reverse-dep scanner.
- **libLLVM `std::` export parity** (`3.5.0-F4`): the table is `primitives/llvm_std_exports.py`
  (`STD_EXPORTS`; add a row, never a second injector), pasted by
  `pkgbuild_patcher.patch_llvm_std_exports`. Thunks are **non-default** `sym@LLVM_<mm>` jumping to a
  `.symver`-pinned `@GLIBCXX_*`: a default export makes the suite bind `@LLVM_*` (`scan_abi_hazards`
  aborts), an unpinned jump loops under lld. Rendered text stays brace-free (`prepare()` span).
- **System libLLVM must keep `AMDGPU`** (mesa's `libgallium` links it unconditionally — dropping it
  bricks the desktop). Enforced at resolution (`llvm_targets._ensure_system_consumer_targets`) +
  symbol gate (`toolchain_safety.check_system_consumer_symbols`). Opt-out `[llvm] targets = []`.
- **Mesa driver filtering** = meson analogue of LLVM target filtering, **inverted**: derive
  `hardware.derive_mesa_drivers`, resolve `mesa_drivers.resolve_or_detect_mesa_drivers`, patch
  `pkgbuild_patcher.patch_mesa_drivers` (only meson injector/validator). Software baseline always kept
  (`_ensure_mesa_software_baseline`). Opt-in `[mesa] filter_drivers`; lib32-mesa is filtered.
  §Hardware detection.
- **Kernel stage**: compiler independent of toolchain (`toolchain/identity.py::compiler_paths`);
  **config review on by default**, resolved only by `kernel/config.py::resolve_config_review`;
  the stage's own questions ask `prompt.is_interactive()` and never re-derive it (`3.5.0-F22`);
  subpackage toggles `kernel/config.py::resolve_subpackages` +
  `pkgbuild_patcher.patch_kernel_subpackages` (disabling
  headers keeps the Gate-1 DKMS warning); boot-safety in `kernel_safety.py`/`device_probe`. §Kernel.

`run toolchain`, `run kernel`, and the PGO profdata-reuse path are stable but default `enabled =
false` (opt-in — don't flip). `run toolchain` defaults `compiler = "gcc"`.
