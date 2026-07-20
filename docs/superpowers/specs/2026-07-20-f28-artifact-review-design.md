# F28 — `artifact review`: interactive opt-in offering

**Roadmap ID:** `1.2.0-F28` (remaining thread; primitive shipped under `2.4.0-F4/F5/F6`)
**Date:** 2026-07-20
**Status:** design approved, pending spec review

## Problem

Today a user with a hand-authored artifact — a script in `~/scripts`, a unit in
`/etc/systemd/system`, a hook in `/etc/pacman.d/hooks` — must *know* to run
`sysforge artifact adopt <path>` to bring it under management. `scan()` already
discovers these candidates (surfaced read-only via `artifact list --unmanaged`),
but nothing proactively **offers** them. F28's remaining thread: sysforge should
*offer* (not force) adoption of user-owned artifacts.

## Decisions (from brainstorming)

1. **Surface:** a dedicated interactive verb, `artifact review` — not a `setup`
   stage prompt, not a doctor finding, not path-less `adopt`.
2. **Decline memory:** declines persist in a **persistent ignore-list**, keyed by
   path + content-hash; a candidate is re-offered only if its content changes.
3. **Verb identity:** new `artifact review` verb; `artifact adopt <path>` stays a
   pure non-interactive one-shot. On no-TTY, `review` lists candidates + the adopt
   hint and exits 0 (never prompts).

## Architecture

```
sysforge/verbs/artifact.py       ← + ArtifactReviewVerb (read-only, no sentinel)
sysforge/primitives/artifacts.py ← + IgnoreList, iter_offerable()
  <state_dir>/artifacts.toml        (unchanged — managed set)
  <state_dir>/artifacts-ignored.toml (NEW — declined candidates: path → hash)
```

**Why a separate state file.** `artifacts.toml` is documented as *regenerable*
(rebuildable from managed content). The ignore-list is irreplaceable **user
intent** and must survive a registry rebuild. Coupling them would let a registry
regen silently forget every decline. Hence a sibling file with its own load/save.

## Components

### `IgnoreList` (primitives/artifacts.py)

Mirrors `ArtifactRegistry`'s shape:

- `load() -> dict[Path, str]` — path → content-hash recorded at decline time.
- `add(path, hash)` / `save()`.
- Backed by `<state_dir>/artifacts-ignored.toml`.
- Same corruption guard as the registry: unreadable/corrupt → `ArtifactRegistryError`
  with repair guidance, not a traceback.
- **Self-pruning on load:** drop entries whose file no longer exists, so a
  deleted-then-recreated file is legitimately re-offered.

### `iter_offerable(registry, ignore) -> list[Candidate]`

The single composition point over `scan()`. Excludes:

- sysforge-owned candidates (`OWNER_SYSFORGE`),
- already-managed (present in `registry.load()`),
- ignored **with matching hash** (changed content re-surfaces).

This is exactly the filter `artifact list --unmanaged` open-codes today
(`verbs/artifact.py`). **Refactor that call site to reuse `iter_offerable`** so the
two surfaces cannot drift. (`list --unmanaged` passes an empty/loaded ignore-list
as appropriate — it still shows ignored candidates for visibility; `review`
suppresses them. Concretely: `iter_offerable` takes the ignore-list; `list` calls
the shared scan+manage filter and layers ignore-display on top, so the *manage/own*
exclusion is shared and only the ignore step differs.)

### `ArtifactReviewVerb`

- `name = "artifact-review"`, `requires_sentinel = False`.
- Per offerable candidate, prompt `[a]dopt / [s]kip / [i]gnore / [q]uit`:
  - **adopt** → `artifacts.adopt(registry, c.path, cls=c.cls)` (existing, copy-only,
    touches no live system file).
  - **ignore** → `ignore.add(c.path, current_hash)` then `save()`.
  - **skip** → nothing (re-offered next run).
  - **quit** → stop the walk, exit 0.
- Wire via `set_defaults(verb_cls=ArtifactReviewVerb)`.

## Non-TTY behavior & error handling

- **No TTY** (`sys.stdin.isatty()` is false): print the offerable list + the
  `sysforge artifact adopt <path>` hint, exit 0. Never block a script on a prompt.
  Mirrors the `build` repo-pkg gate ("prompts on TTY or aborts non-interactive").
- Privileged classes (`systemd-unit`, `pacman-hook`) are *offered* here, but
  adoption is copy-only and unprivileged; actual deployment stays the separate
  sentinel-gated `deploy`. So `review` needs no sentinel.
- Corrupt ignore-list → surface repair guidance, exit 1 (same as the registry path).

## CLI / completions / docs (lockstep, per project conventions)

- `cli.py`: register `artifact review` subverb via `set_defaults(verb_cls=…)`.
- `completions/_sysforge` + bash completion: add `review` in the same change.
- Manpage (`docs/design/verbs/…` → `make man`) + `make check-shipped` parity.
- DESIGN via `docs/design/*` (+ `make design`) → README → CLAUDE.md order.
- Remove the F28 remaining-thread entry from ROADMAP.md and append a release-note
  entry to `docs/release-notes/unreleased.md` in the landing commit.

## Testing

Dual-surface parity per project convention:

- `IgnoreList`: round-trip; self-prune on missing file; corruption guard.
- `iter_offerable`: filters all three exclusion classes (own / managed /
  ignored-with-matching-hash); changed-hash re-surfaces.
- **Refactor-parity test:** `list --unmanaged` and `review` share the own/managed
  filter (identical candidate universe before the ignore step).
- `ArtifactReviewVerb`: TTY walk exercising adopt/skip/ignore/quit (monkeypatched
  input) **and** non-TTY degradation (lists, exits 0, no prompt) — the required
  interactive/non-interactive pair.

## Scope boundary

F28 stops at "sysforge can proactively offer." Explicitly **out of scope here**:

- Deploying offered units/hooks — remains `artifact deploy` (sentinel-gated).
- *Firing* the offering from inside a pacman transaction — that is **F4**
  (`2.3.0-F4`, alpm PreTransaction/PostTransaction hooks), the next spec in the
  cluster. F28's `iter_offerable` is the seam F4 will later call from a hook.
