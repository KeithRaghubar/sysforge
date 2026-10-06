---
name: completions-sync
enabled: true
event: file
action: warn
conditions:
  # Each verb's flags live in its own module's add_parser (3.2.0-F7), so the
  # surface is any sysforge/ edit that touches an argparse call or a VerbGroup.
  - field: file_path
    operator: regex_match
    pattern: (?:^|/)sysforge/.*\.py$
  - field: new_text
    operator: regex_match
    pattern: \badd_(?:argument|parser|subparsers|mutually_exclusive_group)\(|\bVerbGroup\(
---

**CLI surface edited — update `completions/_sysforge` in this same change.**

Project convention: `completions/_sysforge` (zsh completion) stays in lockstep with the CLI. Do not defer it as a follow-up.

If you added a verb, flag, or subcommand:

- Add the matching entry to `completions/_sysforge`.
- Re-test completion behavior in a fresh zsh shell if the structure changed.

If you only changed implementation behavior (no surface change), this warning is a no-op — proceed.

See sysforge/CLAUDE.md `Project Conventions` section.
