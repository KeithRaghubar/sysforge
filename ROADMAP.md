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

- **`2.3.0-F1` — Expand the `ruff` lint select to the security/bug rulesets.** `[tool.ruff.lint]`
  currently sets only `ignore` (E402), so the effective select is the default `E`/`F` — the
  bundled bandit port (`S`) and `flake8-bugbear` (`B`) never run. For a tool that shells out at
  224 subprocess sites, `S` is the highest-leverage gate available at zero new dependency: it flags
  `shell=True` (`S602/S604`), hardcoded `/tmp` paths (`S108`), and weak-hash use, forcing every such
  site to carry an explicit justified `# noqa`. The one known `shell=True`
  (`toolchain_preflight.py:_run_fix`, `:477`) is defensible — the run string is internally generated
  and must be byte-identical to what's printed as the suggested fix — and would get a documented
  `# noqa: S602`. `B` (mutable-default args, bare-`except` swallowing) is the stability half. Land as
  `select = ["E", "F", "S", "B", "SIM", "PTH"]`, sweep the resulting findings (expect mostly `noqa`
  annotations on the run seam plus a few real fixes), and keep `make lint` green in the same change.
  *Priority: medium (one-time sweep; retroactively audits all subprocess sites; no runtime dep).*

- **`2.3.0-F2` — Enforced type-checking gate in `pre-release`.** `pyright` is configured in
  `pyproject.toml` (it owns type analysis; ruff owns lint-style overlap) but nothing in the Makefile
  runs it, so type regressions only surface in-editor. Add a `make typecheck` target using the same
  ephemeral overlay pattern as `make coverage` (`uv run --no-sync --with pyright`, no venv mutation)
  and wire it into the `pre-release` chain. Catches None-handling and argument-shape bugs — the
  stability half of the hardening — before release. Baseline may need a first-pass sweep to reach a
  clean floor. *Priority: medium (stability gate; dev-time only).*

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

- **`2.3.0-F6` — Mirror system-mutating verbs to `journald`.** The `log.py` unified run-log is the right
  user-facing capture and stays authoritative. The complementary hook is a second sink: emit the
  privileged/system-mutating operations (the sentinel-gated verbs) to `journald` via `systemd-cat`/
  `sd_journal` so sysforge's changes appear in `journalctl` alongside everything else that touched the
  system — the place an admin looks during incident review. Structured fields (verb, target, exit) make
  it queryable. Not a replacement for the run-log; an additive, system-integrated sink for mutations
  only. Gate on systemd presence. *Priority: low (observability/defense-in-depth; additive).* **Spec:**
  `systemd.journal-fields(7)`, `sd_journal_send(3)`/`systemd-cat(1)`. **Standards home on adoption:** new
  standards row (status `target`→`enforced` once a `test_standards_compliance.py` case guards it) +
  rationale in `docs/design/12-logging.md`, in the landing commit.

- **`2.3.0-F7` — Warn when `cpu_quota` exceeds host core count (promoted from `2.3.0-Q1`).**
  `_coerce_cpu_quota` (`build_throttle.py`) accepts arbitrarily large quotas (`"9999%"`, or a
  fraction like `"2.0"`) with no check against `os.cpu_count()`, so a typo or a config copied from a
  bigger box silently asks for more cores than exist (systemd clamps effectively, but the user gets
  no signal). Emit a warning — not a drop or clamp — when the **resolved** percentage exceeds
  `cpu_count*100`, guarding the single resolved `pct` *after* both the `N%` and fraction branches
  converge (both forms can exceed; the earlier "only the `N%` form is affected" framing was wrong —
  the fraction form's `round(frac*cores*100)` overshoots for `frac > 1`). Warn-only keeps the
  module's "never raise, degrade with a warning" contract; systemd's own effective cap does the
  harmless clamping. *Priority: low.*

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

- **`2.3.0-F9` — `systemd-run --scope` as the primary tier for *all* build resource enforcement
  (promoted from `2.3.0-Q3`).** As shipped by `2.2.0-F4`, `build_throttle.wrapper_argv` already
  routes a `cpu_quota` build (and its `MemoryMax`) through a `systemd-run --scope --user` cgroup, but
  a `mem_limit` set **alone** (no `cpu_quota`) still falls to an `RLIMIT_AS` preexec — the escapable
  path, since an rlimit on the single preexec child leaks across makepkg's fork tree whereas a
  cgroup `MemoryMax` is kernel-enforced hierarchically over all descendants. Emit a scope carrying
  `-p MemoryMax=` even when `cpu_quota` is absent, demoting `RLIMIT_AS` to a pure non-systemd
  fallback; `resolve_child_mem_cap` already arbitrates so the two never double-apply. *Priority: low
  (hardening; "hook the OS, don't layer").* **Spec:** `systemd.resource-control(5)`
  (`MemoryMax`/`CPUQuota`/`IOWeight`), `systemd-run(1)`, cgroup-v2. **Standards home on adoption:**
  new standards row (status `target`→`enforced` once a `test_standards_compliance.py` case guards
  it) + rationale in the throttle/resource-control design chapter, in the landing commit.

- **`2.3.0-F10` — Privilege seam for system-mutating operations (promoted from `2.3.0-Q4`).** The
  escalation *model* is already de-facto decided — sysforge runs unprivileged and escalates per
  operation via `sudo` — but ad-hoc: ~20 hardcoded `["sudo", …]` prefixes across `pacman.py`,
  `kernel.py`, `reconfigure.py`, `toolchain.py`, `fs_provision.py`, so there is no single audit
  point, no way to offer `polkit` scoping, and no guarantee a new mutating verb escalates
  consistently. Introduce a `run_privileged(argv)` seam all mutating operations route through,
  layered on the `2.3.0-STD1` run seam (not a parallel abstraction). First task is the audit of where
  code assumes it is already root (`reconfigure.py` branches on `os.geteuid() == 0`) vs. shells out
  expecting escalation, so the actual surface is known. *Priority: medium (security posture; touches
  every mutating verb — decide the seam before the surface grows).* **Spec:** `sudoers(5)`,
  `polkit(8)` + `polkit.action(5)`. **Standards home on adoption:** new standards row + rationale in
  a (likely new, cross-cutting) privilege-seam design chapter, in the landing commit; pairs with
  `2.3.0-STD1`.

- **`1.2.0-F20` — Rule priority auto-calculation (from the DESIGN roadmap).**
  Auto-calculate a baseline specificity score from rule conditions (mirrors CSS
  specificity: more AND'd conditions = higher weight), with manual `priority`
  override for ties. Deferred until enough real rules exist to validate whether
  auto-priority causes ordering problems in practice. *Priority: low (candidate, not
  a commitment).*

- **`1.2.0-F28` — User-owned artifact inventory primitive.** Track the user-owned
  system artifacts now scattered across `~/scripts`, `/etc/systemd/system/`,
  `/etc/pacman.d/hooks/`, etc.: a tracked-file inventory, a repo-controlled
  source-of-truth dir, an install/sync command, and drift detection vs the
  filesystem, tied into the existing config/profile/manifest layers. Anything that
  mutates the system stays behind a sentinel-gated verb. Stays inside the boundary in
  DESIGN.md §Scope & Non-Goals (this is steady-state health of a managed system, not
  backup/config-management). *Priority: low (strategic — coarse; decompose before
  building).* **Sub-thread to design: opt-in offering of user-owned systemd
  services and pacman hooks.** The user keeps custom units and hooks on the live
  system; sysforge should be able to *offer* (not force) them — open question whether
  the surface is the `setup` stage, a dedicated verb, or a sync mode of this
  inventory. Discuss the UX before committing; it is a concrete first slice of this
  primitive. **Drift detection should hook `pacman`, not reimplement it:** for any artifact
  that belongs to a package, prefer `pacman -Qkk` (file integrity/ownership/permission
  verification against alpm's stored `mtree`) over a hand-rolled tree walk — the OS already
  knows which files belong to which package and whether they changed. The hand-rolled path is
  only for genuinely package-less user artifacts.

- **`1.2.0-F43` — Logging re-levelling audit for interactive/bootstrap stages.**
  Follow-up to the shipped `1.2.0-F36` slice (configurable `[log] verbosity` + `--quiet`,
  the DESIGN.md §Logging rubric, and the re-levelled day-to-day `build`/`update` path
  under a golden-output test). **Remaining:** sweep the interactive/bootstrap-time stages
  against the same rubric — `reconfigure.py` (94 `ui()`), `configure.py` (47), `kernel.py`
  (42), `toolchain.py` (60), `hardware.py` (19), `partition.py` (14) — demoting progress
  narration to `info()` while keeping prompts/plan-tables/results as `ui()`. Extend the
  golden guard to a representative stage run. *Priority: low (bootstrap-time output, not the
  day-to-day regression, which is resolved under F36).*

### Standards

- **`2.3.0-STD1` — All external-command execution routes through the run seam.** `primitives/run.py`
  (`run_or_raise`) already centralizes the "run a command, raise a tagged error" pattern, but nothing
  enforces that the 224 subprocess sites actually use it — modules still call `subprocess.run` directly
  and could regress to `shell=True` or string commands without review. Promote the existing convention
  to an enforced standard in `docs/design/21-standards.md`, checked by `tools/check_standards.py` +
  `tests/test_standards_compliance.py` (same mechanism as "user paths → `primitives/paths.py`, colour →
  `log.use_color()`"): external commands go through the run seam, argv-**list** form only (never a
  shell string), and any `shell=True` requires a justified inline `# noqa: S602` naming why the input is
  trusted. Pairs with `2.3.0-F1` — the ruff `S` rules catch new `shell=True`; this standard governs the
  seam discipline the rules can't express. Scope decision needed on how much of the 224-site surface must
  migrate to the seam vs. be grandfathered with a documented carve-out (streaming/interactive callers
  that deliberately bypass `run_or_raise`'s capture). *Priority: medium (locks in an existing invariant;
  prevents subprocess-seam drift).*

---

## Abandoned / decided against

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
