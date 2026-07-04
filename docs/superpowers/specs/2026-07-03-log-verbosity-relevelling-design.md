# F36 — Audit logging verbosity levels + configurable default verbosity

Roadmap ID: `1.2.0-F36`
Date: 2026-07-03
Status: design approved

## Problem

Over successive features, warning/info-grade messages — and, crucially,
progress narration — crept into the **always-printed** path (`log.ui`), eroding
the meaning of the verbosity levels. At the shipped default (verbosity 0) the
output is now noisy: it carries not just "errors + the answer" but a running
commentary on producing the answer.

Two fixes, shipped together:

1. **Re-levelling audit** (full sweep): reclassify every logging call site so its
   level matches intent, against a written rubric. The dominant motion is
   demoting progress-chatter `ui()` → `info()`.
2. **Configurable default verbosity**: a user-settable `[log] verbosity` key so
   users can opt into a quieter or more verbose baseline **without** changing the
   shipped default (which stays 0). CLI flags still win.

The user personally prefers `info` as a default but it is too noisy to force on
everyone — so the fix is *correct levelling* plus a *user-settable* default, not
changing the shipped default.

## Non-goals

- Changing the shipped default verbosity (stays 0 = errors + UI).
- Adding a parallel verbosity switch inside `log.py`. Resolution happens once at
  the CLI entry point and calls the existing `log.set_verbosity` seam.
- Reworking colour/unicode gating (`use_color`/`use_unicode` are untouched).

## The level rubric (audit authority)

Every call site is audited against this fixed contract. `ui()` is
verbosity-immune by design and became a magnet for "make this show up"; the
audit reclaims it for *primary output only*.

| Level | Fn                    | Gate    | Reserved for |
|-------|-----------------------|---------|--------------|
| UI    | `ui()`                | always  | The primary output the user ran the command to see — final summaries, `doctor` findings, `state`/`log`/`env` verb bodies, prompts, tables. **Not** progress narration. |
| ERROR | `error()` / `fatal()` | always  | Failures that abort or degrade the run. |
| WARN  | `warn()`              | `-v`    | Recoverable anomalies: skips, fallbacks, soname/ABI mismatches the user should know about but that don't block. |
| INFO  | `info()`              | `-vv`   | Progress/status narration: "syncing X", "wrote temp conf", "building 3/7". Most leaked `ui()` calls land here. |
| DEBUG | `debug()`             | `-vvv`  | Full body dumps: config/profile/conf contents, resolved argv, env snapshots. |

Decision test for each site: **is this the answer, or narration about producing
the answer?** The answer → `ui()`; narration → `info()` (or `debug()` for full
dumps). File logs are unaffected — every level is always written to the file log
regardless of stderr gating, so demotion never loses forensic detail.

Scope: full sweep across `sysforge/` (≈384 `ui()`, 309 `warn`, 270 `info`, 35
`debug`, 62 `error` sites), executed file-by-file during implementation. The
rubric above is the spec the audit executes; individual reclassification
decisions are not enumerated here.

## Config key + precedence

### Key

- New `[log]` section in `sysforge.toml`, key `verbosity` (integer 0–3).
- Shipped **commented-out**, documenting that the shipped default is 0 and the
  0–3 meaning (0 errors only, 1 +warnings, 2 +info, 3 +debug).
- Test fixtures (`tests/data/etc/sysforge/sysforge.toml`) updated in lockstep;
  `check_shipped` `_KNOWN_SECTIONS` / `_KNOWN_TOP_KEYS` extended for
  `[log]` / `verbosity` in the same change.

### Resolution

A new `_resolve_verbosity(args)` in `cli.py`, mirroring the existing
`_resolve_color_mode` pattern, resolves once at entry and calls
`log.set_verbosity(...)`. The `log` seam stays the single verbosity authority;
no resolution logic leaks into `log.py`.

Precedence (highest first):

1. Global `--quiet` passed → level 0 (wins over everything).
2. Else `-v/-vv/-vvv` passed (argparse `count` > 0) → that level wins.
3. Else `[log] verbosity` config value (clamped to 0–3).
4. Else 0.

Invalid config (non-int, out of range) degrades gracefully — clamp or ignore,
never abort startup — matching the posture of `set_color_mode`/`set_unicode_mode`.

### Global `--quiet`

New top-level flag, hoisted ahead of the subcommand like `-v` (via
`_hoist_verbosity_flags`). `doctor` already defines a **local** `--quiet/-q`
with different semantics (suppress clean lines). To avoid clobbering
`args.quiet`, the global flag uses a **distinct dest** (`args.quiet_global`);
`doctor`'s `args.quiet` is untouched. The global flag participates only in
verbosity resolution.

## Lockstep surfaces (project conventions)

CLI-surface change ⇒ updated in the same change:

- Both completions: `completions/_sysforge` + bash — add global `--quiet`.
- Manpage (scdoc source) — add `--quiet` and the `[log] verbosity` config mention.
- `docs/design/*.md` Logging section (+ `make design`); the doc-update order is
  design source → README → CLAUDE.md.
- README.md — user-facing `[log] verbosity` config documentation.
- `tools/check_shipped.py` allowlists — `[log]` / `verbosity`.
- `docs/release-notes/unreleased.md` — append the F36 entry (Keep a Changelog
  section, inline roadmap ID).
- ROADMAP.md — remove the F36 entry in the landing commit; re-sort remains.

## Testing

- **Precedence matrix** (table-driven unit test): `{--quiet, -v×n, config, none}`
  combinations → resolved level.
- **Config parse**: valid int, out-of-range, non-int, missing → graceful
  resolution.
- **Golden default-output regression**: assert that default-level
  (`verbosity=0`) output of a representative `update`/`doctor` dry-run contains
  no `[INFO]` / `[WARN]` lines and only the intended UI/ERROR lines. This locks
  in the quiet default and is the primary guard against future features
  re-leaking narration into `ui()`.
- **Dual-toolchain parity**: any reclassified site that branches on resolved
  compiler keeps both gcc-path and llvm-path coverage (existing convention).

## Risks

- **Sweep size / churn.** 384+ sites is large; each is a judgment call. Mitigation:
  the rubric is the single decision authority, and the golden-output test locks
  the observable default. Reclassification never loses file-log detail (all
  levels always written to file).
- **Global `--quiet` dest collision** with `doctor`'s local `--quiet`. Mitigated
  by the distinct `quiet_global` dest.
