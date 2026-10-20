# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""profile_writer.py — the single programmatic writer for profiles.toml.

Only writes the auto-managed [package_compiler_overrides] table (pkgbase ->
inline table of ``toolchain`` (+ ``ld``), or raw cc/cxx/ld for a hand-entered
pair), recovered from interactive build failures. Line-
level and comment-preserving, mirroring verbs.shared.rewrite_packages_toml:
never round-trips the whole document through a TOML emitter (which would strip
comments and reorder the user's hand-authored profiles/rules).
"""
from __future__ import annotations

import re
from pathlib import Path

from sysforge import log
from sysforge.primitives.config import toml_literal

_log = log.get_logger("PROFILE")

_SECTION = "[package_compiler_overrides]"
_HEADER = (
    "# Auto-managed by sysforge: compiler/linker overrides recovered from "
    "build failures.\n# Keyed by pkgbase. toolchain swaps the whole compiler "
    "bundle; raw cc/cxx/ld\n# apply last. Both win over the matched profile; "
    "--cc/--cxx/--ld win over both.\n"
)


def _row(pkgbase: str, cc: str, cxx: str, ld: str,
         toolchain: str | None = None) -> str:
    """One override row, key and values as TOML basic strings (3.5.0-B45).

    A bare key broke on any pkgbase outside ``[A-Za-z0-9_-]``: a ``+``
    (``libstdc++``) made the file invalid TOML and a ``.``
    (``python-ruamel.yaml``) a nested table the lookup never matched; an
    unescaped ``"`` in a manually entered compiler did the same to a value.
    """
    q = toml_literal
    if toolchain:
        # 3.5.0-F12: the bundle form; ld only when one was chosen.
        ld_part = f", ld = {q(ld)}" if ld else ""
        return f"{q(pkgbase)} = {{ toolchain = {q(toolchain)}{ld_part} }}\n"
    return f"{q(pkgbase)} = {{ cc = {q(cc)}, cxx = {q(cxx)}, ld = {q(ld)} }}\n"


def _row_re(pkgbase: str) -> re.Pattern:
    """Match this pkgbase's row, quoted or in the bare form written before 3.5.0-B45."""
    return re.compile(rf"^\s*(?:{re.escape(toml_literal(pkgbase))}|{re.escape(pkgbase)})\s*=")


def write_package_compiler_override(
    path: Path, pkgbase: str, cc: str, cxx: str, ld: str,
    *, toolchain: str | None = None,
) -> bool:
    """Upsert one [package_compiler_overrides] row. Best-effort: False on OSError.

    With ``toolchain`` the row is ``{ toolchain = …[, ld = …] }`` and cc/cxx
    are not written (the bundle supplies them); without, the raw cc/cxx/ld row.
    """
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as e:
        _log.warn(f"profiles.toml override write skipped (read failed): {e}")
        return False

    new_row = _row(pkgbase, cc, cxx, ld, toolchain)
    lines = text.splitlines(keepends=True)

    # Locate the section header line, if present.
    sect_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip() == _SECTION), None
    )
    row_re = _row_re(pkgbase)

    if sect_idx is None:
        # Append a fresh section at EOF (ensure a trailing blank-line gap).
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append(_HEADER)
        lines.append(_SECTION + "\n")
        lines.append(new_row)
    else:
        # Search for an existing row within this section (until the next table
        # header or EOF) and replace it; else insert right after the header.
        end = len(lines)
        for i in range(sect_idx + 1, len(lines)):
            if lines[i].lstrip().startswith("["):
                end = i
                break
        replaced = False
        for i in range(sect_idx + 1, end):
            if row_re.match(lines[i]):
                lines[i] = new_row
                replaced = True
                break
        if not replaced:
            lines.insert(sect_idx + 1, new_row)

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text("".join(lines))
        tmp.replace(path)
    except OSError as e:
        _log.warn(f"profiles.toml override write failed: {e}")
        return False
    what = (f"toolchain={toolchain}" + (f" ld={ld}" if ld else "")
            if toolchain else f"cc={cc} cxx={cxx} ld={ld}")
    _log.info(f"Recorded compiler override for {pkgbase}: {what}")
    return True
