# SysForge Roadmap

Planned features and changes. This is the single tracked home for
forward-looking work; **`DESIGN.md` describes only implemented design** and
never carries roadmap IDs.

**Purposely-excluded and abandoned ideas live in
[`docs/ROADMAP-ABANDONED.md`](docs/ROADMAP-ABANDONED.md)**, together with their
rationale and reopen conditions. That file is history rather than backlog, but
its IDs remain part of this file's ID namespace — see `## ID scheme`.

**Shipped work is not recorded here** — it lives in `docs/release-notes/` and git
history (the commit that lands an item is its record).

## ID scheme

IDs are `<version>-<TYPE><n>`, e.g. `1.2.0-F1` (feature), `1.2.0-B1` (bug),
`1.2.0-Q1` (open question), `1.2.0-STD1` (standards), `1.2.0-DOC1` (user-facing
documentation), `1.2.0-DEV1` (contributor-only work). The version prefix is the
current `pyproject.toml` version. The per-type counter **resets to 1 only on a
major or minor version bump** (`X.Y.Z` → `(X+1).0.0` / `X.(Y+1).0`), never on a
patch bump — i.e. the counter is scoped to the minor-release cycle and stays
monotonic across patch releases within it. The version prefix keeps IDs globally
unique and records the cycle an item originated in. An item still open at release
time keeps its existing ID
(it records the cycle the item originated in, not its target). IDs appear only here,
in `docs/ROADMAP-ABANDONED.md`, and in release notes.

**Those three files are one ID namespace.** An abandoned ID stays spent — it is
never reissued — so `make next-id` reads all three, and `make check-standards`
rejects an ID listed both here and in `docs/ROADMAP-ABANDONED.md`.

**Never hand-pick an ID — derive it.** The open items above keep their origin-cycle
prefixes, so eyeballing a neighbour gives the wrong cycle (and the wrong counter)
right after a release. Run `make next-id TYPE=F` (or `B`/`DEV`/`DOC`/`Q`/`STD`) — it reads the
current `pyproject.toml` version, scopes to that cycle's counter, and prints the next
free ID (e.g. `2.4.0-F1`). `make check-standards` also flags collisions and
active-cycle sequence gaps.

Within each subsection, entries are kept in **ascending ID order** (by type
counter, then version) — sort on every add so the list stays scannable. Every
entry opens `- **`<ID>` — <title sentence>.**` and is separated from its
neighbour by a `---` rule, so a single item is readable on its own rather than
running into the next one. `docs/release-notes/` entries use the same two rules.

**`DOC` covers user-facing documentation only** — guides under `docs/guides/`, concept
pages under `docs/concepts/`, `README.md`, the man page, and the comments in shipped config files
(`etc/sysforge/*.toml`) — so doc work stays visible in release notes without
being forced into `F` or `B`. A `DOC` entry always lands under `## Changed`: the
section drives the derived SemVer bump, and documentation changes no API (a
`DOC` under `## Added` would demand a minor release). `make check-standards`
enforces this. Internal docs (`docs/design/`, `CLAUDE.md`, this file) get no ID,
and wrong `--help`/man output is a `B`, since it is CLI behaviour.

**`DEV` covers contributor-only work** — a fix or feature that touches only
`tests/`, `tools/`, the `Makefile`, `.claude/` or dev-only docs (`tools/vm/README.md`
and the like). Anything under `sysforge/` or a shipped file stays `B`/`F` even when a
dev problem prompted it, and changing a *rule* (a standards row, a process
convention) stays `STD`. A `DEV` item files its release-note entry under
`## Changed` only, like `DOC`: it changes no API, and the section drives the derived
SemVer bump, so it may only ever imply a patch (`3.4.0-STD1`; until then `DEV` filed
no entry, and those items' squash-commit subjects remain their only record, which
`make next-id` and the gap check still read). `make check-standards` rejects a
`DEV`-filed entry under any other section.

**Open questions (`Q`) must be resolved before any implementation.** A `Q`
entry is undecided by definition; investigation/spikes to inform the decision
are fine, but before writing production code the question must first be either
**promoted** to a proper `F`/`B`/`STD`/`DOC`/`DEV` entry (which then follows the normal
landing flow) or moved to `docs/ROADMAP-ABANDONED.md` with a rationale. Never
implement straight off a `Q`.

## Priority, effort & bump tags

Every **Planned** entry ends with a machine-readable tag of the form
`*Priority: <level> · Effort: <size> · Bump: <kind>* — <rationale>`:

- **Priority** ∈ `low` / `med` / `high` — impact *within this backlog*. The backlog
  is polish by construction (anything correctness-bearing is implemented, not parked),
  so judge relatively: `low` = cosmetic/churn/latent; `med` = observability, or a gap
  that is *currently active* on a real system; `high` = user-visible friction now.
- **Effort** ∈ `small` / `medium` / `large` — implementation cost, independent of
  priority. On a uniformly-low-impact backlog this is often the deciding axis
  (cheapest-to-land first). Effort is stable; it does not decay the way a per-host
  "is this live right now" flag would, which is why urgency folds into *priority*
  rather than a separate tag.
- **Bump** ∈ `patch` / `minor` / `major` — the SemVer impact the entry will carry
  when it lands (standards row 3). `major` means it changes or removes an
  existing contract and can therefore only ship in an `X.0.0` release; `minor`
  is purely additive; `patch` is a fix or an internal change with no surface
  effect. This is **planning-time** advice: an item is removed from ROADMAP by
  the commit that lands it, so by release time the authoritative record is the
  release-notes accumulator, from which `make next-bump` derives the required
  bump.

`docs/ROADMAP-ABANDONED.md` entries carry no tag — the tag is planning advice,
and an abandoned item has no landing to plan. The summary table below is **generated** from these
tags — run `make roadmap-table` after any add/remove/retag; `make check-roadmap-table`
(wired into `pre-release`) fails if the committed table has drifted.

The committed table is always in triage order (priority, then effort, then ID).
To read the backlog along another axis without touching the file, use `make
roadmap-view SORT=<triage|id|item|priority|effort|bump>` (add `REVERSE=1` to
flip it) — e.g. `SORT=effort` for cheapest-to-land first. That view is
read-only by construction, so it can never be committed in place of the
canonical ordering.

---

## Planned

<!-- BEGIN roadmap-table (generated by tools/gen_roadmap_table.py — do not edit by hand; run `make roadmap-table`) -->
| ID | Item | Priority | Effort | Bump |
|----|------|----------|--------|------|
| `3.5.0-B142` | an artifact command in your first session crashes on the state directory | med | small | patch |
| `3.5.0-B141` | run kernel/toolchain/packages --dry-run shows nothing and skips the stage | med | medium | patch |
<!-- END roadmap-table -->

### Features

### Bugs

- **`3.5.0-B141` — `run kernel/toolchain/packages --dry-run` shows nothing and skips the stage.**
  Each promises "Show what would run without executing anything", but `pipeline.runner` (the
  standalone-stage path) short-circuits on `options.dry_run` before `stage.run`, logging only
  `[dry-run] would run stage: <name>` at INFO, which the default verbosity hides. So the command
  prints nothing at all, and the dry-run handling the stages carry themselves is unreachable: the
  kernel stage's "Kernel build plan" resolution summary (compiler, variant, bootloader, source,
  kconfig counts; §Kernel stage calls it "the readable core of `--dry-run`"), its boot-entry prune
  preview, `--autofdo=capture --dry-run`, and the pre-build snapshot's dry-run branch. None of
  `toolchain.toml` or `kernel.toml` past `enabled` is read, so a misconfiguration surfaces only
  when the real run starts. Found writing the `3.5.0-DEV23` end-to-end scenarios on 2026-10-24
  (`tools/e2e/scenarios/95-stage-dry-run.sh` is `xfail` against it). Fix: pass the dry run into
  `stage.run` and have each stage stop at its first mutation (the toolchain stage before any
  build, the kernel stage after its resolution summary and gates), printing its plan through
  `ui()`; `run pipeline --dry-run` shares the runner and needs the same check.
  *Priority: med · Effort: medium · Bump: patch* — a documented preview flag that previews
  nothing; the stage runs that most need one take hours.

- **`3.5.0-B142` — an `artifact` command in your first session crashes on the state directory.**
  `artifacts.ArtifactRegistry` and `artifacts.IgnoreList` resolve the state directory themselves
  (argument, then `SYSFORGE_STATE_DIR`, then a private `/var/lib/sysforge` constant) instead of
  through `paths.resolve_state_dir`, the one home for it and the place `3.5.0-B134` provisions
  the `sysforge` group and the per-user ACL that opens the directory before the group applies at
  the next login. So when `artifact adopt` (or any registry write) is the first thing a user runs
  after installing, before a `build` has provisioned the directory, it ends in a Python
  traceback: `PermissionError: … /var/lib/sysforge/.artifacts.toml.*.tmp`. Found by the
  `3.5.0-DEV23` end-to-end scenario `85-artifacts` in its own container; the scenario now builds
  first, because in CI's shared system an earlier scenario has always provisioned the directory,
  so the tier cannot reproduce a first-session bug past its first scenario and the fix needs a
  unit test (a registry write as a user the directory is closed to). Fix: resolve
  through `paths.resolve_state_dir` (provisioning on write), and check the other private copies of
  the default path for the same gap: `build_sandbox.py` (local repo), `init_notice.py` and
  `stage_sentinel.py`, keeping any that are deliberately read-only.
  *Priority: med · Effort: small · Bump: patch* — a traceback on a documented command in the
  first session, the one `3.5.0-B134` fixed for `build` and `update`.

### Developer tooling

### Open questions
