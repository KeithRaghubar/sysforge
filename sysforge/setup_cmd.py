"""
setup_cmd.py — first-time sysforge configuration

Checks whether pacman is configured to ignore the 'sf-build' group during
system upgrades, and offers to add the setting automatically.

Why this matters
----------------
Every package sysforge builds is stamped with the 'sf-build' pacman group
via append_groups in profiles.toml. Without IgnoreGroup = sf-build in
/etc/pacman.conf, a plain 'pacman -Syu' will overwrite those packages with
unoptimized repo binaries, silently discarding the compiler flags sysforge
applied.

sysforge update already handles sysforge-managed packages correctly (it only
rebuilds packages recorded in build_state.toml, using the same profiled flags).
The risk is specifically from running pacman -Syu outside of sysforge.

Public API
----------
    cmd_setup(args)
"""
import re
import sys
from pathlib import Path

from sysforge.primitives.prompt import prompt_choice


PACMAN_CONF = Path("/etc/pacman.conf")
_IGNORE_GROUP = "sf-build"

# Pattern matching any uncommented IgnoreGroup line (case-insensitive key).
_IGNOREGROUP_RE = re.compile(r"^[ \t]*IgnoreGroup[ \t]*=[ \t]*(.+)$", re.MULTILINE | re.IGNORECASE)


# ---------------------------------------------------------------------------
# Pure helpers (testable without filesystem)
# ---------------------------------------------------------------------------

def _check_ignore_group(conf_text: str) -> bool:
    """Return True if sf-build is already in an uncommented IgnoreGroup line."""
    for m in _IGNOREGROUP_RE.finditer(conf_text):
        groups = m.group(1).split()
        if _IGNORE_GROUP in groups:
            return True
    return False


def _patch_conf_text(conf_text: str) -> str:
    """
    Return conf_text with sf-build added to IgnoreGroup.

    If an uncommented IgnoreGroup line exists, sf-build is appended to it.
    Otherwise a new 'IgnoreGroup = sf-build' line is inserted inside [options].
    If no [options] section is found, it is appended at the end of the file.
    """
    # Append to an existing IgnoreGroup line.
    m = _IGNOREGROUP_RE.search(conf_text)
    if m:
        original = m.group(0)
        patched = original.rstrip() + f" {_IGNORE_GROUP}"
        return conf_text.replace(original, patched, 1)

    # Insert a new IgnoreGroup line inside [options].
    lines = conf_text.splitlines(keepends=True)
    in_options = False
    insert_after = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^\[options\]", stripped, re.IGNORECASE):
            in_options = True
            insert_after = i
        elif in_options and stripped.startswith("["):
            break
        elif in_options:
            insert_after = i

    if insert_after is not None:
        lines.insert(insert_after + 1, f"IgnoreGroup = {_IGNORE_GROUP}\n")
        return "".join(lines)

    # Fallback: no [options] section found — append at end.
    return conf_text.rstrip("\n") + f"\n\nIgnoreGroup = {_IGNORE_GROUP}\n"


# ---------------------------------------------------------------------------
# Write helper
# ---------------------------------------------------------------------------

def _write_conf(path: Path, text: str) -> bool:
    """
    Write text to path. Returns True on success, False on PermissionError.
    """
    try:
        path.write_text(text)
        return True
    except PermissionError:
        return False


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------

_RISK_WARNING = """\
[SYSFORGE] pacman.conf not modified.

  Be aware: running 'pacman -Syu' will overwrite any package in the
  sf-build group with an unoptimized repo binary, silently discarding
  the compiler flags sysforge applied at build time.

  To avoid this, use 'sysforge update' instead of 'pacman -Syu' for
  packages managed by sysforge. If you change your mind, re-run:

      sysforge setup
"""

_MANUAL_INSTRUCTION = """\
[SYSFORGE] Could not write {path} (permission denied).

  To apply the setting manually, add the following line to the
  [options] section of {path}:

      IgnoreGroup = sf-build

  Or re-run as root:

      sudo sysforge setup
"""


def cmd_setup(args) -> None:
    """Entry point for 'sysforge setup'."""
    conf_path = Path(getattr(args, "pacman_conf", None) or PACMAN_CONF)

    if not conf_path.exists():
        print(
            f"[SYSFORGE] {conf_path} not found — nothing to configure.",
            file=sys.stderr,
        )
        return

    conf_text = conf_path.read_text()

    if _check_ignore_group(conf_text):
        print(f"[SYSFORGE] Already configured: IgnoreGroup = {_IGNORE_GROUP} is present in {conf_path}.")
        return

    print(
        f"\n  sysforge stamps every package it builds with the '{_IGNORE_GROUP}' pacman group.\n"
        f"  Adding 'IgnoreGroup = {_IGNORE_GROUP}' to {conf_path} prevents 'pacman -Syu'\n"
        f"  from overwriting those packages with unoptimized repo binaries.\n"
    )

    answer = prompt_choice(
        f"  Add 'IgnoreGroup = {_IGNORE_GROUP}' to {conf_path}? [y/N] ",
        choices=("y", "yes", "n"),
        default="n",
    )

    if answer not in ("y", "yes"):
        print(_RISK_WARNING, file=sys.stderr)
        return

    patched = _patch_conf_text(conf_text)
    if _write_conf(conf_path, patched):
        print(f"[SYSFORGE] Added 'IgnoreGroup = {_IGNORE_GROUP}' to {conf_path}.")
    else:
        print(_MANUAL_INSTRUCTION.format(path=conf_path), file=sys.stderr)


# ---------------------------------------------------------------------------
# Verb wrapper
# ---------------------------------------------------------------------------

from sysforge.verbs import ExecResult, PreCheckResult, Verb  # noqa: E402


class SetupVerb(Verb):
    """Configure pacman.conf IgnoreGroup for sysforge-built packages."""

    name = "setup"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        cmd_setup(args)
        return ExecResult()
