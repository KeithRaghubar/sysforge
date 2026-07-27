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

## Removed

- The flat `doctor` flags (2.6.1-F1): the axis flags `--graphics --gfxperf
  --hardware --distro --toolchain --rust --cache --pacman --state --boot
  --restart --storage --services --audio --network --integrity`, the target
  selectors `--all` and `--repo`, and the bare `PKG` positional no longer parse
  at the top level. Each prints its
  replacement and exits 2. The hint table is deleted in 3.1.0.

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
