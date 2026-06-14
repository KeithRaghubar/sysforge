---
name: completions-sync
enabled: true
event: file
action: warn
conditions:
  - field: file_path
    operator: regex_match
    pattern: (?:^|/)sysforge/cli(?:\.py$|/)
---

**CLI surface edited — update `completions/_sysforge` in this same change.**

Project convention: `completions/_sysforge` (zsh completion) stays in lockstep with the CLI. Do not defer it as a follow-up.

If you added a verb, flag, or subcommand:

- Add the matching entry to `completions/_sysforge`.
- Re-test completion behavior in a fresh zsh shell if the structure changed.

If you only changed implementation behavior (no surface change), this warning is a no-op — proceed.

See sysforge/CLAUDE.md `Project Conventions` section.
