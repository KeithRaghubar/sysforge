#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
gen_options.py — generate the COMMANDS sections of sysforge(1) from argparse.

Reads the hand-written scdoc template (man/sysforge.1.scd.in), replaces the
@OPTIONS@ marker line with per-command scdoc sections derived from
``sysforge.cli._build_parser()``, and writes the complete scdoc source
(man/sysforge.1.scd — an intermediate, not committed; ``scdoc`` then renders
man/sysforge.1, which is committed).

The hand-written prose (NAME / SYNOPSIS / DESCRIPTION / FILES / EXAMPLES / …)
lives in the template; the per-command option blocks are generated here so
they can never drift from the CLI. The check_shipped ``manpage`` group reruns
this exact pipeline and diffs against the committed page on every release.

Usage:
    python tools/gen_options.py --template man/sysforge.1.scd.in \
        --out man/sysforge.1.scd
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sysforge.cli import _build_parser  # noqa: E402

MARKER = "@OPTIONS@"

# Per-command configuration/environment summary, rendered as a trailer after
# each command's option blocks. Values are raw scdoc (not escaped). Keyed by
# the qualified command name; commands without an entry get no trailer. The
# FILES / ENVIRONMENT sections of the template carry the inverse index
# ("Read by: ...") — update both when a verb gains or loses a config source.
_VERB_CONFIG: dict[str, tuple[str, str]] = {
    "build": (
        "profiles.toml, packages.toml ([build]), sysforge.toml ([git])",
        "$SYSFORGE_CONFIG_DIR, $SYSFORGE_STATE_DIR, $PAGER (review diffs)",
    ),
    "fetch": (
        "profiles.toml ([paths]), sysforge.toml ([git])",
        "$SYSFORGE_CONFIG_DIR",
    ),
    "update": (
        "profiles.toml, packages.toml, sysforge.toml ([git], "
        "failure\\_handling)",
        "$SYSFORGE_CONFIG_DIR, $SYSFORGE_STATE_DIR, $PAGER (review diffs)",
    ),
    "resolve": (
        "profiles.toml",
        "$SYSFORGE_CONFIG_DIR",
    ),
    "doctor": (
        "profiles.toml; toolchain.toml and kernel.toml on their axes",
        "$SYSFORGE_CONFIG_DIR, $SYSFORGE_STATE_DIR",
    ),
    "packages": (
        "packages.toml (read/write)",
        "$SYSFORGE_CONFIG_DIR",
    ),
    "state": (
        "build\\_state.toml under the state directory",
        "$SYSFORGE_STATE_DIR, $PAGER",
    ),
    "env": (
        "profiles.toml",
        "$SYSFORGE_CONFIG_DIR",
    ),
    "log": (
        "per-package logs under the state directory",
        "$SYSFORGE_STATE_DIR, $PAGER",
    ),
    "run pipeline": (
        "profiles.toml, packages.toml, toolchain.toml, kernel.toml, "
        "sysforge.toml (per stage reached)",
        "$SYSFORGE_CONFIG_DIR, $SYSFORGE_STATE_DIR",
    ),
    "run hardware": (
        "writes the hardware profile under the state directory",
        "$SYSFORGE_STATE_DIR",
    ),
    "run reconfigure": (
        "profiles.toml, packages.toml, toolchain.toml, kernel.toml, "
        "sysforge.toml (reviewed interactively)",
        "$SYSFORGE_STATE_DIR, $SYSFORGE_EDITOR (then $EDITOR, $VISUAL)",
    ),
    "run toolchain": (
        "toolchain.toml, profiles.toml, sysforge.toml ([git], [aur], "
        "[safety])",
        "$SYSFORGE_CONFIG_DIR, $SYSFORGE_STATE_DIR, $RUSTUP_TOOLCHAIN",
    ),
    "run packages": (
        "packages.toml, profiles.toml",
        "$SYSFORGE_CONFIG_DIR, $SYSFORGE_STATE_DIR",
    ),
    "run kernel": (
        "kernel.toml, profiles.toml",
        "$SYSFORGE_CONFIG_DIR, $SYSFORGE_STATE_DIR",
    ),
}


def _esc(text: str) -> str:
    """Escape scdoc formatting characters in argparse help text."""
    return (text.replace("\\", "\\\\")
                .replace("*", "\\*")
                .replace("_", "\\_"))


def _pos_token(action) -> str:
    mv = action.metavar or action.dest.upper()
    if action.nargs == "*":
        return f"[{mv}...]"
    if action.nargs == "+":
        return f"{mv}..."
    if action.nargs == "?":
        return f"[{mv}]"
    return mv


def _iter_commands(parser, prefix=""):
    """Yield (qualified name, subparser, help) depth-first in CLI order."""
    for act in parser._actions:
        if not isinstance(act, argparse._SubParsersAction):
            continue
        helps = {ca.dest: ca.help for ca in act._choices_actions}
        for name, sub in act.choices.items():
            # Parsers registered without help= are internal plumbing
            # (`completions`); they stay out of the man page.
            if helps.get(name) is None:
                continue
            yield f"{prefix}{name}", sub, helps[name]
            yield from _iter_commands(sub, prefix=f"{prefix}{name} ")


def _command_section(name, parser, help_txt) -> list[str]:
    positionals, optionals = [], []
    has_subcommands = False
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            has_subcommands = True
        elif a.dest == "help":
            continue
        elif a.option_strings:
            optionals.append(a)
        else:
            positionals.append(a)

    synopsis = f"*sysforge {name}*"
    if optionals:
        synopsis += " [_OPTIONS_]"
    if has_subcommands:
        synopsis += " _SUBCOMMAND_"
    for a in positionals:
        synopsis += f" _{_pos_token(a)}_"

    lines = [f"## {name}", "", synopsis, ""]
    if help_txt:
        sentence = help_txt[0].upper() + help_txt[1:]
        if not sentence.endswith("."):
            sentence += "."
        lines += [_esc(sentence), ""]
    for a in positionals:
        if a.help:
            lines += [f"*{a.metavar or a.dest.upper()}*",
                      f"\t{_esc(a.help)}", ""]
    for a in optionals:
        head = ", ".join(f"*{o}*" for o in a.option_strings)
        if a.nargs != 0:  # store/append take a value; store_true/count don't
            head += f" _{a.metavar or a.dest.upper()}_"
        lines += [head, f"\t{_esc(a.help or '')}", ""]
    if name in _VERB_CONFIG:
        config_txt, env_txt = _VERB_CONFIG[name]
        lines += [f"*Configuration:* {config_txt}", "",
                  f"*Environment:* {env_txt}", ""]
    return lines


def main() -> int:
    ap_ = argparse.ArgumentParser(description=__doc__)
    ap_.add_argument("--template", required=True)
    ap_.add_argument("--out", required=True)
    ns = ap_.parse_args()

    parser = _build_parser()
    body_lines: list[str] = []
    for name, sub, help_txt in _iter_commands(parser):
        body_lines += _command_section(name, sub, help_txt)
    body = "\n".join(body_lines).rstrip() + "\n"

    template = Path(ns.template).read_text(encoding="utf-8")
    if MARKER not in template:
        print(f"error: marker {MARKER} not found in {ns.template}",
              file=sys.stderr)
        return 1
    rendered = template.replace(MARKER + "\n", body, 1)
    if MARKER in rendered:  # marker had no trailing newline form
        rendered = rendered.replace(MARKER, body, 1)
    Path(ns.out).write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
