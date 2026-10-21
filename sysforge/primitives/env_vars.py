# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
env_vars.py — every environment variable sysforge reads, in one table (3.5.0-B128).

The man page's ENVIRONMENT section is generated from ``ENV_VARS``
(``tools/gen_options.py``, the ``@ENVIRONMENT@`` marker), and
``tests/test_env_vars.py`` fails on a ``SYSFORGE_*`` name the code reads that
is not here. Before this table the man page listed three of the eleven, and
``SYSFORGE_PROFILE_STORE`` was documented nowhere.

``audience`` is ``"user"`` for a setting a user is expected to change and
``"diagnostic"`` for a switch meant for bug reports; the man page shows both,
diagnostics last. Descriptions are scdoc text, one paragraph each.

Public API:
    EnvVar
    ENV_VARS
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnvVar:
    name: str
    audience: str  # "user" | "diagnostic"
    text: str


ENV_VARS: tuple[EnvVar, ...] = (
    EnvVar("SYSFORGE_STATE_DIR", "user",
           "Override the state directory (stage progress, build_state.toml, logs, "
           "sentinels). Read by every command that touches build state: *build*, "
           "*update*, *state*, *log*, *doctor*, and the *run* stages."),
    EnvVar("SYSFORGE_CONFIG_DIR", "user",
           "Override the configuration directory searched before /etc/sysforge. "
           "Read by every command that loads a config file (see FILES)."),
    EnvVar("SYSFORGE_EDITOR", "user",
           "Editor sysforge opens: *run reconfigure*'s config review, *artifact "
           "edit* and *config merge*. Checked first, then sysforge.toml [ui] editor, "
           "$EDITOR and $VISUAL."),
    EnvVar("SYSFORGE_MERGE", "user",
           "Merge tool for *config merge*. Checked first, then sysforge.toml [ui] "
           "merge, $DIFFPROG, then vimdiff."),
    EnvVar("SYSFORGE_ASCII", "user",
           "Any non-empty value prints ASCII in place of decorative glyphs "
           "(arrows, bullets, progress fills), for terminals and fonts without them."),
    EnvVar("SYSFORGE_PGO_STORE", "user",
           "Directory for the toolchain's PGO profiles, used when toolchain.toml "
           "sets no pgo_store."),
    EnvVar("SYSFORGE_PROFILE_STORE", "user",
           "Root directory of every other profile store (pgo/<pkgbase>, autofdo, "
           "propeller, bolt), used when toolchain.toml sets no profile_store."),
    EnvVar("PAGER", "user",
           "Pager for *sysforge log*, *state* listings, and PKGBUILD review diffs "
           "in *build*/*update* (falls back to less -RF, then more)."),
    EnvVar("SYSFORGE_PTY_RAW_DUMP", "diagnostic",
           "A file path: every byte a build writes to its terminal is appended there, "
           "after a line giving the terminal size sysforge handed it. For reports "
           "about garbled build output."),
    EnvVar("SYSFORGE_PROGRESS_TRACE", "diagnostic",
           "A file path: the progress bar logs each repaint, resize and handover "
           "there. For reports about the progress bar."),
    EnvVar("SYSFORGE_PACMAN_NO_PYALPM", "diagnostic",
           "Any non-empty value makes sysforge query pacman through the pacman "
           "command rather than pyalpm, to tell a pyalpm problem from a sysforge one."),
)
