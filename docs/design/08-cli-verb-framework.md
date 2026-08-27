## CLI Verb Framework

Every top-level CLI verb (`build`, `update`, `fetch`, `doctor`, `resolve`, `env`, `help`, `setup`, `log`, `completions`, `packages …`, `state …`, `config …`, `run …`) is a `Verb` subclass — the `Verb` ABC and the `PreCheckResult`/`ExecResult` result types live in `sysforge/verbs/base.py`, while each concrete verb lives in its own per-command module (`build_cmd.py`, `run_cmd.py`, `env_cmd.py`, `help_cmd.py`, `completions_cmd.py`, `update.py`, `packages_cmd.py`, …). Verbs are dispatched through `run_verb()` in `sysforge/verbs/runner.py`. The framework is intentionally thin: three phases, two result types, one runner, one shared sentinel primitive. Argparse wiring in `cli.py` attaches the verb class via `parser.set_defaults(verb_cls=XVerb)` (never a `func=` callback), and `main()` resolves it via `sys.exit(_dispatch(args.verb_cls, args))` — a thin wrapper around `run_verb` that adds the optional cProfile harness (see *Global profiling flags* below).

**Parent-verb subcommand default (invariant).** A verb namespace declares a
default subverb via `set_defaults(verb_cls=…, <dest>=…)` on the *parent* parser
**iff** it has a single obvious read-only "show me" view; otherwise its
subparsers set `required = True`. Today: `doctor` → `system`, `packages` →
`list`, `artifact` → `list`, `state` → `list` carry defaults; `config` and `run`
require a subcommand, because their subverbs mutate or diverge with no natural
landing point. A new namespace picks a side by this test, not by precedent from
whichever namespace was copied. Set the subparser `dest` alongside `verb_cls` so
downstream code sees a consistent subcommand name either way.

**Three-phase contract.** Each verb implements:

- `pre_check(args) -> PreCheckResult` — validate args, load config, run preflights (LLVM safety, dirty-state guards, sudo checks). No state mutation. Returns one of three terminal shapes:
  - **proceed**: `skip_reason=None, blocker=None`, optional `ctx` dict carried into later phases.
  - **skip** (success short-circuit): `skip_reason="…"` — verb exits 0 with the reason logged.
  - **block** (failure short-circuit): `blocker="…", exit_code=N` — verb exits non-zero with the message logged.
- `execute(args, pre) -> ExecResult` — do the work. May mutate state. `ExecResult.exit_code` propagates to the process; `ExecResult.artifacts` is a free-form dict for `post_validate` to read.
- `post_validate(args, pre, result) -> None` — verify post-conditions, write final state, raise `RuntimeError` on failure. Default is a no-op.

**Result types** (`PreCheckResult` and `ExecResult`) are plain dataclasses with `ctx` / `artifacts` dicts; the runner does not inspect their contents. This keeps phase boundaries loose enough for ad-hoc data flow within a verb without inventing a per-verb context class.

**Sentinel handling.** Verbs whose `execute` mutates the live system set `requires_sentinel = True`. The runner wraps `execute + post_validate` in `sentinel_scope(state_dir, verb.name, recovery_cmd=…, retry_cmd=…, **metadata)` from `primitives/stage_sentinel.py`. On entry, the sentinel writes `stage_in_progress.toml`; on normal completion (both phases pass), it clears. On `RuntimeError` or `CleanExitRequested`, the sentinel is left in place so the next sysforge invocation blocks at the CLI-entry recovery prompt. `sentinel_scope` also installs an `InterruptScope`, so verbs participate in the same first-Ctrl-C-defers-to-safe-boundary behaviour as the toolchain stage. It additionally holds the `stage_in_progress.lock` liveness lock for the scope: if another run already holds it, entering the scope raises `RuntimeError` **before** `mark_started`, which the runner's existing handler converts to exit 1 without clobbering the live run's sentinel (§Pipeline layer, *Liveness guard*). The toolchain pipeline stage uses the same primitive — there is one implementation, shared.

**Read-only verbs** (`env`, `help`, `resolve`, `log`, `state list`, `state orphans` without `--prune`, `state failed` without `--clear`/`--clear-all`, `packages list`, `doctor` without `--apply`) implement `execute` (the work is printing) and return `ExecResult()`; `post_validate` defaults to no-op and `requires_sentinel = False`. They use the same dispatch path as mutating verbs — no second code path.

**Error model.**
- `RuntimeError` raised from any phase → `_log.error(msg)`, return 1. Sentinel preserved if active.
- `SystemExit` → propagate verbatim (lets tests and signal handlers see the raw exit).
- `CleanExitRequested` → caught inside `sentinel_scope`, logged, re-raised as `RuntimeError` with the verb's `retry_cmd` and `recovery_cmd` in the message; sentinel preserved.

**Per-verb phase mapping** (current shape; phases reflect where each piece of work lives, not which primitives it calls):

| Verb | pre_check | execute | post_validate | sentinel |
|------|-----------|---------|---------------|----------|
| `build` | load config + LLVM preflight + `--cleansrc` validation | `build_core.build_and_install` (dep prep → build loop → install) | (build_state written by makepkg_wrapper) | yes |
| `update` | `--install-only` conflict check + config + state load + pacman-hook sentinel consumption | assemble → sync → vercmp → summary → `build_core.build_and_install` | (build_state written inline) | yes |
| `fetch` | load config + LLVM preflight | scheduler sync per pkg | non-blocker SyncResult statuses verified | no |
| `doctor` | load config + target expansion | depends/soname/ABI scan | invoke `BuildVerb` flow when `--apply` | delegated |
| `resolve` | load config | match rules + print | null | no |
| `env` | null | collect + format + print env chain | null | no |
| `help` | null | walk the subparser chain for `[COMMAND …]`; `print_help()` | null | no |
| `setup` | read pacman.conf | check + patch IgnoreGroup | re-read confirms write | no |
| `log` | null | resolve unified/per-pkg log path; page through `$PAGER` | null | no |
| `packages {list,add,remove}` | load packages.toml + validate override fields | rewrite TOML | null | no |
| `state {list,repair,orphans}` | load state dir | inspect / repair / prune | null | `repair` only |
| `config merge` | null | scan config dir for `.sfnew`/`.pacnew`; pacdiff-style view/merge/remove loop | null | no |
| `revert-to-stock` | resolve state dir + `plan_revert` over `BuildState` (pure, no mutation) | prompt/`--force`/`--dry-run` gate, then per-target reinstall / atomic replace / derename-then-reinstall via `pacman` | (state written inline) | yes |
| `search` | null | print installed → repo → AUR sections (fixed order, empty omitted) for one term | null | no |
| `uninstall` | resolve state dir + `plan_uninstall` over `BuildState` (pure) | `pacman -Rnsu`, then demote any tracked target via `state forget` + reconcile | (state written inline) | yes |
| `run …` namespace | build `RunOptions` | delegate to `pipeline.run_pipeline` / `run_stage_standalone` | pipeline framework | (pipeline owns it) |
| `artifact list` | null | `primitives/artifacts.unified_rows()` + PATH check + optional `scan()` for `--unmanaged` | null | no |
| `artifact review` | null | interactively offer discovered candidates for adoption via `primitives/artifacts.iter_offerable()`; off-TTY lists candidates + adopt hint | null | no |
| `artifact adopt <path>` | null | `primitives/artifacts.adopt()` — copy live → managed, seed registry entry | null | no |
| `artifact edit <name>` | null | launch editor on managed copy, then `primitives/artifacts.rehash()` | null | no |
| `artifact deploy <name>\|--all` | null | `primitives/artifacts.deploy()` — per-class live write + post-deploy action; refuses on `drifted`/`conflict` without `--force`/`--adopt-live` | null | yes |
| `artifact remove <name>` | null | `primitives/artifacts.remove()` — per-class pre-remove action + live unlink; refuses on `drifted`/`conflict` without `--force`; `--purge` also drops the managed copy | null | yes |

### `revert-to-stock`

Undoes a source-built or optimized package back to its official repo version. `pre_check` resolves the state dir and calls `plan_revert(bs, targets)` (pure — no mutation) to classify each target against `BuildState`, including a reverse lookup via the shared `install_reconcile.resolve_installed_name(bs, name)` helper (iterated in sorted key order so a stock base always resolves to the same pkgname deterministically) so naming the *stock* base of a renamed build (e.g. `mesa` when the tracked entry is `mesa-sysforge`) still resolves. That helper is the single home for the reverse lookup — `uninstall` uses it too. Four actions, distinguished by `profile.is_optimized_build_mode(mode)` and — for optimized builds — `profile.rename_mode_for_build_mode(mode)`:

- **`skip`** — untracked or already `build_mode = "pacman"`; nothing to do.
- **`reinstall`** — plain `source_built`, installed under the stock name: just reinstall the repo package (`pacman -S <name>`).
- **`replace`** — optimized, `conflict` rename (mesa, `pgo`, and every optimization except kernel FDO): the `-sysforge` build declares `provides`/`conflicts` for the stock name, so reverse deps depend on the *stock* name (satisfied by the provides). A `pacman -R <renamed>` would therefore fail ("breaks dependency"). Instead `execute` runs `pacman -S <origin_pkgbase>` **alone** — pacman detects the conflict with the installed `-sysforge` build and removes-plus-installs stock atomically in one transaction, keeping reverse deps satisfied throughout. **No explicit remove.**
- **`derename`** — optimized, `coexist` rename (kernel FDO only): the renamed build genuinely coexists with stock (parallel-installed, version-suffixed /boot files), so reverting needs `pacman -R <renamed>` **then** `pacman -S <origin_pkgbase>`.

This reuses the same `is_optimized_build_mode`/`rename_mode_for_build_mode` predicates the rename/coexist machinery uses elsewhere — no second "is this optimized?" or mode test. `execute` narrates every plan (including skips), then gates on `--dry-run` (report only) and `--force` (skip the confirmation prompt; non-interactive without `--force` refuses with a hint). Actionable targets are processed in order via `pacman.remove_pkgs`/`pacman.reinstall_repo_pkgs`; a `CalledProcessError` **stops processing remaining targets** and returns exit 1 (partial batches never silently continue), with a message naming the step that actually failed: a `derename` remove-step failure reports "nothing changed" (system intact), a `derename` reinstall-step failure warns the system is left without the package (with a recovery command), and a `replace`/`reinstall` single-call failure reports the atomic reinstall failed (system intact). Each successfully-reverted target calls `cmd_state_forget` (the same `state forget` verb code, not a duplicate) so `update` stops rebuilding it, then a final `BuildState.reconcile_external_installs(install_reconcile.external_install_targets())` pass demotes anything pacman now owns as a belt-and-suspenders check — reusing the one-home demotion path rather than adding a parallel one. `requires_sentinel = True` since `execute` mutates installed packages and state.

### `search`

Read-only lifecycle verb (`requires_sentinel = False`). One term is queried against three sources in fixed order — installed (`pacman -Qs`), repo sync DBs (`pacman -Ss`), and the AUR (RPC v5 `search/<term>?by=name-desc`) — and each non-empty section is printed under a header; an empty section is omitted. Local/repo are pacman passthroughs captured with `--color always` (native rendering preserved, emptiness detectable via `pacman.search_local`/`search_repo`); the AUR section is sysforge-rendered from `aur.aur_search` (`aur/<name> <version>` + indented description, colour-matched to the pacman blocks — coloured source prefix, bold name, green version, via `log.use_color()`). Consecutive non-empty sections are separated by a blank line. AUR is the one optional source: `aur_search` swallows network/timeout/JSON errors and returns `[]`, so a search never hard-fails on the third source.

### `uninstall`

Mutating lifecycle verb (`requires_sentinel = True`). `pre_check` resolves each name through the shared `resolve_installed_name` (so naming a stock base reaches its `-sysforge` build) and builds a pure `plan_uninstall` classifying each target's installed name + whether it's tracked. `execute` narrates the plan, removes via `pacman.uninstall_pkgs` (`pacman -Rnsu`, interactive — pacman prints its own confirmation; a `CalledProcessError` returns exit 1 and demotes nothing), then for any tracked target demotes it out of the build-state authority via `cmd_state_forget` (the same `state forget` code) followed by a `BuildState.reconcile_external_installs(install_reconcile.external_install_targets())` pass. This is the exact demotion composition `revert-to-stock` uses — no parallel path — so `update` stops rebuilding an uninstalled package.

### `artifact list`

Read-only lifecycle verb (`requires_sentinel = False`). All logic lives in
`primitives/artifacts.py` (see §Primitives Layer → `artifacts.py`); the verb is a thin shell that
renders `unified_rows()` (managed registry entries joined with sysforge's own hooks via
`pacman_hooks.diff_status()`) as a `STATUS OWNER CLASS NAME` table, warns when
`script_root_on_path()` confirms `False` (never on `None` — an escalated `sudo` invocation
abstains rather than false-warn), and with `--unmanaged` additionally lists `scan()` discovery
candidates not already in the registry and not sysforge-owned.

### `artifact review`

Read-only lifecycle verb (`requires_sentinel = False`) — adoption it triggers is copy-only, never a
live-system write, so it needs no sentinel. Interactively offers discovered candidates for adoption:
`primitives/artifacts.iter_offerable(registry, ignore)` composes discovery
(`scan()`) with the existing managed/sysforge-owned exclusions and a third — declined candidates
recorded in a persistent ignore-list (`<state_dir>/artifacts-ignored.toml`, `path → content-hash`).
The ignore-list is a sibling of the registry, deliberately kept in its own file: the registry is
documented as regenerable (rebuildable from managed content), so folding declines into it would let
a registry rebuild silently forget every "no". It is keyed by both path and the content-hash seen at
decline time, and self-prunes entries whose file no longer exists on `load()`, so a candidate
re-surfaces once its content changes or the ignore entry goes stale. For each remaining candidate the
verb prompts `[a]dopt / [s]kip / [i]gnore / [q]uit` — `a` calls `artifacts.adopt()` as `artifact
adopt` does, `i` records the path+hash into the ignore-list, `s` leaves it to be re-offered next run,
`q` stops the walk early. Off a TTY it never prompts: it lists the reviewable candidates and prints a
`sysforge artifact adopt <path>` hint, exiting 0.

### `artifact adopt` / `artifact edit`

Both read-only-of-the-live-system lifecycle verbs (`requires_sentinel = False`) — they mutate only
`USER_DATA_DIR/artifacts/` and `artifacts.toml`, never a live file, so neither needs the sentinel
protection that guards actual system mutation. All logic lives in `primitives/artifacts.py` (see
§Primitives Layer → `artifacts.py`); the verbs are thin shells.

`artifact adopt <path> [--class C]` copies the file at `<path>` into the managed set via
`artifacts.adopt()` and prints the resulting name/class/dest. `--class` overrides the
root-inferred class; an unknown class, an unreadable source, an already-managed name, or an
attempt to adopt a sysforge-owned hook by name all fail with a clear message and exit 1.

`artifact edit <name>` resolves `name` against the registry (exit 1 if unmanaged), opens the
managed copy in the configured editor (`primitives/editor.py`), and on a clean editor exit calls
`artifacts.rehash()` to recompute `auth_hash` from the saved content. It then prints
`status_of()`'s result for the artifact — `ok`/`pending`/`drifted`/`conflict`/`missing`, computed
fresh from the three-way `auth_hash`/`deployed_hash`/live-file-hash comparison described in
§Primitives Layer → `artifacts.py` — with a `sysforge artifact deploy <name>` hint when the result
is `pending` (a plain edit's expected outcome, since the edit alone can't touch `deployed_hash` or
the live file). A nonzero editor exit status skips the re-hash so a botched edit session doesn't
falsely promote a corrupted save to "current."

### `artifact deploy` / `artifact remove`

Mutating lifecycle verbs (`requires_sentinel = True`) — the only two `artifact` subcommands that
touch the live filesystem, so they carry the sentinel protection and the journal mirror
(`journal_target` returns the artifact name, or `"all"` for a batched deploy — §Standards row 20)
that `list`/`adopt`/`edit` don't need. All contract logic lives in `primitives/artifacts.py` (see
§Primitives Layer → `artifacts.py` → *Per-class deploy/remove contracts*); the verbs are thin shells
over `deploy()`/`remove()`.

`artifact deploy <name>` pushes one managed artifact's authoritative content to its live
destination; `--all` deploys every registered artifact in one run, tallying failures rather than
aborting the batch on the first refusal. A `drifted`/`conflict` artifact (the live file changed
outside sysforge, or already held unrelated content) makes `deploy` **refuse** rather than silently
pick a side — resolve it with one of two mutually exclusive escape hatches: **`--force`** (the
managed copy wins, discarding the live edit) or **`--adopt-live`** (the live file wins: its content
is pulled back into the managed copy and re-hashed, then written back out — the artifact ends up
`ok` with the live file's content now authoritative). There is no default resolution — silently
choosing a side is data loss in one direction or the other. `--adopt-live` is fenced to the drift
states plus `ok`: it refuses on `pending` (it would discard an undeployed managed edit) and on
`missing` (no live file to adopt), so neither an unguarded read nor a silent overwrite of
irreplaceable managed content is possible. An already-`ok` artifact is reported as unchanged rather
than as if it were rewritten. A unit deploy runs `systemctl
daemon-reload` afterward so systemd sees the change immediately. After the run, `deploy` prints the
same PATH warning `artifact list` does, but narrower: once per run (not per artifact) and only when
a `script`-class artifact actually landed *and* `script_root_on_path()` confirms `False` — never on
the `None` abstain (an escalated `sudo` invocation, where `PATH` is `secure_path` and unrepresentative
of the user's own shell).

`artifact remove <name>` removes one managed artifact from the live system. Symmetric with `deploy`,
it **refuses** on a `drifted`/`conflict` artifact unless **`--force`**: the live file holds edits
made outside sysforge that exist nowhere else, so unlinking it would destroy them silently
(`deploy --adopt-live` first is the way to keep them). An enabled systemd unit
is disabled (`systemctl disable --now`) before its file is unlinked, so nothing is left
running-but-file-less. Removing a `pacman-hook`-class artifact prints a warning first — there is no
systemd-equivalent quiesce step for a hook, so the verb itself flags that removal changes what
happens on the next pacman transaction. **`--purge`** additionally drops the managed copy and its
registry row; without it, the managed copy survives (`deployed_hash`/`deployed_at` cleared) so the
same artifact can be `deploy`ed again later without re-adopting it — removing from the live system
and discarding the content are different decisions.

### Top-level help tiers

`sysforge --help` groups the top-level `COMMAND` list into three usage tiers — **Everyday** (`build`, `update`, `fetch`, `search`, `help`), **Inspect** (`doctor`, `resolve`, `env`, `log`, `state`, `artifact`), and **Maintain** (`setup`, `config`, `packages`, `run`, `revert-to-stock`, `uninstall`) — instead of one flat, registration-ordered block, so a new user can tell routine drivers from ad-hoc introspection. The grouping is presentation-only (no behavioural change, no config flag). argparse folds every subparser into a single `_SubParsersAction` pseudo-group with no per-command category hook, so the tiering lives in `cli._TieredHelpFormatter`, which intercepts that one action and re-emits its choices under the tier headers; every other action (options, the `COMMAND` metavar line, per-verb and sub-verb `--help`) formats via the base `HelpFormatter` untouched. The tier map `cli._COMMAND_TIERS` is the single source of truth: `cli.tiered_command_order()` flattens it, and `tools/gen_options.py` orders the man-page COMMANDS sections by that list so the page stays in lockstep with the help (both completions mirror the order too). A `check_completions`-style parity test asserts the map partitions the user-facing verbs exactly (none missing, none duplicated); the internal `completions` verb is registered without help text and stays out of both the map and the listing.

### The `help` verb

`sysforge help [COMMAND [SUBCOMMAND]]` is a read-only alias for `--help`, for users who reach for a
help *verb* before a help *flag*. `HelpVerb` (`help_cmd.py`, `requires_sentinel = False`) re-enters
`cli._build_parser()` — a function-local import, since `cli` imports `HelpVerb` at module scope —
walks the `_SubParsersAction` chain word by word, and calls `print_help()` on the parser it lands on.
It is an alias rather than a re-implementation: the output is the same parser object's help, so
`sysforge help state failed` and `sysforge state failed --help` are byte-identical. An unrecognised
topic is a usage error (exit 2) naming the offending word and listing the valid topics at that level,
not a traceback. Help text goes to stdout via `print_help()` rather than `log.ui`, so it stays
identical to the flag and never accumulates in the log files.

`-h/--help` itself is argparse-supplied at every level and always worked; what was missing was
discoverability in the hand-written completions. Both files now advertise it from a **single**
dispatch point — zsh appends it with `_describe -o` after the per-verb handler runs
(`_sysforge_help_flag`), bash with `COMPREPLY+=(…)` after its `case` — rather than repeating the flag
in all 42 `_arguments` specs and every bash flag list.

### Global profiling flags

Three top-level flags expose sysforge's own runtime performance (stdlib only, no new dependencies). All are position-independent: `_hoist_global_flags` in `cli.py` (a sibling of `_hoist_verbosity_flags`, run in the same argv-preprocessing pipeline) moves them — including `--py-profile-out`'s value token and its `=FILE` form — before the subcommand so argparse accepts them anywhere.

- **`--py-profile`** — `_dispatch` wraps `run_verb` in `cProfile.Profile()` and prints the top 25 functions by cumulative time to stderr at exit (stderr so piped stdout stays clean; the progress bottom-row is cleared first). The profiler stop/report sits in a `finally`, so verbs that `sys.exit()` from inside `execute` still emit stats. Only `run_verb` is wrapped — argv preprocessing and parser construction stay out of the profile. Known limitation: cProfile is main-thread-only, so `update`'s threaded version check shows up as join-wait, and subprocess work (makepkg/pacman/git) appears as wait time — wall-clock phase costs are `--timings`' job.
- **`--py-profile-out FILE`** — additionally `dump_stats(FILE)` for pstats/snakeviz; implies `--py-profile`. A separate flag (rather than an optional argument) so `sysforge --py-profile update` can't swallow the subcommand as a filename.
- **`--timings`** — promotes the wall-clock phase report to UI output after `build`/`update` runs. The phases are recorded unconditionally via `primitives/timing.PhaseTimer` (see §Primitives Layer → `timing.py`) and always written to the log at info level; the flag only changes where the report surfaces. `update` times source sync, version check, drift detection, and `pacman -Syu` around the engine; `build_core.build_and_install` records `dep prep`, per-package `build: <pkgbase>`, just-in-time `install deps: <pkgbase>` (when an intra-batch dep is installed ahead of a dependent), and `install` onto the caller's timer (or its own, exposed as `BuildOutcome.phase_records`).

A fourth global flag, **`--color=auto|always|never`**, is hoisted the same way (it carries a value token). It feeds the colour authority described in §Logging → Colour: `cli._resolve_color_mode` resolves `--color` flag > `[ui] color` config > `"auto"` and calls `log.set_color_mode()` once at startup. `auto` honours `NO_COLOR`/`FORCE_COLOR` and TTY detection; `always`/`never` force the decision (so colour survives a pager pipe, e.g. the coloured PKGBUILD review diff).

Two further global flags, **`--no-throttle`** and **`--turbo`**, are hoisted the same way (both valueless). They set a run-scoped build-throttle override once at startup: `cli._resolve_throttle_override(args)` maps them to `"bypass"` / `"boost"` (`--turbo` wins when both are given) and calls `build_throttle.set_run_override()`, mirroring the colour authority. `resolve_throttle` reads that process-global when no explicit override is passed, so the flags reach the deep `makepkg_invoke` throttle site without a threaded parameter (see §Flag/Profile System → Build throttling).

Three more global flags implement the **source freeze**: **`--frozen`** (valueless) and **`--no-frozen`** (valueless) override `[security] freeze_sources`, and **`--thaw PKG[,PKG...]`** (repeatable, appends) exempts named pkgbases from an active freeze. `net_policy.resolve_net_policy(args, cfg)` resolves the precedence `--no-frozen` > `--frozen` > config > `false` via the shared `config.resolve_flag_default` seam and is called once at CLI entry, installing the result with `net_policy.set_policy()` — mirroring the colour/throttle authorities, a consulted module-global rather than a threaded parameter. See §Config Layer (`[security] freeze_sources`) and §Primitives Layer → `source_sync.py` (`STATUS_FROZEN`).

**Why not unify with the pipeline `Stage` contract?** Stages already presume multi-stage DAG semantics, per-stage checkpoints, and an opinionated `PipelineState`. Most CLI verbs are single-shot and don't want a pipeline state file. The verb framework reuses `sentinel_scope` for install-bearing protection but otherwise stays independent, so `sysforge env` is not paying for pipeline machinery it doesn't need. The `run` namespace verbs are exactly the thin shim from CLI → pipeline.

### Shared build engine (`build_core.py`)

`build` is a strict subset of `update`: both route their actual building through one engine in `sysforge/build_core.py`, so the two paths cannot drift the way they once did (a `build` that left makepkg's `-s`/`--syncdeps` in place would have makepkg run `pacman -S` on an AUR-only dependency and fail, while `update` stripped those flags and pre-resolved every dep itself). `update` extends the shared core with the things that are genuinely its own — version checking, source-sync scheduling, `--install-only`, toolchain pre-flight, the bulk `pacman -Syu`, and the run summary — but the dependency prep, the per-package makepkg invocation, and the install are identical code. Multi-package `build` runs end with their own `Build complete:` totals block (`build_cmd._print_build_summary`, mirroring `update`'s built/failed/skipped/pgo-skipped lines from the `BuildOutcome`); single-package runs skip it since the per-package narration already tells the whole story.

**Repo-package opt-in gate.** `build` source-builds AUR/git/local targets unconditionally — that is their only path — but a **repo** package is normally a pacman binary, so source-building one is opt-in. Before handing a `source = "repo"` target to the engine, `build_cmd` checks whether it is already opted in (global `repo_mode = "build_from_source"` **or** per-package `enable_build_from_source = true`, resolved through `config.resolve_repo_mode` / `expand_package_groups`). If opted in, it builds silently. Otherwise, on a TTY it prompts (`build from source? [y/N]`); a `yes` builds the package **and** records `enable_build_from_source = true` in `packages.toml` (reusing the `packages_cmd` writers — no parallel mutator), a `no` skips just that target and continues the batch. On a non-TTY it skips the target with a hint (set the key, or pass `--force`). The **`--force`** flag bypasses the gate entirely: it source-builds every argument for that run only and never prompts for or modifies `packages.toml` opt-in keys. `--force` is *only* the opt-in waiver — it never reaches makepkg; **`--rebuild`** is the separate flag that forces the build itself (per-target `-f`, above). The gate lives in `build_cmd` (helpers `_load_repo_optin` / `_repo_pkg_opted_in` / `_write_repo_optin`); the source-origin stamping that classifies a target `repo` happens first, so the stamp is unaffected by the gate decision.

**Instrumentation PGO (`build --pgo=record|use`).** Instrumentation PGO is "just a build flag" — it rides the `compiler_flags_extra` seam (`emit_makepkg_conf` appends it to CFLAGS/CXXFLAGS/LDFLAGS) with no second injector and no meson `-Db_pgo` surgery — so it works on **any** package, not only mesa (F5). mesa remains the seeded/default target and the only one with bespoke graphics handling; it is also the canonical example of a *runtime-exercised library*, where an instrumented build only emits profile data when applications later call into it, so the store path is baked into the build rather than discovered at runtime. Every `mesa_pgo` function takes a `pkgbase`:

- **`--pgo=record`** injects `-fprofile-generate=<store>` (store from `mesa_pgo.resolve_store(pkgbase)`: mesa-family keeps the back-compat `makepkg_pgo.resolve_method_store(method="pgo-mesa")` location so already-collected mesa profiles are never orphaned, while any other target gets its own `resolve_method_store(method="pgo", target=pkgbase)` → `<root>/pgo/<pkgbase>`; provisioned `root:sysforge` setgid via `fs_provision.ensure_writable_dir`). The instrumented build installs over the stock package; *any* process that loads it appends `.profraw` to the store with no per-session env setup. The instrumented build is not optimized, so it keeps its stock package name (`build_mode = source_built`).
**Per-target force rebuild (`force_rebuild`).** `resolve_cleanbuild_flags` computes one `batch_flags` list for the whole batch, because cleanbuild is a batch-wide policy. Forcing makepkg (`-f`) is not: it belongs to the individual target. Both `BuildTarget` and `update._UpdateResult` therefore carry a `force_rebuild` flag, and the build loop appends `-f` to *that target's* `extra_flags` alone. It is set by `update`'s drift promotion (toolchain or flag drift → `NEEDS_REBUILD`) and by `build --rebuild`. Both rebuild at an **unchanged** `pkgver`, so the matching artifact is still in `PKGDEST` and makepkg would exit 13 without it — the batch would report success while reinstalling the very artifacts the drift detector objected to (3.0.0-B9). The flag stays per-target because `AlreadyBuilt → REUSE` is load-bearing for the resume case (a run interrupted between build and install must not rebuild what it already has). On a *forced* target `AlreadyBuilt` is unreachable, so the loop treats it as a hard build failure rather than routing it to the reuse posture — that assertion is what keeps the defect from regressing silently.

Either `--pgo` mode forces a **full clean build** (`makepkg -C -c`) regardless of `--no-cleanbuild` / `[build] cleanbuild`: a `record` or `use` pass must never reuse object files left by a *differently*-instrumented prior run (stale objects silently corrupt the profile). Policy lives in one place — `build_core.resolve_cleanbuild_flags(no_cleanbuild=, extra_flags=, pgo_mode=)` returns the `(batch_flags, strip_flags)` pair; when `pgo_mode` is set it returns `-C -c` (never stripped) and the cleanbuild opt-out is ignored (1.2.0-F24).

- **`--pgo=use`** merges the collected `.profraw` into one `<pkgbase>.profdata` (`mesa_pgo.merge_profraw` → `llvm-profdata`; a clean `MesaPgoError` abort if nothing was collected or the tool is missing). The merge folds any prior `<pkgbase>.profdata` back in as an input (cumulative signal, like the toolchain stage) and then **prunes the consumed `.profraw`** — the merged profdata is the durable store, the raw is transient, so the per-package store stays bounded instead of leaking a fresh raw every `record→use` cycle. It then injects `-fprofile-use=<profdata>` (plus `-Wno-profile-instr-out-of-date`/`-unprofiled` so a `-Werror` build tolerates expected profile skew), and earns the `-sysforge` rename. The recorded `build_mode` is `mesa_pgo.build_mode_for(pkgbase)` — `pgo_mesa` for mesa (back-compat), the generic `pgo` for everything else; both are in `_OPTIMIZED_BUILD_MODES`. The rename is applied in `makepkg_wrapper._run_build` gated on `profile.is_optimized_build_mode` (the one home for "does this build earn the suffix?") and is `conflict` mode: `patch_package_suffix` rewrites every split member's pkgname **and its `package_<name>()` function** (or makepkg aborts at packaging time) and injects `provides`/`conflicts`/`replaces` covering the stock names. Attribution is **per member**: makepkg evaluates these arrays per `package_<name>()`, where an in-body reassignment shadows any top-level (global) array, so a member that owns a literal package function (mesa's `package_mesa()` reassigns `provides`/`conflicts`/`replaces`) gets its *own* stock name injected **inside that body** — surviving the reassignment — while only members with no literal function (a single bare `package()`, or `$pkgbase`-cascaded members) fall back to the global injection. A single global covering every stock name was the B1 regression: it was dropped for members that reassign (so `mesa-sysforge` no longer replaced stock `mesa` and failed to install) and over-attributed every sibling's name to members that don't. `_validate_rename` (G3) checks the *effective* (body-overrides-global) array per member, not just the globals, so the broken attribution can no longer pass validation. `build_state` records the renamed names with `origin_pkgbase = <pkgbase>` so `update`'s source-sync still correlates back to the upstream tree.

**Profile reuse is durable across rebuilds.** A source-tracked package is rebuilt every `update` (and any plain `sysforge build <pkg>`) — and without re-applying the profile the user collected, that rebuild would silently regress to a stock, unprofiled build, contradicting the one-shot `--pgo=use`. So when a rebuild runs with **no** explicit `--pgo` mode, `makepkg_wrapper._run_build` calls `mesa_pgo.reuse_profdata(pkgbase)`: if a merged `<pkgbase>.profdata` already exists in the package's store (the durable signal that this host opted into PGO for it), it re-injects `use_flags` through the same `compiler_flags_extra` seam and re-stamps `build_mode_for(pkgbase)`, so the `-sysforge` rename persists too. No re-merge happens — once `use` swaps the instrumented build for the optimized one, no new `.profraw` accrues, so the existing profile is current; and `use_flags` already demotes the skew warnings, so a slightly-stale profile never `-Werror`-fails the rebuild. Bare `.profraw` with no merged profdata (record-only, never `use`d) is *not* reused — there is nothing consumable yet, so the build falls back to normal. Because this path bypasses `BuildVerb.pre_check`'s LLVM gate, the reuse is itself guarded on `profile.is_llvm_toolchain` (resolved compiler: explicit override > resolved profile > env `CC`) so a clang `.profdata` is never fed to a gcc build. To opt back out, remove the store's `<pkgbase>.profdata` (or `sysforge state forget <pkg>`).

The whole feature is LLVM-only — it instruments with clang and merges with `llvm-profdata` — and `BuildVerb.pre_check` blocks cleanly under `toolchain = gcc` via `profile.is_llvm_toolchain` + `LLVM_REQUIRED_HINT` before any build work. PGO is rarely worth the doubled build + manual record/use workload outside a hot, long-lived library, so `pre_check` emits a **"not recommended"** warning (one home: `config.pgo_warns_for`, reading `sysforge.toml [pgo] allow`) for any target that is neither mesa-family nor allow-listed — a warning only, the build proceeds. lib32 mesa is excluded from the flag injection (the lib32 PGO flag-scrub at conf emit strips `-fprofile-*`). See *Flag/Profile System → Flag guards* and §`primitives-layer` (`mesa_pgo.py`).

- **`build_and_install(targets, *, sync_source, …) -> BuildOutcome`** — the engine. Runs `prepare_deps`, then a per-package build loop, then `install_built`, returning the built/failed/pgo-skipped lists and the install-failed flag. Each makepkg call uses `strip_flags = BATCH_STRIP_FLAGS` (`{-s, --syncdeps, -i, --install}`) and `force_batch` when non-interactive, so makepkg never resolves deps via pacman and never installs inline — sysforge owns both. `targets` is any object exposing `pkgbase`/`pkgnames`/`pkgbuild_path`/`source` (`update._UpdateResult` qualifies directly; `build` builds a `BuildTarget` from the parsed PKGBUILD via `target_from_pkgbuild`). When the caller doesn't pass `pkgdest`, the engine resolves it from the system makepkg.conf (`pacman.get_pkgdest`) — artifacts land in `PKGDEST` when one is set, so the post-build snapshot, the `AlreadyBuilt` artifact scan, and the just-in-time install must all search there, not the PKGBUILD dir (the `build` verb relied on the caller default and silently installed nothing on PKGDEST systems; 2026-06-12 fix).
- **Intra-batch dependency ordering + just-in-time install** — before the build loop, `_order_targets_by_intra_deps` topo-sorts the batch (stdlib `graphlib`) by edges from each target's `depends` + `makedepends` + `checkdepends` matched against the other members' `pkgname`s **and `provides`** (version constraints stripped; soname provides like `libvulkan.so` participate; the parse is purely intra-batch — nothing external is queried; a dependency cycle warns and keeps the original order). The build loop then installs a freshly built member's artifacts (via `install_built`) *before* a dependent member's makepkg call, so the dependent configures against the new version instead of the stale installed one; the final bulk install skips files the just-in-time path already handled. Rationale: `prepare_deps`' AUR resolver only orders *missing* deps — a batch sibling already installed at a stale version never creates an edge there, so an alphabetical batch could build a loader against old headers whose new version sat unbuilt later in the same batch (the Vulkan 1.4.354 failure, 2026-06-12). A failed intra-batch dep only warns: the dependent still builds against the installed version and records its own failure normally.
  - **Exact-pin deadlock** — a member that pins a sibling by exact version (`depends=("egl-wayland-git=$pkgver")`, the `lib32-*` VCS pattern) leaves pacman no valid transaction for the just-in-time install: it refuses to upgrade the sibling while the *installed* dependent still pins the old version, and the only package that would satisfy the new pin is the artifact this run has not built yet. The just-in-time `install_built` therefore passes `allow_break_deps` — the `pkgname`s of every batch member not yet built, the current target included (it is the usual pin holder) — down to `pacman.batch_install_pkgs`, which dry-runs the transaction via `deps_broken_by_install` (`pacman -U --print`: unprivileged, side-effect free; **both** streams are scanned — pacman puts the `::` detail naming the dep and its holder on stdout and only the generic header on stderr, so a one-stream probe reports no breakage however badly the transaction is refused) and adds a **single** `--nodeps` only when every affected holder is in that set. One `--nodeps` skips dependency *version* checks alone, so a genuinely absent dependency still aborts; breakage held by anything outside the batch warns and keeps enforcement on. Correctness rests on the holders being rebuilt and reinstalled later in the same run, which restores consistency at the final bulk install.
- **`prepare_deps(pkgbuild_paths, config, *, building_names, …)`** — pre-installs missing repo *build deps* in one `pacman -S` transaction (`batch_install_makedeps`) and builds AUR/local deps in topo order (`resolve_aur_deps_batch` + `build_resolved_deps`), excluding the packages about to be built themselves. The repo arm collects `depends` + `makedepends` + `checkdepends` (`pacman.collect_builddeps`), **not just makedepends**: the per-package makepkg call runs with `-s` stripped, and makepkg checks runtime `depends` before building too, so a missing repo runtime dep would abort the build with exit 8 ("Could not resolve all dependencies"). It **filters the missing set to sync-repo packages first** (`aur.repo_packages`, the same classifier the AUR resolver uses) — an AUR-only dep mixed into the `pacman -S` transaction makes pacman abort with "target not found" and install *none* of the repo deps either, so AUR deps are excluded here and left to the AUR arm (which resolves `depends + makedepends`). Both arms are best-effort — a failure warns and lets the build proceed, surfacing a genuinely-missing dep as a per-package build failure with a diagnosis rather than aborting the whole batch up front.
- **`install_built(built_pkg_files, *, always_install=frozenset(), interactive=False) -> (files, install_failed)`** — dedupe, re-fetch the installed set (makedep/AUR pre-install may have expanded it), `filter_pkgs_to_installed` for split-pkgbase safety, then one `pacman -U`. The keep-set is the currently-installed pkgnames **union `always_install`** — the pkgnames the caller explicitly asked to build. `build_and_install` passes the build targets' pkgnames, so a fresh `sysforge build <new-pkg>` installs the package the user asked for instead of dropping it for not being installed yet; for `update` the union is a no-op (its targets are already installed). A conflict-mode `-sysforge` rebuild (e.g. `mesa --pgo=use`) emits artifacts renamed off every stock pkgname (`mesa-sysforge`), so `filter_pkgs_to_installed` keeps them via their `replaces` rather than their absent pkgname — otherwise the optimized build would complete and then be silently dropped at install. Reused by `update`'s `--install-only` artifact-scan branch (which keeps the default empty set). `interactive` threads down to `pacman.batch_install_pkgs(..., interactive=…)`: when set it drops `--noconfirm` and inherits pacman's TTY streams so a package-conflict question (`X and Y are in conflict. Remove Y? [y/N]`) is put to the operator instead of auto-answered `N` and aborting the transaction. `build_and_install` passes its own `interactive` from both the just-in-time and final-install call sites; `update`'s non-interactive call keeps the default (`--noconfirm`). The non-interactive path still has to install the conflict-mode rename without an operator at the prompt: pacman only auto-processes `replaces` on a sync upgrade, so on a local `-U` the renamed drop-in (`mesa-sysforge` declaring `replaces = mesa`) still raises the conflict prompt, which `--noconfirm` declines (default `N`) and aborts. `batch_install_pkgs` therefore adds `--ask=4` (`ALPM_QUESTION_CONFLICT_PKG`) **only when** a built package's `replaces` names a currently-installed package — auto-confirming exactly the intended drop-in removal; absent a real replaces-installed relationship the prompt keeps its safe default so an unexpected conflict still aborts.
- **`sync_source`** is the single deliberate caller difference: `update` passes `False` (Phase 2 already synced sources through the scheduler), `build` passes `not --no-update` to keep its inline per-package source sync (which itself routes through `source_sync.get_scheduler()` inside `makepkg_wrapper.run`). `_find_existing_artifacts` and `_record_build_failure` live here too (moved from `update.py`) since both the engine and `update`'s install-only scan use them.
- **`make_build_options(stage, options, **overrides) -> BuildOptions`** — the shared factory the three install-bearing pipeline stages (`kernel`/`toolchain`/`packages`) use instead of hand-assembling a `BuildOptions`. It maps the fields common to every stage's `RunOptions` (`no_pkg_logs` → `pkg_log`, plus `persist_log`/`state_dir`/`abi_check`, the last via `getattr` so a run-options object without it degrades to `False`), layers in the stage's constant defaults from `_STAGE_BUILD_DEFAULTS` (`kernel` → `owner_stage="kernel"` + `no_install=True`; `toolchain` → `pgo_managed=True`; `packages` → none), then applies the caller's per-call `overrides` (which win over both). Fields that differ per stage or per package — `profile_conf`, `log_dir`, `update`, `cc`/`cxx`/`ld_override`, `source`, `toolchain_variant`, `extra_flags`, … — are passed explicitly as overrides; anything a stage omits keeps its `BuildOptions` default (so `toolchain` not passing `log_dir` keeps it `None`). This is where a stage-wide default like the kernel build/install split lives, rather than being repeated at each call site.

---

