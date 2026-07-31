# sysforge (unreleased)

<!--
Running accumulator for the next release. Every landing commit that COMPLETES a
ROADMAP item appends its entry here (in the same commit that drops the item from
ROADMAP.md), under the matching Keep a Changelog section — one of Added, Changed,
Deprecated, Removed, Fixed, Security, in that order. Reference the roadmap ID
inline, e.g. (1.2.0-F35). Flag breaking changes with a **Breaking:** prefix and
the migration path. At release time tools/release.sh (Phase 1) renames this file
to vX.Y.Z.md, stamps the `# ` title with the version and date, and reseeds a fresh
accumulator. Run the release-notes skill first to reconcile/lint the entries and
finalize the one-line summary below (drop this comment). Keep a Changelog:
https://keepachangelog.com/en/1.1.0/
-->

## Added

- `sysforge help [COMMAND [SUBCOMMAND]]` prints help without knowing the flag
  spelling (2.5.0-F2) — `sysforge help`, `sysforge help build`, `sysforge help
  state failed`. It is an alias, not a second help system: the verb walks the
  argparse subparser chain and prints that parser's own help, so it is
  byte-identical to `<COMMAND> --help`. An unknown topic exits 2 naming the
  offending word and the valid topics at that level. Read-only, no sentinel.

  Both completion files now advertise `-h/--help` as well. The flag always
  worked at every level — argparse adds it to every parser — but the
  hand-written zsh/bash completions never offered it, so it read as missing.
  It is appended from a single dispatch point in each file rather than repeated
  across every verb's spec.

- `sysforge artifact` and `sysforge state` now run their `list` subcommand
  instead of erroring (2.6.1-F6), settling an inconsistency the `doctor`
  system/pkg split introduced: `doctor` and `packages` named a default subverb
  while four other namespaces refused a bare parent. The rule is now an
  invariant in the CLI Verb Framework design — a namespace names a default iff
  it has a single obvious read-only view. `config` and `run` deliberately keep
  requiring a subcommand, because their subverbs mutate or diverge with no
  natural landing point. Additive: no existing invocation changes meaning.

- `doctor --distro` reports the running distribution and its support tier, read
  from `os-release(5)` (2.6.1-F2). Arch is the primary base and the bare sweep
  stays silent on it; an Arch derivative (`ID_LIKE=arch`) gets an `info` naming
  what is validated there (packaging, dependency resolution, `makepkg.conf`
  merge) and what is not (bootstrap, kernel staging, graphics/DKMS); anything
  else warns, as does an unreadable `os-release`. No finding on this axis is
  ever an error — a support tier cannot change doctor's exit code.
  `primitives/os_release.py` is now the single home for distro identity, which
  sysforge previously had nowhere to report at all.

- A container test tier for the packaging and portability checks, parameterized
  by distro (2.6.1-F2): `make container-smoke` (Arch) and
  `make container-smoke-cachyos`. It installs a locally-built package into a
  throwaway container and runs the same checks `make vm-smoke` does, in seconds
  instead of a boot and a snapshot. The derivative arm is the point: it covers
  repo/AUR shadowing, a different `makepkg.conf` baseline, and bumped `pkgrel`s
  on core packages — none of which a same-distro test can reach. Bootstrap,
  kernel staging, graphics/DKMS and restart detection stay with the VM tier.
  Requires `podman`; see `tools/container/README.md`.

- `make dev-deps` installs every dev system dependency in one step, and
  `make dev-deps-list` shows what each tier needs and what is already present
  (2.6.1-F3). The dependency sets are now recorded once in the Makefile
  (`DEV_DEPS_CORE`/`_PKG`/`_VM`/`_CONTAINER`/`_RELEASE`) and every `pacman -S`
  in it resolves from them, so a tier cannot grow a private install preamble.
  Previously `make dev` covered three packages and `make vm-deps` four, while
  `devtools`, `podman`, `uv` and `github-cli` were recorded nowhere — a fresh
  `make dev` could not build a package, run either test tier, or cut a release.
  `make dev` and `make vm-deps` are unchanged in name and now delegate to the
  shared sets. Python tooling is deliberately still unrecorded: `pyright`,
  `reuse`, `pip-audit`, `pytest-cov` and `tomlkit` are resolved per-invocation
  by `uv run --with`, which is also why there is no `[dependency-groups] dev`.

- `make lint` now lints shell as well as Python (2.6.1-F4). `shellcheck` covers
  every tracked `*.sh` plus the bash completion, and is wired into the release
  preflight's lint section with the same policy as `ruff` — warn if the tool is
  absent, fail on findings. `make lint-py` / `make lint-sh` run one side alone.
  The repo's shell scripts *are* its test and release tooling, and nothing had
  ever checked them; the three findings this surfaced are fixed below.
  Run at shellcheck's default severity (info and style included), so a genuine
  exception takes an inline `disable=` with a justifying comment rather than a
  laxer global threshold.

- `make roadmap-view` prints the Planned summary table sorted by any column
  (2.6.1-F13) — `make roadmap-view SORT=effort` for cheapest-first,
  `SORT=bump REVERSE=1` for major-first; `SORT` ∈ `triage`/`id`/`item`/
  `priority`/`effort`/`bump`, defaulting to the canonical triage order. The
  three tag columns rank by their vocabulary, not alphabetically, so `effort`
  reads small → medium → large rather than large → medium → small. It is a
  read-only view: `--sort`/`--print`/`--reverse` never rewrite `ROADMAP.md`, so
  the committed table keeps its triage order and `make check-roadmap-table`
  stays deterministic. Developer tooling only — no runtime surface changes.

- `make help` lists every target, grouped, with a one-line description
  (2.6.1-F14). The repo had no target discovery at all: the ~70 targets were
  documented only by comments in the Makefile, so the interface was
  read-the-source or nothing. The list is generated from the file at run time —
  a target appears iff its own line carries a `## description`, under the
  `##@ Group` banner above it — so the help cannot advertise a target that does
  not exist, and `tests/test_makefile_help.py` fails if a `.PHONY` target ships
  without a description. Deliberately uncoloured: standards row 5 makes
  `log.use_color()` the single colour authority, and a recipe emitting its own
  ANSI would be a second one that never sees `NO_COLOR`. `make` with no
  argument still runs the suite — the default goal is unchanged. Also adds
  `typecheck` and `vm-console` to `.PHONY`, where both were missing.

- `make roadmap-view` now prints an aligned, one-row-per-line table (2.6.1-F15).
  It previously emitted the raw markdown source, whose cells are unpadded,
  leaving the Priority/Effort/Bump columns ragged; and the widest rows overflowed
  the terminal and soft-wrapped, so a single entry occupied two lines and the
  columns stopped lining up entirely — exactly what the view exists to make
  scannable. Cells are now padded to their column's width, and `Item` — the one
  unbounded column, the other four being fixed vocabularies — absorbs whatever
  the line has left and is truncated with `...` if that isn't enough, so a row is
  always exactly one line. Width comes from the terminal (80 when piped) and
  `make roadmap-view WIDTH=200` overrides it. All of this is confined to the
  read-only view: the committed table in `ROADMAP.md` stays unpadded and
  untruncated, so its diffs don't churn every time the longest title changes and
  `make check-roadmap-table` keeps comparing against one stable form.

- SemVer impact is now declared rather than inferred (2.6.1-STD2), as standards
  row 24 plus an extension of row 3. `primitives/deprecations.py` is a registry
  of every surface sysforge still honours only for backwards compatibility —
  six of them, each carrying the version it was deprecated in, the version it is
  removed in, and its replacement. Five are compat read paths whose old spelling
  still works (`[git] pull_timeout`, `packages.toml`'s `pkgbuild_patch` key and
  its `repo_mode = "profiled"` token, `build_state.toml`'s
  `build_mode = "profiled"` token, and the legacy `~/.config/sysforge/{cache,state}`
  dirs); two of those have been deprecated since **1.0.0**, carried silently
  across two major boundaries. The sixth is the `doctor` flat-flag hint table,
  which already exits 2 and is therefore removable in a minor. Using a
  deprecated surface now warns once per run, naming the removal version and the
  replacement, with the text built from the registry record so it cannot drift.

  Nothing is removed in this release. What changes is that a removal can no
  longer be forgotten or mis-scheduled: `make check-standards` fails a registry
  record with no read path (and a read path with no record), rejects a compat
  removal declared for anything but an `X.0.0`, and errors when the release
  target is at or past a declared removal with the surface still present.
  `tools/release.sh` derives the required bump from this file's Keep a Changelog
  sections — a `## Removed` section or a `**Breaking:**` bullet forces major —
  and refuses a `--bump` weaker than that, printing the evidence for the
  derivation so a miscategorised entry is visible. `make next-bump` prints the
  derived value. Every `ROADMAP.md` entry now declares its expected impact with
  a `Bump:` tag, and the standards table is split into externally-sourced and
  sysforge-exclusive sections (row numbers unchanged — they are cited from code,
  tests, and published release notes).

## Changed

- **Breaking:** `sysforge doctor` is now two subcommands (2.6.1-F1).
  `doctor system [AXIS…]` runs the system-state axes; `doctor pkg [TARGETS]
  [AXIS…]` runs the package-scoped ones (`--abi`, `--rust`, `--integrity`).
  **Bare `sysforge doctor` is unchanged** — it is `doctor system`, the 13-axis
  full sweep, and remains the everyday entry point. Migration: every removed
  flat flag prints its replacement and exits 2 (that hint table is itself
  removed in 3.1.0); `doctor --all` becomes `doctor system` followed by
  `doctor pkg --all`.

  The split exists because a single `PKG` positional meant three different
  things — a walk target, a scope qualifier for the `rust`/`integrity` axes,
  and, via `--graphics`, a target *injector* — none of which `--help`
  distinguished, so a user could not tell what a bare invocation covered or
  which flags selected packages rather than checks. Two rules now apply at both
  scopes: no axis flag runs that scope's defaults, and a broad target selector
  (`--all`/`--repo`) suppresses the opt-in axes unless named explicitly.

  The depends + ABI linkage walk is now the explicit `--abi` axis rather than an
  implicit side effect of passing `PKG`, so no check is unrequested. `--graphics`
  splits by scope: system-state probes under `doctor system`, the graphics-stack
  target set under `doctor pkg`. Running both reproduces the old behaviour.

  Two output changes follow from routing the walk through the shared renderer:
  `--abi` findings render grouped per package (groups ordered worst-severity
  first), and a **clean package no longer prints a per-package block** — the
  axis clean message and the `Scanned N package(s)` line cover it.

- **Standards row 23** (`os-release(5)`, enforced) records the distro-identity
  invariant adopted above (2.6.1-F2), guarded by the `check_standards`
  `distro_portability` group: identity is read from `/etc/os-release` (then
  `/usr/lib/os-release`) through `primitives/os_release.py` alone, never
  inferred from `pacman.conf` section names, `/etc/arch-release`, or a hostname.

- The smoke checks moved from `tools/vm/smoke.sh` to `tools/smoke.sh` and gained
  a `--transport=ssh|podman` flag (2.6.1-F2), so the VM and container tiers
  share one copy of the checks rather than diverging. `make vm-smoke` is
  unchanged. Both tiers additionally assert the portability invariants — distro
  identity, sync-repo discovery against `pacman.conf`, the system
  `makepkg.conf` merge baseline, and version comparison on a live `pkgver` —
  each as a differential between the host's own `/etc` and what sysforge
  resolved. An unreachable target now exits `3` (was `1`), distinguishing
  absent optional infrastructure from a real failure for callers that warn
  rather than fail.

- `primitives/` no longer imports from `pipeline.stages` for the mandatory
  hardware-baseline tables (2.6.1-F8). `SYSTEM_LIBLLVM_CONSUMER_TARGETS` and
  `MESA_MANDATORY_GALLIUM` / `MESA_MANDATORY_VULKAN` moved to a new leaf module,
  `primitives/hardware_tables.py`, and the stage now imports *down* into it —
  reversing an edge that had `llvm_targets`, `mesa_drivers` and
  `pkgbuild_patcher` reaching *up* into `stages/hardware.py`. All five imports
  were function-level, written to dodge an import cycle rather than for
  laziness. Because `pipeline/stages/__init__.py` instantiates every stage at
  import, reaching up for one driver tuple loaded **11 stage modules**; it now
  loads none, so an import error in one stage can no longer surface as a
  traceback inside an unrelated resolver (which is exactly how this was found).
  No behaviour change — the tables and their enforcement points are unchanged.
  A new structural test, `tests/test_module_layering.py`, pins the edge with a
  shrinking allowlist covering the four live-detection helpers that still cross
  it, so the set cannot silently grow.

- The toolchain pre-flight now reports version skew as a table rather than a
  sentence (2.6.1-F9). A half-installed LLVM suite previously rendered as
  `LLVM suite version skew (clang/lld 22.1.5 vs llvm/llvm-libs 22.1.6)`; it now
  additionally lists each group as `clang/lld: 22.1.5 → 22.1.6`, with the
  already-current group collapsing to `llvm/llvm-libs: 22.1.6 (=)` — the same
  `old → new` vocabulary the post-update summary uses for rebuilt packages, so
  the target version is stated rather than left to be inferred from a `vs`
  clause. The finding is carried structurally on `ToolchainCheck.versions`, so
  the probe reports facts and the renderer owns formatting.

  The three blocks that documented themselves as "mirroring" each other —
  `update_summary`, `llvm_state.render_preflight`, and
  `toolchain_preflight.render_preflight` — now share one home,
  `primitives/render.py` (`arrow`, `version_pair`, `tag_header`). Only the
  `[TAG]` gutter had genuinely been shared; each had its own copy of the rest.

  This fixes a glyph bug in passing: `llvm_state` hardcoded `→` in its version
  pairs and its `HEAD→upstream` divergence note. `→` *is* in
  `log._GLYPH_FALLBACKS`, but the downgrade only runs inside `log.ui`, and every
  pre-flight block was at the time emitted with a bare `print()` — so those
  arrows survived intact on `TERM=linux`. The shared helpers resolve glyphs at
  format time, which makes the output correct regardless of how the caller
  emits it; 2.6.1-F10 below closes the bare-`print()` sites themselves.

- Pre-flight report blocks now reach the unified run-log (2.6.1-F10). Both
  pre-flights — LLVM source state and toolchain availability — were emitted
  with a bare `print(render_preflight(...))` at four sites (`update.py` ×3,
  `build_cmd.py`, `fetch.py`). `log.ui` is what mirrors UI output into the run
  log, so a `sysforge update` log was missing exactly the findings that explain
  why the run behaved as it did — which is what someone re-reads a log for. All
  four now route through `log.ui`; `pipeline/stages/toolchain.py` already did.
  Side effect worth noting: `log`'s output stream is **stderr** (stdout only in
  dry-run mode), so the three blocks that went to stdout now go to stderr with
  the rest of sysforge's human-facing output, leaving stdout clean. The failed
  toolchain pre-flight already wrote to stderr explicitly and is unchanged in
  where it lands. A new `tests/test_preflight_logging.py` asserts the blocks
  appear in an open unified log, and pins the invariant structurally so a
  future bare-`print()` emit fails a test rather than silently losing the
  block.

- A live run's stage sentinel can no longer be cleared by a second run
  (2.6.1-F11). The sentinel detected an interrupted install-bearing stage by the
  *presence* of `stage_in_progress.toml`, which cannot distinguish "a previous
  run died mid-mutation" from "a run is alive right now" — so a second
  invocation during a live `update` was told the sentinel was stale and offered
  `Clear the sentinel and proceed? [y/N]`, a question it had no information to
  answer. Answering `y` unlinked the live run's sentinel; the second run's
  `mark_started` then overwrote the record wholesale, and whichever run finished
  first cleared the other's — silently, since `clear()` suppresses
  `FileNotFoundError`. The user ended up two concurrent install-bearing runs
  deep with no interruption record for either: precisely the state the sentinel
  exists to make impossible.

  `sentinel_scope` now holds an `flock` on `stage_in_progress.lock` for the
  stage's lifetime, so liveness is "is the lock takeable?" — which the kernel
  answers correctly even after `SIGKILL` or power loss. A PID recorded in the
  sentinel could not: recycling would report a dead owner as alive forever, with
  no way out. Two layers use it — the CLI entry probe refuses **without
  prompting** when an owner is alive, naming the holder PID and its stage, and
  the scope's own acquisition catches the probe/acquire race plus any mutating
  verb reached outside the entry gate's allowlist. **There is no override:** a
  live owner is unambiguous, so a prompt would only invite the mistake the guard
  prevents; escaping means killing the owning process, which is honest about
  what is being done. A genuinely stale sentinel keeps today's behaviour
  unchanged, including after a `SIGKILL`ed owner.

  Contention is strict, but `OSError`/`PermissionError` stay lenient (warn and
  proceed) so a read-only state dir cannot lock the user out of every mutating
  verb — the systems least able to recover are the ones a hard failure would
  strand. The lock lives in the state dir, so an isolated `SYSFORGE_STATE_DIR`
  never contends. It reuses the existing `primitives/build_lock.py` rather than
  rolling a second flock path; that primitive's contention noun is now
  caller-supplied (the build stages pass `noun="build"` and their messages are
  unchanged) and its fd gained `O_CLOEXEC` as hardening.

- **Standards row 23 is now the full Arch-derivative portability standard**
  (2.6.1-STD1), extended from the identity-only invariant that shipped with
  2.6.1-F2 to all three sub-invariants: **(a)** no hardcoded sync-repo names —
  the `["core", "extra"]` literal in `primitives/pacman.py` is the sole
  allowlisted occurrence and only as an I/O fallback, and repo membership is
  asked of pacman rather than inferred; **(b)** the system `makepkg.conf` is the
  merge baseline, never replaced, so a derivative's own `-march`/LTO defaults
  survive profile-key override; **(c)** distro identity from `os-release(5)`
  through one primitive. Enforced statically by the `check_standards`
  `distro_portability` group (widened from the identity-only checks) and
  behaviourally by the new `tests/test_distro_portability.py` against a
  synthetic derivative. `config.SYSTEM_MAKEPKG_CONF` is the one home for the
  system conf path, replacing a second `/etc/makepkg.conf` literal in the
  reconfigure stage. The row forbids *assumptions* — sysforge still has no
  distro-conditional behaviour.

- The release preflight gained section 9, running the container tier's
  derivative arm (2.6.1-STD1) — the only check that exercises row 23 against a
  real derivative rather than synthetic input. Same policy as section 8: warn
  when the harness is unavailable (no podman, no network, unpullable image),
  fail on a real break. Because a warn cannot enforce a cadence, the
  release-prep skill now states the rule a script can't: a **minor or major**
  bump needs section 9 *green*, and a yellow section 9 is acceptable for a
  patch release only. Skip with `RUN_DISTRO_SMOKE=0`.

- Documented distribution support tiers (2.6.1-STD1) across `README.md`,
  `docs/design/20-scope.md`, and a new `DISTRIBUTIONS` section in the manpage:
  Arch primary (everything, including bootstrap/kernel/graphics), CachyOS
  validated each minor via the container tier (packaging invariants only), other
  Arch derivatives expected but unvalidated. `sysforge doctor --distro` reports
  which tier the running system is in.

## Deprecated

- `profiles.toml`'s `build_mode = "patched_pkgbuild"` is now a registered
  deprecation, removed in **4.0.0** (2.6.1-STD3). The token still resolves to
  `source_built` exactly as before; what changes is that reading it warns once
  per run, naming the removal version and the replacement. It has been an alias
  since 2.0.0 and was the one compat surface the 2.6.1-STD2 sweep missed —
  registering it now is what makes it removable at the *next* major rather than
  the one after, since a `compat` removal may only land on an `X.0.0` and 3.0.0
  ships with the surface live. Migration: change `build_mode =
  "patched_pkgbuild"` to `build_mode = "source_built"` in `profiles.toml`.

  Standards row 24 records why the gate could not have found this itself: the
  `deprecations` bijection walks registry→call-site, never code→registry, so an
  *unregistered* compat surface is invisible to it by construction. The row now
  states that plainly — a catalogued-empty `compat` half is not proof that none
  exist, and finding the next one is a review obligation rather than something
  the tooling guarantees.

## Removed

- The flat `doctor` flags (2.6.1-F1): the axis flags `--graphics --gfxperf
  --hardware --distro --toolchain --rust --cache --pacman --state --boot
  --restart --storage --services --audio --network --integrity`, the target
  selectors `--all` and `--repo`, and the bare `PKG` positional no longer parse
  at the top level. Each prints its
  replacement and exits 2. The hint table is deleted in 3.1.0.

- **Breaking:** the compat-alias sweep (2.6.1-F5) deletes the five back-compat
  read paths the 2.6.1-STD2 registry catalogued. Every one of these old
  spellings now goes unread: `[git] pull_timeout` as a fallback for
  `fetch_timeout`; `packages.toml`'s legacy `pkgbuild_patch` per-package key
  (`enable_build_from_source` is the only spelling honoured now, including in
  `sysforge packages list`); the `repo_mode = "profiled"` token in
  `packages.toml` (rejected outright by `REPO_MODE_ACCEPTED_INPUTS`, not
  aliased); the `build_mode = "profiled"` token in `build_state.toml`; and the
  one-shot migration of `~/.config/sysforge/{cache,state}` into their XDG
  homes, which ran on every CLI invocation. **Migration:** a `[git]
  pull_timeout` still set in `sysforge.toml` is now ignored silently and the
  fetch timeout falls back to the `30`-second default — rename the key to
  `fetch_timeout` to keep your value. A `build_state.toml`
  still carrying `build_mode = "profiled"` from before this release stops
  resolving — run `sysforge state repair` once to normalize it. A
  `packages.toml` still using `pkgbuild_patch` or `repo_mode = "profiled"`
  needs those keys renamed by hand to `enable_build_from_source` /
  `build_from_source`. A `~/.config/sysforge/cache` or `.../state` directory
  left over from before the migration ran at least once needs moving to
  `$XDG_CACHE_HOME/sysforge` / `$XDG_STATE_HOME/sysforge` by hand. The
  deprecation registry (`primitives/deprecations.py`) now holds exactly one
  record, `doctor.flat_flags`, removable in 3.1.0. The two previously-untested
  warn sites (`paths.legacy_user_dirs`, `packages.pkgbuild_patch`) gained
  characterization tests ahead of the sweep (2.6.1-F7) to guard against a
  silent regression during removal; those tests were deliberately throwaway
  and were deleted along with the surfaces they covered, so nothing from
  2.6.1-F7 remains in the tree — its contribution was making this removal
  safe to land.

## Fixed

- Source sync no longer warns `working tree has local modifications — keeping local
  PKGBUILD` for every `-git` package carrying makepkg's routine `pkgver()` auto-bump
  (2.6.1-B1). The warning contradicted the very next log line, which reset the same
  tree to upstream after re-asking the question VCS-aware; it is now an `INFO` for
  VCS checkouts whose only working-tree change is the generated `pkgver`/`.SRCINFO`
  churn. Deliberate edits, and genuine upstream divergence, still warn as before.
  The sync outcome is unchanged.

- `make vm-iso` no longer requires the installer ISO to be named `archlinux.iso`
  (2.6.1-B2). It now boots the single `*.iso` in the VM directory whatever its
  filename, or the one named by the new `SYSFORGE_VM_ISO` variable (bare filename
  or absolute path) when several are present; zero or ambiguous matches fail with
  remediation instead of a stale path. Lookup happens only in `--iso` mode, so
  other boot modes are unaffected. Combined with `SYSFORGE_VM_DIR`, a VM tree for a
  second Arch-derived distro now needs no code change. Only the Arch install stays
  automated — see `tools/vm/README.md`.

- A stale `packages.toml` no longer greets an upgrading user with a stack trace
  (2.6.1-B3). 2.6.1-F5 made `[build] repo_mode = "profiled"` fail loudly rather
  than silently resolve to `pacman` — but it raised `ValueError`, and
  `verbs/runner.py` converts only `RuntimeError` into a clean error line, so
  `build`, `update` and `reconfigure` printed a traceback with the remediation
  buried inside it. Both `repo_mode` rejects (`config.resolve_repo_mode` and the
  packages stage's raw-value gate, which had the same defect) now raise a new
  `config.ConfigError`, a `RuntimeError` subclass, so the message lands as an
  error line and exit 1. The runner was deliberately *not* widened to catch
  `ValueError`: its error model treats anything but `RuntimeError` as a bug that
  should traceback, and blurring that would make real defects look like tidy
  user errors. A regression test pins both halves.

- Naming a **split-package member** no longer fails with `PKGBUILD not found`
  (2.6.1-B4). AUR git repositories are named by `pkgbase`, so
  `wayland-docs-git` lives in `wayland-git.git`; `find_pkgbuild` cloned by
  `pkgname` instead, and AUR answers an unknown repository name with an *empty*
  repository rather than an error — `git clone` exited 0, left a `.git`-only
  tree in `pkgbuild_src_dir`, and resolution died one step later on the missing
  PKGBUILD, with the junk directory still there to shadow the next attempt.
  `find_pkgbuild` now remaps to the RPC record's `PackageBase` before touching
  disk (the same remap `update_assemble.py` already did), so an existing
  `wayland-git` checkout resolves with no clone at all. `aur_clone` treats a
  PKGBUILD-less clone as a failure and purges the directory, so the empty-repo
  case can no longer be mistaken for success anywhere else either. Relatedly,
  `build` now dedups targets by `pkgbase`: `build wayland-git wayland-docs-git`
  names two members of one base, which makepkg builds in a single run, so the
  base is no longer built twice.

- Installing an AUR `-git` package over its stock counterpart no longer aborts
  with `unresolvable package conflicts detected` (2.6.1-B5). The batch install
  already auto-confirmed pacman's `X and Y are in conflict. Remove Y?` question
  for a deliberate drop-in replacement, but recognised only one way of
  declaring one: an explicit `replaces`. The AUR `-git` idiom uses the other —
  `wayland-git` declares `conflicts=('wayland')` plus `provides=("wayland=$pkgver")`
  and no `replaces` at all — so the heuristic never fired, `--noconfirm`
  auto-answered `N`, and the transaction aborted after a successful build. The
  test that pinned the behaviour used an explicit `replaces`, so it passed.
  Both forms now route through one predicate, `pacman.pkg_supersedes_installed`,
  which reads `replaces` **or** the `conflicts`∩`provides` pair from the built
  package's `.PKGINFO`. Requiring both halves of the second form keeps it
  narrow: a package conflicting with something it does not provide is an
  unexpected collision, not a substitution, and still stops the transaction for
  review. The superseded names are now logged when they are auto-confirmed.

- `make vm-pkg-stable` no longer fails every run with `sysforge-<ver>.tar.gz
  ... FAILED` during source validation (2.6.1-B6). `tools/vm/build-pkg.sh` tars
  the working tree and rewrites the PKGBUILD's `sha256sums` to match, but did
  so with `sed -E "s/^sha256sums=\(.*\)/.../"` — under `-E` (ERE) the `\(` and
  `\)` are *literal* parentheses, so the pattern required the closing paren on
  the same line. That held while `sha256sums` was a one-element array; adding
  the release `.asc` source for GPG signing made it two lines, the pattern
  stopped matching, and `sed` reports no error on a no-match. The rewrite
  silently became a no-op and the chroot validated a freshly-tarred working
  tree against the **published** release checksum. The two arrays are now
  rewritten by a parser that spans lines and hard-fails if a substitution does
  not apply, so a future PKGBUILD reshuffle breaks loudly instead of silently.
  The local build also drops the `.asc` source and `validpgpkeys` outright: no
  signature exists for an unpublished working-tree tarball, and the fetch was
  quietly storing GitHub's 404 page.

- `make container-smoke` no longer fails on two stale assertions in
  `tools/smoke.sh` (2.6.1-B7). Both were surfaces the smoke script asserts but
  does not own, so neither moved when the surface did: the hook count was pinned
  at `3` while the PKGBUILD has installed a fourth hook since the artifact-drift
  hook (2.3.0-F4) shipped, and the distro-identity probe still invoked
  `sysforge doctor --distro`, which the compat-alias sweep removed in favour of
  `sysforge doctor system --distro` — the probe was measuring the exit code of
  the removal error, not of the axis. The container tier is green again on the
  Arch arm (11/11). The `doctor --distro` spelling was also corrected in
  `README.md`, `docs/design/verbs/doctor.md` and the two harness READMEs.

- The container tier now runs pacman's install-time hooks instead of silently
  skipping every one of them (2.6.1-B8). `pacman >= 7.1` isolates each scriptlet
  and hook in its own network namespace; a rootless container has no
  `CAP_SYS_ADMIN` in its user namespace, so that `unshare()` fails with `EPERM`
  and pacman refuses to run the hook — while still exiting `0`, so the install
  read as clean with no hooks having fired. The `tmpfiles.d` check was the only
  one asserting an *effect* rather than a file's presence, so it was the only
  one that caught it. The Containerfile now sets `DisableSandboxNetwork` in
  `[options]`, which drops only the network half of the sandbox and leaves the
  filesystem half in force. Both arms are green (11/11 on Arch and CachyOS).

- The `reconfigure` stage no longer hands the build pipeline an editor it does
  not have (2.6.1-B9). Two gaps, both ending in `No usable $EDITOR` much later,
  inside the PKGBUILD failure-recovery menu with a half-built package. First,
  the editor picked in the stage lived only in a local threaded through the
  step loop; every downstream consumer calls `resolve_editor()` fresh, so
  declining the `Save as sysforge default? [y/N]` prompt (which defaults to
  **N**) discarded the pick the moment the stage returned. The pick is now
  adopted into `SYSFORGE_EDITOR` unconditionally — visible for the rest of the
  run and to any child process — and persisting it to `sysforge.toml` stays the
  user's separate choice. Second, the existing gate only covered the two steps
  that open files *within* the stage (`config`, `makepkg`); a step subset
  skipping both, or a skipped `editor` step, reached the "Ready to proceed to
  toolchain → packages → kernel?" prompt with no editor at all. That handoff is
  now gated too, skipped under `--standalone` (nothing runs after it) and
  downgraded to a warning with no TTY.

- The container harness now exits `3` ("unavailable") rather than `1` when no
  built package is present (2.6.1-STD1). An unbuilt package is a missing
  prerequisite, not a portability break; at `1` the new preflight section would
  have blocked a release over it. Found by running the harness for real.

- `sysforge doctor --rust PKG` no longer appears to ignore its argument
  (2.6.1-F1). It never did — it skipped silently, in two places: a package that
  could not be resolved to a PKGBUILD, and one with no `rust-toolchain.toml`,
  each swallowed by a bare `continue`, so the output was identical to a bare
  `--rust`. Both are now explicit, with severity by provenance: an
  **explicitly named** target that cannot be resolved warns
  (`rust-pin-unresolved`), and one that resolves with no pin reports
  `rust-no-pin`. Targets pulled in by a broad selector collapse into a single
  `rust-pin-survey` line carrying the counts, so `doctor pkg --all --rust`
  summarises instead of emitting several hundred near-identical lines. The
  invocation is now `sysforge doctor pkg PKG --rust`, which also no longer runs
  an unrequested linkage walk.

- Three shell defects the new `shellcheck` gate surfaced (2.6.1-F4): the release
  preflight's `cd "$REPO_ROOT"` was unguarded, so under `set -u` without `-e` a
  failed `cd` would have run every check against the caller's directory and
  reported a green preflight for the wrong tree; `tools/vm/boot.sh` carried a
  dead `SCRIPT_DIR` assignment; and `tools/vm/build-pkg.sh`'s `ls` summary now
  documents why it is not the `find` the linter suggests.
