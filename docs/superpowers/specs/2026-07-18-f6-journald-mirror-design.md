# F6 — Mirror system-mutating verbs to journald

**Roadmap ID:** `2.3.0-F6`
**Date:** 2026-07-18
**Status:** design approved, ready for implementation plan

## Problem

The `log.py` unified run-log is the authoritative, user-facing capture of a
sysforge run, and it stays that way. What's missing is a *system-integrated*
sink: when sysforge mutates the live system (installs packages, rebuilds the
kernel/toolchain, provisions), those changes do not appear in `journalctl`
alongside everything else that touched the machine — the place an admin looks
during incident review. F6 adds that second, additive sink for mutations only.

## Scope

- **In:** emit one structured journald record per **sentinel-gated** (i.e.
  `requires_sentinel = True`) verb invocation, with verb / target / exit as
  queryable fields, on both success and failure paths.
- **Out:** non-mutating verbs, per-privileged-subprocess records, config toggle,
  extra fields (timing, argv), `systemd-cat` fallback. See §Non-goals.

This is additive and defense-in-depth (observability). It never replaces the
run-log and is never load-bearing.

## Architecture

### New primitive: `sysforge/primitives/journal.py`

The single home for all journald interaction. One public function:

```python
def journal_send(**fields: str | int) -> None
```

- Pure-stdlib `AF_UNIX` / `SOCK_DGRAM` write to `/run/systemd/journal/socket`.
- Native journal protocol framing (see §Wire format).
- **No-op** — silent return — when the socket is absent, the connect fails, or
  the send errors. journald is additive; a non-systemd host or sandboxed CI run
  simply gets nothing. `journal_send` **never raises into a verb.**

### Single caller: `verbs/runner.py::_run_verb_inner`

Emit **one record per `requires_sentinel` verb**, after the outcome is known,
on both the normal-return path and the `RuntimeError` failure path (a crashed
mutation is exactly what incident review wants). Non-sentinel verbs, pre-check
skips, and pre-check blocks emit nothing.

This reuses the existing sentinel chokepoint (`runner.py:79`). Coverage is
correct-by-construction: any future mutating verb sets `requires_sentinel = True`
and is mirrored automatically — no enumeration to maintain. The record sits
alongside the unified run-log (`_consolidated_log`), which remains authoritative.

## Structured fields

| Field | Example | Notes |
|---|---|---|
| `MESSAGE` | `sysforge build mesa → exit 0` | human-readable default `journalctl` line |
| `PRIORITY` | `6` / `3` | `6` (info) on exit 0, `3` (err) on nonzero |
| `SYSLOG_IDENTIFIER` | `sysforge` | `journalctl -t sysforge` filters all records |
| `SYSFORGE_VERB` | `build` | `journalctl SYSFORGE_VERB=build` |
| `SYSFORGE_TARGET` | `mesa` | omitted when the verb has no target |
| `SYSFORGE_EXIT` | `0` | queryable |

Custom fields are `SYSFORGE_`-prefixed to avoid colliding with journald's
reserved well-known field names (`MESSAGE`, `PRIORITY`, `_PID`, …), which are
dropped or mis-handled on collision.

### Wire format

Per the `sd_journal_send` socket format:

- Single-line value → `NAME=value\n`.
- Value containing `\n` → binary form: `NAME\n` + little-endian `uint64` byte
  length + `value` + `\n`.

All F6 fields are single-line, but the primitive implements both branches for
correctness; the behavioural test exercises each.

## Per-verb target hook

Add to `Verb` (mirrors the existing `unified_log_basename` pattern):

```python
def journal_target(self, args) -> str | None:
    return None   # default: no SYSFORGE_TARGET field
```

Overridden where meaningful:

- `build` → space-joined `args.packages` (the positional `PKG` list, `cli.py`
  line 508); `None` when the list is empty.
- `update` → default `None`. `update` rebuilds *every* source-built package
  (per the build_state authority), so there is no single meaningful target; the
  verb name alone is the record.
- `run kernel` / `run toolchain` / `setup` / other targetless verbs → default
  `None` (the verb name is the whole story).

The plan confirms the exact arg attribute per overriding verb before wiring it.

A hook (not a hard-coded switch in the runner) keeps the framework's
one-verb-per-class encapsulation.

## Standards

Add a row to `docs/design/21-standards.md` in the **landing commit** (the
in-commit rule for spec conformance):

- **Spec:** `systemd.journal-fields(7)` + native journal socket protocol
  (`sd_journal_send(3)` wire format).
- **Scope:** SysForge mirrors every sentinel-gated (system-mutating) verb to
  journald as a structured record (`SYSFORGE_VERB` / `SYSFORGE_TARGET` /
  `SYSFORGE_EXIT` + `MESSAGE` / `PRIORITY` / `SYSLOG_IDENTIFIER`), additively
  alongside the unified run-log; no-op when journald is absent.
- **Status:** `target` → `enforced` once the coverage test below guards it
  (F6 requires this flip in-commit).
- **Enforcement:** wired into `check_standards.py`'s table + the
  `tests/test_standards_compliance.py` case below.

## Testing

1. **`tests/test_journal.py` — wire correctness.** Bind a fake `AF_UNIX`
   datagram socket; assert exact wire bytes for the single-line branch *and* the
   binary/newline branch. Assert a missing / refused socket is a silent no-op
   (never raises).
2. **`tests/test_standards_compliance.py` — coverage / enforcement.** Monkeypatch
   `journal.journal_send`; run a fake `requires_sentinel=True` verb and a fake
   `requires_sentinel=False` verb through `run_verb`. Assert emit / no-emit and
   the field payload. This asserts the *structural invariant* (emission keyed off
   `requires_sentinel`), not a hard-coded verb list, so it cannot rot as verbs are
   added. Flipping the standards row to `enforced` depends on this test.

## Docs

- Rationale + `journalctl -t sysforge` / `journalctl SYSFORGE_VERB=…` examples →
  `docs/design/12-logging.md`, then `make design`.
- No CLI / completions / manpage changes — invisible plumbing, no new flags.

## Non-goals (YAGNI)

- No config toggle to disable — silent + no-op without journald; a knob for a
  zero-cost additive sink is unearned.
- No per-privileged-subprocess records — verb granularity is deliberate.
- No fields beyond verb/target/exit — add timing/argv later only if incident
  review actually needs them.
- No `systemd-cat` fallback — the socket *is* the systemd path; absent socket =
  absent systemd = no-op.

## Landing checklist (per repo conventions)

- Remove `2.3.0-F6` from `ROADMAP.md` in the landing commit.
- Append the release-note entry to `docs/release-notes/unreleased.md` (Added
  section, inline `(2.3.0-F6)`), keep ascending ID order.
- Add the standards row + wire enforcement in the same commit.
- gcc/llvm dual-path parity: N/A (no compiler-branching logic).
