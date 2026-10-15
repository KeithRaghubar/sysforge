# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
json_out.py — the machine-readable output contract (3.4.0-F7).

``state list``, ``doctor`` and ``update --dry-run`` take ``--json``. In that
mode stdout carries exactly one JSON document and nothing else: every
``ui()`` / ``print()`` the verb would have written to stdout goes to stderr
instead, so a script can parse stdout without filtering prose that is reworded
freely between releases.

The contract (standards table, "JSON output"): every document carries a
top-level ``"schema"`` integer and a ``"kind"`` naming the verb. Adding a field
is a minor change; renaming or removing one is a major change and bumps
:data:`SCHEMA_VERSION`. The committed schemas under ``tests/data/json-schema/``
pin each kind. Exit codes are the same as without ``--json``.

``verbs.runner.run_verb`` enters :func:`reserved_stdout` for the whole verb run
whenever ``args.json`` is set, so the guarantee holds for ``pre_check`` and
``post_validate`` narration too; the verb calls :func:`emit` once.
"""
from __future__ import annotations

import contextlib
import json
import sys
from typing import IO, Any

SCHEMA_VERSION = 1

_REAL_STDOUT: IO[str] | None = None


@contextlib.contextmanager
def reserved_stdout():
    """Route everything written to ``sys.stdout`` to stderr for the duration,
    keeping the real stdout for :func:`emit`.

    Re-entrant: an inner entry while a guard is active is a no-op, since by
    then ``sys.stdout`` is already stderr and must not be mistaken for the real
    stream. ``cli.main`` enters it right after parsing (startup logs before
    dispatch, which ``--dry-run`` sends to stdout); ``verbs.runner.run_verb``
    enters it too, for callers that drive a verb directly.
    """
    global _REAL_STDOUT
    if _REAL_STDOUT is not None:
        yield
        return
    _REAL_STDOUT = sys.stdout
    try:
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        _REAL_STDOUT = None


def emit(kind: str, body: dict[str, Any]) -> None:
    """Write the one JSON document for this run to the real stdout."""
    stream = _REAL_STDOUT if _REAL_STDOUT is not None else sys.stdout
    doc = {"schema": SCHEMA_VERSION, "kind": kind, **body}
    stream.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    stream.flush()


def requested(args) -> bool:
    """Whether this invocation asked for ``--json``."""
    return getattr(args, "json", False) is True
