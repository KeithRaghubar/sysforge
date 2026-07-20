# SysForge Roadmap

Planned features/changes and the rationale for purposely-excluded or abandoned
ideas. This is the single tracked home for forward-looking work; **`DESIGN.md`
describes only implemented design** and never carries roadmap IDs.

**Shipped work is not recorded here** — it lives in `docs/release-notes/` and git
history (the commit that lands an item is its record).

## ID scheme

IDs are `<version>-<TYPE><n>`, e.g. `1.2.0-F1` (feature), `1.2.0-B1` (bug),
`1.2.0-Q1` (open question), `1.2.0-STD1` (standards). The version prefix is the
current `pyproject.toml` version. The per-type counter **resets to 1 only on a
major or minor version bump** (`X.Y.Z` → `(X+1).0.0` / `X.(Y+1).0`), never on a
patch bump — i.e. the counter is scoped to the minor-release cycle and stays
monotonic across patch releases within it. The version prefix keeps IDs globally
unique and records the cycle an item originated in. An item still open at release
time keeps its existing ID
(it records the cycle the item originated in, not its target). IDs appear only here
and in release notes.

**Never hand-pick an ID — derive it.** The open items above keep their origin-cycle
prefixes, so eyeballing a neighbour gives the wrong cycle (and the wrong counter)
right after a release. Run `make next-id TYPE=F` (or `B`/`Q`/`STD`) — it reads the
current `pyproject.toml` version, scopes to that cycle's counter, and prints the next
free ID (e.g. `2.4.0-F1`). `make check-standards` also flags collisions and
active-cycle sequence gaps.

Within each subsection, entries are kept in **ascending ID order** (by type
counter, then version) — sort on every add so the list stays scannable.

**Open questions (`Q`) must be resolved before any implementation.** A `Q`
entry is undecided by definition; investigation/spikes to inform the decision
are fine, but before writing production code the question must first be either
**promoted** to a proper `F`/`B`/`STD` entry (which then follows the normal
landing flow) or moved to **Abandoned** with a rationale. Never implement
straight off a `Q`.

---

## Planned

### Bugs

- **`2.3.0-B2` — `--cache-report` renderer bypasses the logger.** `cache_probe.emit_session_report`
  writes with raw `print(..., file=sys.stderr)` instead of routing through `sysforge.log`. Three
  consequences: (1) the report is **not captured by the unified run-log** (`log.ui`/`info` fan out to
  `_write_to_files` → `_unified_log_fh`/`_pkg_log_fh`; a bare `print` does not), so `sysforge log`
  captures silently omit it; (2) its `─` divider hardcodes the box-drawing glyph, **bypassing
  `log.use_unicode()`/`downgrade_glyphs`** — under `NO_UNICODE`/`--ascii` it still emits U+2500,
  exactly what the glyph gate exists to catch; (3) it hand-rolls the `[SYSFORGE][CACHE]` prefix
  instead of `_format_line`, so it misses `use_color()` gating and any future prefix change. Almost
  certainly age, not intent — `cache_probe.py` predates both the unified run-log and the Unicode gate
  and never got swept into either migration. Fix: route the report through `log.ui`/`info` (+ the
  unicode gate) like the diagnostics renderer (`render_axis`) already does. *Priority: low (cosmetic +
  run-log completeness; no functional build impact).*

- **`2.2.0-B3` — `check-shipped` manpage guard couples to the local scdoc version.**
  The `manpage` check in `tools/check_shipped.py` asserts that committed `man/sysforge.1`
  byte-matches fresh `make man` output on the current machine. But `make man` runs `scdoc`,
  whose rendering changes between versions (e.g. 1.11.4→1.11.5 re-escaped every `-` as `\-`
  and re-stamped the `.TH` date), so the committed artifact is silently pinned to whichever
  scdoc version last regenerated it. A contributor (or CI) on a different scdoc version sees
  the inverse reflow — hundreds of cosmetic `\-`↔`-` lines — and the guard fails on changes
  they never made, conflating "the CLI surface changed" with "the renderer version changed".
  The escaping is functionally inert (roff renders `\-` and `-` identically). Fix: normalize
  the comparison in `check_shipped.py` — strip the generator/date comment line and canonicalize
  hyphen escaping before diffing — so the guard verifies man-page *content* (does it match the
  argparse tree) rather than scdoc-version-specific byte output. *Priority: low (cosmetic churn
  + contributor/CI friction; no runtime or rendered-output impact).*

### Features

- **`2.4.0-F1` — Journal `SYSFORGE_TARGET` for the other mutating verbs (follow-up to `2.3.0-F6`).**
  The journald mirror emits `SYSFORGE_TARGET` only for `build`, because `build` is the only verb that
  overrides `Verb.journal_target` (the others fall to the `None` default and emit the verb name alone).
  But `uninstall`, `revert`, and `state` are all sentinel-gated and carry a meaningful package-name
  subject, so `journalctl SYSFORGE_TARGET=<pkg>` misses the operations most worth correlating during
  incident review. Add a `journal_target` override on each — deriving the target from the verb's own
  args (uninstall/revert package name; `state` subcommand target) — mirroring the `build` override. No
  new seam: `record_verb`/runner emission are unchanged; this only widens which verbs supply a target.
  *Priority: low (observability completeness; additive, no correctness impact).* **Standards home on
  adoption:** extend row 20 (`systemd.journal-fields(7)`) scope note — same mechanism, more verbs
  supplying a target; per-verb tests alongside the existing `test_journal.py` hook tests.

- **`2.4.0-F3` — Doctor probe: report which Rust toolchain a build will actually use.** Rust
  PKGBUILDs declare `makedepends=(cargo|rust)` and invoke bare `cargo`, so the toolchain is decided
  entirely by what owns `/usr/bin/cargo` — the distro `rust` package, or a `rustup` proxy that
  resolves through the user's *default* toolchain. On a workstation whose rustup default is
  `nightly`, every Rust package silently builds under nightly, which is neither what the packager
  tested nor what the user intended. A tree-level `rust-toolchain.toml` overrides both and makes
  rustup *download* a pinned toolchain mid-build, turning an apparent local build into a network
  fetch. All three cases are invisible today. Add a read-only `doctor` probe that resolves the
  `cargo`/`rustc` owner (`pacman -Qo`, or the alpm seam), reports the effective toolchain
  (`rustup show active-toolchain` when rustup owns it, else the `rust` package version), and — when
  given a PKGBUILD dir — flags a `rust-toolchain.toml` pin plus whether that toolchain is already
  installed. Advisory only: report the mismatch, never rewrite the pin (patching a pin builds a
  package against a toolchain its authors never tested, trading reproducibility for nothing).
  Read-only, so no sentinel; subprocess calls go through the existing seam. *Priority: low
  (diagnostic clarity, not a correctness gap — but the nightly-default case is live on this
  workstation).* **Standards home on adoption:** none new — `rust-toolchain.toml` is a rustup
  convention, not a ratified spec; add a row only if the probe grows a documented external contract.

- **`2.4.0-F7` — Doctor probe: `pacman -Qkk` package-file verification (spun out of `1.2.0-F28`).**
  The artifact inventory (`2.4.0-F4`–`F6`) deliberately excludes package-owned files at discovery
  (`pacman.owners_of()`) — it only ever manages user-authored content. The complement — verifying
  that files pacman *does* own still match what the package declared (existence, hash/size where
  recorded, mode/ownership) — is a distinct, standalone concern: full integrity verification against
  alpm's stored `mtree` via `pacman -Qkk` (quiet `-Qk` catches missing files only; `-Qkk` additionally
  checks properties). Add a read-only `doctor` probe that runs `pacman -Qkk` (optionally scoped to a
  package argument) and surfaces mismatches as findings — modified/missing package-owned files a user
  or a misbehaving install script altered outside pacman's own transaction. Read-only, so no sentinel;
  subprocess calls go through the existing seam. *Priority: low (diagnostic completeness — the
  artifact inventory's population and this probe's population are now proven disjoint and jointly
  exhaustive over "files sysforge might care about").* **Standards home on adoption:** a new row
  covering `pacman -Qkk` / `libalpm` mtree verification as a consumed external contract — none of the
  existing rows (11 is authoring artefacts, not verifying installed ones) fit; add when this lands.

- **`2.3.0-F3` — Dev/build-chain supply-chain audit target.** The runtime dep surface is
  near-empty by design (only the `tomli` backport + optional `pyalpm`), but the dev/build toolchain
  (`hatchling`, `pytest`, `ruff`, `pyright`, coverage overlay) is still a supply-chain surface. Add a
  `make audit` target that runs `pip-audit` (or `uv`'s native auditing) over the resolved lockfile to
  flag known CVEs in the build/test chain, run manually and optionally in `pre-release`. Dev-time
  only — nothing enters the shipped wheel or PKGBUILD. *Priority: low (defense-in-depth; the small
  dep surface keeps real exposure low).*

- **`2.3.0-F4` — Emit package-adjacent operations through alpm transaction hooks.** sysforge already
  ships `primitives/pacman_hooks.py` and optionally reads via `pyalpm`, so it is adjacent to alpm's
  native extension point but does not yet *fire from inside the transaction*. For work that must happen
  around package operations — snapshot triggers, the `1.2.0-F28` artifact-drift check, cache/state
  reconciliation — a `PreTransaction`/`PostTransaction` alpm hook runs at exactly the right moment
  inside pacman's own transaction rather than sysforge polling or wrapping `pacman` externally. This is
  the canonical Arch extension mechanism; hooking it means the trigger is correct-by-construction even
  when the user runs bare `pacman`. Decompose per trigger (not one mega-hook) and keep each behind the
  existing sentinel/verb boundary. *Priority: low (strategic; depends on which triggers earn a hook —
  pair with F28).* **Spec:** `alpm-hooks(5)` — already cited by standards row 11. **Standards home on
  adoption:** extend row 11's Scope ("SysForge *fires* alpm hooks, not just ships them") + enforcement
  pointer, in the landing commit.

- **`2.3.0-F5` — Declarative provisioning via `tmpfiles.d`/`sysusers.d` instead of imperative healing.**
  `fs_provision.py`/`stage_ownership.py` create directories and fix ownership/permissions imperatively.
  The Arch-native mechanism for the same outcome is a shipped `tmpfiles.d` snippet run by
  `systemd-tmpfiles` (idempotent, applied at boot, the packaging-expected path), plus `sysusers.d` for
  any service user. Delegating means the rules are declarative and inspectable, and the healing logic
  becomes "invoke `systemd-tmpfiles --create`" rather than a bespoke walk. Scope to the provisioning that
  is genuinely static (fixed dirs/modes/users); anything computed at runtime stays in the primitive.
  Fallback needed for non-systemd hosts. *Priority: low (simplification + OS-integration; overlaps the
  systemd-run decision in `2.3.0-Q3`).* **Spec:** `tmpfiles.d(5)`, `sysusers.d(5)`, `systemd-tmpfiles(8)`.
  **Standards home on adoption:** extend row 2 (FHS + `file-hierarchy(7)`) — same footprint, declarative
  mechanism; rationale in the `fs_provision` design area.

- **`2.3.0-F8` — Shared known-enum resolver for string-valued config (promoted from `2.3.0-Q2`).**
  Three chokepoints handle unrecognized string values three ways: `config.resolve_repo_mode` returns
  unknowns **through unchanged** (`return raw`, so `repo_mode = "pacmn"` flows downstream
  unvalidated), `config.resolve_repo_track` **silently** coerces to `"stable"`, and the throttle
  `_coerce_*` helpers **warn-and-drop**. Adopt one policy — validate against the known vocabulary,
  warn on mismatch, fall back to the documented default — via a shared
  `resolve_enum(raw, known, default, *, key)` seam. Scope to `repo_mode` + `repo_track` first; the
  enum check must run **after** `repo_mode`'s legacy-alias mapping (`profiled`→`build_from_source`),
  not replace it. Defer any `check_standards` enforcement (a future `STD`) until a census
  (`[log] verbosity`, ionice class names, …) shows enough readers to justify locking it down.
  *Priority: low (robustness/consistency; no correctness failure today — bad values either pass
  through inertly or coerce to a safe default).*

- **`1.2.0-F20` — Rule priority auto-calculation (from the DESIGN roadmap).**
  Auto-calculate a baseline specificity score from rule conditions (mirrors CSS
  specificity: more AND'd conditions = higher weight), with manual `priority`
  override for ties. Deferred until enough real rules exist to validate whether
  auto-priority causes ordering problems in practice. *Priority: low (candidate, not
  a commitment).*

- **`1.2.0-F28` — User-owned artifact inventory: opt-in offering.** The inventory primitive
  itself — tracked-file registry, `USER_DATA_DIR` source-of-truth dir, discovery, and drift
  detection — is landing incrementally under its own `2.4.0-Fn` IDs (see release notes and git
  history as each slice ships); this entry now tracks only its unconsumed remainder. **Sub-thread
  to design: opt-in offering of user-owned systemd services and pacman hooks.** The user keeps
  custom units and hooks on the live system; sysforge should be able to *offer* (not force) them
  rather than relying solely on the user running `artifact` verbs unprompted — open question
  whether the surface is the `setup` stage, a dedicated verb, or a sync mode of the inventory.
  Discuss the UX before committing. **`pacman -Qkk` package-file verification spun out as
  `2.4.0-F7`.** Full drift detection for package-owned files (file integrity/ownership/permission
  verification against alpm's stored `mtree`) was scoped out of this primitive entirely: discovery
  here excludes package-owned files at the outset (`pacman.owners_of()`), so `-Qkk` verification
  operates on the complement of this feature's population and is tracked as its own standalone item,
  not a sub-thread of the (genuinely package-less) user-artifact inventory. *Priority: low
  (strategic — a UX question, not a primitive gap).*

- **`1.2.0-F43` — Logging re-levelling audit for interactive/bootstrap stages.**
  Follow-up to the shipped `1.2.0-F36` slice (configurable `[log] verbosity` + `--quiet`,
  the DESIGN.md §Logging rubric, and the re-levelled day-to-day `build`/`update` path
  under a golden-output test). **Remaining:** sweep the interactive/bootstrap-time stages
  against the same rubric — `reconfigure.py` (94 `ui()`), `configure.py` (47), `kernel.py`
  (42), `toolchain.py` (60), `hardware.py` (19), `partition.py` (14) — demoting progress
  narration to `info()` while keeping prompts/plan-tables/results as `ui()`. Extend the
  golden guard to a representative stage run. *Priority: low (bootstrap-time output, not the
  day-to-day regression, which is resolved under F36).*

---

## Abandoned / decided against

- **`2.4.0-Q1` — machine-readable AI-inclusion disclosure — decided against 2026-07-17.**
  The question was whether to adopt a standardized, machine-readable declaration of AI involvement
  (beyond the existing README prose noting Claude Code usage), for visibility to users who prefer to
  avoid AI-touched code. Investigation found **no ratified standard as of mid-2026**, only three
  competing, still-converging conventions: the `Assisted-by:`/`Generated-by:` git commit trailer
  (the strongest convergence, prescribed by the Linux kernel), the `AI_DISCLOSURE.md` +
  `SPDX-AI-Disclosure:` per-file line-tag convention (W3C AI Content Disclosure vocab × SPDX line-tag
  format), and the `AI-DECLARATION.md` + README-badge approach. Decision: **wait for convergence
  rather than bet on one.** Adopting any of these = a Standards-table row committing sysforge to an
  external spec; picking a non-ratified convention risks churning that row (and, for the file-tag
  variant, every source file) when the community settles elsewhere. The existing README prose plus
  the `Co-Authored-By: Claude` commit trailer already give honest, human-readable disclosure. If one
  convention becomes clearly dominant (most likely `Assisted-by:`, given the kernel precedent and the
  Arch/Linux idiom), this reopens as a `STD` naming its target Standards row — not here.

- **`2.2.0-Q1` — build-system cohesion audit — decided against 2026-07-10.** The question was
  whether the kernel, toolchain, and package stages had diverged from a shared build system and
  warranted consolidation. Investigation found the premise doesn't hold: there are two seams, and
  the load-bearing one is already the single home. The **low seam** (`makepkg_wrapper.run` /
  `primitives/makepkg_invoke.py`) — where the real one-home invariants live (flag scrubs, build
  throttle, PGO/FDO/BOLT rename, review gate, recovery menu) — is used by *every* surface: `build`,
  `update`, and all three stages. The **high seam** (`build_core.build_and_install`: dep-resolve →
  batch-order → bulk-install) is used only by `build` and `update`; `packages.py`/`kernel.py`/
  `toolchain.py` call `makepkg_run` directly, but that divergence is **intentional**, not drift:
  `toolchain.py` is a 5-pass staged build with no system-install for passes 1–3 (routing it through
  `build_and_install`'s resolve→build→bulk-install assumption would be wrong), and `kernel.py` is a
  single interactive-by-default package with an `nconfig` pause, post-install steps, and local
  pkgbase rename. The only genuine candidate — `packages.py`'s per-package loop partially
  re-implementing `build_and_install` — is bootstrap-time (not the day-to-day path), carries its own
  stage resume/progress state, and touches the build-state authority, so the net simplification is
  marginal against the risk. If that duplication ever becomes a real maintenance cost it reopens as a
  narrow `F` scoped to the `packages` stage — not a surface-wide audit.

- **`1.2.0-Q11` — proactive kernel driver-class filter — decided against 2026-07-03.**
  The question was whether the kernel stage should *proactively* exclude host-irrelevant
  drivers (deriving `=n` for built-in `=y` options from `hardware_profile`), covering the
  two gaps F37 left: `localmodconfig` touches only unloaded *modules* (`=m`), not built-in
  `=y` options, and its filtering is reactive (keyed off the build machine's loaded module
  set). Decision: **not worth the boot-safety risk for the marginal benefit.** A built-in
  driver compiled into the kernel costs image size and a little build time but is inert at
  runtime; forcing it `=n` from an inferred hardware profile is exactly the kind of
  proactive exclusion that can silently drop a driver the machine needs at *next* boot
  (new hardware, a hotplugged device, a rescue scenario), and the kernel stage's whole
  discipline is that Gate-1/Gate-2 boot-safety stays authoritative. F37's opt-in
  target-sequence plus the accumulating (union) lsmod snapshot already lets a user who
  wants a slimmer kernel opt into `localmodconfig` reactively, which is the safe side of
  the trade. If a concrete boot-size or build-time problem ever motivates revisiting this,
  it would reopen under a new ID — not resume here.

- **`-sysforge` suffix on the PGO-built toolchain — scrapped 2026-06-24.** The PGO
  toolchain keeps installing under stock names (`clang`, `llvm`, `llvm-libs`, …),
  consistent with the CLAUDE.md invariant that the toolchain stage is the in-place
  system replacement and never threads `optimization_build_mode`. The rename would
  have introduced regressions in five exact-pacman-name lookups
  (`_verify_llvm_install`/`_probe_cc` skew arms, `_installed_libllvm_soname` →
  soname-bump gate, BOLT Pass 4a, `collect_llvm_state` provenance) plus a B5 rework,
  for provenance-cosmetic benefit on a default-`enabled=false` path. Not worth the
  risk on the highest-stakes path in the repo.

- **`[env_precedence]` config table — design cancelled.** The original design
  proposed a priority stack (wrapper profile = 100, makepkg.conf = 80, shell
  passthrough = 20, PKGBUILD export = 10) and an `[env_precedence]` TOML table to
  configure it. Superseded by a simpler, more predictable model: build-tool vars
  (`CC`, `CFLAGS`, `LDFLAGS`, etc.) are stripped from the inherited shell env in
  `invoke_makepkg` before makepkg runs — the temp conf is the sole authority for all
  makepkg-managed keys. Shell-env bleed-through is not a configurable priority; it is
  prevented entirely. SysForge bootstrap vars (`SYSFORGE_STATE_DIR`,
  `SYSFORGE_CONFIG_DIR`) are exempt (SysForge's own interface, not build-tool vars).
  The `[env_precedence]` table will not be implemented. (The env-stripping model that
  replaced it is documented in DESIGN.md.)
