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

| # | Standard | Scope | Status | How it is enforced |
|---|----------|-------|--------|--------------------|
| 1 | [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/) | User dirs (`~/.config`, `~/.cache`, `~/.local/state`) | enforced | `primitives/paths.py` (`_xdg_base`); `check_standards` `paths` group; `tests/test_paths.py` |
| 2 | Filesystem Hierarchy Standard + systemd `file-hierarchy(7)` | System roots (`/etc`, `/var/lib`, `/var/cache`, `/run`) | enforced | `paths.py` (`CONFIG_BASE`), `pipeline/state.py`, `makepkg_pgo.py`; `check_standards` `paths` group |
| 3 | [Semantic Versioning 2.0.0](https://semver.org/) | Project version scheme | enforced | `tools/check_shipped.py` `versions` group (format + cross-file parity) |
| 4 | POSIX Utility Conventions + GNU long-options | CLI argument grammar (`-h/--help`, `-V/--version`, `--`) | followed | argparse in `cli.py`; `tests/test_standards_compliance.py` |
| 5 | [NO_COLOR](https://no-color.org/) + `FORCE_COLOR` | Terminal colour control | enforced | `log.use_color()` (single authority); `tests/test_standards_compliance.py` |
| 6 | stdout/stderr separation + exit-code contract | CLI behaviour (data→stdout, diagnostics→stderr; 0/1/2) | followed | `log._out()`, `verbs/runner.py`; `tests/test_standards_compliance.py` |
| 7 | [TOML 1.0.0](https://toml.io/en/v1.0.0) | Config + state file format | followed | `tomllib` everywhere; `check_shipped` `configs` group |
| 8 | RFC 3339 / ISO 8601 (UTC) | Timestamps in state files | followed | central `_now_iso()` helpers; `tests/test_standards_compliance.py` |
| 9 | UTF-8 | Text file encoding | enforced | explicit `encoding="utf-8"`; `check_standards` `encoding` group (ruff `PLW1514 --preview` is the one-shot fixer) |
| 10 | PEP 517 / 518 / 621 / 508 | Python packaging metadata | followed | `pyproject.toml` (hatchling backend, `[project]` table) |
| 11 | `PKGBUILD(5)` · `.SRCINFO` · `alpm-hooks(5)` · `makepkg.conf` + [Arch package guidelines](https://wiki.archlinux.org/title/Arch_package_guidelines) / [VCS package guidelines](https://wiki.archlinux.org/title/VCS_package_guidelines) | Arch packaging artefacts + conventions | enforced | `pkgbuild-spec-check`/`pkgbuild-edit` skills; `check_shipped` `pkgbuild`/`hooks` groups |
| 12 | `man-pages(7)` via scdoc | Manual page | enforced | `make man`; `check_shipped` `manpage` group |
| 13 | [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) | Release notes | enforced | `docs/release-notes/vX.Y.Z.md` + `unreleased.md` accumulator category vocabulary; `check_standards` `changelog` group |
| 14 | [REUSE](https://reuse.software/) / SPDX (license: **MIT**) | Per-file licensing | enforced | SPDX headers + `LICENSES/MIT.txt` + `REUSE.toml`; `check_standards` `spdx` group (`reuse lint`) |
| 15 | Reproducible builds | Builds SysForge produces | followed | does not strip reproducibility OPTIONS / honours `SOURCE_DATE_EPOCH`; `tests/test_standards_compliance.py` |
| 16 | OpenPGP signing (RFC 4880) + makepkg `validpgpkeys` | Release provenance (signed commits, tags, tarball) | followed | `tools/release.sh` (signing preflight + `git tag -s` + tarball `.asc`); `check_shipped` `pkgbuild` group (`validpgpkeys` + signature-aware `SKIP`); verified downstream by `makepkg` |
| 17 | Subprocess-seam discipline (argv-list execution) | External-command execution (all `subprocess` sites) | enforced | argv-**list** form only, `shell=True` needs justified `# noqa: S602`; `primitives/run.py` (`run_or_raise`) sanctioned seam, direct callers a documented carve-out for streaming/returncode/stdout-parsing; ruff `S602` + `check_standards` `run_seam` group |
| 18 | Privilege-escalation seam | Root-escalating subprocess invocations | enforced | `primitives/privilege.py` (`privileged_argv`/`run_privileged`) is the sole home for `sudo`-prefixed escalation; raw `["sudo", …]` argv outside it is forbidden except the allowlisted auth-probe (`sudo -v`, `sudo -n true`) and drop-privilege (`sudo -u <user>`) forms; `check_standards` `privilege_seam` group + `tests/test_standards_compliance.py` |

### Notes on selected standards

**XDG / FHS (1, 2).** User-side roots resolve through `_xdg_base(env, default)`
in `paths.py` — config under `$XDG_CONFIG_HOME`, regenerable cache under
`$XDG_CACHE_HOME`, fallback runtime state under `$XDG_STATE_HOME`. System state
lives at `/var/lib/sysforge` (FHS application state) with the XDG state dir as a
non-root fallback; the regenerable PGO profdata cache lives at
`/var/cache/sysforge` (override: `SYSFORGE_PGO_STORE`). See **Config Layer** and
**Directory Structure**.

**SemVer (3).** Versions are strict `X.Y.Z`; the `-git` package carries the
`X.Y.Z.rN.gHASH` VCS suffix. `make release-{major,minor,patch}` is the only
bump path and keeps `pyproject.toml`, `PKGBUILD`, `PKGBUILD-git`, and the
`<!--version-->` doc markers in lockstep.

**Keep a Changelog (13).** `docs/release-notes/vX.Y.Z.md` *is* the changelog
(there is no separate top-level `CHANGELOG.md` to drift). Entries use the Keep a
Changelog category headings: `Added`, `Changed`, `Deprecated`, `Removed`,
`Fixed`, `Security`. Notes are authored **incrementally**: each landing commit
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
cross-checks `ROADMAP.md` (open items) against `docs/release-notes/` (shipped
items). Errors: an open ID reusing a shipped number, an ID in both Planned and
Abandoned, a shipped `Q`-typed ID. Warn: sequence gaps within the active
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

---

