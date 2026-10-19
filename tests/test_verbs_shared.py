# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_verbs_shared.py — the cross-verb operations relocated by 3.2.0-F8.

``tests/test_state_forget.py`` covers the ``state forget`` verb's *output*;
this file covers ``forget_packages``' return contract directly, which is what
``revert`` and ``uninstall`` consume. They print nothing of their own from the
demotion, so a caller that mistook the two lists would fail silently rather
than visibly — the return value is the API, not the printing.
"""
from pathlib import Path

from sysforge.primitives.build_state import BuildState
from sysforge.verbs.shared import (
    entry_is_inert,
    entry_toml_block,
    forget_packages,
    rewrite_packages_toml,
)


def _seed(state_dir: Path, pkgname: str, pkgbase: str):
    state_dir.mkdir(parents=True, exist_ok=True)
    bs = BuildState(state_dir)
    bs.record(pkgname=pkgname, pkgver="1", pkgrel="1", epoch="0",
              pkgbase=pkgbase, pkgbuild_dir=state_dir, build_mode="source_built",
              source="repo")
    bs.save()


# ---------------------------------------------------------------------------
# forget_packages
# ---------------------------------------------------------------------------

def test_forget_packages_returns_forgotten_and_persists(tmp_path):
    state_dir = tmp_path / "state"
    _seed(state_dir, "mesa", "mesa")

    forgotten, missing = forget_packages(state_dir, ["mesa"])

    assert forgotten == ["mesa"]
    assert missing == []
    # Persisted, not just dropped from the in-memory copy.
    assert BuildState(state_dir).get("mesa") is None


def test_forget_packages_expands_split_siblings_by_pkgbase(tmp_path):
    """A pkgbase name takes every member with it — `forget llvm` drops llvm-libs."""
    state_dir = tmp_path / "state"
    _seed(state_dir, "llvm", "llvm")
    _seed(state_dir, "llvm-libs", "llvm")
    _seed(state_dir, "mesa", "mesa")

    forgotten, missing = forget_packages(state_dir, ["llvm"])

    assert forgotten == ["llvm", "llvm-libs"]
    assert missing == []
    # A package sharing no pkgbase is untouched.
    assert BuildState(state_dir).get("mesa") is not None


def test_forget_packages_reports_unknown_names_separately(tmp_path):
    """An unmatched name lands in `missing`, and does not abort the rest."""
    state_dir = tmp_path / "state"
    _seed(state_dir, "mesa", "mesa")

    forgotten, missing = forget_packages(state_dir, ["ghost", "mesa"])

    assert forgotten == ["mesa"]
    assert missing == ["ghost"]


def test_forget_packages_no_names_is_a_noop(tmp_path):
    state_dir = tmp_path / "state"
    _seed(state_dir, "mesa", "mesa")

    assert forget_packages(state_dir, []) == ([], [])
    assert BuildState(state_dir).get("mesa") is not None


# ---------------------------------------------------------------------------
# packages.toml entry shape / writer
# ---------------------------------------------------------------------------

def test_entry_is_inert_tracks_override_fields_only():
    assert entry_is_inert({"name": "mesa"})
    assert entry_is_inert({"name": "mesa", "source": "aur"})  # source is metadata
    assert not entry_is_inert({"name": "mesa", "cache": False})


def test_rewrite_preserves_comments_and_prunes_inert_entries(tmp_path):
    """The whole reason the writer is line-level rather than a TOML round-trip."""
    path = tmp_path / "packages.toml"
    path.write_text(
        "# hand-written header comment\n"
        "\n"
        "[[package]]\n"
        'name = "inert"\n'
        "\n"
        "[[package]]\n"
        'name = "keeper"\n'
        "cache = false\n",
        encoding="utf-8",
    )

    rewrite_packages_toml(
        path, append=entry_toml_block({"name": "added", "reason": "testing"})
    )

    text = path.read_text(encoding="utf-8")
    assert "# hand-written header comment" in text
    assert '"inert"' not in text      # auto-pruned: no override field
    assert '"keeper"' in text
    assert '"added"' in text


def test_rewrite_writes_once_through_replace_file(tmp_path, monkeypatch):
    """3.5.0-B47: atomic (and escalating) instead of a bare write_text."""
    from sysforge.primitives import atomic_write
    calls = []
    monkeypatch.setattr(atomic_write, "replace_file",
                        lambda path, text, tag: calls.append((path, text)))
    p = tmp_path / "packages.toml"
    p.write_text('[[package]]\nname = "a"\ncache = false\n')
    rewrite_packages_toml(p, drop_name="a",
                          append='\n[[package]]\nname = "a"\nenable_build_from_source = true\n')
    assert len(calls) == 1
    text = calls[0][1]
    assert text.count('name = "a"') == 1 and "enable_build_from_source = true" in text


def test_rewrite_on_missing_file_starts_from_the_header(tmp_path):
    """3.5.0-B48: a first opt-in keeps the header and [build] section."""
    p = tmp_path / "packages.toml"
    rewrite_packages_toml(p, drop_name="mesa",
                          append='\n[[package]]\nname = "mesa"\nenable_build_from_source = true\n')
    import tomllib
    data = tomllib.loads(p.read_text())
    assert data["build"]["pkgbuild_src_dir"] == "~/src"
    assert data["package"] == [{"name": "mesa", "enable_build_from_source": True}]


def test_forget_opt_out_failure_leaves_build_state_untouched(tmp_path, monkeypatch):
    """3.5.0-B57: the opt-out is written before build_state is saved."""
    import pytest
    from sysforge.primitives import opt_out
    _seed(tmp_path, "mesa", "mesa")
    before = (tmp_path / "build_state.toml").read_bytes()

    def _boom(*a, **k):
        raise opt_out.OptOutError("opted-out.toml is corrupt")
    monkeypatch.setattr(opt_out.OptOutList, "add", _boom)
    with pytest.raises(opt_out.OptOutError):
        forget_packages(tmp_path, ["mesa"], opt_out=opt_out.VIA_REVERT)
    assert (tmp_path / "build_state.toml").read_bytes() == before
