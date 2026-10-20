"""`sysforge packages list` and the packages verbs' failure paths (3.5.0-DEV2)."""
from types import SimpleNamespace

import pytest

from sysforge import packages_cmd
from sysforge.packages_cmd import PackagesAddVerb, cmd_packages_list


def _list(path, **kw):
    cmd_packages_list(SimpleNamespace(packages=str(path), orphans=kw.get("orphans", False)))


def test_list_without_a_file_is_fatal(tmp_path):
    with pytest.raises(SystemExit):
        _list(tmp_path / "packages.toml")


def test_list_of_an_empty_file_says_so(tmp_path, capsys):
    p = tmp_path / "packages.toml"
    p.write_text("# nothing\n")
    _list(p)
    assert "No packages defined" in capsys.readouterr().out


def test_list_shows_each_entry_with_its_overrides(tmp_path, capsys):
    p = tmp_path / "packages.toml"
    p.write_text('[[package]]\nname = "mesa"\nsource = "repo"\n'
                 'enable_build_from_source = true\ncache = false\nreason = "pgo"\n'
                 '[[package]]\nname = "htop"\nreason = "x"\n')
    _list(p)
    out = capsys.readouterr().out
    assert "NAME" in out and "OVERRIDES" in out
    mesa = next(line for line in out.splitlines() if "mesa" in line)
    assert "enable_build_from_source" in mesa and "cache=false" in mesa and "reason='pgo'" in mesa


def test_list_shows_groups_as_written(tmp_path, capsys):
    p = tmp_path / "packages.toml"
    p.write_text('[group.cosmic]\npackages = ["cosmic-comp", "cosmic-panel"]\n'
                 'enable_build_from_source = true\n'
                 '[group.broken]\n')
    _list(p)
    out = capsys.readouterr().out
    assert "[group.cosmic]  2 package(s)  defaults: enable_build_from_source=True" in out
    assert "    cosmic-comp" in out
    assert "NAME" not in out  # no explicit entries, no table


def test_list_orphans_keeps_only_uninstalled_entries(tmp_path, capsys, monkeypatch):
    p = tmp_path / "packages.toml"
    p.write_text('[[package]]\nname = "mesa"\nreason = "a"\n'
                 '[[package]]\nname = "gone"\nreason = "b"\n'
                 '[group.g]\npackages = ["x"]\n')
    monkeypatch.setattr("sysforge.primitives.pacman.get_all_installed_packages",
                        lambda: {"mesa": "1-1"})
    _list(p, orphans=True)
    out = capsys.readouterr().out
    assert "gone" in out and "mesa" not in out
    assert "[group.g]" not in out  # --orphans is an explicit-entry tool


def test_list_orphans_when_every_target_is_installed(tmp_path, capsys, monkeypatch):
    p = tmp_path / "packages.toml"
    p.write_text('[[package]]\nname = "mesa"\nreason = "a"\n')
    monkeypatch.setattr("sysforge.primitives.pacman.get_all_installed_packages",
                        lambda: {"mesa": "1-1"})
    _list(p, orphans=True)
    assert "No orphan entries" in capsys.readouterr().out


def test_packages_file_defaults_to_the_configured_path(tmp_path, monkeypatch):
    monkeypatch.setattr(packages_cmd, "load_config", lambda: {"x": 1})
    monkeypatch.setattr(packages_cmd, "resolve_packages_path",
                        lambda cfg: tmp_path / "from-config.toml")
    assert packages_cmd._resolve_packages_file(None) == tmp_path / "from-config.toml"


def test_add_verb_pre_check_blocks_source_only():
    args = SimpleNamespace(pkg="mesa", enable_build_from_source=False,
                           no_cache=False, reason=None, source="repo")
    pre = PackagesAddVerb().pre_check(args)
    assert pre.blocker and "--source alone is metadata" in pre.blocker
    assert pre.exit_code == 1


def test_add_exits_when_the_opt_out_cannot_be_cleared(tmp_path, monkeypatch, capsys):
    from sysforge.primitives import opt_out

    def boom(self, names):
        raise opt_out.OptOutError("opt-out file is corrupt")
    monkeypatch.setattr(opt_out.OptOutList, "clear", boom)
    args = SimpleNamespace(pkg="mesa", packages=str(tmp_path / "packages.toml"),
                           source=None, enable_build_from_source=False, no_cache=False,
                           reason="r", state_dir=str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        packages_cmd.cmd_packages_add(args)
    assert exc.value.code == 1
    assert "opt-out file is corrupt" in capsys.readouterr().err


def test_add_reports_a_lifted_opt_out(tmp_path, monkeypatch, capsys):
    from sysforge.primitives import opt_out

    monkeypatch.setattr(opt_out.OptOutList, "clear", lambda self, names: ["mesa"])
    args = SimpleNamespace(pkg="mesa", packages=str(tmp_path / "packages.toml"),
                           source="repo", enable_build_from_source=True, no_cache=False,
                           reason=None, state_dir=str(tmp_path))
    packages_cmd.cmd_packages_add(args)
    assert "mesa: maintained again" in capsys.readouterr().out


def test_remove_without_a_file_is_fatal(tmp_path):
    with pytest.raises(SystemExit):
        packages_cmd.cmd_packages_remove(
            SimpleNamespace(pkg="mesa", packages=str(tmp_path / "packages.toml")))
