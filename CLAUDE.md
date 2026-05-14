# SysForge — Claude Code Context

Read DESIGN.md before proposing architecture or API changes. It is the source of truth for module layout, public APIs, CLI structure, feature status, and known gaps. Do not duplicate it here.

Repo: <https://github.com/KeithRaghubar/sysforge.git> — Language: Python, Config: TOML. The Makefile is the canonical entry point: `make test` / `make test-v` / `make lint` / `make release-{major,minor,patch}` / `make vm-*`. Don't invoke `pytest` directly.

## Dev Environment

- Ryzen 7 5800X3D, RTX 5070, Arch Linux, COSMIC on Wayland, nvidia-open-dkms
- `pkgbuild_src_dir = ~/src` (sources); builds at `~/builds`
- `etc/sysforge/` = shipped defaults (installed by PKGBUILD); `tests/data/etc/sysforge/` = Keith's live personal config for dev/testing. Separate dirs, not symlinked — update both explicitly when a change affects both.

## Known Bugs & Gotchas

1. **`tests/test_pipeline.py`** — imports from both `sysforge.primitives.config` and `sysforge.primitives.profile`. Splitting or moving symbols across those modules will break it; update the test in the same change.
2. **`match_rules` and `pkgbase`** — rules match against `pkgbase` too for split packages; don't regress this.
3. **Source sync goes through the scheduler, not `git pull --rebase`** — any new `fetch`/`update`/`build` code path that needs a fresh PKGBUILD must call `sysforge.primitives.source_sync.get_scheduler().request(SyncRequest(...))`. The scheduler handles RPC short-circuit, rate limiting, and dedup. See DESIGN.md §`source_sync.py` for status semantics (`STATUS_DIVERGED` is a warning; `STATUS_FAILED` / `STATUS_RATE_LIMITED` / `STATUS_PURGE_REFUSED` are blockers).

## Project Conventions

- **Doc update order**: DESIGN.md is the source of truth — update it first, then README.md, then CLAUDE.md. Don't update downstream docs without a corresponding DESIGN.md change.
- **Completions stay in lockstep with the CLI**: `completions/_sysforge` is updated in the same change as the CLI surface, not as a follow-up.
- **PKGBUILD parsing/detection/patching**: cross-check against `PKGBUILD(5)` before changing — easy to miss spec details (e.g. array vs string fields, escape rules).
- **LLVM source-state inspection**: `primitives/llvm_state.collect_llvm_state` is the only allowed entry point for new code that needs to inspect LLVM-toolchain source trees (variant, origin, dirty/diverged, install origin, build_mode). Do not call `git_is_dirty` + URL parsing directly — those checks drift out of sync with the rule set otherwise.
- **Dual-toolchain test parity**: any new logic that branches on the resolved compiler (gcc vs llvm/clang) must ship with both a gcc-path test and an llvm/clang-path test in the same change. Regressing one path silently is the failure mode we're guarding against.
- **CLI verbs go through the Verb framework**: every top-level command is a `Verb` subclass dispatched by `sysforge.verbs.runner.run_verb`. New verbs implement `pre_check` / `execute` / `post_validate` and set `requires_sentinel=True` if they mutate the live system. Read-only verbs (no install) leave `post_validate` as the default no-op and `requires_sentinel=False`. Argparse `set_defaults(verb_cls=XVerb)` wires the class into dispatch — do not add `func=` callbacks. See DESIGN.md §CLI Verb Framework.
- **Install-bearing scopes share one sentinel primitive**: both the verb runner and the toolchain pipeline stage use `primitives.stage_sentinel.sentinel_scope()`. Do not roll a separate `StageSentinel` install/clear path for a new install-bearing stage or verb — extend the existing primitive instead.

## Experimental (post-1.0)

`run toolchain` (stage 6), `run kernel` (stage 8), and the PGO-toolchain profdata-reuse path in `sysforge update` (`build_mode = "pgo_llvm_toolchain"`) are shipped but reclassified as experimental for 1.0 — they emit a runtime `[WARN]` at entry and default to disabled. Keep the implementation intact but do not treat them as part of the v1.0 stable surface. See DESIGN.md §Release Plan for full scope.

`run toolchain` defaults to `compiler = "gcc"` when the key is unset; LLVM is opt-in only. The shipped `[profiles.standard]` uses system gcc/binutils; LLVM components (`clang`, `lld`, `llvm`, `compiler-rt`) live in `optdepends` and are only required when the LLVM profile or `run toolchain --compiler=llvm` is selected.

**Toolchain stage never builds GCC.** The `compiler = "gcc"` path is register-only — it writes the system `/usr/bin/gcc` paths into pipeline state and returns. Stock `gcc-libs` from `base-devel` provides the runtime. Do not reintroduce a `_build_gcc` helper or any code path that runs `makepkg` for `gcc`/`gcc-libs` under the toolchain stage; if a future change needs a GCC build path, it lives in `packages`, not here.

**Kernel stage compiler is independent of the toolchain stage.** `kernel.toml compiler` (and `sysforge run kernel --compiler {gcc,llvm}`) lets the kernel use a different compiler than the system. Resolution: CLI flag > `kernel.toml compiler` > toolchain-stage pipeline state. Use the `_compiler_paths` helper in `sysforge/pipeline/stages/toolchain.py` when mapping the resolved name to cc/cxx paths — don't hardcode `/usr/bin/clang` etc. anywhere else.

**Kernel stage is interactive by default.** Unlike the other stages, `sysforge run kernel` defaults to running the PKGBUILD's interactive kconfig target (typically `make nconfig`). The non-interactive path is opt-in via `--non-interactive` or `kernel.toml interactive = false`. This is the only verb in the CLI where the unattended path is opt-in rather than opt-out; preserve that asymmetry.

## Interaction Preferences

- Be direct. Own mistakes and fix them immediately.
- When a design decision is made, update DESIGN.md immediately in the same turn — don't wait to be reminded.
