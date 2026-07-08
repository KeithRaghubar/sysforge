# SysForge — Claude Code Context

**DESIGN.md is the source of truth** (module layout, APIs, CLI, feature status, gaps) — read
before any architecture/API change. It is **generated**: edit `docs/design/*.md`, run `make
design`, never edit `DESIGN.md` directly (`make check-design` guards it). This file carries only
always-on guardrails (rule + one-home seam + §DESIGN pointer) — don't re-inline design detail.

Repo: <https://github.com/KeithRaghubar/sysforge.git> — Python, TOML config.

## Commands (canonical: the Makefile — never invoke `pytest` directly)

```bash
make test / test-x   # full suite / stop on first failure
make lint            # ruff
make design          # regenerate DESIGN.md after editing docs/design/*
make check-design    # guard: DESIGN.md in sync with sources
make check-shipped   # guard: etc/sysforge, PKGBUILD*, hooks, completions, manpage parity
make check-standards
make sync-config     # adopt new shipped defaults into untracked live config (add-only)
make release-{major,minor,patch}
```

Shipped-file edits must pass `make check-shipped`; doc/design edits `make check-design`.

## Dev Environment

- Ryzen 7 5800X3D, RTX 5070, Arch Linux, COSMIC/Wayland, nvidia-open-dkms.
- `pkgbuild_src_dir = ~/src` (sources); builds at `~/builds`.
- `etc/sysforge/` = shipped defaults; `tests/data/etc/sysforge/` = git-tracked fixtures, kept in
  parity by `make check-shipped` (conftest forces `SYSFORGE_CONFIG_DIR` to it) — a shipped-default
  change updates fixtures too. Live config is a separate untracked dir (`make sync-config`).
  §Config Layer.

## Gotchas

- **`tests/test_pipeline.py`** imports from both `primitives.config` and `…profile` — moving
  symbols across them breaks it; update the test in the same change.
- **`match_rules` matches against `pkgbase`** too (split packages) — don't regress.
- **Source sync goes through the scheduler** (`source_sync.get_scheduler().request(...)`), never
  `git pull --rebase`. Fetch is full-history (`git_fetch_and_compare`), never `--depth=1`.
  `STATUS_DIVERGED` auto-resolves (hard-reset) when clean per `git_is_dirty(is_vcs=…)` (ignores
  `.SRCINFO`/pkgver/pkgrel bumps — don't re-narrow). `FAILED`/`RATE_LIMITED`/`PURGE_REFUSED` are
  blockers. §`source_sync.py` / §`git_ops.py`.

## Process Conventions

- **Doc update order**: `docs/design/*.md` (+ `make design`) → README.md → CLAUDE.md.
- **DESIGN = implemented only; `/ROADMAP.md` = planned + abandoned.** Roadmap IDs
  (`<version>-<TYPE><n>`, e.g. `1.2.0-F1`; counters reset on major/minor bump, not patch) live in
  ROADMAP + `docs/release-notes/`, never DESIGN. Triage `notes.txt` into ROADMAP. Implementing an
  item **removes it from ROADMAP in the same commit** (git history is the record — drop the whole
  entry, no "done" marker) and **appends its release-note entry to `docs/release-notes/unreleased.md`**
  (Keep a Changelog section, inline roadmap ID). Keep entries in ascending ID order — re-sort on
  every add/remove. `tools/release.sh` Phase 1 renames the accumulator to `vX.Y.Z.md` + reseeds;
  the `release-notes` skill only reconciles before a release. `make check-standards` lints it.
- **Completions stay in lockstep with the CLI** (`completions/_sysforge` + bash) in the same change.
- **CLI verbs go through the Verb framework**: `Verb` subclass dispatched by `verbs.runner.run_verb`
  (`pre_check`/`execute`/`post_validate`; `requires_sentinel=True` if it mutates). Wire via
  `set_defaults(verb_cls=…)`, not `func=`. §CLI Verb Framework.
- **Dual-toolchain test parity**: logic branching on resolved compiler (gcc vs llvm) ships both a
  gcc-path and an llvm-path test in the same change.
- **PKGBUILD parsing/patching**: cross-check `PKGBUILD(5)` (array vs string, escapes). Arch array
  families merge into the canonical key (`_ARCH_ARRAY_FAMILIES` in `pkgbuild_meta.py`); never read
  `_<arch>` keys. Static parser never sources the PKGBUILD — unresolved `${…}`/`$(…)` caught by
  `_looks_unresolved`, rescued via AUR RPC `.SRCINFO`. cmake-arg injection anchors the cmake
  configure invocation, gated by `validate_patched_pkgbuild`. §`pkgbuild_meta.py`/§`pkgbuild_patcher.py`.
- **Releases are GPG-signed**: `tools/release.sh` signs commit+tag+tarball, gated by a signing
  preflight + sentinel-fingerprint publish gate. Stable PKGBUILD verifies via `validpgpkeys` + a
  `.asc` source (`SKIP`); `-git` exempt. §Release Process / §Standards row 16.
- **Shipped-file allowlists** (`_KNOWN_SECTIONS`/`_KNOWN_TOP_KEYS` in `tools/check_shipped.py`):
  extend in the same change. §Shipped-file pre-release checks.
- **Standards have one home**: `docs/design/21-standards.md`; enforced by `tools/check_standards.py`
  + `tests/test_standards_compliance.py`. User paths → `primitives/paths.py`; colour →
  `log.use_color()`.

## One-home invariants (don't add parallel paths)

Each is the sole home for its concern — reuse the seam, never add a second writer/injector/loop.
Mechanism lives in the cited §DESIGN section.

- **Flag scrubs** (lib32/musl-static/PGO): `emit_makepkg_conf(is_lib32=|is_musl_static=)`, reuse
  `_strip_lld_flags`/`_strip_pgo_flags`; musl via `pkgbuild_meta.is_musl_static_build`. §Flag/Profile.
- **Build throttle** (nice/ionice/cpu_quota/jobs): `build_throttle.resolve_throttle`; channels
  `wrapper_argv` + `apply_jobs_to_makeflags`. §Flag/Profile.
- **makepkg path resolution**: `pacman.get_pkgdest()`/`get_builddir()`/`get_srcdest()`/
  `get_logdest()` — never read `os.environ["BUILDDIR"]` or assume `~/builds`. Write conf keys via
  `config.set_makepkg_conf_keys`. §primitives-layer.
- **build ⊂ update, both via `build_core.build_and_install`** (`prepare_deps` + per-pkg loop +
  `install_built`); makepkg runs with `BATCH_STRIP_FLAGS`+`force_batch`, batch members topo-ordered
  + JIT-installed. No second dep/build loop. Tests monkeypatch `sysforge.build_core.X`. §CLI Verb.
- **PKGBUILD review gate**: `build_and_install(review=…)` → `pkgbuild_review.review_target` (build
  `prompt`, update `auto`); sticky `reviewed_commit`. New build_state fields go in
  `BuildState._serialize`'s key tuple. §primitives-layer.
- **Unified run-log**: `log.open_unified_log`/`close_unified_log`; verbs opt in via
  `Verb.unified_log_basename`. §Logging.
- **Flag-drift**: `flag_drift.resolve_flag_drift` (pure); sole consumer `update` Phase 4.3. §update.
- **Config→flag precedence**: `config.resolve_flag_default(args, attr, cfg, key)` is the sole seam
  for `store_true` CLI flags falling back to a config default (CLI wins). §primitives / §`update.py`.
- **`toolchain` profile-field expansion**: `profile._expand_toolchain`; `[defaults] toolchain` sync
  via `config.set_default_toolchain`. Distinct from `toolchain.toml compiler`/`toolchain_variant`.
- **packages.toml `[group.*]` expansion**: `config.expand_package_groups` — never iterate
  `data.get("package")`. Desktop catalog: `primitives/pkg_catalog.py` (add a DE via `DESKTOP_CATALOG`
  + `bootstrap.toml [desktop]` + both completions). §Package Manifest.
- **`build_state.toml` = steady-state tracking authority** (not packages.toml): `update` rebuilds
  every source-built pkg (`build_mode != "pacman"`), so `build mesa` is durable. Demotion on external
  `pacman -S` via `BuildState.reconcile_external_installs`; stop via `state forget`. §update.
- **`build` repo-pkg opt-in gate**: `build_cmd` prompts on TTY (writes via `packages_cmd`) or aborts
  non-interactive; `--force` builds this run only, never writes. One packages.toml writer. §CLI Verb.
- **Vocabulary** (renamed, legacy aliased on read): build_mode `source_built` (legacy `profiled`);
  repo_mode `build_from_source` (via `config.resolve_repo_mode`); per-pkg `enable_build_from_source`
  (legacy `pkgbuild_patch`). One read chokepoint per surface — no scattered `or "profiled"`.
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
    `-sysforge`. Per-package stores (mesa keeps back-compat `pgo-mesa`); durable reuse via
    `reuse_profdata`. LLVM-only; warn-list `config.pgo_warns_for`.
  - **Kernel sample-based FDO** (`primitives/kernel_fdo.py`): `run kernel --autofdo=record|capture|
    use` (+`--propeller`); `capture` read-only; `use` injects via the `extra_env` make-var seam.
    LLVM-only. §Kernel stage.
  - **BOLT post-link** (`primitives/bolt.py`): EXPERIMENTAL **and BLOCKED** (dylib-only LLVM can't
    link standalone BOLT tools; `standalone_build_viable()` guards) — **keep `[bolt] enabled =
    false`**. Toolchain Pass 5, in-place stock name.
  - §CLI Verb / §Flag-Profile / §`pkgbuild_patcher.py`.
- **Build-failure recovery**: `makepkg_invoke._run_recovery_menu`; persists a cc/ld swap via
  `profile_writer.write_package_compiler_override` (sole `profiles.toml` writer). §makepkg-wrapper.
- **Build failures**: reserved `[failures]` namespace in `build_state.toml`; diagnostics in
  `build_diag.py` (`_MATCHERS`, on `strip_ansi`-cleaned lines). §`build_diag`.
- **First-install notice**: `init_notice.maybe_emit_init_notice` (reads/deletes only; marker created
  solely by the PKGBUILD `post_install` scriptlet). §`init_notice.py`.
- **LLVM source-state**: `llvm_state.collect_llvm_state` — don't call `git_is_dirty`+URL-parse.
- **Editor/merge-tool launch**: `primitives/editor.py` (`resolve_editor`, `resolve_merge_tool`,
  `run_tty_argv`); `.sfnew` adoption in `config_cmd.ConfigMergeVerb`. §Config Layer.
- **Dir/group provisioning**: `primitives/fs_provision.py` (`root:sysforge` setgid 2775); `/etc/
  sysforge` stays root-owned; shipped tmpfiles.d/sysusers.d reuse `SYSFORGE_GROUP`/`SYSFORGE_DIR_MODE`.
  §07.
- **libalpm-hook refresh**: `primitives/pacman_hooks.py` (consumers: `setup_cmd`,
  `doctor._collect_hook_findings`); a new hook updates `HOOK_NAMES` + shipped `.hook` +
  `pyproject.toml` force-include in lockstep. §`pacman_hooks.py`.
- **Install sentinel**: `stage_sentinel.sentinel_scope()`. §Toolchain stage.
- **`doctor` axes**: each a producer → `list[diagnostics.Finding]`, read-only (no `pacman -Sy` /
  `BuildState.save()`; never import `pipeline`). Register in `doctor.py` + `cli.py` + both
  completions + manpage + `_patch_axes_clean` in the same change. §doctor.
- **ABI checker** (`abi_check.py`): symbol-version precise (`sym@@VER`/`sym@VER`) — don't regress.
- **Toolchain preflight assumes rustup**: `toolchain_preflight._probe_cc` reads `$RUSTUP_TOOLCHAIN`;
  new cross targets plug into `collect_required_toolchains` + `rust:cross:<target>`.
- **Stage-owned packages** stamp `owner_stage` (kernel/toolchain) so `update` skips them; policy
  `stage_ownership.owner_of`; `lib32-*` excluded from prefix-ownership. §Toolchain stage.

## Toolchain & kernel deep invariants (rationale: DESIGN.md §07)

- **Health = exactly two checkers**: `toolchain.py::_verify_llvm_install` (`run toolchain`) +
  `toolchain_preflight._probe_cc` (`update`), over `LLVM_LOCKSTEP_SUITE`. `toolchain_safety.py` /
  `llvm_state.detect_toolchain_config_mismatch` are facts, **not** a third checker.
- **Stages = 3 gates, build split from install, snapshot+auto-restore.** Build is `install=False`;
  Gate 2 outside `sentinel_scope`, install→Gate-3→rollback inside. **GCC path never builds GCC**
  (register-only). LLVM `pgo=false` + repo_mode=`pacman` → install stock from repos; **PGO always
  builds from source**. `repo_mode` read via `config.resolve_repo_mode` only.
- **Pass-3 builds non-pgo against the libLLVM it ships** (`CMAKE_PREFIX_PATH` **and** forced
  `-DLLVM_DIR` via `patch_llvm_dir`; staging3 needs both `llvm-libs`+`llvm`). Build reuse opt-in via
  `build_fingerprint.py` (`--reuse-built`, fail-safe to rebuild; bump `_SCHEMA` on new inputs).
- **Pass-2 training corpus**: `_resolve_training_corpus`; extras (mesa) compile with the instrumented
  stage1 clang so profraw merges into the one `clang.profdata` — never installed, never
  `-fprofile-use`. Best-effort, PGO path only.
- **libLLVM soname-bump → consumer rebuild**: `assess_libllvm_soname_impact` / `_gate_soname_consumers`
  (after Gate 3, outside sentinel). No parallel reverse-dep scanner.
- **System libLLVM must keep `AMDGPU`** (mesa's `libgallium` links it unconditionally — dropping it
  bricks the desktop). Enforced at resolution (`llvm_targets._ensure_system_consumer_targets`) +
  symbol gate (`toolchain_safety.check_system_consumer_symbols`). Opt-out `[llvm] targets = []`.
- **Mesa driver filtering** = meson analogue of LLVM target filtering, **inverted**: derive
  `hardware.derive_mesa_drivers`, resolve `mesa_drivers.resolve_or_detect_mesa_drivers`, patch
  `pkgbuild_patcher.patch_mesa_drivers` (only meson injector/validator). Software baseline always kept
  (`_ensure_mesa_software_baseline`). Opt-in `[mesa] filter_drivers`; lib32-mesa is filtered.
  §Hardware detection.
- **Kernel stage**: compiler independent of toolchain (`_compiler_paths`); **interactive by default**;
  subpackage toggles `_resolve_subpackages` + `pkgbuild_patcher.patch_kernel_subpackages` (disabling
  headers keeps the Gate-1 DKMS warning); boot-safety in `kernel_safety.py`/`device_probe`. §Kernel.

`run toolchain`, `run kernel`, and the PGO profdata-reuse path are stable but default `enabled =
false` (opt-in — don't flip). `run toolchain` defaults `compiler = "gcc"`.

## Interaction Preferences

- Be direct. Own mistakes and fix them immediately.
- When a design decision is made, update the relevant `docs/design/*.md` (+ `make design`) in the
  same turn — don't wait to be reminded.
