# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_module_layering.py — structural guard on the primitives → pipeline/ui edge.

``sysforge/primitives/`` is the leaf layer: pipeline stages compose primitives,
never the reverse. Every upward import is a latent import cycle, and the ones
that exist today are written as *function-level* imports precisely to dodge one
at module load — the deferral hides the cycle rather than removing it.

The blast radius is not theoretical. ``pipeline/stages/__init__.py`` eagerly
instantiates all seven stages, so a primitive reaching up for even a single
constant transitively imports every stage and everything they import. A broken
import in ``stages/packages.py`` once surfaced as a traceback seven frames deep
inside mesa driver resolution (2.6.1-F8), because ``mesa_drivers`` reached up
for two driver-name tuples.

``_ALLOWED_UPWARD_IMPORTS`` is a **shrinking** allowlist, not a config knob.
Adding a name is a layering regression — move the code down instead. Removing
one is progress.

3.2.0-F1 closed the three leaks the allowlist was built for — ``resolve_state_dir``
(now ``primitives/paths.py``), the ``ui.progress`` surface (now the
``progress_hooks`` protocol) and live hardware detection plus ``BootstrapConfig``
(now ``primitives/hardware_probe.py`` and ``primitives/archinstall_config.py``) —
so the guard widened from ``sysforge.pipeline.stages`` to ``sysforge.pipeline``
and ``sysforge.ui`` in full. That widening surfaced one remaining reach-up,
pinned below.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRIMITIVES_DIR = REPO_ROOT / "sysforge" / "primitives"

# The layers a primitive may never import from.
_FORBIDDEN_PREFIXES = ("sysforge.pipeline", "sysforge.ui")

# module filename -> frozenset of names it may still import from a forbidden
# layer. Shrink this; never grow it.
_ALLOWED_UPWARD_IMPORTS: dict[str, frozenset[str]] = {
    # The first-run advisory reads the pipeline's own checkpoint file to decide
    # whether the bootstrap stages are done. PipelineState is the single home
    # for that file's format, so the fix is relocating the reader, not
    # duplicating the parse — surfaced by F1's widened guard, tracked as its own
    # roadmap item.
    "init_notice.py": frozenset({"PipelineState"}),
}


def _is_forbidden(mod: str) -> bool:
    return any(
        mod == prefix or mod.startswith(prefix + ".") for prefix in _FORBIDDEN_PREFIXES
    )


def _upward_imports(path: Path) -> set[str]:
    """Return every name `path` imports out of a forbidden (upper-layer) module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if _is_forbidden(node.module or ""):
                found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden(alias.name):
                    found.add(alias.name)
    return found


def test_primitives_do_not_import_upper_layers():
    """No module under primitives/ imports from pipeline/ or ui/ beyond the allowlist."""
    violations: list[str] = []
    for path in sorted(PRIMITIVES_DIR.glob("*.py")):
        allowed = _ALLOWED_UPWARD_IMPORTS.get(path.name, frozenset())
        for name in sorted(_upward_imports(path) - allowed):
            violations.append(f"{path.name} imports {name} from an upper layer")
    assert not violations, (
        "primitives/ must not import from pipeline/ or ui/ — move the code down "
        "into a leaf module, or invert the dependency with a protocol "
        "(see primitives/progress_hooks.py):\n  " + "\n  ".join(violations)
    )


def test_layering_allowlist_has_no_dead_entries():
    """Every allowlisted name is still imported — a stale entry hides a regression.

    Without this, removing an upward import leaves its allowlist slot behind as
    a pre-authorised hole that a future reintroduction would slip through.
    """
    stale: list[str] = []
    for filename, allowed in _ALLOWED_UPWARD_IMPORTS.items():
        path = PRIMITIVES_DIR / filename
        assert path.exists(), f"allowlist names a missing module: {filename}"
        for name in sorted(allowed - _upward_imports(path)):
            stale.append(f"{filename}: {name}")
    assert not stale, (
        "_ALLOWED_UPWARD_IMPORTS entries no longer imported — delete them:\n  "
        + "\n  ".join(stale)
    )
