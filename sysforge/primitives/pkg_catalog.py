# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
pkg_catalog.py — curated package-group catalog + guided selection.

Single home for the shipped desktop-environment catalog, the shared interactive
selection prompt, and the ``[group.*]`` writer that turns a choice into a
packages.toml table. Lives in the primitives layer because every surface that
offers the guided selection — the bootstrap configure stage, the reconfigure
step, and the ``sysforge packages add-group`` command — consumes it, and
primitives must not import the pipeline or CLI layers.

The catalog is intentionally small and conservative: each entry installs a core
session plus a display manager, and users extend their own ``[group.<name>]``
afterward. The *read* side of groups (expansion into package entries) stays in
:func:`sysforge.primitives.config.expand_package_groups`; this module only adds
``[group.*]`` text — it never re-implements expansion.

Public API:
    DESKTOP_CATALOG                       -> dict[str, DesktopEntry]
    valid_desktops()                      -> list[str]
    display_manager_for(de_key)           -> str | None
    select_desktop(*, interactive, preselected) -> str | None
    group_toml_block(name, members, defaults=None) -> str
    write_desktop_group(path, de_key)     -> None
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sysforge import log
from sysforge.primitives.prompt import (
    is_interactive,
    prompt_choice,
    prompt_key,
)

_log = log.get_logger("CATALOG")


@dataclass(frozen=True)
class DesktopEntry:
    """A curated desktop-environment package group."""

    key: str
    display_name: str
    packages: tuple[str, ...]
    # The display-manager package that boots into this session. Its systemd
    # unit (``<display_manager>.service``) is what the packages stage enables
    # so the install lands at a graphical login instead of a TTY. The package
    # MUST also appear in ``packages`` (it can't be enabled if it isn't
    # installed); enforced by tests. For lightdm-based desktops the catalog
    # also bundles a greeter, since lightdm shows nothing usable without one.
    display_manager: str = ""
    # Optional per-group defaults inherited by every member at expansion time
    # (e.g. {"source": "repo"}). Mirrors the [group.*] defaults that
    # config.expand_package_groups understands.
    defaults: dict = field(default_factory=dict)


# Catalog keys double as packages.toml group names, so keep them short and
# lowercase. Package lists target official-repo names — a core session plus a
# display manager is enough to boot into the desktop; everything else is the
# user's call.
DESKTOP_CATALOG: dict[str, DesktopEntry] = {
    "gnome": DesktopEntry(
        key="gnome",
        display_name="GNOME",
        display_manager="gdm",
        defaults={"source": "repo"},
        packages=(
            "gnome-shell",
            "gdm",
            "gnome-control-center",
            "nautilus",
            "gnome-console",
            "xdg-desktop-portal-gnome",
        ),
    ),
    "kde": DesktopEntry(
        key="kde",
        display_name="KDE Plasma",
        display_manager="sddm",
        defaults={"source": "repo"},
        packages=(
            "plasma-meta",
            "sddm",
            "konsole",
            "dolphin",
            "xdg-desktop-portal-kde",
        ),
    ),
    "xfce": DesktopEntry(
        key="xfce",
        display_name="Xfce",
        display_manager="lightdm",
        defaults={"source": "repo"},
        packages=(
            "xfce4",
            "xfce4-goodies",
            "lightdm",
            "lightdm-gtk-greeter",
            "xdg-desktop-portal-gtk",
        ),
    ),
    "mate": DesktopEntry(
        key="mate",
        display_name="MATE",
        display_manager="lightdm",
        defaults={"source": "repo"},
        packages=(
            "mate",
            "mate-extra",
            "lightdm",
            "lightdm-gtk-greeter",
            "xdg-desktop-portal-gtk",
        ),
    ),
    "cinnamon": DesktopEntry(
        key="cinnamon",
        display_name="Cinnamon",
        display_manager="lightdm",
        defaults={"source": "repo"},
        packages=(
            "cinnamon",
            "gnome-terminal",
            "lightdm",
            "lightdm-gtk-greeter",
            "xdg-desktop-portal-gtk",
        ),
    ),
    "lxqt": DesktopEntry(
        key="lxqt",
        display_name="LXQt",
        display_manager="sddm",
        defaults={"source": "repo"},
        packages=(
            "lxqt",
            "sddm",
            "breeze-icons",
            "xdg-desktop-portal-lxqt",
        ),
    ),
    "budgie": DesktopEntry(
        key="budgie",
        display_name="Budgie",
        display_manager="lightdm",
        defaults={"source": "repo"},
        packages=(
            "budgie-desktop",
            "gnome-control-center",
            "nautilus",
            "gnome-terminal",
            "lightdm",
            "lightdm-gtk-greeter",
            "xdg-desktop-portal-gtk",
        ),
    ),
    "cosmic": DesktopEntry(
        key="cosmic",
        display_name="COSMIC",
        display_manager="cosmic-greeter",
        defaults={"source": "repo"},
        packages=(
            "cosmic-session",
            "cosmic-greeter",
            "cosmic-files",
            "cosmic-term",
            "xdg-desktop-portal-cosmic",
        ),
    ),
}


def valid_desktops() -> list[str]:
    """Catalog keys, in catalog order — for argparse ``choices`` and validation."""
    return list(DESKTOP_CATALOG)


def display_manager_for(de_key: str) -> str | None:
    """Return the display-manager package for a catalog key, or ``None``.

    Used by the packages stage to enable ``<display_manager>.service`` after a
    desktop group installs, so the system boots into a graphical login instead
    of a TTY. ``None`` for unknown keys or entries with no display manager.
    """
    entry = DESKTOP_CATALOG.get(de_key)
    if entry is None or not entry.display_manager:
        return None
    return entry.display_manager


# ---------------------------------------------------------------------------
# Guided selection
# ---------------------------------------------------------------------------

def select_desktop(*, interactive: bool, preselected: str | None) -> str | None:
    """Resolve a desktop-environment choice.

    Resolution order:

    1. ``preselected`` (e.g. bootstrap.toml ``[desktop] environment``) — when
       set, it wins and is returned without prompting. An unknown value is a
       warning + ``None`` (the caller's config validation should have caught it
       first; this is a defensive fallback).
    2. Interactive + a real terminal — ask whether to install a GUI, then show a
       numbered menu of catalog entries.
    3. Otherwise — return ``None`` (no desktop), so unattended/non-TTY runs never
       block.

    Returns the chosen catalog key, or ``None`` to skip.
    """
    if preselected:
        key = preselected.strip().lower()
        if key in DESKTOP_CATALOG:
            return key
        _log.warn(
            f"Unknown desktop environment {preselected!r}; skipping. "
            f"Valid: {', '.join(valid_desktops())}."
        )
        return None

    if not (interactive and is_interactive()):
        return None

    want = prompt_choice(
        "Install a graphical desktop environment? [y/N]: ",
        choices=("y", "n"),
        default="n",
        tag="DESKTOP",
    )
    if want != "y":
        return None

    entries = list(DESKTOP_CATALOG.values())
    _log.ui("Available desktop environments:")
    for i, entry in enumerate(entries, 1):
        _log.ui(f"  [{i}] {entry.display_name}  ({', '.join(entry.packages)})")
    while True:
        try:
            raw = prompt_key(
                f"  Pick a desktop [1-{len(entries)}, Enter to skip]: ",
                tag="DESKTOP",
            )
        except EOFError:
            return None
        if not raw:
            return None
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(entries):
                return entries[idx - 1].key
        _log.warn(f"  Invalid selection: {raw!r}")


# ---------------------------------------------------------------------------
# [group.*] serialization + write
# ---------------------------------------------------------------------------

_GROUP_HEADER = "# packages.toml — managed by sysforge packages\n"
_GROUP_BUILD = '\n[build]\npkgbuild_src_dir = "~/src"\n'


def group_toml_block(name: str, members, defaults: dict | None = None) -> str:
    """Serialise a ``[group.<name>]`` table to TOML text (no leading newline).

    ``members`` is the ordered package list; ``defaults`` are optional
    per-group fields (``source`` / ``enable_build_from_source`` / ``cache`` /
    ``reason``) written above the ``packages`` array, matching the read side in
    :func:`config.expand_package_groups`.
    """
    lines = [f"[group.{name}]"]
    for key, val in (defaults or {}).items():
        if isinstance(val, bool):
            lines.append(f"{key} = {'true' if val else 'false'}")
        elif isinstance(val, str):
            lines.append(f'{key} = "{val}"')
        else:
            lines.append(f"{key} = {val!r}")
    members = list(members)
    if not members:
        lines.append("packages = []")
    else:
        lines.append("packages = [")
        for m in members:
            lines.append(f'  "{m}",')
        lines.append("]")
    return "\n".join(lines) + "\n"


def _strip_group_block(lines: list[str], name: str) -> list[str]:
    """Remove an existing ``[group.<name>]`` table (header through the line
    before the next top-level table header / EOF).

    Blank-line separators are not pruned here — a final collapse pass in
    :func:`write_desktop_group` squashes any runs that removal leaves behind,
    so a following table never loses its blank separator from the block above.
    """
    header = f"[group.{name}]"
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() == header:
            i += 1
            # Skip the body up to the next table header (single or array) or EOF.
            while i < n and not lines[i].lstrip().startswith("["):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return out


def _collapse_blank_runs(lines: list[str]) -> list[str]:
    """Collapse runs of 2+ blank lines down to a single blank line."""
    out: list[str] = []
    prev_blank = False
    for line in lines:
        blank = line.strip() == ""
        if blank and prev_blank:
            continue
        out.append(line)
        prev_blank = blank
    return out


def write_desktop_group(path: Path, de_key: str) -> None:
    """Write (or replace) the ``[group.<de_key>]`` table in ``path``.

    Idempotent: an existing same-named group block is removed first, then the
    fresh block is appended. Only ``[group.*]`` headers are touched —
    ``[[package]]`` blocks and other tables are preserved byte-for-byte. A
    missing file is created with the standard self-documenting header.
    """
    entry = DESKTOP_CATALOG.get(de_key)
    if entry is None:
        raise ValueError(
            f"Unknown desktop environment {de_key!r}. "
            f"Valid: {', '.join(valid_desktops())}."
        )

    path = Path(path)
    if path.exists():
        text = path.read_text()
    else:
        text = _GROUP_HEADER + _GROUP_BUILD

    lines = text.splitlines(keepends=True)
    lines = _strip_group_block(lines, de_key)
    lines = _collapse_blank_runs(lines)

    # Drop trailing blank-line runs so we don't accumulate blank lines across
    # repeated writes, then separate the new block with a single blank line.
    while lines and lines[-1].strip() == "":
        lines.pop()
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"

    block = group_toml_block(entry.key, entry.packages, entry.defaults)
    new_text = "".join(lines)
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    new_text += "\n" + block

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text)
