# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
verbs/registry.py — the ordered verb registry and the top-level parser.

Each verb owns its argparse surface (:meth:`Verb.add_parser`), and each
namespace is a :class:`VerbGroup` declared beside its verbs. This module only
lists them in order and adds what belongs to no single verb: the global flags
and the tiered ``sysforge --help`` layout (3.2.0-F7). ``cli._build_parser`` is a
re-export of :func:`build_parser`.

Verb modules must not import this module at load time: it imports all of them.
``help_cmd`` imports it inside ``execute``.
"""
from __future__ import annotations

import argparse

from sysforge.build_cmd import BuildVerb
from sysforge.completions_cmd import CompletionsVerb
from sysforge.config_cmd import CONFIG_GROUP
from sysforge.doctor import DOCTOR_GROUP
from sysforge.env_cmd import EnvVerb
from sysforge.fetch import FetchVerb
from sysforge.help_cmd import HelpVerb
from sysforge.log_cmd import LogVerb
from sysforge.packages_cmd import PACKAGES_GROUP
from sysforge.resolve import ResolveVerb
from sysforge.revert_cmd import RevertToStockVerb
from sysforge.run_cmd import RUN_GROUP
from sysforge.search_cmd import SearchVerb
from sysforge.setup_cmd import SetupVerb
from sysforge.state_cmd import STATE_GROUP
from sysforge.uninstall_cmd import UninstallVerb
from sysforge.update import UpdateVerb
from sysforge.verbs.artifact import ARTIFACT_GROUP
from sysforge.verbs.base import Verb, VerbGroup

#: Every top-level command, in registration order. This order is what argparse
#: prints in an "invalid choice" error; the ``--help`` listing uses
#: ``_COMMAND_TIERS`` instead. ``completions`` (used by the shell completion
#: scripts, not user-facing) is last and in no tier.
COMMANDS: tuple[type[Verb] | VerbGroup, ...] = (
    ARTIFACT_GROUP,
    BuildVerb,
    FetchVerb,
    UpdateVerb,
    ResolveVerb,
    DOCTOR_GROUP,
    PACKAGES_GROUP,
    STATE_GROUP,
    RevertToStockVerb,
    RUN_GROUP,
    SearchVerb,
    SetupVerb,
    UninstallVerb,
    EnvVerb,
    HelpVerb,
    LogVerb,
    CONFIG_GROUP,
    CompletionsVerb,
)


# Top-level COMMAND help tiers (2.5.0-F1). Presentation-only grouping so a new
# user can tell routine verbs from ad-hoc introspection instead of reading one
# flat, registration-ordered block. This tuple is the single source of truth:
# `_TieredHelpFormatter` renders `sysforge --help` from it, and
# tools/gen_options.py orders the man-page COMMANDS sections by
# `tiered_command_order()` — keep every user-facing verb in exactly one tier
# (a `check_completions`-style parity test guards against drift). The internal
# `completions` verb is deliberately absent (it carries no help text and never
# appears in the listing).
_COMMAND_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Everyday", ("build", "update", "fetch", "search", "help")),
    ("Inspect", ("doctor", "resolve", "env", "log", "state", "artifact")),
    ("Maintain",
     ("setup", "config", "packages", "run", "revert-to-stock", "uninstall")),
)


def tiered_command_order() -> list[str]:
    """Flat top-level COMMAND order matching the ``sysforge --help`` tiers
    (2.5.0-F1). Consumed by tools/gen_options.py to keep the man-page COMMANDS
    section in lockstep with the help grouping."""
    return [name for _label, names in _COMMAND_TIERS for name in names]


class _TieredHelpFormatter(argparse.HelpFormatter):
    """Render the top-level COMMAND list grouped into usage tiers (2.5.0-F1)
    rather than one flat block.

    argparse collapses every subparser into a single ``_SubParsersAction``
    pseudo-group, so there is no per-command category hook — we intercept that
    one action here and re-emit its choices under ``_COMMAND_TIERS`` headers.
    Every other action (options, the ``COMMAND`` metavar line, per-verb help)
    formats exactly as the base class would; sub-verb help (``sysforge build
    --help``) is untouched because those parsers use the default formatter."""

    def _format_action(self, action):
        if not isinstance(action, argparse._SubParsersAction):
            return super()._format_action(action)

        subactions = {sa.dest: sa for sa in action._get_subactions()}
        # Bare ``COMMAND`` metavar line (matches argparse's no-help header).
        parts = ["%*s%s\n" % (self._current_indent, "",
                              self._format_action_invocation(action))]
        self._indent()  # tier labels one level under COMMAND
        for label, names in _COMMAND_TIERS:
            parts.append("\n%*s%s:\n" % (self._current_indent, "", label))
            self._indent()  # commands one level under the tier label
            for name in names:
                sa = subactions.get(name)
                if sa is not None:
                    parts.append(super()._format_action(sa))
            self._dedent()
        self._dedent()
        return self._join_parts(parts)


def build_parser() -> argparse.ArgumentParser:
    """Return the top-level ArgumentParser: the global flags, then one subparser
    per ``COMMANDS`` entry. Called by ``cli.main`` (as ``cli._build_parser``),
    ``help_cmd``, tools/gen_options.py (man-page COMMANDS generation) and
    tools/check_shipped.py (completions parity)."""
    from sysforge import __version__
    parser = argparse.ArgumentParser(
        prog="sysforge",
        description="Arch Linux build and maintenance suite with compiler-optimized builds.",
        formatter_class=_TieredHelpFormatter,
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"sysforge {__version__}",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help=(
            "Verbosity level. Default: errors only. "
            "-v adds warnings. -vv adds informational messages. "
            "-vvv adds debug output."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        dest="quiet_global",
        help=(
            "Suppress all non-error output (verbosity 0), overriding -v and the "
            "[log] verbosity config default. File logs are unaffected."
        ),
    )
    parser.add_argument(
        "--py-profile",
        action="store_true",
        dest="py_profile",
        help=(
            "Run the verb under cProfile and print the top functions "
            "(by cumulative time) to stderr at exit."
        ),
    )
    parser.add_argument(
        "--py-profile-out",
        metavar="FILE",
        dest="py_profile_out",
        help=(
            "Write raw cProfile stats to FILE for later analysis "
            "(pstats/snakeviz). Implies --py-profile."
        ),
    )
    parser.add_argument(
        "--timings",
        action="store_true",
        help="Print a wall-clock phase timing report after build/update runs.",
    )
    parser.add_argument(
        "--no-throttle",
        action="store_true",
        dest="no_throttle",
        help=(
            "Ignore the configured build throttle (nice/ionice/cpu_quota/jobs) "
            "for this run — build at normal, unthrottled priority."
        ),
    )
    parser.add_argument(
        "--turbo",
        action="store_true",
        dest="turbo",
        help=(
            "Run the build at *higher* than default priority (negative niceness, "
            "best-effort IO, no CPU/job cap). Stronger than --no-throttle; "
            "lowering niceness may need privilege (best-effort — degrades quietly)."
        ),
    )
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default=None,
        help=(
            "Colorize output. 'auto' (default) colours when writing to a "
            "terminal and honours NO_COLOR/FORCE_COLOR; 'always' forces colour "
            "on (e.g. when piping into a pager); 'never' disables it. Overrides "
            "the [ui] color config key."
        ),
    )
    parser.add_argument(
        "--frozen",
        action="store_true",
        dest="frozen",
        help=(
            "Source freeze: refuse all new source downloads (AUR clones, git "
            "fetches, VCS version probes) for this run. Existing checkouts "
            "still build. Overrides [security] freeze_sources = false."
        ),
    )
    parser.add_argument(
        "--no-frozen",
        action="store_true",
        dest="no_frozen",
        help=(
            "Lift the source freeze for this run, overriding "
            "[security] freeze_sources = true. Wins over --frozen."
        ),
    )
    parser.add_argument(
        "--thaw",
        action="append",
        metavar="PKG[,PKG...]",
        default=None,
        help=(
            "Exempt the named pkgbases from the source freeze; everything else "
            "stays frozen. Repeatable, and accepts a comma-separated list. "
            "Run-scoped only — there is no persistent trust list."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True
    for entry in COMMANDS:
        if isinstance(entry, VerbGroup):
            entry.build(sub)
        else:
            entry.add_parser(sub)
    return parser
