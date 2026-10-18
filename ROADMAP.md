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

**`DOC` covers user-facing documentation only** — guides under `docs/guides/`,
`README.md`, the man page — so doc work stays visible in release notes without
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
| `3.5.0-DOC3` | concept page: profile-guided and post-link optimization (pilot) | med | medium | patch |
| `3.5.0-DOC4` | concept page: shared libraries and ABI | med | medium | patch |
| `3.5.0-DOC5` | concept page: how an Arch package is built | med | medium | patch |
| `3.5.0-DOC6` | concept page: compiler optimization basics | med | medium | patch |
| `3.5.0-DOC9` | concept page: measuring performance honestly | low | small | patch |
| `3.5.0-DOC10` | glossary | low | small | patch |
| `3.5.0-DOC7` | concept page: bootstrapping an optimized compiler | low | medium | patch |
| `3.5.0-DOC8` | concept page: kernel builds and boot basics | low | medium | patch |
<!-- END roadmap-table -->

### Features

### Bugs

### Documentation

The user docs explain tasks; these items add the general concepts behind them, so the docs
teach how the techniques work beyond sysforge. Shared shape for every page under
`docs/concepts/`: explain how it works and when it is (and isn't) worth doing, without
sysforge vocabulary; cite upstream sources (Arch wiki, LLVM, GCC, kernel docs); quote no
performance numbers measured on one machine (upstream figures, or how to measure); end with a
short "In sysforge" section linking the guide. Guides link the concept pages instead of
re-explaining them, and the README lists the section beside the guides.

- **`3.5.0-DOC3` — concept page: profile-guided and post-link optimization (pilot).** Start
  `docs/concepts/` with the topic people search for most. Instrumented PGO vs. sampled profiles
  (AutoFDO), Propeller, BOLT; what each changes (inlining and block layout vs. function and
  block order in the binary); why the training workload decides the result; profile staleness;
  when none of them pay off. Move pgo.md's "The idea in one minute" and the method-choice
  background here, leaving pgo.md the tasks. Sets the section's layout, the README link and the
  conventions above for the pages after it.
  *Priority: med · Effort: medium · Bump: patch*

- **`3.5.0-DOC4` — concept page: shared libraries and ABI.** Sonames, symbol versioning, what
  "ABI-compatible" means, why a library's soname bump forces its consumers to rebuild, and how
  C++ standard-library symbols leak through another library's exports, using libLLVM's std::
  re-exports as the worked case. drift.md and the toolchain guide's audit sections link it.
  *Priority: med · Effort: medium · Bump: patch*

- **`3.5.0-DOC5` — concept page: how an Arch package is built.** PKGBUILD and its arrays,
  makepkg and `makepkg.conf`, pkgbase vs. split packages, BUILDDIR and PKGDEST, `.SRCINFO`,
  `-git` packages and pkgver(), source signatures and checksums. The base most other pages
  assume.
  *Priority: med · Effort: medium · Bump: patch*

- **`3.5.0-DOC6` — concept page: compiler optimization basics.** What `-O2`/`-O3` change,
  `-march=native` and the portability it gives up, full vs. thin LTO, why the linker matters
  (lld, `-fno-plt`), and what the hardening flags cost and buy. Profiles in the README and
  `profiles.toml` comments link it.
  *Priority: med · Effort: medium · Bump: patch*

- **`3.5.0-DOC7` — concept page: bootstrapping an optimized compiler.** Why building a compiler
  with itself takes stages, what instrumented, training and optimized passes each produce, why
  the live toolchain must never be half-replaced, and how a PGO+BOLT build differs from a plain
  stage-2 bootstrap. The toolchain section of pgo.md links it.
  *Priority: low · Effort: medium · Bump: patch*

- **`3.5.0-DOC8` — concept page: kernel builds and boot basics.** Kconfig and
  `olddefconfig`, modules vs. built-in, initramfs, boot loaders and entries (systemd-boot
  sort-key vs. filename order), and why a fallback kernel matters. kernel.md and
  boot-entries.md link it.
  *Priority: low · Effort: medium · Bump: patch*

- **`3.5.0-DOC9` — concept page: measuring performance honestly.** Run-to-run noise, warm
  vs. cold caches, repeated runs and spread, what a before/after comparison needs to hold
  fixed, and `perf` basics. pgo.md's "Did it help?" and `bench toolchain` docs link it.
  *Priority: low · Effort: small · Bump: patch*

- **`3.5.0-DOC10` — glossary.** One page defining the terms the docs use (pkgbase, split
  package, PKGDEST, soname, LTO, profdata, training workload, pass, sentinel, drift), each with
  a link to the concept page or guide that covers it, so pages link a term instead of
  re-explaining it. Grows with each concept page.
  *Priority: low · Effort: small · Bump: patch*

### Open questions
