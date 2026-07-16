# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""profile_writer.py — the single programmatic writer for profiles.toml.

Only writes the auto-managed [package_compiler_overrides] table (pkgbase ->
inline table of cc/cxx/ld), recovered from interactive build failures. Line-
level and comment-preserving, mirroring packages_cmd._rewrite_packages_toml:
never round-trips the whole document through a TOML emitter (which would strip
comments and reorder the user's hand-authored profiles/rules).
"""
from __future__ import annotations

import re
from pathlib import Path

from sysforge import log

_log = log.get_logger("PROFILE")

_SECTION = "[package_compiler_overrides]"
_HEADER = (
    "# Auto-managed by sysforge — compiler/linker overrides recovered from "
    "build failures.\n# Keyed by pkgbase; applied last in resolve_profile "
    "(wins over the matched profile).\n"
)


def _row(pkgbase: str, cc: str, cxx: str, ld: str) -> str:
    return f'{pkgbase} = {{ cc = "{cc}", cxx = "{cxx}", ld = "{ld}" }}\n'


def write_package_compiler_override(
    path: Path, pkgbase: str, cc: str, cxx: str, ld: str
) -> bool:
    """Upsert one [package_compiler_overrides] row. Best-effort: False on OSError."""
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as e:
        _log.warn(f"profiles.toml override write skipped (read failed): {e}")
        return False

    new_row = _row(pkgbase, cc, cxx, ld)
    lines = text.splitlines(keepends=True)

    # Locate the section header line, if present.
    sect_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip() == _SECTION), None
    )
    row_re = re.compile(rf"^\s*{re.escape(pkgbase)}\s*=")

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
    _log.info(f"Recorded compiler override for {pkgbase}: cc={cc} cxx={cxx} ld={ld}")
    return True
