# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""search_cmd.py — the ``sysforge search`` verb.

Search three sources in fixed order — local (installed), repo (sync DBs), AUR
— printing a header per non-empty section. Local/repo are pacman passthroughs
(captured with forced colour so an empty section can be omitted while native
rendering is preserved); AUR is sysforge-rendered (no pacman equivalent) and
its failure is non-fatal.

Installed status is shown in-line (3.2.0-F16): the AUR section carries
pacman-style ``[installed]`` / ``[installed: <ver>]`` markers, and both the repo
and AUR sections mark a name that an installed package stands in for —
a ``-sysforge`` build (``[installed as mesa-sysforge]``) or a drop-in such as an
AUR ``-git`` package (``[installed as cosmic-comp-git: <ver>]``). pacman's own
marker matches by exact name, so it cannot see either.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sysforge import log
from sysforge.primitives import aur, kernel_fdo, pacman
from sysforge.primitives.pkgbuild_patcher import RENAME_SUFFIX
from sysforge.primitives.pty_runner import strip_ansi
from sysforge.verbs.base import ExecResult, PreCheckResult, Verb

_log = log.get_logger("SEARCH")

_VARIANT_SUFFIX = f"-{RENAME_SUFFIX}"


@dataclass(frozen=True)
class InstalledMarkers:
    """Installed-state lookups for one search.

    ``stock`` maps every installed pkgname to its version; ``variants`` maps a
    name to the ``[(installed_name, version)]`` standing in for it — its
    installed ``-sysforge`` build and/or a drop-in replacement.
    """

    stock: dict[str, str] = field(default_factory=dict)
    variants: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


def installed_markers(
    facts: dict[str, tuple[str, int | None]],
    substitutes: dict[str, list[tuple[str, str]]] | None = None,
) -> InstalledMarkers:
    """Build :class:`InstalledMarkers` from the local DB.

    ``facts`` is ``pacman.get_installed_facts()``; ``substitutes`` is
    ``pacman.get_installed_substitutes()`` (the ``replaces`` / conflicts+provides
    drop-in rule). The ``-sysforge`` suffix is matched by name as well, because a
    coexist-mode rename (kernel FDO, including its ``-profiling`` / ``-fdo`` /
    ``-propeller`` role names, stripped via ``kernel_fdo.strip_role``) declares
    no relation to its stock name.
    The local DB, not build_state, is the authority: the marker describes what
    is installed right now, and a stale build_state entry must not claim a
    variant pacman no longer has.
    """
    stock = {name: ver for name, (ver, _size) in facts.items()}
    variants: dict[str, set[tuple[str, str]]] = {}
    for name, ver in stock.items():
        base = kernel_fdo.strip_role(name)
        if base.endswith(_VARIANT_SUFFIX) and len(base) > len(_VARIANT_SUFFIX):
            variants.setdefault(base[: -len(_VARIANT_SUFFIX)], set()).add((name, ver))
    for target, subs in (substitutes or {}).items():
        variants.setdefault(target, set()).update(subs)
    return InstalledMarkers(
        stock=stock, variants={k: sorted(v) for k, v in variants.items()},
    )


def _variant_marker(name: str, version: str, markers: InstalledMarkers) -> str:
    """``[installed as X]`` (``X: <ver>`` on version skew; comma-joined), or ``""``."""
    hits = markers.variants.get(name)
    if not hits:
        return ""
    parts = [vname if vver == version else f"{vname}: {vver}" for vname, vver in hits]
    return log.cyan(f"[installed as {', '.join(parts)}]")


def render_aur(results: list, markers: InstalledMarkers | None = None) -> str:
    """Render AUR results as pacman-style ``aur/name version`` + indented desc.

    Colouring mirrors the pacman-rendered sections (coloured source prefix, bold
    name, green version) so the AUR block doesn't read as visibly plainer next to
    them. Routed through ``log`` helpers, which gate on ``log.use_color()`` — so
    ``NO_COLOR``/non-TTY degrade to the original plain rendering. With
    ``markers``, an installed result gets pacman's ``[installed]`` /
    ``[installed: <ver>]`` tag and a name with an installed stand-in
    (``-sysforge`` build or drop-in) gets the variant tag.
    """
    markers = markers or InstalledMarkers()
    lines = []
    for r in results:
        name = r.get("Name", "?")
        ver = r.get("Version", "")
        desc = r.get("Description") or ""
        head = log.cyan("aur/") + log.bold(name)
        if ver:
            head += " " + log.green(ver)
        local = markers.stock.get(name)
        if local is not None:
            head += " " + log.cyan("[installed]" if local == ver else f"[installed: {local}]")
        variant = _variant_marker(name, ver, markers)
        if variant:
            head += " " + variant
        lines.append(head)
        if desc:
            lines.append(f"    {desc}")
    return "\n".join(lines) + ("\n" if lines else "")


def mark_repo_variants(body: str, markers: InstalledMarkers) -> str:
    """Append the stand-in (``-sysforge`` / drop-in) tag to ``pacman -Ss`` headers.

    pacman already renders ``[installed]`` for exact-name matches; this adds
    only what it cannot know. Header lines are the unindented ones
    (``repo/name version …``, possibly SGR-wrapped under forced colour), so the
    name/version are read from the ANSI-stripped text and the tag is appended
    to the raw line, leaving pacman's own rendering byte-for-byte intact.
    """
    if not markers.variants:
        return body
    out = []
    for line in body.splitlines(keepends=True):
        text = line.rstrip("\n")
        plain = strip_ansi(text)
        if plain and not plain[0].isspace():
            tokens = plain.split()
            name = tokens[0].partition("/")[2]
            version = tokens[1] if len(tokens) > 1 else ""
            tag = _variant_marker(name, version, markers)
            if tag:
                line = f"{text} {tag}" + line[len(text):]
        out.append(line)
    return "".join(out)


class SearchVerb(Verb):
    """Search installed, repo, and AUR packages for a term."""

    name = "search"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        # Read-only verb: nothing to validate or resolve ahead of execute.
        return PreCheckResult(ctx={})

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        term = args.term
        markers = installed_markers(
            pacman.get_installed_facts(), pacman.get_installed_substitutes(),
        )
        sections = [
            ("Installed", pacman.search_local(term)),
            ("Repo", mark_repo_variants(pacman.search_repo(term), markers)),
            ("AUR", render_aur(aur.aur_search(term), markers)),
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
