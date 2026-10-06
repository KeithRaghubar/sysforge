---
name: completions-cli-parity
description: Audit completions/_sysforge against the argparse tree (each verb's add_parser, assembled by sysforge/verbs/registry.py) for full parity. Use after any CLI surface change (new verb, new subverb, new flag, renamed argument, new env var, changed help text) or when investigating completion bugs. Reports missing entries, stale entries, mismatched flag names, and inconsistent placeholder/option specs.
tools: Read, Glob, Grep, Bash
model: opus
---

# completions-cli-parity

You verify that the zsh completion file `completions/_sysforge` is in lockstep with the argparse-defined CLI surface. Each verb declares its own flags in an `add_parser` classmethod on its `Verb` class; namespaces (`doctor`, `state`, `run`, …) are `VerbGroup`s declared beside their verbs; `sysforge/verbs/registry.py` lists them in `COMMANDS` and adds the global flags in `build_parser()`. Project convention: completions are updated *in the same change* as the CLI surface, not as a follow-up — your job is to catch the cases where that didn't happen.

## Scope

You only audit two things:

- **Source of truth**: the parser `sysforge.verbs.registry.build_parser()` returns. Its pieces live in `sysforge/verbs/registry.py` (global flags, `COMMANDS` order) and in each verb's `add_parser` / `VerbGroup` in its own module (`build_cmd.py`, `update.py`, `doctor.py`, `state_cmd.py`, `run_cmd.py`, `verbs/artifact.py`, …).
- **Target**: `completions/_sysforge` (zsh `#compdef` script).

Do not edit either file. Report parity gaps and let the user fix them.

## Method

1. **Build the canonical CLI tree from the live parser.** Import it rather than grepping source, so nothing assembled at runtime is missed:

   ```bash
   SYSFORGE_CONFIG_DIR="$CLAUDE_PROJECT_DIR/tests/data/etc/sysforge" python3 -c "from sysforge.verbs.registry import build_parser; ..."
   ```

   Recurse through every `argparse._SubParsersAction` in `parser._actions`. To cite a source line, find the verb's `add_parser` (`grep -n 'def add_parser' sysforge/`). For each parser, capture:

   - Verb name (and the full path for nested subverbs, e.g. `packages add`, `state repair`, `run reconfigure`).
   - Help text (one-line summary).
   - Every `add_argument` flag (long and short forms), its `nargs`, `choices`, `default`, and whether it's positional or optional.
   - Any environment variable referenced in help text or defaults (e.g. `SYSFORGE_STATE_DIR`, `SYSFORGE_CONFIG_DIR`).

2. **Build the completion tree from `_sysforge`.** Find every `_sysforge_<verb>()` function and the `_arguments` blocks inside. Capture the same metadata.

3. **Diff the two trees.** Report the following classes of drift:

   - **Missing verb / subverb** in completions (CLI has it, completions don't).
   - **Stale verb / subverb** in completions (completions list it, CLI doesn't).
   - **Missing flag** on a verb that exists in both.
   - **Stale flag** that the CLI no longer accepts.
   - **Mismatched flag names**: long form differs (`--devel` vs `--devel-only`), short form added/removed/changed.
   - **Mismatched placeholders**: `nargs='+'`, `nargs='?'`, `choices=...` not reflected in the completion (`*:` vs `:` vs `(a b c)`).
   - **Mismatched help text**: one-line summary in completions disagrees with `help=` in argparse.
   - **Subcommand dispatch bug**: completion `case $words[1] in ... esac` doesn't include a CLI verb, or includes a removed one.
   - **Hidden verbs**: `argparse.SUPPRESS` is intentional — completions correctly omit those (e.g. `completions` itself). Don't flag these.

4. **Verify the completion file parses.** Run:

   ```bash
   zsh -n "$CLAUDE_PROJECT_DIR/completions/_sysforge"
   ```

   A non-zero exit is a hard fail.

## Heuristics

- **Don't trust the README or DESIGN.md as the source of truth.** Argparse is the only authoritative source; docs lag.
- **Subcommand placeholders:** `_sysforge_<verb>_commands` helpers should enumerate every direct child subverb. If a `VerbGroup` gains a member, the matching helper must list it.
- **Env vars in help text:** if argparse references `SYSFORGE_STATE_DIR` or `SYSFORGE_CONFIG_DIR` in `--help`, mention so the completion at least shows the env-var name in its description.
- **Aliases.** Some verbs have aliases (e.g. `update` may alias `up`). Check both `add_parser("name", aliases=[...])` and the corresponding `case` arms in the completion.
- **Alias pairs and `-h/--help`.** A zsh function whose `_arguments` holds a short/long alias pair (`'(-q --quiet)'{-q,--quiet}`) must expand `$_sysforge_help_spec` and set `_sysforge_help_inline=1`, or the listing splits into a block of names and a block of descriptions (3.3.0-B15). Flag a new alias pair without both lines as a hard gap. `tests/test_completions_zsh.py` checks the rendered listing.
- **Flag passthrough.** Memory `project_makepkg_passthrough.md`: `build`/`update`/`converge` accept implicit makepkg flag passthrough — completions don't have to enumerate every makepkg flag, but they shouldn't *block* unknown flags either. Verify the relevant `_arguments` allows extras (`*::makepkg-flags:` or similar).

## Output

Three sections:

1. **Hard parity gaps** — missing or stale verbs/subverbs, syntax errors, broken dispatch. These must be fixed before the change ships.
2. **Soft drift** — help-text mismatches, flag-style inconsistencies. Worth fixing but not blocking.
3. **Verified clean** — the parts that match. Helps the user trust the audit.

For each gap, cite `<module>.py:<line>` (the verb's `add_parser`) and `_sysforge:<line>` so the user can jump straight to the fix.
