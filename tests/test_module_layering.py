# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_module_layering.py — structural guard on the primitives → pipeline edge.

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

``_ALLOWED_STAGE_IMPORTS`` is a **shrinking** allowlist, not a config knob. The
remaining entries are live-detection helpers whose relocation is a larger job;
they are pinned here so the set cannot silently grow. Adding a name is a
layering regression — move the code down instead. Removing one is progress.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRIMITIVES_DIR = REPO_ROOT / "sysforge" / "primitives"

# module filename -> frozenset of names it may still import from a pipeline
# stage module. Shrink this; never grow it.
_ALLOWED_STAGE_IMPORTS: dict[str, frozenset[str]] = {
    # Bootstrap config dataclass — shared shape, not a stage behaviour.
    "archinstall_config.py": frozenset({"BootstrapConfig"}),
    # Live hardware detection (lspci/uname). Relocating these means moving the
    # probe itself out of the stage; tracked separately from the F8 table move.
    "llvm_targets.py": frozenset(
        {"derive_llvm_targets", "detect_host_arch", "parse_gpu_vendors"}
    ),
    "mesa_drivers.py": frozenset({"derive_mesa_drivers", "parse_gpu_vendors"}),
}


def _stage_imports(path: Path) -> set[str]:
    """Return every name `path` imports out of a sysforge.pipeline.stages module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "sysforge.pipeline.stages" or mod.startswith(
                "sysforge.pipeline.stages."
            ):
                found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("sysforge.pipeline.stages"):
                    found.add(alias.name)
    return found


def test_primitives_do_not_import_pipeline_stages():
    """No module under primitives/ imports from pipeline.stages beyond the allowlist."""
    violations: list[str] = []
    for path in sorted(PRIMITIVES_DIR.glob("*.py")):
        allowed = _ALLOWED_STAGE_IMPORTS.get(path.name, frozenset())
        for name in sorted(_stage_imports(path) - allowed):
            violations.append(f"{path.name} imports {name} from sysforge.pipeline.stages")
    assert not violations, (
        "primitives/ must not import from pipeline.stages — move the code down "
        "into a leaf module instead:\n  " + "\n  ".join(violations)
    )


def test_layering_allowlist_has_no_dead_entries():
    """Every allowlisted name is still imported — a stale entry hides a regression.

    Without this, removing an upward import leaves its allowlist slot behind as
    a pre-authorised hole that a future reintroduction would slip through.
    """
    stale: list[str] = []
    for filename, allowed in _ALLOWED_STAGE_IMPORTS.items():
        path = PRIMITIVES_DIR / filename
        assert path.exists(), f"allowlist names a missing module: {filename}"
        for name in sorted(allowed - _stage_imports(path)):
            stale.append(f"{filename}: {name}")
    assert not stale, (
        "_ALLOWED_STAGE_IMPORTS entries no longer imported — delete them:\n  "
        + "\n  ".join(stale)
    )
