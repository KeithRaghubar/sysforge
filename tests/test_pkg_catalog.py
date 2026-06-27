"""Tests for primitives/pkg_catalog.py — desktop catalog, guided selection,
and the [group.*] writer."""
import tomllib

import pytest

from sysforge.primitives import pkg_catalog
from sysforge.primitives.config import expand_package_groups
from sysforge.primitives.pkg_catalog import (
    DESKTOP_CATALOG,
    display_manager_for,
    group_toml_block,
    select_desktop,
    valid_desktops,
    write_desktop_group,
)


# ---------------------------------------------------------------------------
# Catalog integrity
# ---------------------------------------------------------------------------

class TestCatalog:
    def test_valid_desktops_matches_catalog_keys(self):
        assert valid_desktops() == list(DESKTOP_CATALOG)

    def test_expected_environments_present(self):
        assert set(valid_desktops()) >= {"gnome", "kde"}

    def test_entry_key_matches_dict_key(self):
        for key, entry in DESKTOP_CATALOG.items():
            assert entry.key == key
            assert entry.packages, f"{key} has no packages"
            assert entry.display_name

    def test_curated_set_present(self):
        assert set(valid_desktops()) >= {
            "gnome", "kde", "xfce", "mate", "cinnamon", "lxqt", "budgie", "cosmic",
        }


# ---------------------------------------------------------------------------
# Display-manager pairing (F27 + boot-into-desktop fix)
# ---------------------------------------------------------------------------

class TestDisplayManager:
    def test_every_entry_declares_a_display_manager(self):
        for key, entry in DESKTOP_CATALOG.items():
            assert entry.display_manager, f"{key} has no display_manager"

    def test_display_manager_is_in_the_package_list(self):
        # The DM must actually be installed by the group, or enabling its
        # service post-install would fail.
        for key, entry in DESKTOP_CATALOG.items():
            assert entry.display_manager in entry.packages, (
                f"{key}: {entry.display_manager} not in packages"
            )

    def test_display_manager_for_returns_the_pairing(self):
        assert display_manager_for("gnome") == "gdm"
        assert display_manager_for("kde") == "sddm"

    def test_display_manager_for_unknown_returns_none(self):
        assert display_manager_for("nope") is None

    def test_lightdm_desktops_bundle_a_greeter(self):
        # lightdm shows nothing usable without a greeter — bundle one.
        for key, entry in DESKTOP_CATALOG.items():
            if entry.display_manager == "lightdm":
                assert any(p.endswith("-greeter") for p in entry.packages), (
                    f"{key}: lightdm without a greeter package"
                )


# ---------------------------------------------------------------------------
# group_toml_block serialization
# ---------------------------------------------------------------------------

class TestGroupTomlBlock:
    def test_round_trips(self):
        block = group_toml_block("gnome", ["gnome-shell", "gdm"])
        data = tomllib.loads(block)
        assert data["group"]["gnome"]["packages"] == ["gnome-shell", "gdm"]

    def test_defaults_emitted(self):
        block = group_toml_block(
            "x", ["a"], {"source": "aur", "enable_build_from_source": True}
        )
        data = tomllib.loads(block)["group"]["x"]
        assert data["source"] == "aur"
        assert data["enable_build_from_source"] is True

    def test_empty_members(self):
        data = tomllib.loads(group_toml_block("x", []))
        assert data["group"]["x"]["packages"] == []


# ---------------------------------------------------------------------------
# select_desktop resolution
# ---------------------------------------------------------------------------

class TestSelectDesktop:
    def test_preselected_wins(self):
        # No prompting even if interactive — preselection short-circuits.
        assert select_desktop(interactive=True, preselected="kde") == "kde"

    def test_preselected_case_insensitive(self):
        assert select_desktop(interactive=True, preselected="GNOME") == "gnome"

    def test_preselected_unknown_returns_none(self):
        assert select_desktop(interactive=True, preselected="bogus") is None

    def test_non_interactive_returns_none(self):
        assert select_desktop(interactive=False, preselected=None) is None

    def test_interactive_but_no_tty_returns_none(self, monkeypatch):
        # interactive=True but the TTY check fails → skip (no blocking).
        monkeypatch.setattr(pkg_catalog, "is_interactive", lambda: False)
        assert select_desktop(interactive=True, preselected=None) is None

    def test_prompt_decline_returns_none(self, monkeypatch):
        monkeypatch.setattr(pkg_catalog, "is_interactive", lambda: True)
        monkeypatch.setattr(pkg_catalog, "prompt_choice", lambda *a, **k: "n")
        assert select_desktop(interactive=True, preselected=None) is None

    def test_prompt_accept_then_pick(self, monkeypatch):
        monkeypatch.setattr(pkg_catalog, "is_interactive", lambda: True)
        monkeypatch.setattr(pkg_catalog, "prompt_choice", lambda *a, **k: "y")
        monkeypatch.setattr(pkg_catalog, "prompt_key", lambda *a, **k: "1")
        choice = select_desktop(interactive=True, preselected=None)
        assert choice == list(DESKTOP_CATALOG)[0]

    def test_prompt_accept_then_skip(self, monkeypatch):
        monkeypatch.setattr(pkg_catalog, "is_interactive", lambda: True)
        monkeypatch.setattr(pkg_catalog, "prompt_choice", lambda *a, **k: "y")
        monkeypatch.setattr(pkg_catalog, "prompt_key", lambda *a, **k: "")
        assert select_desktop(interactive=True, preselected=None) is None


# ---------------------------------------------------------------------------
# write_desktop_group
# ---------------------------------------------------------------------------

class TestWriteDesktopGroup:
    def test_creates_file_with_header(self, tmp_path):
        path = tmp_path / "packages.toml"
        write_desktop_group(path, "gnome")
        data = tomllib.loads(path.read_text())
        assert "gnome" in data.get("group", {})
        assert data["group"]["gnome"]["packages"] == list(
            DESKTOP_CATALOG["gnome"].packages
        )

    def test_unknown_key_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown desktop"):
            write_desktop_group(tmp_path / "packages.toml", "bogus")

    def test_idempotent_replace_no_duplicate(self, tmp_path):
        path = tmp_path / "packages.toml"
        write_desktop_group(path, "gnome")
        write_desktop_group(path, "gnome")
        text = path.read_text()
        assert text.count("[group.gnome]") == 1
        assert tomllib.loads(text)  # still valid

    def test_preserves_existing_package_block(self, tmp_path):
        path = tmp_path / "packages.toml"
        path.write_text(
            "# header\n\n[build]\nrepo_mode = \"pacman\"\n\n"
            "[[package]]\nname = \"llvm\"\npkgbuild_patch = true\n"
        )
        write_desktop_group(path, "kde")
        data = tomllib.loads(path.read_text())
        names = [e["name"] for e in expand_package_groups(data)]
        assert "llvm" in names                       # untouched
        assert "plasma-meta" in names                # group expanded
        assert data["build"]["repo_mode"] == "pacman"

    def test_two_groups_coexist(self, tmp_path):
        path = tmp_path / "packages.toml"
        write_desktop_group(path, "gnome")
        write_desktop_group(path, "kde")
        data = tomllib.loads(path.read_text())
        assert set(data["group"]) == {"gnome", "kde"}

    def test_no_blank_line_accumulation(self, tmp_path):
        path = tmp_path / "packages.toml"
        for _ in range(5):
            write_desktop_group(path, "gnome")
            write_desktop_group(path, "kde")
        assert "\n\n\n" not in path.read_text()
