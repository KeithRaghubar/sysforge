# Stage-sentinel liveness guard (2.6.1-F11)

## Problem

`primitives/stage_sentinel.py` detects an interrupted install-bearing stage by the
*presence* of `<state_dir>/stage_in_progress.toml`. Presence alone cannot distinguish
two situations:

1. A previous run died mid-mutation. The sentinel is stale and should be cleared.
2. A run is alive right now, holding the sentinel legitimately.

`get_active` names this ambiguity in its own docstring ("either it is still running
(rare; sysforge does not parallelise stages) or it was interrupted"), and
`check_and_recover_stale_sentinel` resolves it by asking the operator:

    Clear the sentinel and proceed? [y/N]

In case 2 that question is unanswerable from the information shown, and answering `y`
does real damage:

- `sentinel.clear()` unlinks the live run's sentinel.
- The second run's `mark_started` then overwrites the file wholesale (`_write` replaces
  the whole document), so the first run's stage identity is lost.
- Whichever run finishes first calls `clear()` and deletes the other's sentinel.
  `clear()` uses `suppress(FileNotFoundError)`, so the clobber is silent.

The user is then two concurrent runs deep into install-bearing work with no interruption
record for either — the precise state the sentinel exists to make impossible.

## Goal

Make the "is the owner alive?" question answerable by the code, so that a sentinel with a
live owner cannot be cleared and a second install-bearing run cannot start. A genuinely
stale sentinel keeps today's behaviour unchanged.

## Non-goals

- Serialising sysforge runs in general. Read-only verbs are unaffected;
  `_gate_sentinel_check` (`cli.py:1397`) already scopes the check to
  `build`/`update`/`run`/`setup` and exempts `--dry-run`.
- Replacing `build_lock`'s existing role as a build-area mutual-exclusion guard.
- Any override flag. See "Decisions".

## Design

### Mechanism

`sentinel_scope` acquires an exclusive, non-blocking `flock` on
`<state_dir>/stage_in_progress.lock` and holds it for the lifetime of the stage.

Liveness reduces to "is the lock takeable?":

- **Takeable** — no live owner. The sentinel, if present, is genuinely stale.
- **Not takeable** — an owner is alive.

`flock` is released by the kernel when the owning file description closes, including on
`SIGKILL`, segfault, and power loss. It therefore cannot report a dead owner as alive.
This property is why a lock is used rather than a PID recorded in the sentinel: PIDs are
recycled, so a stored PID can report a dead owner as alive indefinitely — unrecoverable
given the no-override decision below.

### Reuse of `build_lock`

`primitives/build_lock.py` is the existing flock primitive and its module docstring
requires that callers "share this one primitive — don't roll a second flock path". It is
reused, with two contained changes:

1. **Noun generalisation.** The contention message hardcodes *build*
   (`build_lock.py:50`: `f"Another sysforge {label} build is running"`). The noun becomes
   caller-supplied so the sentinel can report an install stage without the word "build".
   The two existing callers (`pipeline/stages/kernel.py:2003`,
   `pipeline/stages/toolchain.py:2159`) pass the build noun explicitly and keep their
   current wording verbatim.
2. **`O_CLOEXEC`** on the `os.open` at `build_lock.py:36`. This is hardening, not a bug
   fix: every subprocess path in the tree already prevents fd inheritance
   (`subprocess` defaults to `close_fds=True`; `pty_runner.py:143` sets it explicitly;
   there are no raw `os.exec`/`forkpty` callers). It guards against a future caller
   passing `close_fds=False`, which would let a surviving grandchild hold the lock after
   the owner exits and produce a false "alive".

The `build_lock` docstring paragraph beginning "This is *not* the stage sentinel" is
amended: it remains accurate about *purpose* (transient mutual exclusion vs. durable
interruption record) but must no longer imply the sentinel does not use this primitive.

### Layer 1 — CLI entry

`check_and_recover_stale_sentinel` probes the lock before prompting. The probe attempts
acquisition and releases immediately on success.

- **Lock held** — return `False` without prompting. `False` is the existing
  "caller should refuse to proceed" contract, already wired at `cli.py:1506`. The error
  names the holder PID and the recorded stage, and states that the run must finish or be
  killed.
- **Lock takeable** — today's behaviour, unchanged: the stale-sentinel warning and the
  existing recovery or clear prompt.

The clear prompt is therefore unreachable while an owner is alive.

### Layer 2 — Scope acquisition

`sentinel_scope` acquires the lock for real, alongside `mark_started`. This covers what
layer 1 structurally cannot:

- the race between layer 1's probe and the subsequent acquisition;
- any mutating verb reached by a path outside `_gate_sentinel_check`'s command allowlist.

Contention raises `RuntimeError`, which `verbs/runner.py:110-115` already catches, logs,
and converts to exit 1 — the comment there ("a RuntimeError from entering the
log/sentinel scope itself lands here unlogged") already anticipates this path.

### Failure posture

The sentinel is deliberately lenient today: a write failure warns and continues
(`stage_sentinel.py:328-332`), trading detection for not blocking the user on a broken
state dir. The lock splits that leniency:

| Condition | Posture |
|---|---|
| `BlockingIOError` (contention) | **Strict.** Hard refusal, no prompt, no override. |
| `OSError` / `PermissionError` (unwritable or missing state dir) | **Lenient.** Warn that concurrency detection is unavailable for this run; proceed. |

Collapsing these would let a read-only state dir block every mutating verb with no
override, turning the guard into a foot-gun on the systems least able to recover.

## Decisions

- **No override.** A live owner is an unambiguous situation, so a prompt would only invite
  the mistake it exists to prevent. Escaping requires killing the owning process, which is
  honest about what is being done. No `--force`.
- **Both layers.** Entry-level refusal and scope-level acquisition protect different
  failure modes (a second run starting at all vs. a race or an uncovered code path) from
  one lock, with no second source of truth.
- **Per-state-dir scoping is automatic.** The lock lives in the state dir, so a run under
  an isolated `SYSFORGE_STATE_DIR` (test fixtures, VM) never contends with a live run.

## Testing

Contention must be exercised with a real second process: `flock` is per open file
description, so two acquisitions within one process do not model the cross-process case.
Tests spawn a child that holds the lock, then assert:

1. Probe under contention — `check_and_recover_stale_sentinel` returns `False` **and** the
   prompt function is never called (asserting non-invocation is the strongest available
   form of "unreachable").
2. Owner killed with `SIGKILL` — the lock is takeable and the existing stale-sentinel
   prompt runs unchanged. Guards against false positives.
3. `sentinel_scope` under contention — raises `RuntimeError`; through `run_verb`, exit 1.
4. Unwritable state dir — warns and proceeds; does not refuse.
5. Existing `build_lock` callers (kernel, PGO) keep their current contention wording.

No logic here branches on the resolved compiler, so the dual-toolchain test-parity
convention imposes no gcc/llvm pair.

## Affected files

- `sysforge/primitives/build_lock.py` — noun parameter, `O_CLOEXEC`, docstring amendment
- `sysforge/primitives/stage_sentinel.py` — probe in `check_and_recover_stale_sentinel`,
  acquisition in `sentinel_scope`, module docstring (lock file in the schema section)
- `sysforge/pipeline/stages/kernel.py`, `sysforge/pipeline/stages/toolchain.py` — pass the
  build noun
- `tests/` — cases above
- `docs/design/` — sentinel/one-home sections; then `make design`
- `sysforge/CLAUDE.md` — the "Install sentinel" one-home line gains the lock
- `docs/release-notes/unreleased.md` — entry under Added, inline `2.6.1-F11`
- `ROADMAP.md` — entry removed in the implementing commit

*Priority: med · Effort: small · Bump: minor*
