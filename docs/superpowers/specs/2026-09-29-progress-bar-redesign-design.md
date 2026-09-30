<!--
SPDX-FileCopyrightText: 2026 Keith Raghubar

SPDX-License-Identifier: MIT
-->

# Progress bar redesign — one state model, one painter, one owner per concern

**Roadmap ID:** to be filed as the next `F` (`make next-id TYPE=F`; `3.3.0-F2` at time of writing)
**Date:** 2026-09-29
**Status:** design approved, not implemented

## Motivation

`ui/progress.py` has accreted a fix per incident — `3.2.0-B11` (trace), `3.2.0-B13` (heartbeat
repaint), `3.2.0-F14` (elapsed/ETA), `3.2.0-B17` (terminal-query filter), `3.2.0-F1b` (hooks
protocol). Each was correct locally; together they left the module without a single owner for
three things:

- **What the line says.** Six globals plus a tracker closure each hold part of it:
  `_last_status`, `_last_base`, `_phase`, `_time_suffix_fn`, `_reserved`, and `_Tick.i/label`.
  Each mutator updates a different subset, which produces real disagreements:
  - tracker exit under a phase sets `_last_status` but leaves `_last_base` at the tracker's last
    tick (`progress.py:535`), so a later `heartbeat()` repaints a dead tick line;
  - `suspend_for_prompt()` clears `_last_status` but not `_last_base`, so any repaint keyed on
    the latter resurrects a bar the prompt hid;
  - nested trackers stack `_time_suffix_fn` but not the counter/base, and an inner exit without
    a phase releases the outer's region.
- **How the terminal is handed away.** `clear()`, `suspend_for_prompt()` and `suspended()` all
  mean "give the terminal back"; `build_core.py:285,907` call `clear()` before prompts that
  already `suspend_for_prompt()` inside `prompt.py`, using the variant that function's docstring
  calls unreliable.
- **When the bar repaints.** Painting is purely event-driven. The only mid-item repaint is the
  `makepkg` 30 s idle heartbeat, so the clock visibly moves every 30 s during builds and not at
  all between items elsewhere. Every liveness feature so far added its own paint path.

Also: `_paint` issues four separate writes per frame (interleavable by another thread's log
line); `_on_sigwinch` paints from inside a signal handler (re-entrant against an interrupted
paint); `shutdown()` is called from five sites including two in `log.py` (a lower module
reaching into `ui`); and the docs have drifted (module docstring still prescribes `clear()`
before `input()`, `suspended()` claims to wrap makepkg, `docs/design/09` lists dry-run as a
plain-mode trigger contrary to `3.1.0-B7`).

## Goals

- One state object; one compose function; the rendered line is derived, never stored twice.
- One terminal-handover API (`yield_terminal`) replacing `suspend_for_prompt()`/`suspended()`.
- One paint function emitting one write per frame.
- Exactly one scheduled-repaint owner at any time; bar refreshes ~1/s.
- Tracker clock pauses while the terminal is yielded.
- One lifecycle owner (`cli.main`).
- Nested trackers disallowed.
- Call-site API shape preserved: `phase`, `tracker`/`tick`/`note`/`resume`, `progress_hooks`.

## Non-goals

- Build-history-aware ETA — `3.3.0-F1`, which lands on top of this (see Follow-ups).
- Pausing for sudo prompts — two follow-ups, below.
- Any change to plain-mode output shape or to exact TTY bytes of existing scenarios.

## Design

Approach: keep `ui/progress.py` a single module and keep the **module object** as the registered
`ProgressHooks` implementation (`3.2.0-F1b`'s choice — test patches on module functions keep
reaching every primitive). Internals are replaced by a private state object. Rejected: a
registered display-class instance (reverses F1b, adds a delegation layer); a `ui/progress/`
package (four files for ~600 lines, more cross-module patch seams).

### 1. State model and composition

```python
@dataclass
class _Tracker:
    total: int
    prefix: str
    started: float                     # active-clock time at entry
    index: int = 0                     # items ticked; item `index` is in flight
    label: str = ""                    # in-flight item's label
    note: str | None = None            # tick.note() overlay; None → show label
    last_tick: float = 0.0             # active-clock time the in-flight item started
    expected: list[int] | None = None  # per-item seconds; reserved for 3.3.0-F1
    paused_total: float = 0.0          # wall seconds spent paused since entry
    paused_since: float | None = None  # set while paused

@dataclass
class _Bar:
    mode: str | None = None            # "tty" | "plain" | None (uninitialised)
    phase: str | None = None           # single slot: phase(x) replaces, phase(None) clears
    tracker: _Tracker | None = None    # at most one
    detail: str | None = None          # heartbeat detail; cleared by tick/note/resume/phase
    yielded: str | None = None         # None | "prompt" | "child"
    yield_depth: int = 0
    reserved: bool = False
    resize_pending: bool = False
    rows: int = 0
    cols: int = 0
    drawn: str | None = None           # exact text last written to the bar row
```

`phase` stays a single slot, not a stack: `run_verb` paints `<verb>: starting…` and `update`
deliberately overrides it; a stack would leave stale labels beneath.

`_compose(bar, now) -> str | None` is the only line constructor:

- tracker open → `[SYSFORGE][PROGRESS] [i/n] <prefix> · <note or label, or "starting..." when
  i == 0><suffix>`. The tracker line wins while open; `phase()` during a tracker updates the
  slot and shows after exit (no current caller does this — `build_core`'s
  `"installing built packages"` at `:1206` follows the `building` tracker at `:1017`).
- phase only → `[SYSFORGE][PROGRESS] <phase>` (no suffix outside a tracker).
- neither → `None`.
- `detail` set → append ` · <detail>`.

The time suffix is a pure function of `(tracker, now)` implementing today's F14 rules (5 s floor,
two completions before an ETA, drop on overrun) over the **active clock** (§2 pause).

Removed: `_last_status`, `_last_base`, `_phase`, `_time_suffix_fn`, the tracker closure state.
`render()` becomes private (no external callers).

Plain mode composes identically; `log.ui()` emits on tick and on phase change, deduped against
`drawn`. Heartbeat and scheduled refresh never emit in plain mode.

### 2. Paint, handover, resize, pause

**`_draw()`** — sole bar writer:

1. Return if plain mode or `yielded`.
2. If `resize_pending`: refresh size, reset region, clear the flag — within this frame.
3. `text = _compose(bar, now)`. `None` → release region if reserved; return. `text == drawn`
   and no resize → return (no bytes).
4. Build the frame as one string — `SAVE`, goto bottom row, `CLEAR_LINE`, glyph-downgraded and
   width-truncated text, `RESTORE` (prefixed by region establishment on first draw) — emit with
   a single `_write()`; set `drawn = text`.

**Lock.** A module `threading.Lock` guards *mutate-then-`_draw()`* in every public entry point
and the ticker. Never held across a blocking call or a `yield_terminal` body. Never taken by the
signal handler.

**Resize.** `_on_sigwinch` only sets `resize_pending = True` (no I/O, no lock). The scheduled
owner (§3) applies it within ~1 s. `pty_runner` keeps its own child-winsize propagation.

**`yield_terminal(kind)`** — context manager; replaces `suspend_for_prompt()` and `suspended()`
in `ui/progress.py` **and** the `ProgressHooks` protocol (all call sites migrate in the same
change: `prompt.py` ×3, `pager.py`, `editor.py`):

- `"prompt"` — blank the bar row in place, keep the region (today's `suspend_for_prompt` bytes).
- `"child"` — release the region fully (today's `suspended`).
- While yielded, state keeps updating; nothing draws.
- Nested yields supported via `yield_depth`; the strongest kind (`child` > `prompt`) applies;
  outermost exit restores region if released and **repaints immediately**.
- `reserved_rows()` → 1 while prompt-yielded, 0 while child-yielded.
- No-op in plain mode. The `progress_hooks` default becomes a bare `yield`.

**`clear()`** becomes teardown-only (release region, reset `drawn`, state untouched). The
pre-prompt `clear()` calls in `build_core.py:285,907` are deleted.

**Pause.** Pause state is independent of `yielded` (yield implies pause; pause does not imply
yield — follow-up B needs pause-without-yield).

- `_active(t, now) = now − t.paused_total − (now − t.paused_since if t.paused_since else 0)`.
- `started`/`last_tick` are recorded in active time; elapsed = `_active(now) − started`;
  in-flight = `_active(now) − last_tick`; rate = `(last_tick − started) / completed`. A
  prompt during item *k* is excluded from item *k*'s duration, so it does not inflate the rate.
- Outermost `yield_terminal` entry sets `paused_since`; outermost exit folds it into
  `paused_total`. A tracker opened while yielded starts paused. No tracker → nothing to pause.
- Covers every prompt: all interactive input goes through `primitives/prompt.py` (no raw
  `input()` elsewhere), including the build-failure recovery menu and its retry prompts
  (`makepkg_invoke.py:795,914,935`).
- Plain mode never yields, so never pauses.

### 3. Scheduled repaint

Rule: exactly one scheduled painter at a time, chosen by whether a **raw byte stream** is being
forwarded to the terminal. Single-write frames are safe against line-oriented writers (`log.*`,
`print`, TTY-inheriting pacman/git); only a raw byte forwarder can be mid-escape or mid-UTF-8.

**Owner A — ticker thread (default).** Daemon thread started lazily on the first TTY draw,
stopped by `shutdown()`/`clear()` via an `Event`. Every 1.0 s: take the lock, `_ticker_step()` →
`_draw()` unless yielded or forwarding is active. Frame dedup makes it write nothing until the
text changes. Applies `resize_pending`. Covers every tracker and phase outside a forwarded build.

**Owner B — `pty_runner` while forwarding.** New hooks:

- `forwarding_output()` — context manager; while active the ticker does not draw.
- `refresh()` — draw now if the composed text changed.

`run_with_pty` enters `forwarding_output()` only when `forward_bytes` is true (at `-vvv`, output
arrives as whole log lines and Owner A stays in charge). The `select` timeout becomes
`min(until next refresh, until idle heartbeat)`. After any chunk or timeout, if ≥ 1 s since the
last refresh **and** the forwarded stream is at a safe boundary, call `refresh()`.

**`_StreamBoundary`** (new, in `pty_runner`): tracks forwarded bytes for mid-sequence state —
CSI, OSC (BEL or ST terminated), DCS/APC/PM/SOS strings, two-byte ESC sequences (`ESC (` etc.),
and incomplete UTF-8 (2/3/4-byte). Not derived from `_TerminalQueryFilter`'s pending-tail regex,
which misses two-byte ESC, DCS and UTF-8. Refresh is deferred, never forced; the stream reaches a
boundary whenever the child pauses.

**Heartbeat** keeps its 30 s cadence for the `[heartbeat]` log line (`makepkg_invoke._on_idle`);
on the bar it only sets `bar.detail`. `heartbeat()` stops painting directly.

**Failure.** Ticker exceptions are swallowed and recorded to `SYSFORGE_PROGRESS_TRACE`; the thread
continues. `shutdown()` joins with a 1 s timeout.

Cost: one wakeup/s; ~one short write/s only while a tracker is past the 5 s floor.

### 4. Lifecycle and tracker rules

**Lifecycle.**

- `init()` stays in `_main` after verbosity/colour/dry-run resolution (mode detection needs them).
- New `progress.session()` context manager whose exit calls `shutdown()`. `main()` wraps
  `_main()` in it inside the existing `try`; the context exits before the
  `KeyboardInterrupt`/`BrokenPipeError` handlers, so the region is released before
  `aborted (Ctrl-C)` prints. Their explicit `shutdown()` calls are removed.
- Removed: `shutdown()` in `log.fatal` and `log.close_unified_log` (present since `d346286` with
  no stated reason). `log.py` no longer imports `sysforge.ui`.
- Removed: `--py-profile`'s `clear()` (`cli.py:1680`).
- atexit remains, documented as the backstop for non-`cli.main` importers. `shutdown()` stays
  idempotent and stops the ticker.

**Tracker rules.**

1. **One tracker at a time.** `tracker()` entry with `bar.tracker` set:
   - strict (`_strict_nesting = True`, set by an autouse `tests/conftest.py` fixture) → raise
     `RuntimeError("tracker '<inner>' opened inside '<outer>'")`;
   - lenient (production) → `debug` log, yield `progress_hooks._NoOpTick`; outer untouched.
   No current path nests (all nine trackers verified to run under a phase, none inside another).
2. **Tracker-opening functions guard at entry.** `require_no_tracker(owner)` (same
   strict/lenient policy; on `ProgressHooks` since `aur_resolve` is a primitive) is called first
   in `build_core.build_and_install`, `build_core.prepare_deps` and
   `aur_resolve.build_resolved_deps` — failing before sync/resolution side effects. Motivating
   hazard: `build_and_install` opens its own `building` tracker, and the build-⊂-update invariant
   invites routing more callers (e.g. the pipeline `packages` stage) through it. `tracker()`'s
   docstring names `note()`/`resume()` as the sanctioned sub-step mechanism.
3. **Tick semantics.** `tick(label)` advances, clears `note`/`detail`, stamps `last_tick`
   (active clock). `note()`/`resume()` set/clear the overlay. Exit sets `bar.tracker = None`.
   A stale `Tick` after exit is a no-op. Ticks are thread-safe under the lock.

### ProgressHooks after this change

`yield_terminal(kind)`, `reserved_rows()`, `heartbeat(detail)`, `refresh()`,
`forwarding_output()`, `tracker(total, prefix)`, `require_no_tracker(owner)`. Removed:
`suspend_for_prompt`, `suspended`. Every member has a no-op default.

## Testing

Autouse fixture: fresh `_bar`, `_strict_nesting = True`, `_ticker_enabled = False`. Scheduled
paints are driven by calling `_ticker_step()` with the existing fake clock
(`tests/test_progress.py:542`). Capture fixtures read the final buffer, so single-write framing
is transparent.

**Existing 48 in `tests/test_progress.py`:**

| Group | Count | Disposition |
|---|---|---|
| Mode detection, region, cursor, trace, plain | 23 | Unchanged — exact-byte guard for the refactor |
| Tracker/phase/ETA exact strings (F14 guard) | 16 | Unchanged except `test_suffix_cleared_after_tracker_exits` (asserts via `_bar`) |
| `suspend_for_prompt` / `suspended` | 4 | Rewritten against `yield_terminal`; same bytes; + repaint-on-exit |
| Heartbeat | 5 | Follow `heartbeat()` with `refresh()`/`_ticker_step()`; strings unchanged |

The 25 private-state references move to `_bar.<field>`. `test_prompt.py`, `test_pager.py`,
`test_editor_chain.py`, `test_progress_hooks.py`, `test_pty_runner.py` migrate to
`yield_terminal`; `test_progress_hooks.py` covers each new member's no-op default.

**New:**

- Compose: every shape (tracker, note, phase-only, `None`, detail) exact-string; tracker wins
  over a mid-tracker phase; stale `Tick` no-op.
- Paint: each frame is exactly one `write()` (write-recording stream); unchanged frame writes
  nothing; `resize_pending` applied next frame; `None` releases.
- Pause: yield mid-item excluded from elapsed and rate; nested yields pause once; tracker opened
  while yielded starts paused; pause-without-yield freezes the clock but still paints; a
  recovery-menu prompt via `prompt.prompt_choice` pauses an open tracker.
- Scheduling: `_ticker_step()` paints on change, skips while yielded or forwarding; no thread in
  plain mode; one real-thread test for start/stop via `shutdown()` and exception swallowing.
- `_StreamBoundary`: table-driven — plain text; CSI split at every byte; OSC with BEL and ST;
  DCS; `ESC (`; UTF-8 2/3/4-byte split at every offset.
- pty integration: fake child emitting chunks split mid-sequence/mid-codepoint with a recording
  hooks impl — `refresh()` only at boundaries, ≤ ~1/s; `forwarding_output()` only when
  `forward_bytes`.
- Nesting: strict raises naming both prefixes; lenient yields no-op with outer intact;
  `require_no_tracker` fires at entry of the three guarded functions before side effects.
- Lifecycle: `KeyboardInterrupt` from `_main` → region release bytes precede
  `aborted (Ctrl-C)`; `log.fatal` exits through `session()`; `log.py` has no `sysforge.ui`
  import (layering-style assertion).

**Verification:** `make test`, `make lint`, `make check-standards` (`sysforge/CLAUDE.md`
citations of `suspend_for_prompt`/`suspended` must be updated), `make design` +
`make check-design` after rewriting `docs/design/09-primitives-layer.md` §`ui/progress.py` to
the built state. Manual: real-TTY `sysforge update` with a large source sync and a `mesa` build
under `SYSFORGE_PROGRESS_TRACE` — ~1 frame/s, no corruption, clock pauses at a recovery prompt.

## Docs touched on implementation

- `docs/design/09-primitives-layer.md` §`ui/progress.py` and §`progress_hooks.py` — rewritten to
  the built design, including a tracker-owner table.
- `docs/design/11-makepkg-wrapper.md` — heartbeat now sets detail only; pty owns repaint while
  forwarding.
- `sysforge/CLAUDE.md` — any `suspend_for_prompt`/`suspended` citations → `yield_terminal`.
- `ui/progress.py` module docstring — drop the `clear()`-before-`input()` guidance.
- Release note in `docs/release-notes/unreleased.md` under the filed ID.

Fixed now, independent of implementation (wrong today): `docs/design/09` listing `_DRY_RUN` as a
plain-mode trigger (contradicts `3.1.0-B7`, `progress.py:160`).

## Roadmap filing plan

| Item | Type | Depends on | Tag |
|---|---|---|---|
| This redesign (incl. 1 s refresh, yield pause) | F (next-id) | — | Priority med · Effort large · Bump minor |
| `3.3.0-F1` build-aware ETA | already filed | this redesign | amend: add dependency; recorded `build_seconds` should subtract yielded time via the same active clock |
| A: sysforge-owned sudo prompts pause | F (next-id) | this redesign | Priority low · Effort small · Bump patch |
| B: sudo inside forwarded build output pauses | F (next-id) | this redesign | Priority low · Effort medium · Bump patch |

**Follow-up A.** `sudo_session.authenticate()` (`sudo -v`, inherited stdio) and
`privilege.run_privileged`/`privileged_argv` (`pacman -U/-S` from `install_built`, including the
JIT install inside the `building` tracker) prompt without yielding when credentials have lapsed.
Probe `sudo -n true` (an auth probe, permitted by the privilege-escalation guardrail); if
uncached, run `sudo -v` inside `yield_terminal("prompt")` before the real command. Lives in the
privilege seam.

**Follow-up B.** `makepkg --install`/`--syncdeps` (toolchain passes use `--install`) invoke sudo
inside the forwarded pty stream. Set a `SUDO_PROMPT` sentinel in the child env, detect it in
`pty_runner`'s partial-line buffer, and pause the clock (pause-without-yield hook) until the next
output. Caveat: sudoers `passprompt_override` defeats `SUDO_PROMPT`; the alternative — pointing
`PACMAN_AUTH` at `sudo -n` so the build never prompts and relies on `keepalive` — is a behaviour
trade-off for the item to decide.
