# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/identity.py — active-toolchain identity and the register-only path.
"""
from unittest.mock import MagicMock

from tests.toolchain_helpers import (  # noqa: F401 — autouse fixture
    _identity,
    _toolchain_gates_clean,
)


def test_compiler_paths_gcc():
    from sysforge.pipeline.stages.toolchain import compiler_paths
    assert compiler_paths("gcc") == ("/usr/bin/gcc", "/usr/bin/g++", None)

def test_compiler_paths_llvm():
    from sysforge.pipeline.stages.toolchain import compiler_paths
    assert compiler_paths("llvm") == ("/usr/bin/clang", "/usr/bin/clang++", "lld")

def test_identity_lines_unchanged_field_prints_once():
    """A field that did not move renders as a single value, not a transition."""
    from sysforge.pipeline.stages.toolchain import toolchain_identity_lines

    before = _identity(cc="gcc (GCC) 15.1.0", variant="gcc")
    lines = toolchain_identity_lines(before, before)
    assert lines == ["cc: gcc (GCC) 15.1.0", "variant: gcc"]

def test_identity_lines_gcc_path_reports_no_ld_and_no_fingerprint():
    """gcc path: compiler_paths returns ld=None, so the ld row is omitted."""
    from sysforge.pipeline.stages.toolchain import toolchain_identity_lines

    before = _identity(cc="gcc (GCC) 15.1.0", cxx="g++ (GCC) 15.1.0", variant="system")
    after = _identity(cc="gcc (GCC) 15.1.0", cxx="g++ (GCC) 15.1.0", variant="gcc")
    lines = toolchain_identity_lines(before, after)
    assert not any(line.startswith("ld:") for line in lines)
    assert not any(line.startswith("fingerprint:") for line in lines)
    assert lines[-1] == "variant: system → gcc"

def test_identity_lines_llvm_path_reports_ld_variant_and_fingerprint_moves():
    """llvm path: ld/variant/fingerprint all move and render as transitions."""
    from sysforge.pipeline.stages.toolchain import toolchain_identity_lines

    before = _identity(cc="clang version 20.1.0", ld="", variant="system", fingerprint=None)
    after = _identity(
        cc="clang version 21.1.0", ld="lld", variant="pgo_llvm", fingerprint="abc123"
    )
    lines = toolchain_identity_lines(before, after)
    assert "cc: clang version 20.1.0 → clang version 21.1.0" in lines
    assert "ld: lld" in lines
    assert "variant: system → pgo_llvm" in lines
    assert "fingerprint: abc123" in lines

def test_identity_lines_flag_delta_renders_added_and_removed():
    """Flag deltas come through diff_flags' +added / -removed vocabulary."""
    from sysforge.pipeline.stages.toolchain import toolchain_identity_lines

    before = _identity(flags={"llvm": "CFLAGS=-O2\nLDFLAGS=-fuse-ld=bfd"})
    after = _identity(flags={"llvm": "CFLAGS=-O3\nRUSTFLAGS=-Ctarget-cpu=native"})
    lines = toolchain_identity_lines(before, after)
    assert "flags (llvm):" in lines
    body = [line for line in lines if line.startswith("  ")]
    assert any(line.startswith("  CFLAGS:") and "→" in line for line in body)
    assert any(line.startswith("  -LDFLAGS:") for line in body)
    assert any(line.startswith("  +RUSTFLAGS:") for line in body)

def test_identity_lines_are_empty_when_nothing_is_known():
    """No identity at all yields no block, rather than a header with no rows."""
    from sysforge.pipeline.stages.toolchain import toolchain_identity_lines

    assert toolchain_identity_lines(_identity(), _identity()) == []

def test_identity_lines_degrade_the_arrow_under_the_ascii_gate(monkeypatch):
    """The transition arrow routes through render.arrow(), not a literal glyph."""
    from sysforge import log
    from sysforge.pipeline.stages.toolchain import toolchain_identity_lines

    monkeypatch.setattr(log, "use_unicode", lambda: False)
    lines = toolchain_identity_lines(_identity(variant="gcc"), _identity(variant="pgo_llvm"))
    assert lines == ["variant: gcc -> pgo_llvm"]

def test_probe_never_raises_when_state_is_broken(tmp_path):
    """A state object that raises degrades to empty fields, not an exception."""
    from sysforge.pipeline.stages.toolchain import probe_toolchain_identity

    class Boom:
        def get_stage_result(self, name):
            raise RuntimeError("state unreadable")

    options = MagicMock(state_dir=tmp_path)
    ident = probe_toolchain_identity(Boom(), options)
    assert ident.cc == "" and ident.variant == "" and ident.flags == {}

def test_probe_collects_only_toolchain_owned_flags(tmp_path):
    """Flags come from build_state, restricted to owner_stage == toolchain."""
    from sysforge.primitives.build_state import BuildState
    from sysforge.pipeline.stages.toolchain import probe_toolchain_identity

    bs = BuildState(tmp_path)
    bs.record("llvm", "21.1.0", "1", "", "llvm", tmp_path,
              build_mode="source_built", owner_stage="toolchain",
              flags_string="CFLAGS=-O3")
    bs.record("mesa", "25.0", "1", "", "mesa", tmp_path,
              build_mode="source_built", flags_string="CFLAGS=-O2")
    bs.save()

    state = MagicMock()
    state.get_stage_result.return_value = {}
    ident = probe_toolchain_identity(state, MagicMock(state_dir=tmp_path))
    assert ident.flags == {"llvm": "CFLAGS=-O3"}
