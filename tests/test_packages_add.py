"""
test_packages_add.py — coverage for `sysforge packages add` and the
auto-prune-on-write-back behavior of packages.toml mutations.

The new model: packages.toml stores override rules only. `add` requires at
least one behavior-changing override flag (--enable-build-from-source,
--no-cache, --reason); --source is metadata that doesn't satisfy validation. Every
write-back drops inert entries (entries without a behavior-changing
override field) so the file converges toward a minimal override set.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from sysforge.packages_cmd import (
    cmd_packages_add,
    cmd_packages_add_group,
    cmd_packages_remove,
    entry_is_inert,
    _rewrite_packages_toml,
)


def _args(pkg, packages, **overrides):
    defaults = dict(
        pkg=pkg,
        packages=str(packages),
        source=None,
        enable_build_from_source=False,
        no_cache=False,
        reason=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _seed(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


# ---------------------------------------------------------------------------
# Validation: at least one behavior-changing override is required
# ---------------------------------------------------------------------------

def test_add_rejects_no_overrides(tmp_path, capsys):
    path = tmp_path / "packages.toml"
    with pytest.raises(SystemExit):
        cmd_packages_add(_args("foo", path))
    err = capsys.readouterr().err
    assert "behavior-changing override" in err


def test_add_rejects_source_only(tmp_path, capsys):
    """--source alone is metadata, not a behavior-changing override."""
    path = tmp_path / "packages.toml"
    with pytest.raises(SystemExit):
        cmd_packages_add(_args("foo", path, source="aur"))
    err = capsys.readouterr().err
    assert "behavior-changing override" in err


def test_add_accepts_enable_build_from_source(tmp_path):
    path = tmp_path / "packages.toml"
    cmd_packages_add(_args("mesa-git", path, enable_build_from_source=True))
    text = path.read_text()
    assert 'name = "mesa-git"' in text
    assert "enable_build_from_source = true" in text


def test_add_accepts_no_cache(tmp_path):
    path = tmp_path / "packages.toml"
    cmd_packages_add(_args("llvm", path, no_cache=True, source="repo"))
    text = path.read_text()
    assert 'name = "llvm"' in text
    assert 'source = "repo"' in text
    assert "cache = false" in text


def test_add_accepts_reason(tmp_path):
    path = tmp_path / "packages.toml"
    cmd_packages_add(_args("steam", path, reason="vendored binary; ABI check noisy"))
    text = path.read_text()
    assert 'reason = "vendored binary; ABI check noisy"' in text


# ---------------------------------------------------------------------------
# Update vs add: existing entry is replaced atomically
# ---------------------------------------------------------------------------

def test_add_updates_existing_entry(tmp_path):
    path = tmp_path / "packages.toml"
    _seed(
        path,
        '# header\n\n[build]\npkgbuild_src_dir = "~/src"\n\n'
        '[[package]]\nname = "mesa-git"\nenable_build_from_source = true\n',
    )
    cmd_packages_add(_args("mesa-git", path, no_cache=True))
    text = path.read_text()
    # Old block replaced; only one mesa-git survives.
    assert text.count('name = "mesa-git"') == 1
    assert "cache = false" in text
    assert "enable_build_from_source = true" not in text


# ---------------------------------------------------------------------------
# Auto-prune: writes drop inert entries
# ---------------------------------------------------------------------------

def test_inert_predicate():
    assert entry_is_inert({"name": "foo"})
    assert entry_is_inert({"name": "foo", "source": "aur"})
    assert not entry_is_inert({"name": "foo", "enable_build_from_source": True})
    assert not entry_is_inert({"name": "foo", "cache": False})
    assert not entry_is_inert({"name": "foo", "reason": "x"})


def test_inert_predicate_honors_legacy_key():
    """A legacy ``pkgbuild_patch`` entry must count as non-inert so the
    auto-prune on write-back never silently deletes a pre-rename config."""
    assert not entry_is_inert({"name": "foo", "pkgbuild_patch": True})


def test_add_prunes_existing_inert_entries(tmp_path):
    """A new add triggers a write-back; pre-existing inert entries get pruned."""
    path = tmp_path / "packages.toml"
    _seed(
        path,
        '# header\n\n[build]\npkgbuild_src_dir = "~/src"\n\n'
        '[[package]]\nname = "yay"\nsource = "aur"\n\n'              # inert
        '[[package]]\nname = "neovim"\nsource = "repo"\n\n'           # inert
        '[[package]]\nname = "llvm"\nsource = "repo"\ncache = false\n',  # real override
    )
    cmd_packages_add(_args("mesa-git", path, enable_build_from_source=True))
    text = path.read_text()
    assert "yay" not in text
    assert "neovim" not in text
    assert 'name = "llvm"' in text          # behavior-changing override survives
    assert 'name = "mesa-git"' in text      # new entry written


def test_remove_prunes_inert_entries(tmp_path):
    """packages remove triggers a write-back; inert siblings are pruned alongside."""
    path = tmp_path / "packages.toml"
    _seed(
        path,
        '# header\n\n[build]\npkgbuild_src_dir = "~/src"\n\n'
        '[[package]]\nname = "yay"\nsource = "aur"\n\n'
        '[[package]]\nname = "mesa-git"\nenable_build_from_source = true\n',
    )
    cmd_packages_remove(_args("mesa-git", path))
    text = path.read_text()
    assert "mesa-git" not in text
    assert "yay" not in text                # auto-pruned alongside the explicit remove
    assert "[build]" in text                # header preserved


def test_rewrite_preserves_header_comment(tmp_path):
    path = tmp_path / "packages.toml"
    _seed(
        path,
        '# packages.toml — managed by sysforge packages\n'
        '# Second comment line.\n\n'
        '[build]\npkgbuild_src_dir = "~/src"\n',
    )
    _rewrite_packages_toml(path, append='\n[[package]]\nname = "x"\nenable_build_from_source = true\n')
    text = path.read_text()
    assert text.startswith("# packages.toml — managed by sysforge packages\n")
    assert "# Second comment line." in text
    assert 'name = "x"' in text


# ---------------------------------------------------------------------------
# Back-compat: legacy pkgbuild_patch key
# ---------------------------------------------------------------------------

def test_legacy_key_not_pruned_and_migrated_on_rewrite(tmp_path):
    """A pre-rename entry carrying only ``pkgbuild_patch`` survives a write-back
    (not pruned as inert) and is migrated in place to the new key name."""
    path = tmp_path / "packages.toml"
    _seed(
        path,
        '# header\n\n[build]\npkgbuild_src_dir = "~/src"\n\n'
        '[[package]]\nname = "mesa"\npkgbuild_patch = true\n',
    )
    # An unrelated add triggers a rewrite touching the whole file.
    cmd_packages_add(_args("llvm", path, no_cache=True))
    text = path.read_text()
    assert 'name = "mesa"' in text                      # not pruned
    assert "pkgbuild_patch" not in text                 # legacy key gone
    assert "enable_build_from_source = true" in text    # migrated


def test_remove_missing_entry_is_fatal(tmp_path, capsys):
    path = tmp_path / "packages.toml"
    _seed(path, '[build]\npkgbuild_src_dir = "~/src"\n')
    with pytest.raises(SystemExit):
        cmd_packages_remove(_args("ghost", path))


# ---------------------------------------------------------------------------
# add-group
# ---------------------------------------------------------------------------

def _group_args(desktop, packages):
    return SimpleNamespace(desktop=desktop, packages=str(packages))


def test_add_group_writes_group(tmp_path, capsys):
    import tomllib
    path = tmp_path / "packages.toml"
    _seed(path, '[build]\npkgbuild_src_dir = "~/src"\n')
    cmd_packages_add_group(_group_args("gnome", path))
    data = tomllib.loads(path.read_text())
    assert "gnome" in data.get("group", {})
    assert "Wrote [group.gnome]" in capsys.readouterr().out


def test_add_group_creates_missing_file(tmp_path):
    import tomllib
    path = tmp_path / "packages.toml"
    cmd_packages_add_group(_group_args("kde", path))
    assert "kde" in tomllib.loads(path.read_text()).get("group", {})
