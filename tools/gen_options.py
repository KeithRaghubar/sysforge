#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
gen_options.py — generate the COMMANDS sections of sysforge(1) from argparse.

Reads the hand-written scdoc template (man/sysforge.1.scd.in), replaces the
@OPTIONS@ marker line with per-command scdoc sections derived from
``sysforge.verbs.registry.build_parser()`` and the @SHARED_OPTIONS@ marker with
the options several commands carry verbatim (written once, each command naming
them in a ``Shared options`` line), and writes the complete scdoc source
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

from sysforge.verbs.registry import build_parser, tiered_command_order  # noqa: E402

MARKER = "@OPTIONS@"
SHARED_MARKER = "@SHARED_OPTIONS@"

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
    "state boot-entries": (
        "kernel.toml (bootloader, boot\\_entries)",
        "$SYSFORGE_CONFIG_DIR, $PAGER",
    ),
    "env": (
        "profiles.toml",
        "$SYSFORGE_CONFIG_DIR",
    ),
    "log": (
        "profiles.toml ([paths] pkgbuild\\_src\\_dir, for per-package logs)",
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


def _value_token(action) -> str:
    """The placeholder for an option's value: its metavar, else its choices as
    ``a|b`` (what --help shows), else argparse's dest-derived name."""
    if action.metavar:
        return f"_{action.metavar}_"
    if action.choices:
        return "|".join(f"_{c}_" for c in action.choices)
    return f"_{action.dest.upper()}_"


def _option_head(action) -> str:
    head = ", ".join(f"*{o}*" for o in action.option_strings)
    if action.nargs != 0:  # store/append take a value; store_true/count don't
        head += f" {_value_token(action)}"
    return head


def _split_actions(parser):
    """(positionals, optionals, has_subcommands) for one parser."""
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
    return positionals, optionals, has_subcommands


def _shared_options(commands) -> dict[tuple[str, str], list[str]]:
    """The options written once in SHARED OPTIONS: ``(head, help)`` -> the
    commands that carry it.

    An option is shared when two or more commands carry it with the same head
    and the same help text. A flag whose help differs between commands shares
    only its most common text (first-seen on a tie), so SHARED OPTIONS never
    lists one flag twice; the other texts stay inline where they mean
    something different.
    """
    users: dict[tuple[str, str], list[str]] = {}
    for name, sub, _ in commands:
        for a in _split_actions(sub)[1]:
            users.setdefault((_option_head(a), a.help or ""), []).append(name)
    best: dict[str, tuple[str, str]] = {}
    for key, names in users.items():
        if len(names) < 2:
            continue
        held = best.get(key[0])
        if held is None or len(names) > len(users[held]):
            best[key[0]] = key
    return {key: users[key] for key in best.values()}


def _iter_commands(parser, prefix="", order=None):
    """Yield (qualified name, subparser, help) depth-first.

    Top-level commands follow ``order`` (the ``sysforge --help`` usage tiers,
    2.5.0-F1) so the man-page COMMANDS section stays in lockstep with the help
    grouping; any command absent from ``order`` sorts to the end (defensive —
    should never happen while the parity test holds). Sub-commands keep their
    registration order (``order`` is only threaded at the top level)."""
    for act in parser._actions:
        if not isinstance(act, argparse._SubParsersAction):
            continue
        helps = {ca.dest: ca.help for ca in act._choices_actions}
        names = list(act.choices)
        if order is not None:
            names.sort(key=lambda n: order.index(n) if n in order else len(order))
        for name in names:
            sub = act.choices[name]
            # Parsers registered without help= are internal plumbing
            # (`completions`); they stay out of the man page.
            if helps.get(name) is None:
                continue
            yield f"{prefix}{name}", sub, helps[name]
            yield from _iter_commands(sub, prefix=f"{prefix}{name} ")


def _command_section(name, parser, help_txt, shared) -> list[str]:
    positionals, optionals, has_subcommands = _split_actions(parser)

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
    shared_here = []
    for a in optionals:
        head = _option_head(a)
        if (head, a.help or "") in shared:
            shared_here.append(", ".join(f"*{o}*" for o in a.option_strings))
            continue
        lines += [head, f"\t{_esc(a.help or '')}", ""]
    if shared_here:
        lines += [f"*Shared options:* {', '.join(shared_here)} "
                  "(see SHARED OPTIONS)", ""]
    if name in _VERB_CONFIG:
        config_txt, env_txt = _VERB_CONFIG[name]
        lines += [f"*Configuration:* {config_txt}", "",
                  f"*Environment:* {env_txt}", ""]
    return lines


def render(parser, order) -> tuple[list[str], list[str]]:
    """(SHARED OPTIONS lines, COMMANDS lines) for the parser tree."""
    commands = list(_iter_commands(parser, order=order))
    shared = _shared_options(commands)

    shared_lines: list[str] = []
    for (head, help_txt), names in sorted(shared.items()):
        used_by = ", ".join(f"*{n}*" for n in names)
        shared_lines += [head, f"\t{_esc(help_txt)}", f"\tUsed by: {used_by}.", ""]

    command_lines: list[str] = []
    for name, sub, help_txt in commands:
        command_lines += _command_section(name, sub, help_txt, shared)
    return shared_lines, command_lines


def _fill(template: str, marker: str, lines: list[str]) -> str | None:
    """Replace ``marker`` (alone on its line) with ``lines``; None if absent."""
    if marker not in template:
        return None
    body = "\n".join(lines).rstrip() + "\n"
    rendered = template.replace(marker + "\n", body, 1)
    if marker in rendered:  # marker had no trailing newline form
        rendered = rendered.replace(marker, body, 1)
    return rendered


def main() -> int:
    ap_ = argparse.ArgumentParser(description=__doc__)
    ap_.add_argument("--template", required=True)
    ap_.add_argument("--out", required=True)
    ns = ap_.parse_args()

    shared_lines, command_lines = render(build_parser(), tiered_command_order())

    rendered = Path(ns.template).read_text(encoding="utf-8")
    for marker, lines in ((SHARED_MARKER, shared_lines), (MARKER, command_lines)):
        filled = _fill(rendered, marker, lines)
        if filled is None:
            print(f"error: marker {marker} not found in {ns.template}",
                  file=sys.stderr)
            return 1
        rendered = filled
    Path(ns.out).write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
