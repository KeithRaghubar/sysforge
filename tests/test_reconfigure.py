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
    _EDITOR_SUGGESTIONS,
    _KNOWN_EDITORS,
    _STEP_FNS,
    _STEP_KEYS,
    _choose_install_package,
    _confirm_unknown_editor,
    _edit_needs_sudo,
    _editor_usable,
    _open_in_editor,
    _packages_providing,
    _parse_step_selection,
    _require_usable_editor,
    _run_selected_steps,
    _select_new_editor,
    _set_repo_mode,
    _step_build_mode,
    _step_desktop,
    _step_preview,
    _try_install_editor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _files_db_present():
    """Default the pacman files db to "present" so editor-picker tests never
    trigger a real `sudo pacman -Fy`. Tests that exercise the auto-sync path
    override this with their own patch."""
    with patch(
        "sysforge.pipeline.stages.reconfigure.files_db_present",
        return_value=True,
    ):
        yield


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
enable_build_from_source = true
"""

_REPO_MODE_PROFILED_TOML = """\
[build]
pkgbuild_src_dir = "~/src"
repo_mode = "build_from_source"

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


def test_desktop_registered():
    assert "desktop" in _STEP_KEYS
    assert "desktop" in _STEP_FNS


def test_desktop_step_order():
    """desktop sits right after build_mode."""
    keys = list(_STEP_KEYS)
    assert keys.index("desktop") == keys.index("build_mode") + 1


def test_step_desktop_writes_selected_group(tmp_path):
    pkg_path = make_packages_toml(tmp_path, _BASIC_TOML)
    config = {"paths": {"packages": str(pkg_path)}}
    with patch(
        "sysforge.pipeline.stages.reconfigure.resolve_packages_path",
        return_value=pkg_path,
    ), patch(
        "sysforge.pipeline.stages.reconfigure.select_desktop",
        return_value="gnome",
    ):
        _step_desktop(config, None, make_options(), "vi")
    data = tomllib.loads(pkg_path.read_text())
    assert "gnome" in data.get("group", {})
    # Pre-existing entries survive.
    assert any(e["name"] == "htop" for e in data.get("package", []))


def test_step_desktop_no_selection_is_noop(tmp_path):
    pkg_path = make_packages_toml(tmp_path, _BASIC_TOML)
    before = pkg_path.read_text()
    with patch(
        "sysforge.pipeline.stages.reconfigure.resolve_packages_path",
        return_value=pkg_path,
    ), patch(
        "sysforge.pipeline.stages.reconfigure.select_desktop",
        return_value=None,
    ):
        _step_desktop({}, None, make_options(), "vi")
    assert pkg_path.read_text() == before


def test_step_desktop_dry_run_does_not_write(tmp_path):
    pkg_path = make_packages_toml(tmp_path, _BASIC_TOML)
    before = pkg_path.read_text()
    with patch(
        "sysforge.pipeline.stages.reconfigure.resolve_packages_path",
        return_value=pkg_path,
    ), patch(
        "sysforge.pipeline.stages.reconfigure.select_desktop",
        return_value="kde",
    ):
        _step_desktop({}, None, make_options(dry_run=True), "vi")
    assert pkg_path.read_text() == before


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
    _set_repo_mode(p, "build_from_source")
    assert 'repo_mode = "build_from_source"' in p.read_text()


def test_set_repo_mode_inserts_after_build_header(tmp_path):
    p = make_packages_toml(tmp_path, '[build]\npkgbuild_src_dir = "~/src"\n')
    _set_repo_mode(p, "build_from_source")
    text = p.read_text()
    assert 'repo_mode = "build_from_source"' in text
    # Should appear after [build]
    assert text.index("[build]") < text.index('repo_mode = "build_from_source"')


def test_set_repo_mode_no_build_section_appends(tmp_path):
    p = make_packages_toml(tmp_path, '[[package]]\nname = "htop"\nsource = "repo"\n')
    _set_repo_mode(p, "build_from_source")
    text = p.read_text()
    assert "[build]" in text
    assert 'repo_mode = "build_from_source"' in text


def test_set_repo_mode_roundtrip_valid_toml(tmp_path):
    p = make_packages_toml(tmp_path, _BASIC_TOML)
    _set_repo_mode(p, "build_from_source")
    with open(p, "rb") as f:
        data = tomllib.load(f)
    assert data["build"]["repo_mode"] == "build_from_source"


def test_set_repo_mode_preserves_other_content(tmp_path):
    p = make_packages_toml(tmp_path, _BASIC_TOML)
    _set_repo_mode(p, "build_from_source")
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
         patch("sysforge.pipeline.stages.reconfigure._prompt_choice", return_value="s"):
        _step_build_mode(config, None, make_options(dry_run=True), "vi")

    assert p.read_text() == original


def test_step_build_mode_missing_file_skips(tmp_path):
    config = {"packages_file": str(tmp_path / "nonexistent.toml")}
    # Should return without raising
    result = _step_build_mode(config, None, make_options(), "vi")
    assert result == "vi"


def test_step_build_mode_interactive_sets_build_from_source(tmp_path):
    p = make_packages_toml(tmp_path, '[build]\nrepo_mode = "pacman"\n\n[[package]]\nname = "htop"\nsource = "repo"\n')
    config = {"packages_file": str(p)}

    with patch("sysforge.pipeline.stages.reconfigure._interactive", return_value=True), \
         patch("sysforge.pipeline.stages.reconfigure._prompt_choice", return_value="s"):
        _step_build_mode(config, None, make_options(), "vi")

    with open(p, "rb") as f:
        data = tomllib.load(f)
    assert data["build"]["repo_mode"] == "build_from_source"


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
    assert "mesa-git" in combined   # enable_build_from_source package listed


# ---------------------------------------------------------------------------
# _step_preview — repo_mode reflected
# ---------------------------------------------------------------------------

def test_step_preview_repo_mode_build_from_source_shown(tmp_path):
    p = make_packages_toml(tmp_path, _REPO_MODE_PROFILED_TOML)
    config = {"packages_file": str(p), "rules": [], "defaults": {}}
    logged = []

    with patch("sysforge.log.ui", side_effect=lambda tag, msg: logged.append(msg)):
        _step_preview(config, None, make_options(), "vi")

    combined = " ".join(logged)
    assert "build_from_source" in combined
    assert "repo_mode" in combined


def test_step_preview_enable_build_from_source_shown(tmp_path):
    """A repo package with enable_build_from_source=true shows source build action."""
    toml = """\
[build]
pkgbuild_src_dir = "~/src"

[[package]]
name = "mold"
source = "repo"
enable_build_from_source = true
"""
    p = make_packages_toml(tmp_path, toml)
    config = {"packages_file": str(p), "rules": [], "defaults": {}}
    logged = []

    with patch("sysforge.log.ui", side_effect=lambda tag, msg: logged.append(msg)):
        _step_preview(config, None, make_options(), "vi")

    combined = " ".join(logged)
    assert "build_from_source" in combined
    assert "enable_build_from_source" in combined


def test_step_preview_default_repo_mode_shows_pacman(tmp_path):
    p = make_packages_toml(tmp_path, _BASIC_TOML)
    config = {"packages_file": str(p), "rules": [], "defaults": {}}
    logged = []

    with patch("sysforge.log.ui", side_effect=lambda tag, msg: logged.append(msg)):
        _step_preview(config, None, make_options(), "vi")

    combined = " ".join(logged)
    assert "pacman -S --needed" in combined  # htop has no enable_build_from_source


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


def test_edit_needs_sudo_when_file_not_writable(tmp_path):
    """Root-owned config files report W_OK=False → sudo required."""
    target = tmp_path / "profiles.toml"
    target.write_text("x = 1\n")

    def fake_access(path, mode):
        return False  # neither file nor parent writable

    with patch("sysforge.pipeline.stages.reconfigure.os.access", side_effect=fake_access):
        assert _edit_needs_sudo(target) is True


def test_edit_needs_sudo_when_dir_not_writable(tmp_path):
    """File bit loose but parent dir read-only → still sudo (atomic-rename save)."""
    target = tmp_path / "packages.toml"
    target.write_text("x = 1\n")

    def fake_access(path, mode):
        return Path(path) == target  # file writable, parent not

    with patch("sysforge.pipeline.stages.reconfigure.os.access", side_effect=fake_access):
        assert _edit_needs_sudo(target) is True


def test_edit_needs_no_sudo_when_writable(tmp_path):
    """A user-owned file in a writable dir needs no sudo."""
    target = tmp_path / "user.toml"
    target.write_text("x = 1\n")
    assert _edit_needs_sudo(target) is False


def test_open_in_editor_uses_sudo_for_root_owned(tmp_path):
    """The config-review launch prepends sudo when the file isn't writable."""
    target = tmp_path / "profiles.toml"
    target.write_text("x = 1\n")
    with patch(
        "sysforge.pipeline.stages.reconfigure._editor_usable", return_value=True
    ), patch(
        "sysforge.pipeline.stages.reconfigure._edit_needs_sudo", return_value=True
    ), patch(
        "sysforge.pipeline.stages.reconfigure._run_editor_argv", return_value=0
    ) as run:
        assert _open_in_editor(target, "vim") is True
    run.assert_called_once_with(["sudo", "vim", str(target)])


def test_open_in_editor_no_sudo_when_writable(tmp_path):
    """No sudo prefix for a user-writable file."""
    target = tmp_path / "user.toml"
    target.write_text("x = 1\n")
    with patch(
        "sysforge.pipeline.stages.reconfigure._editor_usable", return_value=True
    ), patch(
        "sysforge.pipeline.stages.reconfigure._edit_needs_sudo", return_value=False
    ), patch(
        "sysforge.pipeline.stages.reconfigure._run_editor_argv", return_value=0
    ) as run:
        assert _open_in_editor(target, "vim") is True
    run.assert_called_once_with(["vim", str(target)])


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


# ---------------------------------------------------------------------------
# Editor install: auto-detect pacman package from binary name
# ---------------------------------------------------------------------------


def _fake_completed(stdout: str = "", returncode: int = 0):
    """Minimal subprocess.CompletedProcess stand-in for run() mocks."""

    class _R:
        pass

    r = _R()
    r.stdout = stdout
    r.stderr = ""
    r.returncode = returncode
    return r


def test_packages_providing_strips_repo_prefix_and_dedups():
    """``pacman -Fq`` returns ``repo/pkg`` lines; we want bare package names."""
    with patch(
        "sysforge.pipeline.stages.reconfigure.shutil.which",
        return_value="/usr/bin/pacman",
    ), patch(
        "sysforge.pipeline.stages.reconfigure.subprocess.run",
        return_value=_fake_completed(stdout="core/nano\nextra/orbiton-nano\ncore/nano\n"),
    ):
        result = _packages_providing("nano")
    assert result == ["nano", "orbiton-nano"]


def test_packages_providing_empty_when_no_match():
    """Exit code 1 with empty stdout (no match / stale DB) → []."""
    with patch(
        "sysforge.pipeline.stages.reconfigure.shutil.which",
        return_value="/usr/bin/pacman",
    ), patch(
        "sysforge.pipeline.stages.reconfigure.subprocess.run",
        return_value=_fake_completed(stdout="", returncode=1),
    ):
        assert _packages_providing("zzz-nonexistent") == []


def test_packages_providing_empty_when_pacman_missing():
    """Non-Arch environments without ``pacman`` on PATH → []."""
    with patch(
        "sysforge.pipeline.stages.reconfigure.shutil.which",
        return_value=None,
    ):
        assert _packages_providing("nvim") == []


def test_choose_install_package_single_match_confirm_yes():
    """One candidate, user confirms → return that package."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._packages_providing",
        return_value=["neovim"],
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt_choice",
        return_value="y",
    ):
        assert _choose_install_package("nvim") == "neovim"


def test_choose_install_package_single_match_confirm_no():
    """One candidate, user declines → None (cancel)."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._packages_providing",
        return_value=["neovim"],
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt_choice",
        return_value="n",
    ):
        assert _choose_install_package("nvim") is None


def test_choose_install_package_multi_match_picks_by_index():
    """Multiple candidates, user enters '2' → second package."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._packages_providing",
        return_value=["nano", "orbiton-nano"],
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt_key",
        return_value="2",
    ):
        assert _choose_install_package("nano") == "orbiton-nano"


def test_choose_install_package_multi_match_blank_cancels():
    """Multiple candidates, user presses Enter on blank → None."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._packages_providing",
        return_value=["nano", "orbiton-nano"],
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt_key",
        return_value="",
    ):
        assert _choose_install_package("nano") is None


def test_choose_install_package_no_match_falls_back_to_typed_name():
    """No pacman -F match → user types a package name → validated via pacman -Si."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._packages_providing",
        return_value=[],
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt",
        return_value="some-editor-pkg",
    ), patch(
        "sysforge.pipeline.stages.reconfigure.subprocess.run",
        return_value=_fake_completed(returncode=0),
    ) as run_mock:
        assert _choose_install_package("some-editor") == "some-editor-pkg"
    run_mock.assert_called_once()
    args = run_mock.call_args.args[0]
    assert args[:2] == ["pacman", "-Si"]
    assert args[2] == "some-editor-pkg"


def test_choose_install_package_no_match_rejects_invalid_typed_name():
    """No pacman -F match, typed name not in repos → None."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._packages_providing",
        return_value=[],
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt",
        return_value="not-a-real-pkg",
    ), patch(
        "sysforge.pipeline.stages.reconfigure.subprocess.run",
        return_value=_fake_completed(returncode=1),
    ):
        assert _choose_install_package("some-editor") is None


def test_choose_install_package_no_match_blank_cancels():
    """No pacman -F match, user presses Enter → None (cancel)."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._packages_providing",
        return_value=[],
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt",
        return_value="",
    ):
        assert _choose_install_package("some-editor") is None


def test_choose_install_package_syncs_files_db_when_absent():
    """Files db never synced → auto-run `sudo pacman -Fy` before the lookup."""
    with patch(
        "sysforge.pipeline.stages.reconfigure.files_db_present",
        return_value=False,
    ), patch(
        "sysforge.pipeline.stages.reconfigure.sync_files_db",
        return_value=True,
    ) as sync_mock, patch(
        "sysforge.pipeline.stages.reconfigure._packages_providing",
        return_value=["neovim"],
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt_choice",
        return_value="y",
    ):
        assert _choose_install_package("nvim") == "neovim"
    sync_mock.assert_called_once()


def test_choose_install_package_skips_sync_when_files_db_present():
    """Files db already present → no sync attempt."""
    with patch(
        "sysforge.pipeline.stages.reconfigure.files_db_present",
        return_value=True,
    ), patch(
        "sysforge.pipeline.stages.reconfigure.sync_files_db",
    ) as sync_mock, patch(
        "sysforge.pipeline.stages.reconfigure._packages_providing",
        return_value=["neovim"],
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt_choice",
        return_value="y",
    ):
        assert _choose_install_package("nvim") == "neovim"
    sync_mock.assert_not_called()


def test_choose_install_package_dry_run_does_not_sync():
    """Dry-run reports the sync but never runs it."""
    with patch(
        "sysforge.pipeline.stages.reconfigure.files_db_present",
        return_value=False,
    ), patch(
        "sysforge.pipeline.stages.reconfigure.sync_files_db",
    ) as sync_mock, patch(
        "sysforge.pipeline.stages.reconfigure._packages_providing",
        return_value=["neovim"],
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt_choice",
        return_value="y",
    ):
        opts = make_options(dry_run=True)
        assert _choose_install_package("nvim", opts) == "neovim"
    sync_mock.assert_not_called()


def test_try_install_editor_cancelled_picker_returns_false():
    """User cancels package picker → no subprocess invocation, return False."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._choose_install_package",
        return_value=None,
    ), patch(
        "sysforge.pipeline.stages.reconfigure.subprocess.run",
    ) as run_mock:
        assert _try_install_editor("nvim", make_options()) is False
    run_mock.assert_not_called()


def test_try_install_editor_dry_run_does_not_invoke_pacman():
    """Even with a chosen package, dry_run must short-circuit before sudo pacman."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._choose_install_package",
        return_value="neovim",
    ), patch(
        "sysforge.pipeline.stages.reconfigure.subprocess.run",
    ) as run_mock:
        assert _try_install_editor("nvim", make_options(dry_run=True)) is False
    run_mock.assert_not_called()


def test_try_install_editor_success_path():
    """Picker resolves → sudo pacman succeeds → which() finds binary → True."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._choose_install_package",
        return_value="neovim",
    ), patch(
        "sysforge.pipeline.stages.reconfigure.subprocess.run",
        return_value=_fake_completed(returncode=0),
    ), patch(
        "sysforge.pipeline.stages.reconfigure.shutil.which",
        return_value="/usr/bin/nvim",
    ):
        assert _try_install_editor("nvim", make_options()) is True


def test_try_install_editor_install_succeeds_but_binary_missing():
    """sudo pacman returns 0 but the binary still isn't on PATH → False."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._choose_install_package",
        return_value="neovim",
    ), patch(
        "sysforge.pipeline.stages.reconfigure.subprocess.run",
        return_value=_fake_completed(returncode=0),
    ), patch(
        "sysforge.pipeline.stages.reconfigure.shutil.which",
        return_value=None,
    ):
        assert _try_install_editor("nvim", make_options()) is False


def test_try_install_editor_writes_sentinel_during_pacman_call(tmp_path):
    """The editor install is wrapped in a sentinel scope — interrupting the
    sudo pacman call leaves the sentinel for the next sysforge run to surface."""
    from sysforge.primitives.stage_sentinel import StageSentinel

    state_dir = tmp_path / "state"
    seen = {"present": False, "stage": None, "package": None}

    def check_during_pacman(*_a, **_kw):
        record = StageSentinel(state_dir).get_active()
        if record is not None:
            seen["present"] = True
            seen["stage"] = record.get("stage")
            seen["package"] = record.get("package")
        return _fake_completed(returncode=0)

    with patch(
        "sysforge.pipeline.stages.reconfigure._choose_install_package",
        return_value="neovim",
    ), patch(
        "sysforge.pipeline.stages.reconfigure.subprocess.run",
        side_effect=check_during_pacman,
    ), patch(
        "sysforge.pipeline.stages.reconfigure.shutil.which",
        return_value="/usr/bin/nvim",
    ):
        assert _try_install_editor("nvim", make_options(state_dir=state_dir)) is True

    assert seen["present"] is True
    assert seen["stage"] == "reconfigure-editor"
    assert seen["package"] == "neovim"
    # Cleared on clean exit
    assert StageSentinel(state_dir).get_active() is None


# ---------------------------------------------------------------------------
# Editor allowlist: known-editor check + override confirmation
# ---------------------------------------------------------------------------


def test_known_editors_superset_of_suggestions():
    """Drift guard: every shown-as-suggestion editor must also pass validation
    silently — otherwise users would see ``nano`` suggested then warned."""
    for name in _EDITOR_SUGGESTIONS:
        assert name in _KNOWN_EDITORS, f"{name!r} suggested but not in allowlist"


def test_confirm_unknown_editor_yes_returns_true():
    with patch(
        "sysforge.pipeline.stages.reconfigure._prompt_choice",
        return_value="y",
    ):
        assert _confirm_unknown_editor("htop") is True


def test_confirm_unknown_editor_no_returns_false():
    with patch(
        "sysforge.pipeline.stages.reconfigure._prompt_choice",
        return_value="n",
    ):
        assert _confirm_unknown_editor("htop") is False


def test_select_known_editor_on_path_no_confirm_prompt():
    """Entering 'nano' (already on PATH and known) returns without firing the
    unknown-editor confirm prompt."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._prompt",
        return_value="nano",
    ), patch(
        "sysforge.pipeline.stages.reconfigure.shutil.which",
        return_value="/usr/bin/nano",
    ), patch(
        "sysforge.pipeline.stages.reconfigure._confirm_unknown_editor",
    ) as confirm_mock:
        result = _select_new_editor("", have_prev=False, options=make_options())
    assert result == "nano"
    confirm_mock.assert_not_called()


def test_select_unknown_editor_on_path_confirm_yes_returns_it():
    with patch(
        "sysforge.pipeline.stages.reconfigure._prompt",
        return_value="htop",
    ), patch(
        "sysforge.pipeline.stages.reconfigure.shutil.which",
        return_value="/usr/bin/htop",
    ), patch(
        "sysforge.pipeline.stages.reconfigure._confirm_unknown_editor",
        return_value=True,
    ) as confirm_mock:
        result = _select_new_editor("", have_prev=False, options=make_options())
    assert result == "htop"
    confirm_mock.assert_called_once_with("htop")


def test_select_unknown_editor_on_path_confirm_no_re_prompts():
    """User enters 'htop' (on PATH but off-list), declines override, then
    enters 'nano' on the next iteration."""
    prompts = iter(["htop", "nano"])
    with patch(
        "sysforge.pipeline.stages.reconfigure._prompt",
        side_effect=lambda *a, **k: next(prompts),
    ), patch(
        "sysforge.pipeline.stages.reconfigure.shutil.which",
        return_value="/usr/bin/whatever",
    ), patch(
        "sysforge.pipeline.stages.reconfigure._confirm_unknown_editor",
        return_value=False,
    ):
        result = _select_new_editor("", have_prev=False, options=make_options())
    assert result == "nano"


def test_select_unknown_editor_after_install_confirm_yes_returns_it():
    """Install path resolves 'tmux' as the user's editor; override confirmed."""
    with patch(
        "sysforge.pipeline.stages.reconfigure._prompt",
        return_value="tmux",
    ), patch(
        "sysforge.pipeline.stages.reconfigure.shutil.which",
        return_value=None,
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt_choice",
        return_value="i",
    ), patch(
        "sysforge.pipeline.stages.reconfigure._try_install_editor",
        return_value=True,
    ), patch(
        "sysforge.pipeline.stages.reconfigure._confirm_unknown_editor",
        return_value=True,
    ) as confirm_mock:
        result = _select_new_editor("", have_prev=False, options=make_options())
    assert result == "tmux"
    confirm_mock.assert_called_once_with("tmux")


def test_select_unknown_editor_after_install_confirm_no_re_prompts():
    """Install path resolves 'tmux'; user declines override; next iteration
    types 'vim' (already on PATH after first install)."""
    prompts = iter(["tmux", "vim"])
    # which: None for tmux (triggers install), then "/usr/bin/vim" for the
    # follow-up iteration so vim takes the PATH-existing branch.
    which_results = iter([None, "/usr/bin/vim"])
    with patch(
        "sysforge.pipeline.stages.reconfigure._prompt",
        side_effect=lambda *a, **k: next(prompts),
    ), patch(
        "sysforge.pipeline.stages.reconfigure.shutil.which",
        side_effect=lambda _: next(which_results),
    ), patch(
        "sysforge.pipeline.stages.reconfigure._prompt_choice",
        return_value="i",
    ), patch(
        "sysforge.pipeline.stages.reconfigure._try_install_editor",
        return_value=True,
    ), patch(
        "sysforge.pipeline.stages.reconfigure._confirm_unknown_editor",
        return_value=False,
    ):
        result = _select_new_editor("", have_prev=False, options=make_options())
    assert result == "vim"
