# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Sole definition of the SemVer bump vocabulary (standards row 3).

Imported by both `check_standards.py` (which derives the required bump from the
release-notes accumulator) and `gen_roadmap_table.py` (which validates the
`Bump:` tag on ROADMAP entries). One definition, so the two tools cannot drift
apart on what a valid bump is.
"""
from __future__ import annotations

# Weakest first; the index is the rank, so `BUMP_ORDER.index(a) < BUMP_ORDER.index(b)`
# means "a is weaker than b".
BUMP_ORDER = ["patch", "minor", "major"]
