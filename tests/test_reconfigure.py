"""
test_reconfigure.py — unit tests for sysforge.pipeline.stages.reconfigure

Covers _set_repo_mode, _step_build_mode, _step_preview, _parse_step_selection,
and the new build_mode step registration. No real filesystem I/O beyond tmp_path.
"""
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from sysforge.pipeline.stages.base import RunOptions
from sysforge.pipeline.stages.reconfigure import (
    _EDITOR_NEEDING_STEPS,
    _STEP_FNS,
    _STEP_KEYS,
    _editor_usable,
    _parse_step_selection,
    _require_usable_editor,
    _run_selected_steps,
    _set_repo_mode,
    _step_build_mode,
    _step_preview,
)


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


# ---------------------------------------------------------------------------
# Editor gate: _editor_usable / _require_usable_editor / gated _run_selected_steps
# ---------------------------------------------------------------------------

def test_editor_needing_steps_set_covers_config_and_makepkg():
    """The gate must cover the two steps that actually prompt for $EDITOR."""
    assert "config" in _EDITOR_NEEDING_STEPS
    assert "makepkg" in _EDITOR_NEEDING_STEPS


def test_editor_usable_empty_string_is_false():
    assert _editor_usable("") is False


def test_editor_usable_missing_binary_is_false():
    with patch("sysforge.pipeline.stages.reconfigure.shutil.which", return_value=None):
        assert _editor_usable("nonexistent-xyz") is False


def test_editor_usable_present_binary_is_true():
    with patch(
        "sysforge.pipeline.stages.reconfigure.shutil.which",
        return_value="/usr/bin/nano",
    ):
        assert _editor_usable("nano") is True


def test_require_usable_editor_returns_prev_when_usable():
    """Early-return path: don't prompt when editor is already fine."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._editor_usable",
        return_value=True,
    ), patch(
        "sysforge.pipeline.stages.reconfigure._select_new_editor"
    ) as picker:
        result = _require_usable_editor("vim", make_options(), needed_for="config")
    assert result == "vim"
    picker.assert_not_called()


def test_require_usable_editor_picks_new_editor_and_skips_save():
    """User picks nano; declines to save as default; returns nano."""
    # _editor_usable: False for "" (initial gate check), True for "nano" (post-pick).
    def fake_usable(editor):
        return editor == "nano"

    with patch(
        "sysforge.pipeline.stages.reconfigure._editor_usable",
        side_effect=fake_usable,
    ), patch(
        "sysforge.pipeline.stages.reconfigure._select_new_editor",
        return_value="nano",
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt_choice",
        return_value="n",
    ), patch(
        "sysforge.pipeline.stages.reconfigure._save_sysforge_toml_ui"
    ) as save:
        result = _require_usable_editor("", make_options(), needed_for="config")

    assert result == "nano"
    save.assert_not_called()


def test_require_usable_editor_saves_when_user_accepts():
    """User picks nano and accepts the save-as-default prompt."""
    def fake_usable(editor):
        return editor == "nano"

    with patch(
        "sysforge.pipeline.stages.reconfigure._editor_usable",
        side_effect=fake_usable,
    ), patch(
        "sysforge.pipeline.stages.reconfigure._select_new_editor",
        return_value="nano",
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt_choice",
        return_value="y",
    ), patch(
        "sysforge.pipeline.stages.reconfigure._save_sysforge_toml_ui"
    ) as save:
        result = _require_usable_editor("", make_options(), needed_for="config")

    assert result == "nano"
    save.assert_called_once_with("editor", "nano")


def test_require_usable_editor_raises_when_picker_cancels():
    """User cancels the picker; gate must raise so the stage aborts cleanly."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._editor_usable",
        return_value=False,
    ), patch(
        "sysforge.pipeline.stages.reconfigure._select_new_editor",
        return_value=None,
    ):
        with pytest.raises(RuntimeError, match="Aborted"):
            _require_usable_editor("", make_options(), needed_for="config")


def test_run_selected_steps_gates_config_when_editor_unusable():
    """Queue [config] with no usable editor → gate fires, recovers, step runs with new editor."""
    captured: list[str] = []

    def fake_config_step(config, state, options, editor):
        captured.append(editor)
        return editor

    fake_step_fns = {**_STEP_FNS, "config": fake_config_step}

    def fake_usable(editor):
        return editor == "nano"

    with patch(
        "sysforge.pipeline.stages.reconfigure._resolve_editor",
        return_value=("", "none"),
    ), patch(
        "sysforge.pipeline.stages.reconfigure._editor_usable",
        side_effect=fake_usable,
    ), patch(
        "sysforge.pipeline.stages.reconfigure._select_new_editor",
        return_value="nano",
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt_choice",
        return_value="n",
    ), patch(
        "sysforge.pipeline.stages.reconfigure._STEP_FNS",
        fake_step_fns,
    ):
        _run_selected_steps(["config"], {}, None, make_options())

    assert captured == ["nano"]


def test_run_selected_steps_aborts_when_gate_cancelled():
    """Queue [editor, config], editor pick yields '', gate picker also cancels → RuntimeError."""
    def fake_editor_step(config, state, options, editor):
        # Simulate editor step failing to produce a usable editor.
        return ""

    fake_step_fns = {**_STEP_FNS, "editor": fake_editor_step}

    with patch(
        "sysforge.pipeline.stages.reconfigure._resolve_editor",
        return_value=("", "none"),
    ), patch(
        "sysforge.pipeline.stages.reconfigure._editor_usable",
        return_value=False,
    ), patch(
        "sysforge.pipeline.stages.reconfigure._select_new_editor",
        return_value=None,
    ), patch(
        "sysforge.pipeline.stages.reconfigure._STEP_FNS",
        fake_step_fns,
    ):
        with pytest.raises(RuntimeError, match="Aborted"):
            _run_selected_steps(["editor", "config"], {}, None, make_options())


def test_run_selected_steps_skips_gate_for_non_editor_steps():
    """Queue [build_mode] with no editor: gate must NOT fire (build_mode is not in _EDITOR_NEEDING_STEPS)."""
    seen: list[str] = []

    def fake_build_mode_step(config, state, options, editor):
        seen.append(editor)
        return editor

    fake_step_fns = {**_STEP_FNS, "build_mode": fake_build_mode_step}

    with patch(
        "sysforge.pipeline.stages.reconfigure._resolve_editor",
        return_value=("", "none"),
    ), patch(
        "sysforge.pipeline.stages.reconfigure._editor_usable",
        return_value=False,
    ), patch(
        "sysforge.pipeline.stages.reconfigure._select_new_editor"
    ) as picker, patch(
        "sysforge.pipeline.stages.reconfigure._STEP_FNS",
        fake_step_fns,
    ):
        _run_selected_steps(["build_mode"], {}, None, make_options())

    assert seen == [""]
    picker.assert_not_called()
