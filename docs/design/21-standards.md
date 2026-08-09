## Standards & Specifications

SysForge commits to a set of external specifications so that its on-disk
footprint, CLI behaviour, data formats, and packaging are predictable,
portable, and interoperable with the wider Linux/Arch ecosystem. This section
is the **canonical list**. Every change that touches paths, the CLI surface,
output, versioning, encoding, or packaging is expected to cross-check the
relevant standard here; the release gate (`make check-standards` plus the
behavioural `tests/test_standards_compliance.py`) enforces the mechanically
checkable subset.

Status legend: **enforced** = a check/lint/test guards it · **followed** =
adhered to, partially or fully guarded · **target** = adopted, gap being closed.

### Committed standards

#### Externally-sourced

Standards defined outside sysforge, which the project conforms to.

| # | Standard | Scope | Status | How it is enforced |
|---|----------|-------|--------|--------------------|
| 1 | [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/) | User dirs (`~/.config`, `~/.cache`, `~/.local/state`, `~/.local/share`) | enforced | `primitives/paths.py` (`_xdg_base`); `check_standards` `paths` group; `tests/test_paths.py` |
| 2 | Filesystem Hierarchy Standard + systemd `file-hierarchy(7)` | System roots (`/etc`, `/var/lib`, `/var/cache`, `/run`) | enforced | `paths.py` (`CONFIG_BASE`), `pipeline/state.py`, `makepkg_pgo.py`; `check_standards` `paths` group |
| 3 | [Semantic Versioning 2.0.0](https://semver.org/) | Project version scheme **and declared bump selection** | enforced | Two facets. **Format + cross-file parity**: `tools/check_shipped.py` `versions` group. **Bump selection** (§§6–8: patch for a compatible fix, minor for a compatible addition, major for an incompatible change): the required bump is derived from the release-notes accumulator's Keep a Changelog sections (row 13) — a `## Removed` section or a `**Breaking:**` bullet forces major, `## Added` minor, the rest patch — and `tools/release.sh` preflight refuses a `--bump` weaker than the derived value, printing the derived value with the evidence line that produced it so the inference is auditable. Planning-time counterpart: every `ROADMAP.md` Planned entry carries a `Bump:` tag (`tools/gen_roadmap_table.py`, sole parser). `make next-bump` prints the derived value; `check_standards` `semver_bump` group + `tests/test_check_standards_bump.py` |
| 4 | POSIX Utility Conventions + GNU long-options | CLI argument grammar (`-h/--help`, `-V/--version`, `--`) | followed | argparse in `cli.py`; `tests/test_standards_compliance.py` |
| 5 | [NO_COLOR](https://no-color.org/) + `FORCE_COLOR` | Terminal colour control | enforced | `log.use_color()` (single authority); `tests/test_standards_compliance.py` |
| 6 | stdout/stderr separation + exit-code contract | CLI behaviour (data→stdout, diagnostics→stderr; 0/1/2) | followed | `log._out()`, `verbs/runner.py`; `tests/test_standards_compliance.py` |
| 7 | [TOML 1.0.0](https://toml.io/en/v1.0.0) | Config + state file format | followed | `tomllib` everywhere; `check_shipped` `configs` group |
| 8 | RFC 3339 / ISO 8601 (UTC) | Timestamps in state files | followed | central `_now_iso()` helpers; `tests/test_standards_compliance.py` |
| 9 | UTF-8 | Text file encoding | enforced | explicit `encoding="utf-8"`; `check_standards` `encoding` group (ruff `PLW1514 --preview` is the one-shot fixer) |
| 10 | PEP 517 / 518 / 621 / 508 | Python packaging metadata | followed | `pyproject.toml` (hatchling backend, `[project]` table) |
| 11 | `PKGBUILD(5)` · `.SRCINFO` · `alpm-hooks(5)` · `makepkg.conf` + [Arch package guidelines](https://wiki.archlinux.org/title/Arch_package_guidelines) / [VCS package guidelines](https://wiki.archlinux.org/title/VCS_package_guidelines) | Arch packaging artefacts + conventions; **also** the on-disk shape (`.hook` sections/keys) of *user-authored* pacman hooks that the artifact inventory discovers, adopts, and deploys — sysforge inventories these, not only ships its own | enforced | `pkgbuild-spec-check`/`pkgbuild-edit` skills; `check_shipped` `pkgbuild`/`hooks` groups; `primitives/artifacts.py` (`CLASS_HOOK` discovery/deploy); `check_shipped` `config_comments` group extends this to the shipped configs' *prose*: no comment may name a `*.toml` or a `[section]` that does not exist, and a key whose validator accepts multiple surface forms must show every form (`_GRAMMAR_DOCS`, a hand-maintained table — widening a `_coerce_*` grammar updates it in the same commit, since no static signal distinguishes an accepted-form branch from any other conditional) |
| 12 | `man-pages(7)` via scdoc | Manual page | enforced | `make man`; `check_shipped` `manpage` group |
| 13 | [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) | Release notes | enforced | `docs/release-notes/vX.Y.Z.md` + `unreleased.md` accumulator category vocabulary; `check_standards` `changelog` group |
| 14 | [REUSE](https://reuse.software/) / SPDX (license: **MIT**) | Per-file licensing | enforced | SPDX headers + `LICENSES/MIT.txt` + `REUSE.toml`; `check_standards` `spdx` group (`reuse lint`) |
| 15 | Reproducible builds | Builds SysForge produces | followed | does not strip reproducibility OPTIONS / honours `SOURCE_DATE_EPOCH`; `tests/test_standards_compliance.py` |
| 16 | OpenPGP signing (RFC 4880) + makepkg `validpgpkeys` | Release provenance (signed commits, tags, tarball) | followed | `tools/release.sh` (signing preflight + `git tag -s` + tarball `.asc`); `check_shipped` `pkgbuild` group (`validpgpkeys` + signature-aware `SKIP`); verified downstream by `makepkg` |
| 19 | [`systemd.resource-control(5)`](https://www.freedesktop.org/software/systemd/man/systemd.resource-control.html) (cgroup-v2 `CPUQuota`/`MemoryMax` via `systemd-run(1)`) | Build resource enforcement (CPU/memory ceilings on the makepkg fork tree) | enforced | a configured `cpu_quota`/`mem_limit` is enforced by a kernel-level cgroup `systemd-run --scope` (hierarchical over all build descendants), not solely an escapable `RLIMIT_AS` preexec, whenever `systemd-run` is available; `primitives/build_throttle.py` (`wrapper_argv`/`_scope_owns_mem_cap`/`resolve_child_mem_cap`); `tests/test_standards_compliance.py` |
| 20 | [`systemd.journal-fields(7)`](https://www.freedesktop.org/software/systemd/man/systemd.journal-fields.html) + native journal socket protocol (`sd_journal_send(3)` wire format) | System-mutating operations mirrored to the journal | enforced | every sentinel-gated verb emits one structured record (`SYSFORGE_VERB`/`SYSFORGE_TARGET`/`SYSFORGE_EXIT` + `MESSAGE`/`PRIORITY`/`SYSLOG_IDENTIFIER`), additively alongside the unified run-log, no-op when journald absent; `SYSFORGE_TARGET` is supplied by every mutating verb via `Verb.journal_target` and namespaced `pkg:<names>` / `mode:<subcommand>` (one-home formatters `journal.pkg_target`/`journal.mode_target`); `primitives/journal.py` (`journal_send`/`record_verb`), `verbs/runner.py`; `tests/test_standards_compliance.py` + `tests/test_journal.py` |
| 21 | `systemctl(1)` unit lifecycle (`daemon-reload`, `is-enabled`, `disable --now`) | Deploying/removing a user-authored systemd unit through the artifact inventory | enforced | `primitives/artifacts.py` (`post_deploy` runs `daemon-reload` after a unit write via `run_privileged`; `unit_is_enabled` queries `systemctl is-enabled --quiet` unprivileged; `pre_remove` runs `systemctl disable --now` before unlinking an enabled unit); `tests/test_artifacts.py` |
| 22 | `pacman -Qk`/`-Qkk` package-file verification against libalpm's stored `mtree` | Verifying package-owned files still match what the package declared (existence, size, mode, hash, type) | followed | read-only `doctor --integrity` axis consumes `pacman -Qkk` with pacman's own backup-vs-altered classification (backup-array edits → `info`, non-backup drift → `warn`, missing → `error`, mtime-only → `info`); run unprivileged, access-error reasons (`failed to calculate SHA256 checksum`, `Permission denied`) are stripped before classification — a path with only access-error reasons is access-limited, not drift, and rolls into one counted `integrity_partial_coverage` `info` advisory rather than a per-path finding, while a path with genuine signal alongside an access error keeps its real drift severity; `primitives/pkgfiles_probe.py` (`collect_integrity_findings`); `tests/test_pkgfiles_probe.py` + `tests/test_standards_compliance.py` |
| 23 | [`os-release(5)`](https://www.freedesktop.org/software/systemd/man/os-release.html) | Arch-derivative portability: repo, toolchain-default, and distro-identity assumptions on an Arch-derived host | enforced | Three sub-invariants. **(a) No hardcoded sync-repo names** — a derivative carries its own sync DBs, often ordered ahead of `core`/`extra`; the `["core", "extra"]` literal in `primitives/pacman.py` is the sole allowlisted occurrence and only as an I/O fallback when `/etc/pacman.conf` is unreadable. Repo membership is *asked* of pacman (`aur.repo_packages` → `pacman -Si`), never inferred — this is the `build_core.prepare_deps` repo-vs-AUR makedep split, the failure class behind the exit-8 regression at `build_core.py:268`. **(b) The system `makepkg.conf` is the merge baseline, never replaced** — `config.SYSTEM_MAKEPKG_CONF` / `config.parse_system_makepkg_conf` is the only reader of that path (values returned verbatim, unnormalized), and `primitives/makepkg_conf.py::emit_makepkg_conf` both loads the system assignments and emits them, substituting profile keys inline, so a derivative's own `-march`/LTO defaults survive profile-key override intact. **(c) Distro identity is read from `os-release(5)` through one primitive** — `primitives/os_release.py`: `/etc/os-release` then `/usr/lib/os-release`, shell-style `KEY=value` with quote stripping, `ID` defaulting to `linux`, `ID_LIKE` as the space-separated parent list; never inferred from `pacman.conf` section names, `/etc/arch-release`, `/etc/lsb-release`, or a hostname. Surfaced by the `doctor --distro` axis, which reports the support tier (Arch = primary; `ID_LIKE=arch` = derivative, packaging invariants validated; otherwise `warn`) and never contributes an error, so a support tier cannot change doctor's exit code. Scope note: the row forbids *assumptions*; it does not introduce per-distro code paths — sysforge has no distro-conditional behaviour. `check_standards` `distro_portability` group (all three, statically) + `tests/test_standards_compliance.py` + `tests/test_distro_portability.py` (behavioural, synthetic derivative input) + `tests/test_os_release.py`; validated against a real derivative each minor by the container tier (`make container-smoke-cachyos`, release preflight section 9) |

#### SysForge-exclusive

House policies with no external specification. They are enforced exactly like
the rows above and share one global counter with them — the next row is 27
regardless of which subsection it joins, because the number is the row's
identity (it is cited from code, tests, and published release notes) while the
subsection is only presentation.

| # | Standard | Scope | Status | How it is enforced |
|---|----------|-------|--------|--------------------|
| 17 | Subprocess-seam discipline (argv-list execution) | External-command execution (all `subprocess` sites) | enforced | argv-**list** form only, `shell=True` needs justified `# noqa: S602`; `primitives/run.py` (`run_or_raise`) sanctioned seam, direct callers a documented carve-out for streaming/returncode/stdout-parsing; ruff `S602` + `check_standards` `run_seam` group |
| 18 | Privilege-escalation seam | Root-escalating subprocess invocations | enforced | `primitives/privilege.py` (`privileged_argv`/`run_privileged`) is the sole home for `sudo`-prefixed escalation; raw `["sudo", …]` argv outside it is forbidden except the allowlisted auth-probe (`sudo -v`, `sudo -n true`) and drop-privilege (`sudo -u <user>`) forms; `check_standards` `privilege_seam` group + `tests/test_standards_compliance.py` |
| 24 | Deprecation registry (declared removal version + warn-on-use) | Every config key, state token, CLI flag, or path sysforge still honours only for backwards compatibility | enforced | `primitives/deprecations.py` is the single home: each record carries `surface`/`kind`/`function`/`deprecated_in`/`removed_in`/`replacement`, and each compat read path calls `warn_used(surface)` so the warning text is built from the record and cannot drift from the version the gate enforces. Two `function` values, because removal is not uniformly breaking — a `compat` surface still works (removing it is breaking, so `removed_in` must be `X.0.0`, and presence is proven by its `warn_used` call sites), while a `shim` already fails and is kept only to name its replacement (removal is not breaking, `removed_in` may be `X.Y.0`, presence proven by an exact repo-relative `anchor`). `check_standards` `deprecations` group: registry↔call-site bijection both ways, major-only removal for `compat`, and an error when the release target is at or past a declared `removed_in` with the surface still present (`--target-version`); a registry that parses to zero records is itself an error, because a check that cannot fail is worse than no check. The release-note removal parity check matches the registry's literal surface string against the `## Removed` section body, so a removal note must spell the canonical identifier (e.g. `build_state.build_mode=profiled`), not just prose describing it. `tests/test_deprecations.py` (behavioural, incl. once-per-run dedup and per-surface resolution) + `tests/test_standards_compliance.py`. **First application:** the 2.6.1-F5 sweep removed all five `compat` surfaces the registry catalogued at `2.6.1-STD2` shipping time, proving the major-only removal gate end to end. Two records ship in 3.0.0: the `shim` `doctor.flat_flags` (due out in `3.1.0`) and the `compat` `profiles.build_mode=patched_pkgbuild` (`2.6.1-STD3`, due out in `4.0.0`) — `primitives/profile.py`'s `_LEGACY_BUILD_MODE_ALIASES` (`patched_pkgbuild` → `source_built`), a `profiles.toml` `build_mode` token honoured on read through `normalize_build_mode`, whose alias branch now calls `warn_used`. It predates the registry and `2.6.1-STD2` missed it, because the bijection walks registry→call-site and never code→registry: **an unregistered compat surface is invisible to the gate by construction.** A catalogued-empty `compat` half is therefore never proof that none exist, and finding the next one is a review obligation, not a tooling guarantee — a code→registry direction is unimplemented (no reliable static signal distinguishes a compat alias table from any other lookup dict). Its `removed_in` is `4.0.0` rather than `3.0.0` because 3.0.0 ships with the surface present, and the gate refuses a release at or past a declared removal with the surface still live. |
| 25 | Log-level rubric (UI / ERROR / WARN / INFO / DEBUG) | Every call site that emits user-visible output | followed | `docs/design/12-logging.md` is the single home: the five-level table (fn, gate, reserved-for), the decision test that resolves most call sites (*is this the answer, or narration about producing the answer?*), and the explicit anti-pattern that `ui()` is not a "make this always show up" escape hatch — `ui()` is the answer the user ran the command to see, `info()`/`warn()` are narration and are gated behind `-vv`/`-v`. Enforcement is deliberately partial and the row does not claim otherwise. The one mechanical guard is the `quiet_at_default` fixture (`tests/conftest.py`), a golden-output regression that runs a stage at the shipped default (`verbosity=0`) and again at `-vv`, and fails if the default run emitted an `[INFO]` or `[WARN]` line. That catches the failure mode that matters — narration leaking into always-visible output — but nothing checks WARN-vs-INFO or INFO-vs-DEBUG classification, which stays a review obligation. Coverage is per-stage opt-in: the fixture is anchored on `packages` (`_load_packages`, `tests/test_stage_packages.py`) and `configure`'s dry run (`tests/test_stage_bootstrap.py`). A stage added without requesting the fixture is unguarded by construction, so **coverage thins with every stage that does not opt in** — requesting it is part of adding a stage, not a later cleanup. |
| 26 | Lint scope (every Python tree in the repo) | `sysforge/`, `tests/`, and `tools/` — all Python the repo owns, not only the shipped package | enforced | `make lint-py` runs `ruff check sysforge/ tests/ tools/`, and `.claude/hooks/ruff-on-edit.sh` blocks on the same three trees. The two must name the identical set: before 2.6.1-STD8 the gate covered `sysforge/` alone while the hook already blocked on `tests/`, so 189 violations accumulated in a tree nothing reported on and the hook fired on pre-existing debt in files an author had merely touched — enforcement strict enough to interrupt, scoped so it could only ever surface someone else's backlog. Rule selection is uniform (`[tool.ruff.lint] select`); the only per-tree relaxation is `[tool.ruff.lint.per-file-ignores]` `"tests/**" = ["S", "PTH", "SIM117"]`, each with a stated reason — `S` because test code is not production attack surface (2.3.0-F1), `PTH` because it fires on the `sys.path` import bootstrap repeated across ~25 test files, `SIM117` because collapsing stacked monkeypatch/mock context managers costs a line-length violation per test and reads worse. A new relaxation is a per-file-ignores entry with a comment, never a directory dropped from the gate. Scope note: `tools/` is deliberately **not** relaxed — it holds `check_standards.py`, `check_shipped.py`, and `release.sh`'s Python siblings, which the release process depends on. |

### Notes on selected standards

**XDG / FHS (1, 2).** User-side roots resolve through `_xdg_base(env, default)`
in `paths.py` — config under `$XDG_CONFIG_HOME`, regenerable cache under
`$XDG_CACHE_HOME`, fallback runtime state under `$XDG_STATE_HOME`, and
authoritative user-authored data under `$XDG_DATA_HOME`. System state
lives at `/var/lib/sysforge` (FHS application state) with the XDG state dir as a
non-root fallback; the regenerable PGO profdata cache lives at
`/var/cache/sysforge` (override: `SYSFORGE_PGO_STORE`). See **Config Layer** and
**Directory Structure**.

**SemVer (3).** Versions are strict `X.Y.Z`; the `-git` package carries the
`X.Y.Z.rN.gHASH` VCS suffix. `make release-{major,minor,patch}` is the only
bump path and keeps `pyproject.toml`, `PKGBUILD`, `PKGBUILD-git`, and the
`<!--version-->` doc markers in lockstep.
Which of the three bumps is correct is not a judgement call: it is derived from
the accumulated release notes and enforced in preflight (see row 3's enforcement
column). A ROADMAP entry declares its expected impact via its `Bump:` tag, but
that tag is gone by release time — the landing commit removes the entry — so the
accumulator is the authoritative record. Removing a deprecated surface is the
common cause of a forced major; see row 24.

**Keep a Changelog (13).** `docs/release-notes/vX.Y.Z.md` *is* the changelog
(there is no separate top-level `CHANGELOG.md` to drift). Entries use the Keep a
Changelog category headings: `Added`, `Changed`, `Deprecated`, `Removed`,
`Fixed`, `Security`. Those
headings are also load-bearing for versioning: the required SemVer bump is
derived from which of them the accumulator carries (row 3).
Notes are authored **incrementally**: each landing commit
that completes a ROADMAP item appends its entry to the running accumulator
`docs/release-notes/unreleased.md` in that same commit. At release,
`tools/release.sh` Phase 1 renames the accumulator to `vX.Y.Z.md`, stamps its
`# ` title with the version + ISO date, and reseeds a fresh accumulator; the
`release-notes` skill only reconciles/lints the accumulated entries. The
`changelog` check lints `unreleased.md` under the same vocabulary so entries are
validated as they land, not just at release.

**REUSE / SPDX (14).** SysForge is MIT-licensed (`LICENSE`). First-party source
files carry per-file SPDX headers (a copyright tag plus the `MIT` license
identifier); generated/data files are covered in bulk by `REUSE.toml`; license
texts live under `LICENSES/`. `reuse lint` (when installed) is the authoritative
check, with a header-presence grep fallback.

**Reproducible builds (15).** SysForge must not *undermine* the reproducibility
of packages it builds: it does not inject non-deterministic data, preserves
reproducibility-relevant `OPTIONS`, and passes `SOURCE_DATE_EPOCH` through to the
build environment unmodified.

**Arch packaging (11).** Two tiers underlie this row. The **machine-checkable
specs** — `PKGBUILD(5)`, `.SRCINFO`, `alpm-hooks(5)`, `makepkg.conf` — are
guarded by `check_shipped` (`pkgbuild`/`hooks` groups) and the `pkgbuild-*`
skills, and are the authority any parser/patcher change cross-checks (array vs
string fields, escape and brace-expansion rules, the `_<arch>` array families).
SysForge doesn't just ship `alpm-hooks(5)`-conforming files — it *fires* them:
the four libalpm PostTransaction hooks (kernel, toolchain, buildstate,
artifacts) each drop a sentinel that a corresponding `update` consumer picks up
on the next run (§update.md). `check_shipped`'s `hooks` group parity check
(`check_hooks`) already covers the new `artifacts` hook via `HOOK_NAMES`, so no
enforcement wiring beyond the existing group was needed.
Layered on top are the **prose conventions** that aren't mechanically lintable
but inform how SysForge generates and edits PKGBUILDs:

- Package guidelines — <https://wiki.archlinux.org/title/Arch_package_guidelines>
  (and the per-language sub-pages) — naming, `pkgrel`/`epoch` semantics, split-package
  layout, the `provides`/`conflicts`/`replaces` triad. The split-package handling
  (`match_rules` matching `pkgbase`, the `-sysforge` rename keeping every
  `package_<name>()` function) follows from here.
- VCS package guidelines — <https://wiki.archlinux.org/title/VCS_package_guidelines>
  — `-git` naming, the `pkgver()` auto-bump, full-history fetch (never `--depth=1`,
  which makes every advance look diverged). SysForge's `vcs_pkgver`/`source_sync`
  invariants implement this.
- Authoritative manual pages: [PKGBUILD(5)](https://man.archlinux.org/man/PKGBUILD.5),
  [makepkg(8)](https://man.archlinux.org/man/makepkg.8), and the upstream
  [package-guidelines manual](https://manual.archlinux.page/package-guidelines/).

These are reference conventions, not a separate gate — they back the existing
parser/patcher invariants in `sysforge/CLAUDE.md` (PKGBUILD
parsing/detection/patching, source-sync) rather than adding a parallel check.

**Privilege-escalation seam (18).** Escalation is `sudo`-based and per-operation;
`privileged_argv` makes the single "prepend sudo unless already root" decision so
there is one audit point. Auth probes (`sudo -v`, `sudo -n true`) and
drop-privilege (`sudo -u`) are not escalation and are allowlisted structurally.
Polkit/`pkexec` was evaluated and declined for this tool's TTY-bound execution
model; the seam is the insertion point should that change. See §22.

**CLAUDE.md citation freshness.** Guardrail files (`CLAUDE.md` at the repo root
for process conventions; `sysforge/CLAUDE.md` for code-seam invariants, loaded
lazily per-directory) cite concrete paths and `module.symbol` seams. The
`check_standards` `claude_md` group verifies every backticked citation still
resolves — missing paths and renamed symbols fail; tokens that cannot be mapped
to a repo file are skipped (fail-safe, no prose false positives).

**Roadmap ID collision check.** The `check_standards` `roadmap_ids` group
cross-checks the three homes of one ID namespace: `ROADMAP.md` (open items),
`docs/ROADMAP-ABANDONED.md` (abandoned items — a retired number is never
reissued) and `docs/release-notes/` (shipped items). Errors: an open ID reusing
a shipped number, an ID listed both as Planned and as Abandoned (a cross-file
check since `2.5.1-F4` split the sections), an `## Abandoned` heading back in
`ROADMAP.md`, a shipped `Q`-typed ID. Warn: sequence gaps within the active
`pyproject.toml` version prefix. Allocate the next ID with
`python tools/check_standards.py --next-id <version>-<TYPE>`. Known limitation:
a release-note that mentions a still-Planned ID in prose (a forward or "see
also" reference) is indistinguishable from a shipped citation and will trip
the collision check (check 1) — author release-notes to reference only IDs
that actually shipped in that note.

**OpenPGP signing (16).** Releases are signed end to end with the maintainer key:
the `release: vX.Y.Z` commit (`commit.gpgsign`), an annotated tag (`git tag -s`,
verified with `git tag -v`), and a detached signature of the source tarball
(`sysforge-X.Y.Z.tar.gz.asc`) uploaded to the GitHub release. The stable
`PKGBUILD` declares the maintainer fingerprint in `validpgpkeys` and lists the
`.asc` as a second `source` (paired with `SKIP`), so `makepkg` verifies the
maintainer signature at install time. `tools/release.sh` preflight refuses to run
without a usable signing key, and refuses to publish while the placeholder
fingerprint sentinel is still in the PKGBUILD. See **Release Process** and the
*Verifying releases* section of `README.md`.

### Adding or changing a standard

This list has one home — this file. To add a standard: add a row (with its
enforcement mechanism), wire the mechanical check into `check_standards.py` or a
behavioural test, and update the `check-standards` coverage. Do not maintain a
parallel standards list elsewhere; CLAUDE.md points here.

**Update this table in the same commit as any change that adopts, extends, or
alters conformance to an external spec** — the same in-commit discipline as the
`docs/design/*.md → make design` doc-update rule. If a change starts honouring a
new spec (or a new facet of one already listed), add or extend its row and wire
the enforcement in that commit; a row must never lag the behaviour it records.
Conversely, do not add a row for a spec the code does not yet conform to — those
live in `ROADMAP.md` as `Q`/`F`/`STD` items until the adopting change lands (each
such roadmap entry names its target row here).

The same discipline applies to the **SysForge-exclusive** subsection, whose rows
have no external spec to adopt: add or extend the row in the commit that
establishes or changes the policy, and wire its enforcement there. Choose the
subsection by whether an external specification defines the behaviour, not by
whether the row happens to carry a URL — rows 6 and 15 are externally grounded
without linking a document. New rows take the next number in the single global
sequence shared by both subsections; never renumber an existing row, because row
numbers are cited from code, tests, and published release notes.

---

