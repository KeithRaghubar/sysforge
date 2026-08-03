# Post-build change summary for pipeline stages

Date: 2026-08-02
Roadmap: `2.6.1-F24` (core), `2.6.1-F25` (kernel extras), `2.6.1-F26` (toolchain extras),
`2.6.1-F27` (install stage)

## Problem

`sysforge update` reports what its builds changed: `_print_result_summary`
(`sysforge/update_summary.py:153`, called from `sysforge/update.py:1177`) runs after Phase 5/6
build+install and renders `Built:` / `Pacman-Syu:` / `Failed:` sections with `pkgbase: old → new`
pairs via `_fmt_pkg`.

The pipeline stages have no equivalent. `PackagesStage` ends with a bare
`Total / Built / Failed / Skipped` count line (`sysforge/pipeline/stages/packages.py:502`); the
kernel stage's `_log_resolution_summary` (`sysforge/pipeline/stages/kernel.py:1608`) is a
*pre-build* block, deliberately so — its docstring notes it runs before the long build so
`--dry-run` has a readable summary. After a `sysforge run`, nothing states which package versions
actually changed.

The structural cause: `packages.py:_build_aur` calls `makepkg_run(pkgbuild, ...)` directly rather
than going through `build_core.build_and_install`, so the stages never construct a `BuildOutcome`
and hold no version pairs. The kernel and toolchain stages build the same direct way.

Beyond version pairs, the kernel stage has two build-shape questions it can answer and currently
does not: how much disk the built package costs (the `_resolve_subpackages` headers/docs toggles
and `_resolve_keep_hotplug_drivers` change this materially and invisibly), and which kconfig
symbols differ from the last kernel built.

## Scope

In scope: `packages`, `kernel`, `toolchain`, and `install` stages.

Out of scope:

- `sysforge update` — already has this surface; untouched by this work.
- Removal verbs (`uninstall`, `revert`) — they already report what they touched and have no
  build artifacts to size-diff.
- Any post-build *benchmark*. Considered and rejected: `scripts/kernel-bench.sh` clears
  ccache/sccache, drops VM caches and wipes `~/builds` precisely because an uncontrolled timing
  figure is noise, and a noisy number printed as a summary row would read as signal.

## Architecture

### Opt-in via stage class attributes

`Stage` (`sysforge/pipeline/stages/base.py:94`) gains two attributes beside the existing
`makepkg_bearing`, and for the same stated reason — the property belongs to the stage, not the
pipeline verb:

```python
reports_changes: bool = False        # runner snapshots + renders for this stage
change_root: str | None = None       # None = live root; install resolves a target
```

`packages`, `kernel`, and `toolchain` set `reports_changes = True`. `install` sets it plus a
`change_root` resolved at runtime.

### Runner hook

`pipeline/runner.py` wraps its single `stage.run()` call site (`runner.py:116` in
`run_stage_standalone`, and the equivalent inside `run_pipeline`):

```
before = snapshot(root)  →  stage.run(...)  →  after = snapshot(root)  →  classify  →  render
```

Centralising here means the logic is written once, applies to all four stages, and standalone
invocations (`sysforge run kernel`) get it free, matching how the unified log, `progress.phase()`
and `state.save()` are already handled.

### Why snapshot diffing

A snapshot is `dict[str, PkgFacts]` — `{name: (version, installed_size)}` — from one pyalpm pass
over the local DB, falling back to `pacman -Q` + `-Qi`. Diffing two snapshots is authoritative in
a way stage-side bookkeeping cannot be: it captures the target package, split members, and any
dependency pulled in during the build, with no per-stage instrumentation. Installed size comes
from the same pass (`pkg.isize`), so the size column costs nothing extra.

### Stage-supplied extras

Stages contribute their own blocks through one overridable hook:

```python
def change_extras(self, config, state, options) -> list[ExtraBlock]: return []
```

The runner calls it after `stage.run()` and appends the returned blocks below the version rows.
The runner stays generic — it never knows what a kconfig is.

## `primitives/change_report.py`

New module. Imports only `render`, `log`, and `pacman`; never `pipeline/`, preserving the
downward-only layering rule.

### Data model

```python
@dataclass(frozen=True)
class PkgFacts:    version: str;  isize: int | None

@dataclass(frozen=True)
class ChangeRow:   name: str;  old: PkgFacts | None;  new: PkgFacts | None
                   # old None = added; new None = removed; both set = changed

@dataclass(frozen=True)
class ExtraBlock:  label: str;  lines: list[str]   # stage-supplied, pre-formatted
```

### Public API

```python
snapshot(root: Path | None = None) -> dict[str, PkgFacts]
diff(before, after) -> list[ChangeRow]                    # sorted: changed, added, removed
classify(rows, *, stage_failed: bool, unavailable: str | None) -> ChangeOutcome
render(rows, *, stage, outcome, extras=(), emit=print) -> None
```

`render` takes `emit` exactly as `_print_result_summary` does: it defaults to `print` so tests
capture stdout, and the runner passes `log.ui` to mirror into the unified log.

### Relationship to `update`'s renderer

The two renderers stay separate. `ResultSummary` carries update-only fields
(`pgo_skipped_pkgs`, `cleansrc_failures`, `stage_owned_updates`, `install_only`); merging would
produce one dataclass where half the fields are inapplicable to any caller, and would invert the
layering by making `pipeline/` depend on an update-layer module. The *visual grammar* is shared
instead, by reusing the same helpers: `render.version_pair(old, new, equal_marker=False)` for
version pairs, and the same two-space label / four-space body indentation that
`_print_result_summary._section` uses.

`cache_probe._fmt_bytes` (`sysforge/primitives/cache_probe.py:58`) is promoted to
`render.fmt_bytes()` and `cache_probe` imports it, so size formatting is consistent between the
cache report and this summary.

### Output shape

```
[SYSFORGE] Kernel stage changes: 1 updated, 2 added.
  Updated:
    linux-custom          6.15.4-1 → 6.15.5-1   142.3 MiB → 181.7 MiB  (+39.4 MiB)
  Added:
    linux-custom-headers  — → 6.15.5-1          98.1 MiB  (+98.1 MiB)
  Kconfig changes since 6.15.4-1:
    CONFIG_HOTPLUG_PCI    n → y
```

The size column is omitted entirely when no row carries size data, rather than printing a column
of dashes. Removals render `ver → —` via `render.version_pair`'s existing `_MISSING` em-dash.

## Outcome classification

`ChangeOutcome` is derived from two independent facts — did the stage raise, and did the diff show
changes:

| Outcome | Stage | Changes | Header | Means |
|---|---|---|---|---|
| `COMPLETE` | ok | ≥1 | `Kernel stage changes: 1 updated, 2 added.` | Ran, applied, done. |
| `NO_CHANGES` | ok | 0 | `Kernel stage: no package changes (nothing to apply).` | Success and a genuine no-op. |
| `PARTIAL` | raised | ≥1 | `Packages stage FAILED after applying changes: 30 updated, 10 not built.` | System is in a mixed state. |
| `NONE_APPLIED` | raised | 0 | `Kernel stage FAILED before applying changes — system unchanged.` | Failed clean; safe to retry. |
| `UNKNOWN` | either | undeterminable | `Kernel stage: change summary unavailable (<reason>).` | Snapshot or diff failed. |

Three properties this must hold:

- **`PARTIAL` is the common failure mode, not an exotic one.** The packages stage catches
  `RuntimeError` per package and continues (`packages.py:481`), raising only at the end — so "30 of
  40 installed" is routine there. Where the stage knows specifics, the block lists what did not
  land beside what did.
- **`UNKNOWN` is never rendered as `NO_CHANGES`.** A failed `pacman -Q` or an unresolvable install
  root states its reason. Silence and "nothing changed" must not be confusable.
- **Classification is reporting-only.** It never influences exit codes or the runner's
  success/failure determination; `stage.run()` raising remains the sole authority. A
  misclassification can mislead but can never break a build.

The kernel stage refines the `NONE_APPLIED` reason string using its gate structure: `_gate2_audit`
(`kernel.py:1453`) runs outside the install sentinel specifically so a brick abort leaves the
system untouched, which is distinguishable from a failure after install began.

## Kernel extras (`2.6.1-F25`)

`KernelStage.change_extras()` returns up to three blocks. All advisory: never raise, never block
install.

**Size** — free from the snapshot; no kernel-specific code. Makes the cost of the
headers/docs subpackage toggles and `keep_hotplug_drivers` visible on the run that flips them.

**Kconfig block A — since previous build.** Each successful kernel build archives its resolved
`.config` to `<state_dir>/kconfig-history/<pkgname>-<kernel_release>.config.gz`, sourced from the
existing `_resolve_built_config(pkgbuild_dir)` (`kernel.py:1413`) with the release tag from
`_built_kernel_release()` (`kernel.py:1444`, which reads kbuild's `kernel.release`). Gzipped a
`.config` is roughly 40–60 KiB; **retention keeps the newest 5 per pkgname**, pruned on write, so
state growth is bounded at a few hundred KiB.

The diff is a new `kernel_safety.diff_kconfig(old, new)` — a plain symbol-set diff (`n → y`,
`y → m`, added, removed) — sibling to `diff_requested_kconfig` (`kernel_safety.py:141`) rather than
a reuse of it, since that function is specific to the requested-vs-resolved axis. Both sides parse
through the existing `parse_kconfig()` (`kernel_safety.py:109`).

Output is capped at the first 40 changed symbols followed by `… and N more`, with the full list
always written to the unified log. A kernel major bump can change thousands of symbols and must not
bury the version rows.

**Kconfig block B — requested vs resolved.** This is the existing `_gate2_kconfig_drift`
(`kernel.py:1531`) result, relocated. Today it emits scattered `_log.warn` lines mid-build; the
change has it *return* its drift list so the block can render it. The mid-run warnings stay — a
drift warning is still worth seeing when detected; the summary is an additional surface, not a
replacement.

Block B composes with roadmap item `2.6.1-F23`, which moves the requested-vs-resolved check earlier
(into `prepare()`, at merge time, where `merge_config.sh` already models it) so voided symbols are
caught reliably. If F23 lands later, block B renders a better-sourced drift list with no rework
here. Neither entry supersedes the other.

**Both blocks degrade honestly.** On the AlreadyBuilt path there is no build tree, so
`_resolve_built_config` returns `None` and both blocks are omitted using the existing explicit
"did NOT run" wording (`kernel.py:1550`, from 2.6.1-B6) rather than silently rendering nothing.
With no baseline yet, block A prints `no previous build archived — baseline recorded for next run`.

## Toolchain extras (`2.6.1-F26`)

`ToolchainStage.change_extras()` returns one block, all of it reads of state the stage already
computes:

- **Compiler identity** — `cc`/`cxx`/`ld` version lines via the existing
  `build_fingerprint.compiler_version_line()` (`sysforge/primitives/build_fingerprint.py:106`),
  probed before and after the stage, rendered as `old → new`.
- **Variant and fingerprint** — the `toolchain_variant` already threaded through `_build_pkg`
  (`sysforge/pipeline/stages/toolchain.py:807`) and stamped into `build_state`, plus the Pass-4
  fingerprint.
- **Flags** — `flag_drift.diff_flags(stored, current)` against what `build_state` recorded,
  rendered as `+added` / `-removed` tokens.

## Install stage (`2.6.1-F27`) — build last, defer if hostile

The install stage pacstraps into a target root via `archinstall --silent`, so it has no
before-state: every row is an addition (`— → ver`), effectively a manifest.

Two known obstacles:

1. `pacman.get_all_installed_packages()` (`sysforge/primitives/pacman.py:735`) queries the live
   root with no `root=` parameter. It gains an optional one (pyalpm with the target root, or
   `pacman -Qb <root>/var/lib/pacman`).
2. **No target-root path is modelled anywhere in sysforge.** `archinstall_config.py` describes only
   per-partition `mountpoint` values (`/`, `/boot`); neither `install.py` nor `archinstall_invoke.py`
   records the mount root. Resolution order is an explicit `--target-root`/config value if present,
   otherwise probing `findmnt` at stage end.

The real risk is timing, not path resolution: the after-snapshot must run while the target is
still mounted, and the current code does not establish whether archinstall leaves it mounted when
`install.py` returns. If the root cannot be located or read, the stage emits
`install summary unavailable — target root not resolvable` — the `UNKNOWN` outcome, never a silent
no-op and never a fabricated count.

This item is built **last**, as its own task. If the mount lifetime proves hostile, the other three
stages ship regardless and this defers to a follow-up roadmap item.

## Error handling and edge cases

- **The summary can never fail a stage.** Snapshot, diff, and render are wrapped so any exception
  degrades to a one-line warning; the render call sits after `stage.run()` returns and inside its
  own try/except, outside the stage's success determination.
- **Failed stages still report.** The after-snapshot is taken in the runner's `finally` block, so a
  stage that built 30 of 40 packages before dying still reports what landed (`PARTIAL`).
- **Dry-run prints nothing.** `runner.py` already branches on `options.dry_run` and never calls
  `stage.run()`. No speculative "would change" rendering is added — the existing pre-build
  resolution summaries serve that purpose, and a second predictive surface would duplicate them.
- **Empty diff is stated, not skipped** (`NO_CHANGES`), because silence is ambiguous.
- **Ordering.** The summary emits after `state.save()` but before the stage's existing
  `Stage complete.` line, so the counts line stays last on screen. The packages stage keeps its
  `Total/Built/Failed/Skipped` line: build *outcomes* are a different question from installed-version
  *changes*, and a package can be "built" while a dep it pulled in is the interesting row.
- **Permissions.** `makepkg_bearing` stages already fail fast at euid 0, so the snapshot never runs
  as root there. Kconfig-archive writes degrade on `PermissionError` to a warning, as `state.save()`
  already does (`runner.py:118`).

## Testing

All via `make test`. `snapshot()` is the single monkeypatched seam — no real `pacman` invocation
anywhere. No new fixtures beyond dict literals and `tmp_path`.

- **`tests/test_change_report.py` (new)** — the bulk. `diff()` is pure over two dicts and
  `render()` is pure over rows + `emit`, so these are table tests through captured stdout, mirroring
  how `test_update_summary.py` tests `_print_result_summary`: changed/added/removed rows,
  version-equal-but-size-changed (a `pkgrel` bump that shrinks the package), missing size data
  suppressing the column, the 40-symbol cap and its `… and N more` tail, one case per
  `ChangeOutcome`.
- **`tests/test_pipeline_runner.py`** — wiring. `reports_changes = False` takes no snapshots at all
  (assert `pacman` is never called); `True` snapshots both sides; a raising stage still renders and
  classifies `PARTIAL`/`NONE_APPLIED`; a snapshot that itself throws yields `UNKNOWN` **and leaves
  the stage's own success unaffected** — the explicit guardrail for "reporting can never fail a
  build".
- **`tests/test_stage_kernel.py`** — `change_extras()` against a fabricated build tree: both kconfig
  blocks, the no-baseline first-run message, the AlreadyBuilt path where `_resolve_built_config`
  returns `None` (asserting the explicit "did NOT run" wording, not silence), and archive retention
  pruning to 5.
- **`tests/test_kernel_safety.py`** — `diff_kconfig()` as a pure function: `n → y`, `y → m`, added,
  removed, identical-configs-yield-empty.
- **`tests/test_stage_toolchain.py`** — the identity/flags block with **both a gcc-path and an
  llvm-path case**, per the dual-toolchain parity convention: the resolved compiler determines what
  `compiler_version_line` reports, which is exactly the branch that convention exists for.
- **`tests/test_render.py`** — `fmt_bytes()` after promotion, plus a regression assertion that
  `cache_probe` still formats identically.

## Documentation

Per the doc-update order, implementation updates `docs/design/*.md` (+ `make design`) for the new
`change_report` primitive, the `Stage` attributes, and the kernel/toolchain extras, then README.md
if user-facing, then CLAUDE.md. Each roadmap item is removed from ROADMAP.md in the commit that
implements it, with its release-note entry appended to `docs/release-notes/unreleased.md`.
