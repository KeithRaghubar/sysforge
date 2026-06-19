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
- **Completions stay in lockstep with the CLI**: update `completions/_sysforge` (and the
  bash completion) in the same change as any CLI-surface change, not as a follow-up.
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
- **PKGBUILD review gate has one home**: `build_core.build_and_install(review=…)` →
  `pkgbuild_review.review_target` (`build` defaults `"prompt"`, `update` `"auto"`); AUR
  dep builds gated via `review_deps` in `prepare_deps`. Baseline is sticky `reviewed_commit`
  in `build_state.toml`. Abort is a clean `BuildOutcome.aborted`, not an exception. New
  serialized `build_state` fields go in `BuildState._serialize`'s key tuple. See DESIGN.md
  §`primitives-layer`.
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
  GCC path is register-only — **the stage never builds GCC**.
- **Pass-3 builds non-pgo against the libLLVM it ships** (3a→`_extract_pass2_to_staging`→3b/3c
  with `CMAKE_PREFIX_PATH` **and** forced `-DLLVM_DIR` via `patch_llvm_dir` — prefix-path alone
  is NOT enough). staging3 needs both `llvm-libs` and `llvm`; don't collapse Pass 3 into one pass.
- **libLLVM soname-bump → consumer rebuild**: facts `assess_libllvm_soname_impact`; policy
  `_gate_soname_consumers`/`_rebuild_soname_consumers` (rebuild after Gate 3, outside the
  sentinel). No parallel reverse-dep scanner.
- **Pass-3 build reuse has one home**: `primitives/build_fingerprint.py` (opt-in `--reuse-built`,
  fail-safe to rebuild). New `compute_fingerprint` inputs bump `_SCHEMA`.
- **Kernel stage**: compiler independent of toolchain (`_compiler_paths`, don't hardcode);
  **interactive by default** (only verb where unattended is opt-in); base `.config` via
  `_resolve_base_config`. Boot-safety tables in `kernel_safety.py`/`device_probe`, not inline;
  `device_probe.enumerate_devices` + `kernel_safety.parse_kconfig_text` are the single entry points.
  Headers/docs toggles resolve in `_resolve_subpackages` (CLI `--headers`/`--docs` >
  `kernel.toml build_headers`/`build_docs` > headers-on/docs-off default); dropping a
  subpackage has one home — `pkgbuild_patcher.patch_kernel_subpackages` edits the
  `pkgname=(...)` array (no parallel pkgname editor). Disabling headers must keep the Gate-1
  DKMS/out-of-tree warning. See DESIGN.md §Kernel stage.

`run toolchain` (stage 6), `run kernel` (stage 8), and the PGO profdata-reuse path
(`build_mode = "pgo_llvm_toolchain"`) are stable but default `enabled = false` (opt-in —
don't flip). `run toolchain` defaults `compiler = "gcc"`; LLVM is opt-in only.

## Interaction Preferences

- Be direct. Own mistakes and fix them immediately.
- When a design decision is made, update the relevant `docs/design/*.md` source (and run
  `make design`) immediately in the same turn — don't wait to be reminded.
