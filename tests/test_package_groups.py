"""
test_package_groups.py — [group.*] expansion in packages.toml.

Covers the single expansion point (config.expand_package_groups) and its two
load-bearing consumers: update's override loader and the packages stage
loader. Display-only consumers (completions, packages list, reconfigure
summaries) share the same helper.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sysforge.primitives.config import expand_package_groups


# ---------------------------------------------------------------------------
# expand_package_groups
# ---------------------------------------------------------------------------

def test_no_groups_passthrough():
    data = {"package": [{"name": "htop"}]}
    assert expand_package_groups(data) == [{"name": "htop"}]


def test_basic_expansion_marks_origin():
    data = {"group": {"cosmic": {"packages": ["cosmic-comp-git", "cosmic-session-git"]}}}
    entries = expand_package_groups(data)
    assert entries == [
        {"name": "cosmic-comp-git", "group": "cosmic"},
        {"name": "cosmic-session-git", "group": "cosmic"},
    ]


def test_group_defaults_inherit_to_members():
    data = {"group": {"patched": {
        "packages": ["mesa-git"],
        "enable_build_from_source": True,
        "source": "aur",
    }}}
    (entry,) = expand_package_groups(data)
    assert entry["enable_build_from_source"] is True
    assert entry["source"] == "aur"
    assert entry["group"] == "patched"


def test_legacy_key_no_longer_normalized_on_expand():
    """3.0.0 removed the read-side rename; a pre-rename ``pkgbuild_patch``
    entry passes through unchanged for both explicit entries and group
    defaults (the write-side rewrite in ``packages_cmd`` still migrates it
    on the next file write)."""
    data = {
        "package": [{"name": "htop", "pkgbuild_patch": True}],
        "group": {"patched": {"packages": ["mesa-git"], "pkgbuild_patch": True}},
    }
    entries = {e["name"]: e for e in expand_package_groups(data)}
    assert "enable_build_from_source" not in entries["htop"]
    assert entries["htop"]["pkgbuild_patch"] is True
    assert "enable_build_from_source" not in entries["mesa-git"]
    assert entries["mesa-git"]["pkgbuild_patch"] is True


def test_explicit_entry_wins_over_group():
    """An explicit [[package]] entry beats the group outright — no merge."""
    data = {
        "package": [{"name": "mesa-git", "cache": False}],
        "group": {"patched": {"packages": ["mesa-git"], "enable_build_from_source": True}},
    }
    (entry,) = expand_package_groups(data)
    assert entry == {"name": "mesa-git", "cache": False}  # no enable_build_from_source


def test_first_group_wins_on_duplicate_membership():
    data = {"group": {
        "a": {"packages": ["pkg"], "cache": False},
        "b": {"packages": ["pkg"], "enable_build_from_source": True},
    }}
    (entry,) = expand_package_groups(data)
    assert entry["group"] == "a"
    assert "enable_build_from_source" not in entry


def test_malformed_group_values_ignored():
    data = {"group": {"bad": "not-a-table", "empty": {}, "ok": {"packages": ["x"]}}}
    entries = expand_package_groups(data)
    assert entries == [{"name": "x", "group": "ok"}]


# ---------------------------------------------------------------------------
# update override loader — group members participate, no inert spam
# ---------------------------------------------------------------------------

def test_update_loader_expands_groups(tmp_path, capsys):
    from sysforge.update import _load_overrides
    p = tmp_path / "packages.toml"
    p.write_text(
        '[[package]]\nname = "pipewire"\nsource = "repo"\n'   # inert -> warns
        '[group.cosmic]\npackages = ["cosmic-comp-git"]\n'    # inert, no warn
        '[group.patched]\npackages = ["mesa-git"]\nenable_build_from_source = true\n'
    )
    _, overrides = _load_overrides(p)
    assert set(overrides) == {"pipewire", "cosmic-comp-git", "mesa-git"}
    assert overrides["mesa-git"]["enable_build_from_source"] is True
    err = capsys.readouterr().err
    assert "pipewire" in err and "inert" in err
    assert "cosmic-comp-git" not in err  # group members never get the nudge


# ---------------------------------------------------------------------------
# packages stage loader — bootstrap installs group members
# ---------------------------------------------------------------------------

def test_stage_loader_expands_groups(tmp_path):
    from sysforge.pipeline.stages.packages import _load_packages
    p = tmp_path / "packages.toml"
    p.write_text(
        '[[package]]\nname = "htop"\n'
        '[group.cosmic]\npackages = ["cosmic-comp-git"]\n'
    )
    _, packages = _load_packages({"packages_file": str(p)})
    assert [e["name"] for e in packages] == ["htop", "cosmic-comp-git"]
