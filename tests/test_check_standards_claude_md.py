# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Unit guards for tools/check_standards.py::check_claude_md.

CLAUDE.md is split into a root process file and a lazily-loaded code-seam
fragment (sysforge/CLAUDE.md). The rules in both cite concrete paths and
`module.symbol` seams; the drift mode this lint guards against is a rule going
stale — citing a file or symbol that has since moved or been renamed — which
would silently mislead future sessions.

The checker is deliberately fail-safe: tokens it cannot map to a repo file
(class attributes, CLI flags, prose) are skipped, never flagged. It only
errors when an explicit path is missing or a resolved module lacks the cited
symbol.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "check_standards.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_standards_cm", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_standards_cm"] = mod
    spec.loader.exec_module(mod)
    return mod


check_standards = _load()


def _repo(tmp_path: Path, claude_md: str, files: dict[str, str] | None = None) -> Path:
    for rel, body in (files or {}).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    return tmp_path


def test_existing_path_citation_passes(tmp_path):
    repo = _repo(tmp_path, "Rules live in `tools/release.sh`.\n",
                 {"tools/release.sh": "#!/bin/sh\n"})
    assert check_standards.check_claude_md(repo) == []


def test_missing_path_citation_is_error(tmp_path):
    repo = _repo(tmp_path, "See `tools/release.sh` for the flow.\n")
    findings = check_standards.check_claude_md(repo)
    assert any("tools/release.sh" in f.location or "tools/release.sh" in f.message
               for f in findings), findings


def test_sysforge_relative_path_resolves(tmp_path):
    """`primitives/paths.py` is cited repo-root-relative to sysforge/."""
    repo = _repo(tmp_path, "User paths -> `primitives/paths.py`.\n",
                 {"sysforge/primitives/paths.py": "X = 1\n"})
    assert check_standards.check_claude_md(repo) == []


def test_bare_py_filename_resolved_by_search(tmp_path):
    repo = _repo(tmp_path, "Diagnostics in `build_diag.py`.\n",
                 {"sysforge/primitives/build_diag.py": "M = []\n"})
    assert check_standards.check_claude_md(repo) == []


def test_missing_bare_py_filename_is_error(tmp_path):
    repo = _repo(tmp_path, "Diagnostics in `build_diag.py`.\n")
    findings = check_standards.check_claude_md(repo)
    assert any("build_diag.py" in f.message or "build_diag.py" in f.location
               for f in findings), findings


def test_dotted_symbol_present_passes(tmp_path):
    repo = _repo(tmp_path, "Throttle via `build_throttle.resolve_throttle`.\n",
                 {"sysforge/primitives/build_throttle.py":
                  "def resolve_throttle():\n    pass\n"})
    assert check_standards.check_claude_md(repo) == []


def test_dotted_symbol_missing_is_error(tmp_path):
    repo = _repo(tmp_path, "Throttle via `build_throttle.resolve_throttle`.\n",
                 {"sysforge/primitives/build_throttle.py": "def other():\n    pass\n"})
    findings = check_standards.check_claude_md(repo)
    assert any("resolve_throttle" in f.message for f in findings), findings


def test_unresolvable_tokens_are_skipped(tmp_path):
    """Class attrs, CLI flags, globs, env reads, prose: never flagged."""
    body = (
        "Key tuple `BuildState._serialize`; flag `--reuse-built`; edit\n"
        "`docs/design/*.md`; never `os.environ[\"BUILDDIR\"]`; id `1.2.0-F1`;\n"
        "`vX.Y.Z.md`; `emit_makepkg_conf(is_lib32=)`; `[bolt] enabled = false`.\n"
    )
    repo = _repo(tmp_path, body)
    assert check_standards.check_claude_md(repo) == []


def test_nested_claude_md_is_also_checked(tmp_path):
    repo = _repo(tmp_path, "Root file, nothing cited.\n",
                 {"sysforge/primitives/flag_drift.py": "def renamed():\n    pass\n"})
    frag = repo / "sysforge" / "CLAUDE.md"
    frag.write_text("Seam: `flag_drift.resolve_flag_drift`.\n", encoding="utf-8")
    findings = check_standards.check_claude_md(repo)
    assert any("flag_drift" in f.message for f in findings), findings


def test_path_symbol_form(tmp_path):
    """`file.py::symbol` citations grep the resolved file for the symbol."""
    repo = _repo(tmp_path, "Checker: `toolchain.py::_verify_llvm_install`.\n",
                 {"sysforge/pipeline/stages/toolchain.py":
                  "def _verify_llvm_install():\n    pass\n"})
    assert check_standards.check_claude_md(repo) == []
    (repo / "sysforge/pipeline/stages/toolchain.py").write_text(
        "def renamed():\n    pass\n", encoding="utf-8")
    findings = check_standards.check_claude_md(repo)
    assert any("_verify_llvm_install" in f.message for f in findings), findings
