# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
kbuild_map.py — module→CONFIG_* map derived from a kernel source tree

The kernel's kbuild Makefiles are the authoritative, version-exact mapping
from a module name to the CONFIG_* symbol that builds it::

    obj-$(CONFIG_BLK_DEV_NVME) += nvme.o

A kernel source tree is only on disk transiently (the extracted makepkg
srcdir), so the parsed map is persisted to a JSON cache in the state dir.
Producers/consumers:

  - the kernel stage parses the just-built tree at Gate-2 time (the resolved
    ``.config``'s parent *is* the tree root) and saves the cache;
  - the hardware stage and the Gate-2 device audit load it and hand it to
    ``device_probe.enumerate_devices(kconfig_map=...)`` to widen coverage
    beyond the curated ``_MODULE_TO_KCONFIG`` table (which stays the vetted
    override and the fallback when no cache exists yet).

Accepted gaps (the curated table covers the ones that matter):

  - directory-gated drivers (``obj-$(CONFIG_X) += dir/`` with plain ``obj-y``
    lines inside) are not attributed to the gating symbol;
  - composite modules named only via ``<mod>-y :=`` lists resolve through the
    ``obj-$(CONFIG_X) += <mod>.o`` line, which is present for the vast
    majority of drivers.

Pure module: no logging (callers surface outcomes under their own tag) and
no state-dir resolution (callers pass explicit paths, mirroring
``BuildState(state_dir)``).

Public API:
    parse_kbuild_tree(tree_root) -> dict[str, str]
    save_map(path, mapping, kernel_release) -> None
    load_map(path) -> tuple[dict[str, str], str] | None
    KBUILD_MAP_FILENAME
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

KBUILD_MAP_FILENAME = "kbuild_module_map.json"

# Top-level tree entries that contain no driver kbuild files. Pruned at the
# top level only — a nested dir that happens to share a name stays in scope.
_SKIP_TOP_DIRS = frozenset({
    "Documentation", "tools", "scripts", "samples", "usr", "LICENSES", ".git",
})

# obj-$(CONFIG_X) += a.o b.o   (also tolerates := and = assignment forms)
_OBJ_LINE = re.compile(
    r"^obj-\$\((CONFIG_[A-Z0-9_]+)\)\s*[:+]?=\s*(.+)$"
)


def _iter_kbuild_files(tree_root: Path):
    """Yield every Makefile/Kbuild under the tree, sorted for determinism."""
    for top in sorted(tree_root.iterdir()):
        if top.name in _SKIP_TOP_DIRS:
            continue
        if top.is_file():
            if top.name in ("Makefile", "Kbuild"):
                yield top
            continue
        if not top.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames.sort()
            for fname in ("Kbuild", "Makefile"):
                if fname in filenames:
                    yield Path(dirpath) / fname


def _joined_lines(text: str):
    """Yield logical lines with backslash continuations folded in."""
    pending = ""
    for raw in text.splitlines():
        line = pending + raw
        if line.endswith("\\"):
            pending = line[:-1] + " "
            continue
        pending = ""
        yield line.strip()
    if pending:
        yield pending.strip()


def parse_kbuild_tree(tree_root: Path) -> dict[str, str]:
    """Parse ``obj-$(CONFIG_X) += mod.o`` lines into ``{module: CONFIG_X}``.

    Module names are the object stems with ``-`` normalized to ``_`` — the
    same convention ``modules.alias`` uses (and that ``device_probe``'s
    reference-alias parser applies). First mapping wins on the rare duplicate
    module name; the walk order is sorted, so the result is deterministic.
    """
    mapping: dict[str, str] = {}
    for kfile in _iter_kbuild_files(Path(tree_root)):
        try:
            text = kfile.read_text(errors="replace")
        except (FileNotFoundError, PermissionError, OSError):
            continue
        for line in _joined_lines(text):
            m = _OBJ_LINE.match(line)
            if m is None:
                continue
            config, rhs = m.group(1), m.group(2)
            for token in rhs.split():
                if not token.endswith(".o"):
                    continue  # skips dir/ entries and make-function noise
                stem = token[:-2].rsplit("/", 1)[-1]
                if not stem:
                    continue
                module = stem.replace("-", "_")
                mapping.setdefault(module, config)
    return mapping


def save_map(path: Path, mapping: dict[str, str], kernel_release: str | None) -> None:
    """Persist the map atomically (tmp + rename) with provenance."""
    path = Path(path)
    payload = {
        "kernel_release": kernel_release or "",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entry_count": len(mapping),
        "entries": mapping,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def load_map(path: Path) -> tuple[dict[str, str], str] | None:
    """Load a saved map; None when missing, corrupt, or the wrong shape."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    entries = payload.get("entries")
    release = payload.get("kernel_release", "")
    if not isinstance(entries, dict) or not isinstance(release, str):
        return None
    clean = {
        k: v for k, v in entries.items()
        if isinstance(k, str) and isinstance(v, str)
    }
    return clean, release
