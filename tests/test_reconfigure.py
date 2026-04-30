"""
test_reconfigure.py — unit tests for sysforge.pipeline.stages.reconfigure

Covers _set_repo_mode, _step_build_mode, _step_preview, _parse_step_selection,
and the new build_mode step registration. No real filesystem I/O beyond tmp_path.
"""
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sysforge.pipeline.stages.reconfigure import (
    _STEP_KEYS,
    _STEP_FNS,
    _parse_step_selection,
    _set_repo_mode,
    _step_build_mode,
    _step_preview,
)
from sysforge.pipeline.stages.base import RunOptions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_options(**kwargs):
    defaults = dict(resume=False, start_from=None, force_retry=False,
                    dry_run=False, state_dir=None)
    defaults.update(kwargs)
    return RunOptions(**defaults)


def make_packages_toml(tmp_path, content: str) -> Path:
    p = tmp_path / "packages.toml"
    p.write_text(content)
    return p


_BASIC_TOML = """\
[build]
pkgbuild_src_dir = "~/src"

[[package]]
name = "htop"
source = "repo"

[[package]]
name = "mesa-git"
source = "aur"
pkgbuild_patch = true
"""

_REPO_MODE_PROFILED_TOML = """\
[build]
pkgbuild_src_dir = "~/src"
repo_mode = "profiled"

[[package]]
name = "htop"
source = "repo"

[[package]]
name = "neovim"
source = "repo"
"""

_NO_BUILD_SECTION_TOML = """\
[[package]]
name = "htop"
source = "repo"
"""


# ---------------------------------------------------------------------------
# _STEP_KEYS / _STEP_FNS registration
# ---------------------------------------------------------------------------

def test_build_mode_in_step_keys():
    assert "build_mode" in _STEP_KEYS


def test_build_mode_in_step_fns():
    assert "build_mode" in _STEP_FNS


def test_build_mode_step_order():
    """build_mode must appear after config and before makepkg."""
    keys = list(_STEP_KEYS)
    assert keys.index("build_mode") == keys.index("config") + 1
    assert keys.index("build_mode") < keys.index("makepkg")


# ---------------------------------------------------------------------------
# _parse_step_selection — build_mode name
# ---------------------------------------------------------------------------

def test_parse_step_selection_build_mode_by_name():
    selected, invalid = _parse_step_selection("build_mode")
    assert selected == ["build_mode"]
    assert invalid == []


def test_parse_step_selection_build_mode_by_number():
    # build_mode is the 3rd step (index 2, number 3)
    selected, invalid = _parse_step_selection("3")
    assert selected == ["build_mode"]
    assert invalid == []


def test_parse_step_selection_invalid_input_reports_invalid():
    """Single-letter jibberish must NOT silently fall back to 'all' — it must
    surface in the invalid list so the caller can re-prompt."""
    selected, invalid = _parse_step_selection("e")
    assert selected == []
    assert invalid == ["e"]


def test_parse_step_selection_jibberish_reports_invalid():
    selected, invalid = _parse_step_selection("xyz qqq")
    assert selected == []
    assert invalid == ["xyz", "qqq"]


def test_parse_step_selection_partial_invalid():
    """Mixing valid + invalid tokens reports the invalid ones but still
    returns the valid selection — the caller warns and proceeds."""
    selected, invalid = _parse_step_selection("editor xyz 99")
    assert selected == ["editor"]
    assert invalid == ["xyz", "99"]


def test_parse_step_selection_empty_returns_all():
    selected, invalid = _parse_step_selection("")
    assert selected == list(_STEP_KEYS)
    assert invalid == []


def test_parse_step_selection_cancel():
    selected, invalid = _parse_step_selection("0")
    assert selected == []
    assert invalid == []
    selected, invalid = _parse_step_selection("cancel")
    assert selected == []
    assert invalid == []


def test_parse_step_selection_out_of_range_number():
    selected, invalid = _parse_step_selection("99")
    assert selected == []
    assert invalid == ["99"]


def test_parse_step_selection_invalid_range():
    selected, invalid = _parse_step_selection("5-2")
    assert selected == []
    assert invalid == ["5-2"]


# ---------------------------------------------------------------------------
# _set_repo_mode
# ---------------------------------------------------------------------------

def test_set_repo_mode_replaces_existing(tmp_path):
    p = make_packages_toml(tmp_path, '[build]\nrepo_mode = "pacman"\n')
    _set_repo_mode(p, "profiled")
    assert 'repo_mode = "profiled"' in p.read_text()


def test_set_repo_mode_inserts_after_build_header(tmp_path):
    p = make_packages_toml(tmp_path, '[build]\npkgbuild_src_dir = "~/src"\n')
    _set_repo_mode(p, "profiled")
    text = p.read_text()
    assert 'repo_mode = "profiled"' in text
    # Should appear after [build]
    assert text.index("[build]") < text.index('repo_mode = "profiled"')


def test_set_repo_mode_no_build_section_appends(tmp_path):
    p = make_packages_toml(tmp_path, '[[package]]\nname = "htop"\nsource = "repo"\n')
    _set_repo_mode(p, "profiled")
    text = p.read_text()
    assert "[build]" in text
    assert 'repo_mode = "profiled"' in text


def test_set_repo_mode_roundtrip_valid_toml(tmp_path):
    p = make_packages_toml(tmp_path, _BASIC_TOML)
    _set_repo_mode(p, "profiled")
    with open(p, "rb") as f:
        data = tomllib.load(f)
    assert data["build"]["repo_mode"] == "profiled"


def test_set_repo_mode_preserves_other_content(tmp_path):
    p = make_packages_toml(tmp_path, _BASIC_TOML)
    _set_repo_mode(p, "profiled")
    with open(p, "rb") as f:
        data = tomllib.load(f)
    assert data["build"]["pkgbuild_src_dir"] == "~/src"
    assert any(pkg["name"] == "htop" for pkg in data["package"])


# ---------------------------------------------------------------------------
# _step_build_mode — non-interactive (dry_run / no tty)
# ---------------------------------------------------------------------------

def test_step_build_mode_shows_current_mode(tmp_path, capsys):
    p = make_packages_toml(tmp_path, _BASIC_TOML)
    config = {"packages_file": str(p)}

    with patch("sysforge.pipeline.stages.reconfigure._interactive", return_value=False):
        _step_build_mode(config, None, make_options(), "vi")

    # Just verifies it runs without error; log goes to _log not stdout


def test_step_build_mode_dry_run_does_not_write(tmp_path):
    p = make_packages_toml(tmp_path, '[build]\nrepo_mode = "pacman"\n\n[[package]]\nname = "htop"\nsource = "repo"\n')
    original = p.read_text()
    config = {"packages_file": str(p)}

    with patch("sysforge.pipeline.stages.reconfigure._interactive", return_value=True), \
         patch("sysforge.pipeline.stages.reconfigure._prompt_choice", return_value="r"):
        _step_build_mode(config, None, make_options(dry_run=True), "vi")

    assert p.read_text() == original


def test_step_build_mode_missing_file_skips(tmp_path):
    config = {"packages_file": str(tmp_path / "nonexistent.toml")}
    # Should return without raising
    result = _step_build_mode(config, None, make_options(), "vi")
    assert result == "vi"


def test_step_build_mode_interactive_sets_profiled(tmp_path):
    p = make_packages_toml(tmp_path, '[build]\nrepo_mode = "pacman"\n\n[[package]]\nname = "htop"\nsource = "repo"\n')
    config = {"packages_file": str(p)}

    with patch("sysforge.pipeline.stages.reconfigure._interactive", return_value=True), \
         patch("sysforge.pipeline.stages.reconfigure._prompt_choice", return_value="r"):
        _step_build_mode(config, None, make_options(), "vi")

    with open(p, "rb") as f:
        data = tomllib.load(f)
    assert data["build"]["repo_mode"] == "profiled"


def test_step_build_mode_interactive_no_change_on_enter(tmp_path):
    p = make_packages_toml(tmp_path, '[build]\nrepo_mode = "pacman"\n\n[[package]]\nname = "htop"\nsource = "repo"\n')
    original = p.read_text()
    config = {"packages_file": str(p)}

    with patch("sysforge.pipeline.stages.reconfigure._interactive", return_value=True), \
         patch("sysforge.pipeline.stages.reconfigure._prompt_choice", return_value=""):
        _step_build_mode(config, None, make_options(), "vi")

    assert p.read_text() == original


def test_step_build_mode_shows_pkgbuild_patch_overrides(tmp_path):
    p = make_packages_toml(tmp_path, _BASIC_TOML)
    config = {"packages_file": str(p)}
    logged = []

    with patch("sysforge.pipeline.stages.reconfigure._interactive", return_value=False), \
         patch("sysforge.log.ui", side_effect=lambda tag, msg: logged.append(msg)):
        _step_build_mode(config, None, make_options(), "vi")

    combined = " ".join(logged)
    assert "mesa-git" in combined   # pkgbuild_patch package listed


# ---------------------------------------------------------------------------
# _step_preview — repo_mode reflected
# ---------------------------------------------------------------------------

def test_step_preview_repo_mode_profiled_shown(tmp_path):
    p = make_packages_toml(tmp_path, _REPO_MODE_PROFILED_TOML)
    config = {"packages_file": str(p), "rules": [], "defaults": {}}
    logged = []

    with patch("sysforge.log.ui", side_effect=lambda tag, msg: logged.append(msg)):
        _step_preview(config, None, make_options(), "vi")

    combined = " ".join(logged)
    assert "profiled" in combined
    assert "repo_mode" in combined


def test_step_preview_pkgbuild_patch_shown_as_profiled(tmp_path):
    """A repo package with pkgbuild_patch=true shows profiled build action."""
    toml = """\
[build]
pkgbuild_src_dir = "~/src"

[[package]]
name = "mold"
source = "repo"
pkgbuild_patch = true
"""
    p = make_packages_toml(tmp_path, toml)
    config = {"packages_file": str(p), "rules": [], "defaults": {}}
    logged = []

    with patch("sysforge.log.ui", side_effect=lambda tag, msg: logged.append(msg)):
        _step_preview(config, None, make_options(), "vi")

    combined = " ".join(logged)
    assert "profiled" in combined
    assert "pkgbuild_patch" in combined


def test_step_preview_default_repo_mode_shows_pacman(tmp_path):
    p = make_packages_toml(tmp_path, _BASIC_TOML)
    config = {"packages_file": str(p), "rules": [], "defaults": {}}
    logged = []

    with patch("sysforge.log.ui", side_effect=lambda tag, msg: logged.append(msg)):
        _step_preview(config, None, make_options(), "vi")

    combined = " ".join(logged)
    assert "pacman -S --needed" in combined  # htop has no pkgbuild_patch
