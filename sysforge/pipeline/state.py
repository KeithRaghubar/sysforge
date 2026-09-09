# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
state.py — compatibility re-export of the pipeline checkpoint state

The implementation moved down to :mod:`sysforge.primitives.pipeline_state`
in 3.2.0-F12: ``primitives/init_notice.py`` needs to read
``pipeline_state.toml`` to decide whether the bootstrap stages are done, and
``PipelineState`` is the single home for that file's format, so the reader had
to move down rather than the parse be duplicated. That closed the last entry in
``tests/test_module_layering.py``'s ``_ALLOWED_UPWARD_IMPORTS``.

This module stays for one cycle so existing
``from sysforge.pipeline.state import …`` imports — and the tests that patch
names here — keep working. New callers import from
``sysforge.primitives.pipeline_state`` (or ``primitives.paths`` for
``resolve_state_dir``) directly.
"""
from sysforge.primitives.pipeline_state import (  # noqa: F401
    _DEFAULT_STATE_DIR,
    _FALLBACK_STATE_DIR,
    PACKAGE_STATUSES,
    PipelineState,
    STAGE_STATUSES,
    get_toolchain_fingerprint,
    get_toolchain_variant,
    resolve_state_dir,
)

__all__ = [
    "PACKAGE_STATUSES",
    "PipelineState",
    "STAGE_STATUSES",
    "get_toolchain_fingerprint",
    "get_toolchain_variant",
    "resolve_state_dir",
]
