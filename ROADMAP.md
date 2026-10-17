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
| `3.5.0-B13` | toolchain PKGBUILD resolution prefers a stale per-package checkout over the pkgbase that now builds it | high | small | patch |
| `3.5.0-B14` | LLVM target filtering breaks stock libLLVM consumers that initialize all targets | high | medium | patch |
| `3.5.0-F2` | rebuild the PGO toolchain around upstream's single llvm pkgbase | high | large | minor |
<!-- END roadmap-table -->

### Features

- **`3.5.0-F2` — rebuild the PGO toolchain around upstream's single `llvm` pkgbase.** Since
  23.1.3-2, upstream builds clang, lld, lldb, polly, compiler-rt, openmp, offload, libc++, libclc,
  the WASI runtimes and (since -3) `llvm-bolt` from the one `llvm` PKGBUILD: one CMake tree
  (`LLVM_ENABLE_PROJECTS` + `LLVM_ENABLE_RUNTIMES`), one `ninja -C build`, now 18 split packages
  (-4 split `clang-libs` out of `clang` the day after -3 added BOLT, so the set is still moving).
  The separate `clang.git`/`lld.git`/… packaging repos stopped at 23.1.1-1. The four-pass PGO
  sequence assumes the old layout (`toolchain/pgo.py`): Pass 1 instruments `llvm`/`llvm-libs`,
  Pass 2 builds the `non_pgo` suite against stage1, and Pass 4 splits into 4a/4b/4c with
  `patch_llvm_dir` and `CMAKE_PREFIX_PATH` so clang links the libLLVM that ships. So `run
  toolchain` with `compiler = "llvm"` cannot complete today (resolution also fails first, see
  `3.5.0-B13`). In the merged layout Pass 1 builds the whole tree instrumented (clang included,
  so `libclang-cpp` gets `-fprofile-use` for the first time; today only libLLVM does), Pass 2
  goes away, Pass 3 trains, and Pass 4 builds once; the ABI-coherence workarounds are needed
  only for the remaining separate pkgbase, `spirv-llvm-translator`, which is now also an llvm
  makedepend. Work: detect the layout from the `llvm` PKGBUILD's `pkgname` (keep the split path
  for one cycle for older pins, then drop it); derive the package set from that `pkgname` rather
  than `DEFAULT_LLVM_NON_PGO`, `toolchain_preflight`'s suite list and the `toolchain_safety`
  sets; install every *installed* member of the pkgbase so none is left on the old version
  (today `lldb` and `libclc` float as stock 23.1.3 against the sysforge 23.1.1 libLLVM);
  carry the set through stage ownership and the reuse fingerprints. Optional, but each pass is
  much longer without it: trim lldb/libc++/offload device runtimes/libclc/WASI from the
  instrumented and training passes via `pkgbuild_patcher` (CMake args + `pkgname`). Rejected:
  LLVM's in-tree `CLANG_ENABLE_BOOTSTRAP` PGO cache, which renames every install target to
  `stage2-install-*` (breaking all of the PKGBUILD's `package_*()`) and drops profdata reuse, the
  mesa training corpus and the confirm gates. Tests: layout detection both ways; merged-layout
  pass plan (no Pass 2, single Pass 4, installed-member set); split layout unchanged.
  *Priority: high · Effort: large · Bump: minor* — high because the LLVM PGO toolchain cannot be
  rebuilt or updated at all until this lands; large because it reshapes the pass sequence and
  every package-set consumer; minor because the default package lists change shape.

### Bugs

- **`3.5.0-B13` — toolchain PKGBUILD resolution prefers a stale per-package checkout over the
  pkgbase that now builds it.** `toolchain/pkgbuilds.py` resolves each name by looking for
  `<pkgbuild_src_dir>/<name>/PKGBUILD` first (Pass 1) and only then scans resolved PKGBUILDs'
  `pkgname` arrays for split packages (Pass 2). After upstream moved clang/lld/polly/compiler-rt/
  openmp into the `llvm` pkgbase, the old `~/src/clang`-style checkouts still exist, so each
  resolves to its frozen 23.1.1 tree and is then pinned to the repo's 23.1.3 release, which fails
  with `pkgctl repo switch '23.1.3-2' failed: pathspec did not match` for five packages. Resolve
  the pkgbase from the sync DB (`%BASE%`) before the local lookup, so a name whose repo pkgbase
  differs maps to that pkgbase's directory, and warn once that the old checkout is stale. Fixing
  this alone does not make the build succeed (`3.5.0-F2` reshapes the passes), but it turns five
  confusing pin failures into one correct resolution. Tests: a name whose sync-DB pkgbase differs
  resolves to the pkgbase dir even when `<name>/PKGBUILD` exists; a name with no DB entry keeps
  today's order.
  *Priority: high · Effort: small · Bump: patch* — high because every `run toolchain` on the LLVM
  path fails here today; small because it is one lookup ahead of an existing one.

---

- **`3.5.0-B14` — LLVM target filtering breaks stock libLLVM consumers that initialize all
  targets.** With `[llvm] targets = ["X86", "AMDGPU", "NVPTX"]` the system libLLVM omits 17
  backends, and four installed stock packages reference `LLVMInitialize<Target>*` for all of
  them: `lldb` (fails to start: `symbol lookup error ... LLVMInitializeWebAssemblyAsmPrinter`),
  `bpf` (bpftool), `opencl-mesa` and `faust`. These call `InitializeAll*()`, so they need every
  target, not a hardware-derived subset. `llvm_targets._ensure_system_consumer_targets` protects
  only mesa's AMDGPU (`SYSTEM_LIBLLVM_CONSUMER_TARGETS`), and
  `toolchain_safety.check_system_consumer_symbols` checks only that case, so nothing caught it;
  it has been live since the 2026-10-05 toolchain install. Replace the hard-coded consumer with
  a scan: every installed reverse dependency of `llvm-libs`, every ELF file, every undefined
  `LLVMInitialize*` symbol at the LLVM version node; derive the target set they need. Then
  either add those targets (and log which consumer required them) or refuse to filter, and
  wire the same scan into the post-install symbol gate so a consumer installed later is caught
  by `doctor`. Workaround until then: `[llvm] targets = []`. Tests: a fake consumer requiring a
  filtered target adds it (or refuses); the AMDGPU/mesa case still holds; an unrelated
  `LLVMInitialize*` outside the version node is ignored.
  *Priority: high · Effort: medium · Bump: patch* — high because a debugger, bpftool and
  OpenCL fail to load on a system that applied the filter; medium because it replaces a
  constant with a package scan in two places.

### Open questions
