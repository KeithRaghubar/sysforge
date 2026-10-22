# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
Parsed command lines for tests (3.5.0-DEV13).

``cli_args("state", "list", state_dir=d)`` parses ``sysforge state list`` with
the real parser, then sets each keyword on the result. Every field the command
has is therefore present with its real default, and a keyword the command does
not have fails at once, so a renamed flag cannot leave a test passing on a
name production never sets. This replaces ``SimpleNamespace`` stand-ins
(tests/test_stand_ins.py).
"""
from __future__ import annotations

from sysforge.verbs.registry import build_parser


def cli_args(*argv: str, **fields):
    args = build_parser().parse_args(list(argv))
    for name, value in fields.items():
        if not hasattr(args, name):
            raise AttributeError(f"`sysforge {' '.join(argv)}` has no {name!r}")
        setattr(args, name, value)
    return args
