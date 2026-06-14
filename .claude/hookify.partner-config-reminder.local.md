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

`etc/sysforge/` (shipped defaults, installed by PKGBUILD) and `tests/data/etc/sysforge/` (the maintainer's live config used by `SYSFORGE_CONFIG_DIR`) are **separate trees, not symlinked**. Most changes that affect one need an explicit matching change to the other.

Before finishing, decide:

- Is this change shipped-defaults-only? → only `etc/sysforge/` needs to change.
- Is this change live-config-only? → only `tests/data/etc/sysforge/` needs to change.
- Is this a real config update? → update **both** explicitly.

See sysforge/CLAUDE.md `Dev Environment` section.
