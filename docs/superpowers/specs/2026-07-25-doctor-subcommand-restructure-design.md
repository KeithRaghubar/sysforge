# doctor: subcommand restructure (`2.6.1-F1`)

**Status:** design in progress — paused before implementation.
**Target cycle:** 3.0.0 (breaking CLI change; rides the already-accumulated 3.0.0 break).
**ID:** `2.6.1-F1`, allocated via `make next-id TYPE=F` on 2026-07-25. If `pyproject.toml` is
bumped to 3.0.0 before this lands, re-run `make next-id TYPE=F` — the cycle prefix resets on a
major bump and this entry must be renumbered in ROADMAP, the release-note entry, and here.

## Problem

`sysforge doctor` exposes 21 flags in one flat argparse block. The taxonomy exists in code but is
invisible to the user:

- `doctor.py:853` `_SYSTEM_AXIS_ORDER` — the 15 registered axes.
- `doctor.py:860` `_OPT_IN_AXES` — `{gfxperf, integrity, rust}`, subtracted from the bare sweep.
- `doctor.py:864` `_AXIS_FLAGS` — flag attribute → axis name.

Nothing in `--help` distinguishes "runs by default", "opt-in", "selects packages", or "changes
output", so the user cannot tell what a bare invocation covers.

### The `doctor --rust PKG` complaint

`--rust PKG` is not a no-op — it is a *silent* op, in two ways:

1. `rust_probe.collect_pin_findings` (`primitives/rust_probe.py:184`) resolves each package to a
   PKGBUILD dir via `config.find_pkgbuild`. A bare `except: continue` (lines 189–190) swallows
   every unresolvable target — any repo package, or anything without a tree under
   `pkgbuild_src_dir`, disappears with no message.
2. No `rust-toolchain.toml` in the dir → `pin is None` → `continue` (line 201). Also silent.

Either way the output is identical to bare `doctor --rust`, so the argument reads as ignored.

### The structural overload

`PKG` means three different things depending on context:

- a target for the depends + ABI linkage walk (`_check_one`, `doctor.py:1113`);
- a scope qualifier for the `rust` and `integrity` axes;
- and `--graphics` doesn't scope at all — it *injects* `_expand_graphics_targets()`
  (`doctor.py:186`) into the roots, i.e. it is a target selector wearing an axis flag's clothes.

Consequently `doctor --rust foo` also silently runs a full linkage walk of `foo`.

## Decisions made

### 1. Two subcommands (`system` / `pkg`)

```
sysforge doctor                        # = doctor system: 12 default axes
sysforge doctor system [AXIS…]         # --toolchain --cache --hardware --graphics
                                       # --pacman --state --boot --restart --storage
                                       # --services --audio --network
                                       # opt-in: --gfxperf --integrity --rust
sysforge doctor pkg [TARGETS] [AXIS…]  # axes: --abi --rust --integrity
                                       # targets: PKG… | --all | --repo | --graphics
                                       # modifiers: --shallow --quiet --suggest
                                       #            --apply --no-confirm --dry-run
```

Bare `doctor` is unchanged: the fast full system sweep, still the everyday entry point.

Two rules, applied identically at both scopes:

1. **No axis flag → that scope's default axes.** `doctor system` → the 12 non-opt-in axes;
   `doctor pkg mesa` → `abi + rust + integrity` scoped to mesa. Opt-in axes are included at
   package scope because their cost is a whole-system scan, not a per-package one — scoped
   `pacman -Qkk mesa` is already what the current `--integrity` help text recommends.
2. **A broad target selector suppresses the opt-ins.** `doctor pkg --all` / `--repo` runs `--abi`
   only. Name `--integrity` explicitly to override. Reuses the existing `_OPT_IN_AXES` frozenset
   rather than introducing a second policy.

Result: `doctor pkg cosmic-comp-git --rust` means exactly one thing — rust axis, that package, no
linkage walk.

### 2. The ABI walk becomes an explicit axis (`--abi`)

Today the package walk is the only implicit check and the only one that is not a registered axis:
the 15 axes are `Finding` producers rendered through `diag.render_axis`, while the walk is a
hand-rolled loop with its own printer, its own summary line, and its own exit-code contribution
(`affected_pkgs or axis_error_count`, `doctor.py:1235`).

`--abi` covers **depends-satisfaction + soname/ABI linkage together**, not two flags: `_check_one`
returns `dep_issues, abi_issues` from one traversal, sharing `_walk_closure` and the parsed
`ldconfig` set, and `_print_report` renders them jointly. Splitting them would mean splitting the
producer, which buys nothing.

### 3. `--graphics` splits by subcommand, keeping its name

The flag currently does two unrelated jobs. The subcommand boundary separates them with no new
name:

- `doctor system --graphics` → system-state probes only (nvidia-drm modeset, driver version skew,
  Wayland explicit-sync, Steam GPU accel, session type).
- `doctor pkg --graphics` → target set = installed graphics-stack packages; a peer of
  `--all` / `--repo`.
- `doctor system --graphics && doctor pkg --graphics` → today's combined behaviour.

### 4. Migration: hard cut with a hint

The flat flags are removed in 3.0.0. Before argparse errors, a translation table intercepts each
known old flag and prints its replacement (e.g. ``--boot is now `sysforge doctor system --boot```).
The table is deleted in 3.1.0. Rejected: silent hard cut (poor UX for one small table), and
working deprecation aliases (two live parse paths, every future axis wired twice).

## Open questions

- **`doctor --all` successor.** Today `--all` = every system axis *and* every installed package.
  Under the split it becomes `doctor system` then `doctor pkg --all`. Preference stated for two
  honest commands over a third `doctor all` subcommand, but this was not confirmed — it is the one
  capability that costs an extra invocation.
- **Silent-skip fix for the rust axis.** Decided in principle (the original complaint), not yet
  specified: `collect_pin_findings` must emit an explicit INFO for a resolvable package with no
  pin, and a distinguishable INFO/WARN for an unresolvable target, instead of `except: continue`.
  Exact severities and `check_id`s undecided.
- **Not yet designed:** the axis-registry refactor needed to make the package walk a real axis
  (how a package-scoped producer receives its target list, and how `--suggest`/`--apply` — which
  consume walk internals, not `Finding`s — survive that move).
- **Not yet designed:** the docs/completions/test surface. Known obligations: `completions/_sysforge`
  + `completions/sysforge.bash` in lockstep, `man/sysforge.1.scd`, `docs/design/verbs/doctor.md`
  (+ `make design`), `_KNOWN_SECTIONS` review in `tools/check_shipped.py`, and per
  `sysforge/CLAUDE.md` each axis's `clean_msg`.

## Next step

Resume at "Section 3 — axis-registry refactor" of the brainstorm, then the migration hint and the
docs/completions/test surface, before writing an implementation plan.
