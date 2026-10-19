"""
test_setup.py — unit tests for sysforge.setup_cmd

Tests the pure helper functions (_check_ignore_group, _patch_conf_text) and
the cmd_setup entry point (mocked filesystem and stdin).
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge import setup_cmd
from sysforge.primitives import pacman_hooks
from sysforge.setup_cmd import _check_ignore_group, _patch_conf_text, cmd_setup


@pytest.fixture(autouse=True)
def _hooks_in_sync(monkeypatch):
    """Default: pretend sysforge's pacman hooks are already installed so the
    IgnoreGroup-focused tests never touch the real system via sudo. Hook-block
    tests override diff_status explicitly."""
    monkeypatch.setattr(pacman_hooks, "diff_status", lambda: [])


@pytest.fixture(autouse=True)
def _no_prefs(request, monkeypatch):
    if "prefs_env" not in request.fixturenames:
        monkeypatch.setattr(setup_cmd, "_setup_preferences", lambda: None)


@pytest.fixture(autouse=True)
def _no_preset(request, monkeypatch):
    if "preset_env" not in request.fixturenames:
        monkeypatch.setattr(setup_cmd, "_setup_preset", lambda name: None)


# ---------------------------------------------------------------------------
# _check_ignore_group
# ---------------------------------------------------------------------------

_CONF_NO_IGNORE = """\
[options]
HoldPkg = pacman glibc
Architecture = auto
"""

_CONF_WITH_SF_BUILD = """\
[options]
HoldPkg = pacman glibc
IgnoreGroup = sf-build
"""

_CONF_WITH_OTHER_GROUPS = """\
[options]
HoldPkg = pacman glibc
IgnoreGroup = base devel
"""

_CONF_WITH_SF_BUILD_AMONG_OTHERS = """\
[options]
IgnoreGroup = base sf-build devel
"""

_CONF_COMMENTED_IGNORE = """\
[options]
# IgnoreGroup = sf-build
HoldPkg = pacman glibc
"""

_CONF_IGNORE_UPPERCASE_KEY = """\
[options]
IGNOREGROUP = sf-build
"""


def test_check_absent():
    assert _check_ignore_group(_CONF_NO_IGNORE) is False


def test_check_present():
    assert _check_ignore_group(_CONF_WITH_SF_BUILD) is True


def test_check_other_groups_no_sf_build():
    assert _check_ignore_group(_CONF_WITH_OTHER_GROUPS) is False


def test_check_sf_build_among_others():
    assert _check_ignore_group(_CONF_WITH_SF_BUILD_AMONG_OTHERS) is True


def test_check_commented_line_not_counted():
    # Commented-out IgnoreGroup should not count as configured.
    assert _check_ignore_group(_CONF_COMMENTED_IGNORE) is False


def test_check_case_insensitive_key():
    # pacman.conf keys are case-insensitive; sysforge should handle that.
    assert _check_ignore_group(_CONF_IGNORE_UPPERCASE_KEY) is True


# ---------------------------------------------------------------------------
# _patch_conf_text
# ---------------------------------------------------------------------------

def test_patch_appends_to_existing_ignore_group():
    result = _patch_conf_text(_CONF_WITH_OTHER_GROUPS)
    assert "IgnoreGroup = base devel sf-build" in result
    # Original line not duplicated.
    assert result.count("IgnoreGroup") == 1


def test_patch_inserts_new_ignore_group_in_options():
    result = _patch_conf_text(_CONF_NO_IGNORE)
    assert "IgnoreGroup = sf-build" in result
    # Verify it's inside [options] (comes before any subsequent section).
    options_idx = result.index("[options]")
    ignore_idx = result.index("IgnoreGroup = sf-build")
    assert ignore_idx > options_idx


def test_patch_no_options_section_appends_at_end():
    conf = "# minimal conf\nColor\n"
    result = _patch_conf_text(conf)
    assert "IgnoreGroup = sf-build" in result
    assert result.endswith("IgnoreGroup = sf-build\n")


def test_patch_preserves_existing_content():
    result = _patch_conf_text(_CONF_NO_IGNORE)
    assert "HoldPkg = pacman glibc" in result
    assert "Architecture = auto" in result


def test_patch_idempotent_on_existing_line():
    # Patching a conf that already has sf-build should still work structurally
    # (check_ignore_group gates this in practice, but patch_conf_text itself
    # will just append sf-build again — callers are responsible for the guard).
    result = _patch_conf_text(_CONF_WITH_SF_BUILD)
    # sf-build appears at least once.
    assert "sf-build" in result


# ---------------------------------------------------------------------------
# cmd_setup — already configured
# ---------------------------------------------------------------------------

def test_cmd_setup_already_configured(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_WITH_SF_BUILD)

    args = MagicMock()
    args.pacman_conf = str(conf)

    cmd_setup(args)

    out = capsys.readouterr().out
    assert "Already configured" in out


# ---------------------------------------------------------------------------
# cmd_setup — user says yes
# ---------------------------------------------------------------------------

def test_cmd_setup_user_yes(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", return_value="y"):
        cmd_setup(args)

    out = capsys.readouterr().out
    assert "Added" in out
    assert "IgnoreGroup = sf-build" in conf.read_text()


def test_cmd_setup_user_yes_uppercase(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", return_value="Y"):
        cmd_setup(args)

    assert "IgnoreGroup = sf-build" in conf.read_text()


def test_cmd_setup_user_yes_full_word(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", return_value="yes"):
        cmd_setup(args)

    assert "IgnoreGroup = sf-build" in conf.read_text()


# ---------------------------------------------------------------------------
# cmd_setup — user says no
# ---------------------------------------------------------------------------

def test_cmd_setup_user_no_prints_warning(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", return_value="n"):
        cmd_setup(args)

    err = capsys.readouterr().err
    assert "pacman -Syu" in err
    assert "sysforge update" in err
    # conf must be untouched.
    assert "IgnoreGroup" not in conf.read_text()


def test_cmd_setup_user_empty_input_treated_as_no(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", return_value=""):
        cmd_setup(args)

    assert "IgnoreGroup" not in conf.read_text()


def test_cmd_setup_eof_treated_as_no(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", side_effect=EOFError):
        cmd_setup(args)

    assert "IgnoreGroup" not in conf.read_text()


# ---------------------------------------------------------------------------
# cmd_setup — permission denied
# ---------------------------------------------------------------------------

def test_cmd_setup_permission_denied(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", return_value="y"), \
         patch("sysforge.setup_cmd._write_conf", return_value=False):
        cmd_setup(args)

    err = capsys.readouterr().err
    assert "permission denied" in err.lower()
    assert "sudo sysforge setup" in err


# ---------------------------------------------------------------------------
# cmd_setup — conf file not found
# ---------------------------------------------------------------------------

def test_cmd_setup_conf_not_found(tmp_path, capsys):
    args = MagicMock()
    args.pacman_conf = str(tmp_path / "nonexistent.conf")

    cmd_setup(args)

    err = capsys.readouterr().err
    assert "not found" in err


# ---------------------------------------------------------------------------
# cmd_setup — pacman hook provisioning block
# ---------------------------------------------------------------------------

def _conf_already_configured(tmp_path):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_WITH_SF_BUILD)
    args = MagicMock()
    args.pacman_conf = str(conf)
    return args


def _missing_status():
    art = pacman_hooks.HookArtifact(
        pacman_hooks.HOOK_DEST_DIR / "sysforge-kernel.hook", b"x", 0o644)
    return [(art, pacman_hooks.STATE_MISSING)]


def test_setup_hooks_up_to_date(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(pacman_hooks, "diff_status", lambda: [])
    cmd_setup(_conf_already_configured(tmp_path))
    assert "hooks up to date" in capsys.readouterr().out


def test_setup_hooks_install_on_yes(tmp_path, capsys, monkeypatch):
    status = _missing_status()
    monkeypatch.setattr(pacman_hooks, "diff_status", lambda: status)
    provisioned = []

    def fake_provision(s=None):
        provisioned.append(s)
        return [(status[0][0].dest, pacman_hooks.STATE_MISSING)]

    monkeypatch.setattr(pacman_hooks, "provision", fake_provision)
    with patch("builtins.input", return_value="y"):
        cmd_setup(_conf_already_configured(tmp_path))

    assert provisioned  # provision() was called
    assert "installed" in capsys.readouterr().out


def test_setup_hooks_skip_on_no(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(pacman_hooks, "diff_status", _missing_status)
    monkeypatch.setattr(pacman_hooks, "provision",
                        lambda s=None: pytest.fail("must not provision on 'no'"))
    with patch("builtins.input", return_value="n"):
        cmd_setup(_conf_already_configured(tmp_path))
    assert "not modified" in capsys.readouterr().err


def test_setup_hooks_priv_failure_prints_manual(tmp_path, capsys, monkeypatch):
    from sysforge.primitives.fs_provision import FsProvisionError

    monkeypatch.setattr(pacman_hooks, "diff_status", _missing_status)

    def boom(s=None):
        raise FsProvisionError("sudo not available")

    monkeypatch.setattr(pacman_hooks, "provision", boom)
    with patch("builtins.input", return_value="y"):
        cmd_setup(_conf_already_configured(tmp_path))
    assert "sudo sysforge setup" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# preferences (3.1.0-F4)
# ---------------------------------------------------------------------------

@pytest.fixture
def prefs_env(tmp_path, monkeypatch):
    pk = tmp_path / "packages.toml"
    pk.write_text("[build]\n#review = true\n#confirm_plan = true\n")
    sf = tmp_path / "sysforge.toml"
    sf.write_text("[aur]\n#enabled = true\n[security]\nsandbox_chroot_dir = "
                  f'"{tmp_path / "chroot"}"\n')
    import tomllib
    from sysforge.primitives import config, paths
    monkeypatch.setattr(config, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(paths, "resolve_packages_path", lambda cfg: pk)
    monkeypatch.setattr(setup_cmd, "resolve_packages_path", lambda cfg: pk, raising=False)
    monkeypatch.setattr(setup_cmd, "SYSFORGE_TOML_PATH", sf, raising=False)
    monkeypatch.setattr(config, "load_sysforge_toml",
                        lambda: tomllib.loads(sf.read_text()))
    # Patched at the seam: pytest swaps sys.stdout between setup and call,
    # so an isatty patch made here would not reach the test body.
    monkeypatch.setattr(setup_cmd, "_has_terminal", lambda: True)
    writes = []
    monkeypatch.setattr(config, "set_toml_key",
                        lambda path, s, k, v: writes.append((path.name, s, k, v)))
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda name: None)
    return writes, tmp_path


def _answers(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *a: next(it))


def test_preferences_enter_keeps_everything(prefs_env, monkeypatch):
    writes, _ = prefs_env
    _answers(monkeypatch, "", "", "")
    setup_cmd._setup_preferences()
    assert writes == []


def test_preferences_writes_only_changes(prefs_env, monkeypatch, capsys):
    writes, _ = prefs_env
    _answers(monkeypatch, "n", "a", "n")
    setup_cmd._setup_preferences()
    assert writes == [
        ("packages.toml", "build", "confirm_plan", False),
        ("packages.toml", "build", "review", "accept"),
        ("sysforge.toml", "aur", "enabled", False),
    ]
    out = capsys.readouterr().out
    assert "nobody having looked at it" in out             # accept risk
    assert "does not inspect what gets installed" in out  # sandbox off note
    assert "pacman -S devtools" in out                    # sandbox not offered


def test_preferences_offers_sandbox_when_ready(prefs_env, monkeypatch):
    writes, tmp_path = prefs_env
    (tmp_path / "chroot" / "root").mkdir(parents=True)
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda name: "/usr/bin/" + name)
    _answers(monkeypatch, "", "", "", "y")
    setup_cmd._setup_preferences()
    assert writes == [("sysforge.toml", "security", "sandbox_builds", True)]


def test_preferences_skipped_without_terminal(prefs_env, monkeypatch, capsys):
    writes, _ = prefs_env
    monkeypatch.setattr(setup_cmd, "_has_terminal", lambda: False)
    setup_cmd._setup_preferences()
    assert writes == []
    assert "preferences: skipped (no terminal)" in capsys.readouterr().out


def test_preferences_write_failure_prints_manual_edit(prefs_env, monkeypatch, capsys):
    from sysforge.primitives import config
    def _boom(*a):
        raise PermissionError("denied")
    monkeypatch.setattr(config, "set_toml_key", _boom)
    _answers(monkeypatch, "n", "", "")
    setup_cmd._setup_preferences()
    err = capsys.readouterr().err
    assert "confirm_plan = false" in err and "packages.toml" in err


def test_has_terminal_needs_stdin_and_stdout(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert setup_cmd._has_terminal() is False


# ---------------------------------------------------------------------------
# Presets (3.5.0-F9)
# ---------------------------------------------------------------------------

@pytest.fixture
def preset_env(tmp_path, monkeypatch):
    files = {
        "profiles": "[defaults]\nprofile = \"standard\"\ntoolchain = \"gcc\"\n"
                    "[profiles.optimized]\nextends = \"standard\"\n"
                    "[profiles.hardened]\nextends = \"standard\"\n",
        "packages": "[build]\n#review = true\n#confirm_plan = true\n",
        "sysforge": f"[security]\nsandbox_builds = false\nauto_fetch_pgp_keys = false\n"
                    f"sandbox_chroot_dir = \"{tmp_path / 'chroot'}\"\n",
        "toolchain": "#reuse_unchanged = false\n\n[bolt]\n",
        "kernel": "#hardening = false\n\n[fdo]\n",
    }
    paths = {}
    for target, text in files.items():
        p = tmp_path / f"{target}.toml"
        p.write_text(text)
        paths[target] = p
    import tomllib
    from sysforge.primitives import config
    monkeypatch.setattr(setup_cmd, "_preset_paths", lambda: paths)
    monkeypatch.setattr(config, "load_config",
                        lambda *a, **k: tomllib.loads(paths["profiles"].read_text()))
    monkeypatch.setattr(config, "load_sysforge_toml",
                        lambda: tomllib.loads(paths["sysforge"].read_text()))
    monkeypatch.setattr(setup_cmd, "_has_terminal", lambda: True)
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda name: None)
    writes = []
    monkeypatch.setattr(config, "set_toml_key",
                        lambda path, s, k, v: writes.append((path.name, s, k, v)))
    monkeypatch.setattr(
        config, "set_default_toolchain",
        lambda c, path=None: writes.append((path.name, "defaults", "toolchain", c)))
    return writes, tmp_path, paths


def _written_keys(writes):
    return {k for _name, _s, k, _v in writes}


def test_preset_defaults_writes_nothing(preset_env, monkeypatch):
    writes, *_ = preset_env
    _answers(monkeypatch, "d")
    setup_cmd._setup_preset(None)
    assert writes == []


def test_preset_performance_via_menu(preset_env, monkeypatch, capsys):
    writes, *_ = preset_env
    _answers(monkeypatch, "p", "y")
    setup_cmd._setup_preset(None)
    assert writes == [("profiles.toml", "defaults", "profile", "optimized"),
                      ("profiles.toml", "defaults", "toolchain", "llvm")]
    assert "sysforge run toolchain" in capsys.readouterr().out


def test_preset_declined_writes_nothing(preset_env, monkeypatch, capsys):
    writes, *_ = preset_env
    _answers(monkeypatch, "p", "")
    setup_cmd._setup_preset(None)
    assert writes == []
    assert "preset not applied" in capsys.readouterr().out


def test_preset_flag_needs_no_terminal_or_input(preset_env, monkeypatch, capsys):
    writes, *_ = preset_env
    monkeypatch.setattr(setup_cmd, "_has_terminal", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted"))
    setup_cmd._setup_preset("low-friction")
    assert ("packages.toml", "build", "review", False) in writes
    assert ("toolchain.toml", None, "reuse_unchanged", True) in writes
    assert "sandbox_builds" not in _written_keys(writes)  # already false
    out = capsys.readouterr().out
    assert "not logged or listed" in out
    assert "does not inspect what gets installed" in out  # sandbox off note


def test_preset_skipped_without_terminal_or_flag(preset_env, monkeypatch, capsys):
    writes, *_ = preset_env
    monkeypatch.setattr(setup_cmd, "_has_terminal", lambda: False)
    setup_cmd._setup_preset(None)
    assert writes == []
    assert "preset: skipped (no terminal)" in capsys.readouterr().out


def test_secure_without_chroot_skips_sandbox(preset_env, capsys):
    writes, *_ = preset_env
    setup_cmd._setup_preset("secure")
    assert ("kernel.toml", None, "hardening", True) in writes
    assert ("profiles.toml", "defaults", "profile", "hardened") in writes
    assert "sandbox_builds" not in _written_keys(writes)
    # commented-out keys whose shipped default already matches are not written
    assert not _written_keys(writes) & {"confirm_plan", "review"}
    assert "pacman -S devtools" in capsys.readouterr().out


def test_secure_with_chroot_enables_sandbox(preset_env, monkeypatch):
    writes, tmp_path, _ = preset_env
    (tmp_path / "chroot" / "root").mkdir(parents=True)
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda name: "/usr/bin/" + name)
    setup_cmd._setup_preset("secure")
    assert ("sysforge.toml", "security", "sandbox_builds", True) in writes


def test_secure_without_hardened_profile_prints_block(preset_env, capsys):
    writes, _, paths = preset_env
    paths["profiles"].write_text("[defaults]\nprofile = \"standard\"\n")
    setup_cmd._setup_preset("secure")
    assert "profile" not in _written_keys(writes)
    assert ("kernel.toml", None, "hardening", True) in writes
    assert "[profiles.hardened]" in capsys.readouterr().out


def test_preset_already_applied_writes_nothing(preset_env, capsys):
    writes, _, paths = preset_env
    paths["profiles"].write_text("[defaults]\nprofile = \"optimized\"\n"
                                 "toolchain = \"llvm\"\n[profiles.optimized]\n")
    setup_cmd._setup_preset("performance")
    assert writes == []
    assert "nothing to change" in capsys.readouterr().out


def test_preset_write_failure_continues(preset_env, monkeypatch, capsys):
    writes, *_ = preset_env
    from sysforge.primitives import config
    def _maybe_boom(path, s, k, v):
        if k == "confirm_plan":
            raise PermissionError("denied")
        writes.append((path.name, s, k, v))
    monkeypatch.setattr(config, "set_toml_key", _maybe_boom)
    setup_cmd._setup_preset("low-friction")
    err = capsys.readouterr().err
    assert "[build]\n  confirm_plan = false" in err
    assert ("packages.toml", "build", "review", False) in writes


def test_preset_top_level_write_failure_has_no_section_line(preset_env, monkeypatch, capsys):
    from sysforge.primitives import config
    def _boom(path, s, k, v):
        raise PermissionError("denied")
    monkeypatch.setattr(config, "set_toml_key", _boom)
    setup_cmd._setup_preset("secure")
    err = capsys.readouterr().err
    assert "set it by hand:\n  (top level, above the first [section] header)\n" \
           "  hardening = true" in err


def test_setup_parser_accepts_preset():
    import argparse
    sub = argparse.ArgumentParser().add_subparsers()
    p = setup_cmd.SetupVerb.add_parser(sub)
    assert p.parse_args(["--preset", "secure"]).preset == "secure"
    assert p.parse_args([]).preset is None
    with pytest.raises(SystemExit):
        p.parse_args(["--preset", "bogus"])


def test_cmd_setup_runs_preset_between_hooks_and_preferences(tmp_path, monkeypatch):
    args = _conf_already_configured(tmp_path)
    args.preset = "secure"
    order = []
    monkeypatch.setattr(setup_cmd, "_setup_pacman_hooks", lambda: order.append("hooks"))
    monkeypatch.setattr(setup_cmd, "_setup_preset", lambda name: order.append(("preset", name)))
    monkeypatch.setattr(setup_cmd, "_setup_preferences", lambda: order.append("prefs"))
    cmd_setup(args)
    assert order == ["hooks", ("preset", "secure"), "prefs"]


def test_hardened_paste_block_matches_shipped_profile():
    from pathlib import Path
    shipped = (Path(__file__).resolve().parent.parent / "etc" / "sysforge"
               / "profiles.toml").read_text().splitlines()
    block = setup_cmd._HARDENED_BLOCK.split("\n  [profiles.hardened]\n", 1)[1]
    for line in filter(None, (s.strip() for s in block.splitlines())):
        assert line in shipped, line


def test_hardened_paste_block_keeps_dlopen_working():
    assert "nodlopen" not in setup_cmd._HARDENED_BLOCK


def test_performance_note_names_the_llvm_compiler():
    from sysforge.primitives import config_presets
    assert any('compiler = "llvm"' in n for n in config_presets.PRESETS["performance"].notes)


def test_performance_warns_when_enabled_gcc_toolchain_stage_will_reset(preset_env, capsys):
    _writes, _, paths = preset_env
    paths["toolchain"].write_text('enabled = true\ncompiler = "gcc"\n\n[bolt]\n')
    setup_cmd._setup_preset("performance")
    assert "resets [defaults] toolchain" in capsys.readouterr().out


def test_performance_no_reset_warning_when_toolchain_stage_off(preset_env, capsys):
    setup_cmd._setup_preset("performance")
    assert "resets [defaults] toolchain" not in capsys.readouterr().out


def test_top_level_manual_block_says_where_to_paste(preset_env, monkeypatch, capsys):
    from sysforge.primitives import config
    def _boom(path, s, k, v):
        raise PermissionError("denied")
    monkeypatch.setattr(config, "set_toml_key", _boom)
    setup_cmd._setup_preset("secure")
    assert "top level, above the first [section] header" in capsys.readouterr().err


def test_low_friction_warns_when_it_turns_the_sandbox_off(preset_env, capsys):
    _writes, tmp_path, paths = preset_env
    paths["sysforge"].write_text(
        f'[security]\nsandbox_builds = true\nsandbox_chroot_dir = "{tmp_path / "chroot"}"\n')
    setup_cmd._setup_preset("low-friction")
    assert "does not inspect what gets installed" in capsys.readouterr().out


@pytest.fixture
def sudo_user_profiles(tmp_path, monkeypatch):
    home = tmp_path / "home"
    user_file = home / ".config" / "sysforge" / "profiles.toml"
    user_file.parent.mkdir(parents=True)
    user_file.write_text('[defaults]\nprofile = "standard"\n[profiles.optimized]\n')
    import pwd
    monkeypatch.setattr(setup_cmd.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "someone")
    monkeypatch.setattr(pwd, "getpwnam",
                        lambda name: type("P", (), {"pw_dir": str(home)})())
    return user_file


def test_sudo_resolves_the_invoking_users_profiles(sudo_user_profiles):
    assert setup_cmd._invoking_user_profiles() == sudo_user_profiles


def test_no_sudo_user_means_no_override(monkeypatch):
    monkeypatch.delenv("SUDO_USER", raising=False)
    assert setup_cmd._invoking_user_profiles() is None


def test_sudo_preset_targets_and_checks_the_users_file(sudo_user_profiles, monkeypatch):
    from sysforge.primitives import config
    real = config.load_config
    seen = []
    def _load(paths=None):
        seen.append(paths)
        return real(paths) if paths else {}
    monkeypatch.setattr(config, "load_config", _load)
    monkeypatch.setattr(setup_cmd, "resolve_packages_path", lambda cfg: Path("/x/packages.toml"))
    assert setup_cmd._preset_paths()["profiles"] == sudo_user_profiles
    assert "optimized" in setup_cmd._resolved_profiles(sudo_user_profiles)
    assert seen[-1][0] == sudo_user_profiles  # the user's file is read first
