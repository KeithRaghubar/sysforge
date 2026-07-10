# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for install_reconcile.resolve_installed_name (shared rename lookup)."""
from sysforge.primitives.build_state import BuildState
from sysforge.primitives import install_reconcile


def _bs(tmp_path, entries):
    bs = BuildState(tmp_path)
    for name, entry in entries.items():
        bs._data[name] = entry  # direct seed for the test fixture
    return bs


def test_exact_key_returns_unchanged(tmp_path):
    bs = _bs(tmp_path, {"mesa": {"build_mode": "source_built", "pkgbase": "mesa"}})
    assert install_reconcile.resolve_installed_name(bs, "mesa") == "mesa"


def test_stock_base_resolves_to_renamed(tmp_path):
    bs = _bs(tmp_path, {
        "llvm-sysforge": {"build_mode": "pgo", "pkgbase": "llvm-sysforge",
                          "origin_pkgbase": "llvm"},
    })
    assert install_reconcile.resolve_installed_name(bs, "llvm") == "llvm-sysforge"


def test_untracked_returns_unchanged(tmp_path):
    bs = _bs(tmp_path, {})
    assert install_reconcile.resolve_installed_name(bs, "nano") == "nano"
