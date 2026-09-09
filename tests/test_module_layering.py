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
and ``sysforge.ui`` in full. That widening surfaced one last reach-up,
``init_notice`` → ``PipelineState``, which 3.2.0-F12 closed by relocating the
class to ``primitives/pipeline_state.py``.

**The allowlist is now empty, and that is the steady state.** Because the
dead-entry check iterates the allowlist, an empty one makes it pass vacuously —
so ``test_layering_allowlist_is_empty`` pins the door shut. Re-adding an entry
must be a deliberate edit to that test, not a quiet dict insertion.
The same file carries the sibling-verb rule (3.2.0-F8). Verb modules
(``sysforge/*_cmd.py``) are peers, not a hierarchy: nothing makes ``state_cmd``
the natural owner of "stop maintaining this package" just because
``sysforge state forget`` is the verb that spells it. Once ``revert_cmd`` and
``uninstall_cmd`` both imported it from there — and ``build_cmd`` reached into
``packages_cmd`` for the ``packages.toml`` writer — those arbitrary edges were
a fourth informal layer with no guard. Shared operations live in
``verbs/shared.py`` instead.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSFORGE_DIR = REPO_ROOT / "sysforge"
PRIMITIVES_DIR = SYSFORGE_DIR / "primitives"

# The layers a primitive may never import from.
_FORBIDDEN_PREFIXES = ("sysforge.pipeline", "sysforge.ui")

# module filename -> frozenset of names it may still import from a forbidden
# layer. Shrink this; never grow it.
_ALLOWED_UPWARD_IMPORTS: dict[str, frozenset[str]] = {}


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


def test_layering_allowlist_is_empty():
    """No primitive may import from ``pipeline``/``ui`` — the allowlist is closed.

    3.2.0-F12 removed the last entry. Keeping this assertion separate from
    :func:`test_layering_allowlist_has_no_dead_entries` matters because that one
    loops over the allowlist and so passes trivially once it is empty; this one
    fails the moment a name is added back, forcing the re-authorisation to be an
    explicit, reviewed edit rather than a one-line dict insertion.
    """
    assert _ALLOWED_UPWARD_IMPORTS == {}, (
        "a primitive re-acquired an upward import into sysforge.pipeline/"
        "sysforge.ui — move the code down instead:\n  "
        + "\n  ".join(
            f"{fn}: {', '.join(sorted(names))}"
            for fn, names in sorted(_ALLOWED_UPWARD_IMPORTS.items())
        )
    )


# ---------------------------------------------------------------------------
# Verb modules are peers (3.2.0-F8)
# ---------------------------------------------------------------------------

def _cmd_modules() -> list[Path]:
    return sorted(SYSFORGE_DIR.glob("*_cmd.py"))


def test_verb_modules_do_not_import_siblings():
    """No ``*_cmd.py`` imports from another ``*_cmd.py``.

    Verb modules are peers. An edge between two of them is arbitrary — it
    records which verb happened to be written first, not which one owns the
    operation — and a set of such edges is a fourth layer nothing guards.
    Shared operations belong in ``verbs/shared.py`` (cross-verb behaviour) or
    ``verbs/helpers.py`` (small generic utilities); the fix for a violation is
    to relocate the operation's one home, never to duplicate it.

    Function-level imports count: deferring one dodges the import cycle at load
    time without removing the dependency.
    """
    violations: list[str] = []
    for path in _cmd_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            target = node.module.rsplit(".", 1)[-1]
            if target.endswith("_cmd") and target != path.stem:
                names = ", ".join(a.name for a in node.names)
                violations.append(
                    f"{path.name}:{node.lineno} imports {names} from {node.module}"
                )
    assert not violations, (
        "verb modules must not import from a sibling *_cmd.py — move the "
        "shared operation into sysforge/verbs/shared.py:\n  "
        + "\n  ".join(violations)
    )
