---
name: partner-config-reminder
enabled: true
event: file
action: warn
conditions:
  - field: file_path
    operator: regex_match
    pattern: (?:^|/)(etc/sysforge|tests/data/etc/sysforge)/
---

**Partner config tree may need the same change.**

`etc/sysforge/` (shipped defaults, installed by PKGBUILD) and `tests/data/etc/sysforge/` (git-tracked **test fixtures**) are **separate trees, not symlinked** — kept in shipped↔fixture parity by `make check-shipped`. The maintainer's personal live config is a *third*, untracked tree (e.g. `~/sf-config/etc/sysforge`, pointed at by `SYSFORGE_CONFIG_DIR`); it is not in the repo and is adopted from new shipped defaults via `make sync-config`.

Before finishing, decide:

- Is this change shipped-defaults-only (no key the fixtures exercise)? → only `etc/sysforge/` needs to change, but run `make check-shipped` to confirm parity still holds.
- Is this a real config update (new/changed key)? → update **both** `etc/sysforge/` and `tests/data/etc/sysforge/` explicitly so `make check-shipped` passes.

See sysforge/CLAUDE.md `Dev Environment` section.
