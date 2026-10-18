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
| `3.5.0-F4` | rebuild a rust-using libLLVM consumer (mesa) against the host's own libLLVM | med | large | minor |
| `3.5.0-B20` | the consumer symbol gate inspects lockstep packages the same transaction replaces | low | small | patch |
| `3.5.0-B21` | the post-build ABI check also inspects older builds left in PKGDEST | low | small | patch |
<!-- END roadmap-table -->

### Features

- **`3.5.0-F4` — rebuild a rust-using libLLVM consumer (mesa) against the host's own
  libLLVM.** A sandboxed build whose dependencies pull Arch's `rust` gets the *repo* LLVM in
  the container (`build_sandbox.install_args` / `needs_repo_rust`), because that rustc only
  runs against the repo libLLVM. The consumer then links the repo libLLVM too: mesa's
  `libgallium`, `libRusticlOpenCL` and `libvulkan_lvp` bind `std::string` members such as
  `_M_replace`/`_M_assign` to `@LLVM_23.1`, which the PGO libLLVM does not export, so
  `_refuse_skewed_install` correctly refuses to install it (seen 2026-10-17, mesa 26.2.5). As a
  result the toolchain stage's consumer rebuild can never heal a *real* std:: re-export drift in
  mesa, the one consumer where a miss black-screens the desktop; the stage's only recourse is
  the manual restore. Two ways out: (a) give the container a rust that carries its own LLVM
  (rustup's `librustc_driver` links LLVM statically; the host already uses rustup, which
  `provides=rust`) so the host libLLVM can stay in the container; or (b) make the PGO libLLVM
  keep exporting the stock build's `std::` re-exports (diff the stock `llvm-libs` export set
  against the build's, and compile the missing instantiations into libLLVM under the same
  version node), which makes it a drop-in replacement for every repo-linked consumer and
  retires same-soname drift rebuilds altogether. (b) is the larger change but the root fix; (a)
  needs a toolchain download inside the container, which the offline/clean-chroot model does
  not have today. Tests: (a) the container set for a `rust`-pulling target keeps the host
  libLLVM when a self-contained rust is available; (b) a built libLLVM's export set is a
  superset of the stock one's `_ZNSt*@LLVM_*` symbols, and Gate 2 finds no drift against a
  stock-linked consumer.
  *Priority: med · Effort: large · Bump: minor* — med because nothing is broken today (the
  install gate refuses the bad mesa), but the self-heal path the stage relies on for mesa
  cannot work.

---

### Bugs

- **`3.5.0-B20` — the consumer symbol gate inspects lockstep packages the same transaction
  replaces.** `toolchain_safety._consumer_candidates` excludes `LLVM_LOCKSTEP_SUITE`
  (llvm, llvm-libs, clang, lld, compiler-rt, polly, openmp), but since `3.5.0-F2` the
  stage also builds and installs `lldb`, `libclc` and `llvm-lit` from the `llvm` pkgbase. Gate
  2 therefore diffs the *installed* lldb (about to be replaced) against the new libLLVM, and a
  miss there would queue a needless rebuild or, for a target-init symbol, abort an install that
  would have fixed it. `resolve_abi_consumers_to_rebuild` already excludes the batch's own
  names (`all_names`); the candidate scan should exclude every pkgname the built batch ships,
  pre-install, and every pkgname the stage just installed, post-install. Seen 2026-10-17
  (masked then by the `3.5.0-B18` false positive). Tests: an installed package whose name the
  batch ships is not diffed; one the batch does not ship still is.
  *Priority: low · Effort: small · Bump: patch* — latent: only bites if a replaced suite
  member would genuinely miss a symbol.

---

- **`3.5.0-B21` — the post-build ABI check also inspects older builds left in PKGDEST.**
  After mesa 26.2.5 built in the sandbox, `report_post_build_abi` checked the 26.2.4
  artifacts of the same pkgnames too (`vulkan-gfxstream-1:26.2.4`, `mesa-docs-1:26.2.4`, …),
  logging findings for packages this build did not produce. `_artifacts_for_pkgbuild` scopes by
  the `makepkg --packagelist` manifest when one exists and otherwise falls back to pkgname
  only, ignoring version; the sandboxed path evidently reaches the fallback. Either write the
  manifest on the sandboxed path too, or have the fallback keep only the PKGBUILD's own
  `epoch:pkgver-pkgrel`. A stale finding can also mislead the install gate if it reads the same
  list. Tests: with two versions of a pkgname in PKGDEST, only the PKGBUILD's version is
  returned; the sandboxed build leaves a manifest the scoping uses.
  *Priority: low · Effort: small · Bump: patch* — noise today; wrong-file findings at worst.

---

### Open questions
