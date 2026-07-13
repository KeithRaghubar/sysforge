# Design: cache doctor axis (2.2.0-F1) + build mem_limit (2.2.0-F4)

Date: 2026-07-13
Roadmap: implements `2.2.0-F1` and `2.2.0-F4`; logs new bug `2.3.0-B2`.

## Summary

Two related-but-distinct pieces of build-environment work that share one code
seam (child-process resource policy at the makepkg launch point):

- **F1** — a new read-only `doctor` axis (`cache`) reporting ccache/sccache
  *readiness* before a build relies on them.
- **F4** — a real `[build] mem_limit` per-build memory ceiling, delivered via a
  shared child-preexec-policy helper that replaces the three direct
  `lift_for_child` `preexec_fn` sites.

They are grouped because F1's read side (`cache_probe` readiness) and F4's
write side (`resource_guard`/`build_throttle` enforcement) both attach at the
makepkg child launch and share the "best-effort, never-fails-the-build" pattern.
They are **not** merged into one primitive — the roadmap explicitly warns against
forcing `resource_guard` (constrains the controller) and `build_throttle`
(constrains children) together.

Out of scope: `2.3.0-B2` (the `--cache-report` logger-bypass) — already logged
in `ROADMAP.md` under Bugs as a separate item; **not** part of this plan.

---

## Part A — F1: `cache` doctor axis

### Purpose

Answer "is the compile cache set up correctly *before* a build needs it?" —
distinct from the existing `--cache-report` flag, which answers "how well did the
cache *perform* during the build that just ran?" (per-package hit/miss deltas on
build verbs). Same underlying probes (`cache_probe.probe_ccache`/`probe_sccache`),
two lenses: point-in-time readiness vs. time-series effectiveness. No functional
overlap; flag names don't clash (`--cache` is doctor-only; `--cache-report` lives
on build verbs).

### Components

1. **Reader — stays in `primitives/cache_probe.py`** (the one-home for cache
   knowledge; no cache subprocess logic leaks into `doctor.py`):
   - Add `check_cache_readiness() -> list[dict]`. Reuses `probe_ccache()` /
     `probe_sccache()` and adds only the missing readiness bits per tool:
     - installed? (`shutil.which`)
     - cache dir resolvable + writable?
     - a sane, non-zero configured max size?
   - Each dict describes one tool: `{"tool": "ccache"|"sccache", "installed":
     bool, "state": "absent"|"ok"|"misconfigured", "detail": str,
     "remediation": str|None}`.
   - Reading max size: ccache via `ccache --show-config` / `-k max_size`;
     sccache via `SCCACHE_CACHE_SIZE` env / `--show-stats` "Max cache size".
     Kept inside `cache_probe`, best-effort (missing → treat as unknown, not a
     hard failure). Extends the existing reader; does **not** add a parallel one.

2. **Producer — `_collect_cache_findings(config)` in `doctor.py`**: adapts the
   readiness dicts into `diag.Finding`s via the standard axis pattern:
   - neither tool installed → **INFO** ("no compile cache configured; builds
     won't benefit from caching"). Absence is optional, not a defect — matches
     the `_collect_boot_findings` "missing-but-optional = INFO" convention.
   - a tool installed but misconfigured (unwritable dir / unset/zero max size) →
     **WARN** + remediation (`ccache -M <size>` / set `SCCACHE_CACHE_SIZE`).
   - installed + sane → contributes to the axis clean message.

### One-home registration (same change — doctor-axis invariant)

- `doctor.py`: `_collect_cache_findings`, `_SYSTEM_AXIS_ORDER` (insert `"cache"`
  immediately after `"toolchain"`), `_AXIS_FLAGS`, `_system_axes` (with
  `clean_msg`).
- `cli.py`: `--cache` `store_true` flag on the doctor subparser, help text
  cross-referencing `--cache-report` for per-build hit rates.
- `completions/_sysforge` **and** `completions/sysforge.bash`: add `--cache`.
- `man/sysforge.1` via `make man` (regenerated from argparse; do not hand-edit).
- `tests/test_doctor.py`: `_patch_axes_clean` gains the `cache` axis so `--all`
  runs stay quiet.

### Placement in axis order

`toolchain, cache, hardware, graphics, gfxperf, pacman, state, boot, storage,
services, audio, network` — `cache` sits next to `toolchain` as a build-toolchain
health check. Not opt-in (runs in the default/`--all` sweep); it's cheap and
read-only.

---

## Part B — F4: `[build] mem_limit` + shared child-preexec policy

### Purpose

A per-build memory ceiling so a runaway build (OOM-prone link step, sanitizer,
LTO) can't take down the workstation. `RLIMIT_AS` is the roadmap-named mechanism
(same syscall family `lift_for_child` already owns), applied off the systemd
path; `MemoryMax` (cgroup, tighter/more accurate) is used on the systemd-run
path.

### Components

1. **Config knob** (opt-in; unset = no ceiling, no behavior change):
   - `[build] mem_limit` string, e.g. `"24G"`. Shipped
     `etc/sysforge/sysforge.toml` gets a commented example.
   - `_coerce_mem_limit(raw) -> int | None` in `build_throttle.py` — same
     coercion family as `_coerce_cpu_quota` / `_coerce_jobs`: parse size suffixes
     (K/M/G/T, binary), junk/negative → `warn` + `None`. Returns bytes.
   - `BuildThrottle` gains `mem_limit_bytes: int | None`; `resolve_throttle`
     populates it (with the same per-profile override `pick(...)` mechanism).
   - `is_noop()` must account for `mem_limit_bytes` (a throttle carrying only a
     mem cap is not a no-op).

2. **Shared preexec helper — `resource_guard.py`** (replaces the 3 direct
   `lift_for_child` `preexec_fn` sites):
   - `make_child_preexec(rlimit_as_bytes: int | None) -> Callable[[], None]`:
     returns a closure that runs `lift_for_child()` **then**, if
     `rlimit_as_bytes` is not `None`, `setrlimit(RLIMIT_AS, (cap, cap))` —
     clamped to the current hard limit, best-effort (`except (ValueError,
     OSError): pass`), never raising into the child.
   - `rlimit_as_bytes is None` → behaves exactly like today's `lift_for_child`.
   - Call sites switch from `preexec_fn=lift_for_child` to
     `preexec_fn=resource_guard.make_child_preexec(<cap>)`:
     `makepkg_invoke.py:197`, `makepkg_invoke.py:303`, `toolchain.py:1105`.

3. **Dual mechanism for the systemd-run `--scope` path**
   (`build_throttle` stays the one home for *who enforces the throttle*):
   - `wrapper_argv(throttle)`: when it emits `systemd-run --scope` (cpu_quota
     set) **and** `mem_limit_bytes` is set, inject `-p MemoryMax=<bytes>`.
   - `resolve_child_mem_cap(throttle) -> int | None`: the RLIMIT_AS bytes for the
     preexec path — returns `None` when the scope owns the cap (cpu_quota
     active), else `mem_limit_bytes`. Ensures the two mechanisms never
     double-apply and the rlimit is never silently ineffective (a preexec
     `RLIMIT_AS` set on the `systemd-run` client process does not reach the
     scoped payload, which runs as a child of PID 1).

### Threading the cap to call sites

Each makepkg-launch site already has (or can cheaply obtain) the resolved
`BuildThrottle`. The site computes `cap = resolve_child_mem_cap(throttle)` and
passes `make_child_preexec(cap)`. `toolchain.py:1105` (staged toolchain build):
if it has no throttle context, it passes the raw configured cap (best-effort);
verify whether it wraps in systemd-run and mirror the scope rule if so.

### Behavior matrix

| cpu_quota | mem_limit | preexec RLIMIT_AS | systemd-run MemoryMax |
|-----------|-----------|-------------------|-----------------------|
| unset     | unset     | none (lift only)  | n/a                   |
| unset     | 24G       | 24G               | n/a                   |
| set       | unset     | none (lift only)  | none                  |
| set       | 24G       | none (scope owns) | `-p MemoryMax=24G`    |

---

## Testing (TDD — tests written first)

- `tests/test_build_throttle.py`:
  - `_coerce_mem_limit`: valid suffixes (`24G`, `512M`, bare bytes), junk → `None`
    + warning, negative/zero → `None`.
  - `wrapper_argv`: injects `-p MemoryMax=<bytes>` iff cpu_quota **and**
    mem_limit; absent otherwise.
  - `resolve_child_mem_cap`: `None` when cpu_quota active, bytes when not,
    `None` when mem_limit unset.
  - `BuildThrottle.is_noop`: false when only mem_limit set.
- `tests/test_resource_guard.py`:
  - `make_child_preexec(None)` → lift-only (no setrlimit on AS beyond restore).
  - `make_child_preexec(bytes)` → composes lift + `setrlimit(RLIMIT_AS, ...)`
    (assert via a monkeypatched `resource.setrlimit` recorder).
- `tests/test_doctor.py`:
  - `cache` axis: clean (installed + sane), INFO (absent), WARN (misconfigured).
  - `_patch_axes_clean` includes `cache`.

No gcc-vs-llvm branch in either part → dual-toolchain-parity rule does not apply.

---

## Docs & bookkeeping (in the mandated order)

1. `docs/design/*.md`: doctor axis list (add `cache`); Flag/Profile throttle
   knobs (add `mem_limit` + the dual-mechanism note). Run `make design`.
2. `README.md`: config reference for `[build] mem_limit` and the `--cache` axis.
3. `sysforge/CLAUDE.md`: doctor-axes one-home list (add `cache`); build-throttle
   invariant note (mem_limit + preexec helper).
4. Shipped `etc/sysforge/sysforge.toml`: commented `[build] mem_limit` example.
   `make check-shipped` must pass (fixtures updated in lockstep).
5. `ROADMAP.md`: remove `2.2.0-F1` and `2.2.0-F4` (git history is the record).
   (`2.3.0-B2` is already logged under Bugs as a separate item — not touched by
   this plan.)
6. `docs/release-notes/unreleased.md`: append F1 and F4 entries (Keep a
   Changelog, inline roadmap ID, ascending ID order).

Guards to pass: `make test`, `make lint`, `make check-shipped`, `make
check-design`, `make check-standards`.
