---
name: standards-adherence
enabled: true
event: file
action: warn
conditions:
  - field: file_path
    operator: regex_match
    pattern: (?:^|/)(?:sysforge/(?:primitives/paths\.py|cli\.py|log\.py|__init__\.py|ui/progress\.py)|pyproject\.toml|PKGBUILD(?:-git)?)$
---

**Standards-bearing file edited — cross-check `docs/design/21-standards.md`.**

This file participates in one of sysforge's committed standards. Before finishing, confirm the relevant standard still holds:

- `sysforge/primitives/paths.py` → **XDG Base Directory** + **FHS**. User dirs must honour `XDG_*_HOME` and fall back to the spec defaults (`~/.config`, `~/.cache`, `~/.local/state`). No other module may construct a sysforge user dir — that's the `paths` group in `check_standards`.
- `sysforge/cli.py` → **POSIX/GNU CLI** + **stdout/stderr + exit-code** contract. Errors to stderr, UI to stdout; keep `--version`/`--help` working. (Also: completions + manpage stay in lockstep — see the `completions-sync` rule.)
- `sysforge/log.py` / `sysforge/ui/progress.py` → **NO_COLOR + FORCE_COLOR**. `log.use_color()` is the single colour authority; don't add a second gate or hand-write escape codes.
- `pyproject.toml` / `PKGBUILD` / `PKGBUILD-git` / `sysforge/__init__.py` → **SemVer 2.0.0** (`X.Y.Z`, or `X.Y.Z.rN.gHASH` for `-git`) and **PEP 517/518/621** packaging. Version format is gated by `check_shipped`; license metadata feeds **REUSE/SPDX**.

The checkable subset is enforced by `make check-standards` (+ `tests/test_standards_compliance.py` via `make test`) and gates releases. If this edit doesn't touch a standard's surface, this warning is a no-op — proceed.

See `docs/design/21-standards.md` (source of truth) and sysforge/CLAUDE.md `Project Conventions`.
