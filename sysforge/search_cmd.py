# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""search_cmd.py — the ``sysforge search`` verb.

Search three sources in fixed order — local (installed), repo (sync DBs), AUR
— printing a header per non-empty section. Local/repo are pacman passthroughs
(captured with forced colour so an empty section can be omitted while native
rendering is preserved); AUR is sysforge-rendered (no pacman equivalent) and
its failure is non-fatal.
"""
from __future__ import annotations

from sysforge import log
from sysforge.primitives import aur, pacman
from sysforge.verbs.base import ExecResult, PreCheckResult, Verb

_log = log.get_logger("SEARCH")


def render_aur(results: list) -> str:
    """Render AUR results as pacman-style ``aur/name version`` + indented desc.

    Colouring mirrors the pacman-rendered sections (coloured source prefix, bold
    name, green version) so the AUR block doesn't read as visibly plainer next to
    them. Routed through ``log`` helpers, which gate on ``log.use_color()`` — so
    ``NO_COLOR``/non-TTY degrade to the original plain rendering.
    """
    lines = []
    for r in results:
        name = r.get("Name", "?")
        ver = r.get("Version", "")
        desc = r.get("Description") or ""
        head = log.cyan("aur/") + log.bold(name)
        if ver:
            head += " " + log.green(ver)
        lines.append(head)
        if desc:
            lines.append(f"    {desc}")
    return "\n".join(lines) + ("\n" if lines else "")


class SearchVerb(Verb):
    """Search installed, repo, and AUR packages for a term."""

    name = "search"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        # Read-only verb: nothing to validate or resolve ahead of execute.
        return PreCheckResult(ctx={})

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        term = args.term
        sections = [
            ("Installed", pacman.search_local(term)),
            ("Repo", pacman.search_repo(term)),
            ("AUR", render_aur(aur.aur_search(term))),
        ]
        first = True
        for header, body in sections:
            if body.strip():
                if not first:
                    _log.newline()  # blank line delimits consecutive sources
                _log.ui(f"== {header} ==")
                _log.ui(body.rstrip("\n"))
                first = False
        return ExecResult(exit_code=0)
