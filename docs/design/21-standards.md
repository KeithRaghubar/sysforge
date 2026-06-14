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
| 11 | `PKGBUILD(5)` · `.SRCINFO` · `alpm-hooks(5)` · `makepkg.conf` | Arch packaging artefacts | enforced | `pkgbuild-spec-check`/`pkgbuild-edit` skills; `check_shipped` `pkgbuild`/`hooks` groups |
| 12 | `man-pages(7)` via scdoc | Manual page | enforced | `make man`; `check_shipped` `manpage` group |
| 13 | [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) | Release notes | enforced | `docs/release-notes/vX.Y.Z.md` category vocabulary; `check_standards` `changelog` group |
| 14 | [REUSE](https://reuse.software/) / SPDX (license: **MIT**) | Per-file licensing | enforced | SPDX headers + `LICENSES/MIT.txt` + `REUSE.toml`; `check_standards` `spdx` group (`reuse lint`) |
| 15 | Reproducible builds | Builds SysForge produces | followed | does not strip reproducibility OPTIONS / honours `SOURCE_DATE_EPOCH`; `tests/test_standards_compliance.py` |

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
`Fixed`, `Security`.

**REUSE / SPDX (14).** SysForge is MIT-licensed (`LICENSE`). First-party source
files carry per-file SPDX headers (a copyright tag plus the `MIT` license
identifier); generated/data files are covered in bulk by `REUSE.toml`; license
texts live under `LICENSES/`. `reuse lint` (when installed) is the authoritative
check, with a header-presence grep fallback.

**Reproducible builds (15).** SysForge must not *undermine* the reproducibility
of packages it builds: it does not inject non-deterministic data, preserves
reproducibility-relevant `OPTIONS`, and passes `SOURCE_DATE_EPOCH` through to the
build environment unmodified.

### Adding or changing a standard

This list has one home — this file. To add a standard: add a row (with its
enforcement mechanism), wire the mechanical check into `check_standards.py` or a
behavioural test, and update the `check-standards` coverage. Do not maintain a
parallel standards list elsewhere; CLAUDE.md points here.

---

