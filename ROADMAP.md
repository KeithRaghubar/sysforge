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
| `3.5.0-B140` | sysforge.toml [failure_handling] is never read | med | medium | patch |
| `3.5.0-DEV23` | widen the end-to-end tier beyond build, update and setup | med | large | patch |
<!-- END roadmap-table -->

### Features

### Bugs

- **`3.5.0-B140` — `sysforge.toml [failure_handling]` is never read.**
  The shipped `sysforge.toml` documents `[failure_handling]` and `config_schema.KNOWN_SECTIONS`
  lists it there, but both readers take it from the `profiles.toml` dict:
  `makepkg_wrapper.run` calls `load_config()` (the profiles loader) and passes that `config` to
  the auto-repair driver (`_failure_cfg`, the `srcinfo_drift` preflight and `apply_first_match`),
  to `failure.handle_failure` for `pkgbuild_unparseable` and `tempfile_write_failed`, and to
  `dep_analysis.run_dep_analysis` for `abi_mismatch` and `makedep_probe_failed`. Editing the
  documented section therefore changes nothing; the same table in `profiles.toml` would be
  honoured, but the schema calls it an unknown section there. Today's behaviour is the hardcoded
  defaults, which happen to be the safe ones. Three further faults sit in the same seam:
  (1) **two vocabularies, two fallbacks.** The four auto-repair keys take
  `auto_repair`/`auto_repair_with_warning`/`prompt_user`/… and an unknown value falls back to
  `auto_repair` (`_normalise_behaviour`), so a typo in `checksum_mismatch` selects the
  silent-repair behaviour the shipped comment says must never be relaxed. The `failure.py`
  scenarios take only `abort`/`error`/`warn_and_fallback`/`fallback`, and an unknown value
  aborts. The shipped comment lists all seven values for every key. (2) **the
  `checksum_mismatch` guard is not where the config says.** It is safe only because
  `_repair_checksum_mismatch` always prompts, whatever the behaviour. Under `auto_repair` the
  `batch` short-circuit (which only checks for `prompt_user`) is skipped, so a `batch = true`
  profile run at a terminal still stops to ask. (3) **undocumented and dead scenarios.**
  `_FAILURE_DEFAULTS` has nine keys; four have callers (`pkgbuild_unparseable`, `abi_mismatch`,
  `makedep_probe_failed`, and `tempfile_write_failed`, which always aborts), and none of the four
  is in the shipped file. `no_rule_matched`, `profile_missing`, `profile_cycle`, `env_conflict`
  and `dep_unsatisfied` are never raised. `failure_handling` is also still in
  `profile.SYSFORGE_KEYS`, left from when it was a per-profile key. Fix: one resolver in
  `primitives/config.py` reads the table from `load_sysforge_toml()` and validates each key against
  its own vocabulary (an unknown value takes that scenario's safe default, never `auto_repair`);
  pass it to all three readers; make `checksum_mismatch` refuse anything but `prompt_user` or
  `abort`; document the four live `failure.py` keys and each key's values in `sysforge.toml`; drop
  the dead defaults and the `SYSFORGE_KEYS` entry. Found while auditing the shipped configs on
  2026-10-24 (`3.5.0-DOC15`).
  *Priority: med · Effort: medium · Bump: patch* — a documented, security-flagged setting that
  silently does nothing; the defaults keep it safe today, but any edit is ignored and a typo in the
  right file would weaken the checksum guard.

### Developer tooling

- **`3.5.0-DEV23` — widen the end-to-end tier beyond `build`, `update` and setup.**
  The tier (`3.5.0-DEV21`, `tools/e2e/`) has four scenarios that call 4 of the 41 commands in
  `tests/data/surface.json` (`build`, `update`, `run reconfigure`, `run hardware`), all on the
  shipped config except one `profiles.toml` edit and the `packages.toml` opt-in write. Add
  scenarios, local `pkgs/` packages and no network wherever possible, in this order: (1) the
  mutating commands that cross the user/root seam the tier exists for: `uninstall`,
  `revert-to-stock`, `state forget`, `packages add`/`remove`/`add-group`, `config reset`/`clean`,
  `artifact adopt`/`deploy`; (2) read-only commands against the state the earlier scenarios
  leave: `state list`/`failed`/`orphans`, `doctor system`/`pkg`, `config check`, `resolve`, `log`;
  (3) `run toolchain` and `run kernel` as `--dry-run` passes with non-default `toolchain.toml`
  and `kernel.toml` (a real run needs hours or a reboot), plus `sysforge.toml` keys that change a
  path a scenario takes (`[security] freeze_sources`, `[aur] enabled = false`). Pair it with a
  coverage check that lists the commands no scenario calls, so a new verb shows up as uncovered
  instead of silently absent. Separately, decide whether `e2e` becomes a required status check
  beside `gates` on the `main` ruleset; today a red run does not block a merge, and
  `10-quickstart` is networked, so requiring it puts AUR and mirror outages on the merge path.
  *Priority: med · Effort: large · Bump: patch* — most commands have no test of their real
  root/user, terminal and cross-command behaviour, the seams where the 2026-10-23 bugs lived.

### Open questions
