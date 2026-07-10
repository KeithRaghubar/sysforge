# Roadmap-ID Collision Detection (`2.1.0-F1`)

**Status:** design approved, pending implementation
**Date:** 2026-07-09

## Problem

ROADMAP.md and `docs/release-notes/` are two disjoint homes for roadmap IDs:
ROADMAP carries *open* items, release-notes carry *shipped* items (the landing
commit moves an item from the former to the latter). Nothing cross-checks the
two, so an open ID can silently reuse a number already consumed by a shipped
entry. This actually happened: open `2.1.0-B2`/`B3` collided with *shipped*
`2.1.0-B2`–`B7` and had to be hand-renumbered to `B16`/`B17` during triage.

Add a validator (folded into `make check-standards`) that computes the true
high-water mark per `<version>-<TYPE>` across both sources and flags collisions,
plus a `--next-id` helper so triage allocates monotonically.

## ID grammar

`(\d+\.\d+\.\d+)-([A-Z]+)(\d+)` → `(version, type, n)`, e.g.
`2.1.0-F1` → `("2.1.0", "F", 1)`. The version prefix records the minor-release
cycle an item originated in; per-type counters reset only on a major/minor bump,
so IDs are compared *within* a `(version, type)` bucket.

## Housing

One new group `roadmap_ids` in `tools/check_standards.py`, registered in the
`GROUPS` dict so it runs under `make check-standards` and `tools/release.sh`
preflight. A `--next-id <version>-<TYPE>` flag on the same script exposes the
allocation helper. Single shared ID parser; reuses the existing `Finding`
dataclass and `main()` scaffolding. No new tool file.

## Extraction — two pure functions

Both are pure `str -> …` functions (take file text, return IDs), unit-testable
in isolation without the filesystem.

### `roadmap_ids_from_text(text) -> dict[id, "planned"|"abandoned"]`

Anchor on **bold-bullet entries only**: lines matching
`^- \*\*` `` `<ID>` `` (a bullet whose first token is a bold code-span holding an
ID). Classify each by position relative to the `## Abandoned` header
(before → `planned`, after → `abandoned`). This anchoring skips the
`## ID scheme` section's inline prose examples (`e.g. \`1.2.0-F1\` (feature)`)
because those are not bullet-leading bold code-spans. Non-ID abandoned entries
(e.g. `` - **`-sysforge` suffix …`` ``) are naturally excluded — their code-span
content does not match the ID grammar.

### `shipped_ids_from_text(text) -> set[id]`

Strip `<!-- … -->` HTML-comment blocks first, then collect every ID token in the
remainder. Comment-stripping removes the accumulator header's instructional
example (`Reference the roadmap ID inline, e.g. (1.2.0-F35)`), which is the sole
prose-example source in release-notes files. Applied to every `v*.md` plus
`unreleased.md` under `docs/release-notes/`; the union is the *shipped* set.

## Checks

Duplicate checks (1, 2, 4) span **all** prefixes. Gap check (5) is scoped to the
active `pyproject.toml` version prefix only.

| # | Condition | Severity | Rationale |
|---|-----------|----------|-----------|
| 1 | ID is ROADMAP **Planned** *and* **shipped** | error | The core collision — an open item reusing a shipped number. |
| 2 | ID is ROADMAP **Planned** *and* ROADMAP **Abandoned** | error | Contradiction: an ID cannot be both live and decided-against. |
| 4 | A `Q`-typed ID appears in **shipped** release-notes | error | A `Q` shipped without promotion to F/B/STD — enforces the existing Q-before-impl rule (ROADMAP.md "Open questions must be resolved before any implementation"). |
| 5 | Sequence gap for the **active version prefix only** (currently `2.2.0-`) | warn | New-allocation nudge. Historical prefixes (`1.2.0-`, `2.1.0-`) carry legitimate pre-archive and abandonment gaps and are grandfathered. |

**Dropped: cross-release-file duplicate detection.** After comment-stripping, a
legitimate "see also (`1.2.0-F35`)" back-reference between release-notes files is
indistinguishable from a true double-ship, so this class is not checked (would be
prose-prone). Checks 1/2/4 already cover the corruption that actually recurs.

### Gap definition (check 5)

For the active version prefix, per type, let `N = max(n)` across the union
(planned ∪ abandoned ∪ shipped). Any `k` in `1..N` absent from the union is a
gap → one `warn` per missing `k`. Scoping to the active prefix keeps the current
repo green: the `1.2.0`/`2.1.0` cycles have real historical gaps
(`1.2.0-F2..F8`, `F10..F13`, `F23`, `F25`; `1.2.0-B2..B4`) that predate the
release-notes archive (which starts at `v2.0.0`).

## `--next-id <version>-<TYPE>` helper

Parses the argument into `(version, TYPE)`, computes `N = max(n)` for that bucket
across all three sources (planned ∪ abandoned ∪ shipped), prints
`<version>-<TYPE><N+1>` to stdout, exits 0, emits no findings. Including
*abandoned* IDs in the max is deliberate — an abandoned number is consumed and
must not be reissued. Usage: `python tools/check_standards.py --next-id 2.2.0-F`.

## Tests

In `tests/test_standards_compliance.py` (or a dedicated `test_roadmap_ids.py`),
following the repo's drift-detection convention:

- **Extractor unit tests** (pure, on literal strings):
  - Comment-stripped prose example (`e.g. (1.2.0-F35)` inside `<!-- -->`) is *not*
    in the shipped set.
  - A bold-bullet `` - **`2.2.0-F7`** — … `` *is* extracted as planned.
  - `## ID scheme` inline prose (`e.g. \`1.2.0-F1\``) is *not* extracted.
  - A bullet after `## Abandoned` classifies as abandoned.
- **One positive fixture per firing check (1, 2, 4, 5)** proving it emits.
- **Clean-tree test** on the real repo: `check_roadmap_ids(REPO)` yields no
  `error`-severity findings today (warns permitted).
- **`--next-id`** returns `max+1` including an abandoned ID in the max.

Add the new drift cases to the module-header docstring list in
`check_standards.py` (matching the existing "verify these still fire" cases),
e.g.:
  - Add an ID to ROADMAP Planned that is already in a shipped `v*.md`.
  - Ship a `Q`-typed ID in `unreleased.md` without promotion.

## Docs & process

- `docs/design/21-standards.md`: document the `roadmap_ids` group + `--next-id`.
- `check_standards.py` module docstring: add `roadmap_ids` to the Groups list and
  the new drift cases.
- CLAUDE.md process note (ID-scheme / triage section) can reference `--next-id`
  as the allocation command.
- **Same-commit process rule:** remove `2.1.0-F1` from ROADMAP.md and append its
  entry to `docs/release-notes/unreleased.md`. The new check then validates its
  own removal — after landing, `2.1.0-F1` must appear only as shipped, never as
  both Planned and shipped (which would trip check 1).

## Non-goals

- No rewriting/renumbering of historical IDs.
- No back-filling pre-archive gaps into the Abandoned section.
- No auto-allocation on write (triage still copies `--next-id` output by hand).
