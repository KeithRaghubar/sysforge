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
| `4.0.0-B5` | a real drop-in in a batch auto-confirms every other conflict in it | med | small | patch |
| `3.5.0-DEV29` | the build sandbox end to end, in the VM tier | med | large | patch |
| `3.5.0-DEV33` | an end-to-end scenario for unattended runs | low | small | patch |
| `3.5.0-DEV34` | an end-to-end scenario for the --frozen and --thaw flags | low | small | patch |
| `3.5.0-DEV26` | a release-time run of the real toolchain → kernel chain on real hardware | low | medium | patch |
| `3.5.0-DEV32` | an end-to-end scenario for package PGO (build --pgo) | low | medium | patch |
| `3.5.0-F24` | bench package <pkg>: time an installed package's build with the stock and the sysforge clang | low | medium | minor |
<!-- END roadmap-table -->

### Features

- **`3.5.0-F24` — `bench package <pkg>`: time an installed package's build with the stock and the
  sysforge clang.** `bench toolchain` times the compiler on a fixed workload; a user who builds the
  toolchain to get faster package builds has no answer for the package they care about (the
  kernel, mesa). A new `bench` target builds one installed package's `build()` with its **resolved
  profile**, twice per round, changing only the compiler: the stock `clang` + `llvm-libs`
  (+ `clang-libs`) from `bench_core._fetch_stock`, against the installed sysforge build, interleaved
  ABBA through `run_interleaved`, recorded in `bench.toml` keyed by package. Same preconditions as
  `bench toolchain` (a sysforge-built clang whose `pkgver-pkgrel` matches the stock one, so every
  profile flag, `-fprofile-use` included, means the same to both); a package whose profile resolves
  to gcc exits 2, since there is no clang to compare. Design points the spec has to settle: the
  source is extracted and `prepare()`d once into a pristine copy that each timed run starts from
  (untimed copy), since a second `build()` in a built tree is incremental; only `build()` is timed
  (`--noarchive --nocheck`, no download or compression); ccache and `RUSTC_WRAPPER` are off for both
  sides; the stock compiler gets its `LD_LIBRARY_PATH` through a per-compiler wrapper, not the build
  env, so cmake, meson and rustc keep loading the host's libraries; under `sandbox_builds` the stock
  prefix is bind-mounted read-only into the chroot (or the target refuses, in a first cut); the
  default `--runs` is lower than `bench toolchain`'s, since each run is a full build. Unlike
  `bench toolchain` it runs the PKGBUILD's `build()`, so it carries `sysforge build`'s trust, though
  it installs nothing. Kernel-mode packages (`build_mode = "kernel"`) are in scope and build with
  the kernel stage's flags. A package where clang is not the hot path (Rust, Go) reports its ~1.00x
  as measured. Out of scope: whether the built package *runs* faster, which needs a reboot or a
  workload of the user's choosing (as `kernel-bench.sh` is today). No pass/fail threshold, since a
  cutoff tuned on one machine means nothing on another. Promoted from `3.5.0-Q1`.
  *Priority: low · Effort: medium · Bump: minor* — a new verb that reuses the stock fetch, harness
  and record; the pristine-tree reset and wrapper are the new work.

---

### Bugs

- **`4.0.0-B5` — a real drop-in in a batch auto-confirms every other conflict in it.**
  `pacman.batch_install_pkgs` adds `--ask=4` when any built package supersedes an installed one
  (`pkg_supersedes_installed`: `replaces`, or `conflicts` plus `provides` naming it). pacman
  applies that answer to every `ALPM_QUESTION_CONFLICT_PKG` in the transaction, not only the
  drop-in's own. So an `update` that rebuilds `wayland-git` over `wayland` in the same batch as a
  package that merely conflicts with something installed removes that package without asking,
  where the conflict alone would have stopped the install. `4.0.0-B4` closed the case where the
  "drop-in" was a package naming itself; this is the general one. The fix decides before the
  call: collect every conflict the batch would raise (each built package's `conflict` against the
  installed set, and installed packages' conflicts naming a batch member, minus each package's own
  name and the names the batch reinstalls) and pass `--ask=4` only when all of them are in the
  superseded set. Otherwise leave it off, so pacman answers N and the install stops as it would
  with no drop-in present, naming the unexpected conflict. Splitting the batch (drop-ins in their own
  transaction) was the alternative, but the toolchain rollback relies on
  `batch_install_pkgs` being all-or-nothing. Unit tests over a mixed batch; the e2e tier needs
  a stand-in pair that conflicts and provides, beside `e2e-hello-alt` in `45-interactive`.
  *Priority: med · Effort: small · Bump: patch* — silently removes a user's package, but only when
  a drop-in and an unrelated conflict land in one batch; the check is local to one function.

---

### Developer tooling

- **`3.5.0-DEV26` — a release-time run of the real toolchain → kernel chain on real hardware.**
  `96-toolchain-kernel-handoff` checks the handoff with a registered clang; what only a real build
  shows is that the kernel is compiled by the toolchain sysforge just built and is recorded that
  way. A script under `tools/`
  (and a Make target, with a step in `docs/RELEASE-CHECKLIST.md` for minor and major releases) runs
  `run toolchain` then `run kernel` on the workstation, and asserts on provenance, never on speed:
  the built `vmlinux`'s `.comment` section names the clang version `build_state.toml` recorded for
  the toolchain; the kernel's `build_state` entry carries that toolchain's variant (`pgo_llvm`); and
  `update --dry-run` reports no toolchain drift for it. Then the drift leg: with a stock clang
  installed from the pacman cache, `update --dry-run` reports the kernel as drifted, and the script
  puts the sysforge toolchain back from its cached archives (`run toolchain --install-only`) before
  it exits, on success or failure. Not CI and not the VM tier: the last PGO toolchain build here took
  about 3.2 hours, and the VM (4 cores, 4 GiB, `VM_DISK_SIZE` 40G) cannot link libLLVM, though its
  real bootloader would suit a kernel-only leg if one is wanted later. Kernel half of the rubric row
  "state one command leaves for the next" (§Release Process).
  *Priority: low · Effort: medium · Bump: patch* — hours long and run by hand, so a release
  checklist item; it catches a chain the CI scenario can only stand in for.

---

- **`3.5.0-DEV29` — the build sandbox end to end, in the VM tier.** `[security] sandbox_builds = true`
  (clean chroot, local repo) is how every build on the maintainer's system runs, about ten 3.5.0
  fixes were sandbox bugs, and no scenario enables it. It cannot run in the container tier:
  `makechrootpkg` needs `systemd-nspawn`, which an unprivileged podman or Actions container cannot
  start. The VM has full systemd, so the e2e scenarios gain a sandbox mode run there
  (`tools/e2e/run.sh --here` inside the VM, with `sandbox_builds` on): at least an AUR build, a build
  whose dependency is a sysforge build served from the local repo, and a `-git` build, asserting the
  package came out of the chroot and that `build_state.toml` records the sandbox toolchain. A
  release-checklist step, beside `make vm-test`.
  *Priority: med · Effort: large · Bump: patch* — the default build path on the maintainer's system is untested end to end, but it needs VM plumbing.

---

- **`3.5.0-DEV32` — an end-to-end scenario for package PGO (`build --pgo`).** `docs/guides/pgo.md`
  documents an instrumented build, a training run and a rebuild with the profile; the e2e tier runs
  none of it. A stand-in C package whose program has a hot path the training run exercises: `build
  --pgo` (instrument), run it, rebuild with the profile, then assert the profile store holds its data,
  `state profiles` lists it, and the second build used it (the recorded flags). Asserts on what was
  recorded and used, not on speed.
  *Priority: low · Effort: medium · Bump: patch* — a documented guide path, used occasionally; the stand-in package is the work.

---

- **`3.5.0-DEV33` — an end-to-end scenario for unattended runs.** `--unattended` and `update --yes`
  (`3.5.0-F20`) are for timers and scripts: they must never block on a prompt and must refuse what
  needs consent instead of assuming it. The scenario runs `update --unattended` with stdin closed and
  no terminal, against a state that would prompt (a repo package awaiting opt-in, a PKGBUILD change
  awaiting review), and asserts it exits within a timeout, installs nothing it needed consent for, and
  says why; then `uninstall --yes` completes without a terminal.
  *Priority: low · Effort: small · Bump: patch* — new and documented in the kernel guide; the maintainer runs no timers, so lower value here.

---

- **`3.5.0-DEV34` — an end-to-end scenario for the `--frozen` and `--thaw` flags.** `90-source-policy`
  covers `freeze_sources` in config but not the flags `docs/guides/freeze.md` documents for a single
  run. Add to it: `--frozen` refuses a clone with the config off, `--no-frozen` lets one through with
  it on, and `--thaw` behaves as the guide says, each asserting no checkout appears when refused.
  *Priority: low · Effort: small · Bump: patch* — documented flags on an already-covered command; a few lines in an existing scenario.

---

### Open questions
