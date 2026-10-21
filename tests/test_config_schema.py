# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
Live-config checking and repair (3.5.0-F18): `config check`, `config clean`,
`config reset` and the `doctor system --config` axis, over
primitives/config_schema.py.
"""
import shutil
import tomllib
from pathlib import Path

import pytest

from sysforge import config_cmd
from sysforge.primitives import config_schema as cs
from sysforge.verbs.registry import build_parser

REPO = Path(__file__).resolve().parent.parent
SHIPPED = REPO / "etc/sysforge"


def _kinds(name, text):
    return [(i.kind, i.where) for i in cs.inspect_text(name, text)]


# --- the schema against the shipped files -----------------------------------

@pytest.mark.parametrize("d", [SHIPPED, REPO / "tests/data/etc/sysforge"])
def test_shipped_and_fixture_configs_are_clean(d):
    assert cs.inspect(d) == []


@pytest.mark.parametrize("name", ["kernel.toml", "toolchain.toml"])
def test_every_documented_top_level_key_is_known(name):
    """A key the shipped file documents (even commented out) is a real setting;
    toolchain.toml's reuse_unchanged was missing from the list until F18."""
    documented = cs._documented(cs.shipped_text(name))[""]
    assert documented <= cs.KNOWN_TOP_KEYS[name]


def test_retired_replacements_are_current_names():
    for r in cs.RETIRED_KEYS:
        assert r.replacement and r.replacement in (SHIPPED / r.file).read_text()
    for v in cs.RETIRED_VALUES:
        assert v.new in (SHIPPED / v.file).read_text()


def test_defaults_ship_in_the_wheel():
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert include["etc/sysforge"] == "sysforge/_data/config-defaults"


# --- inspection ---------------------------------------------------------------

def test_retired_key_and_value_are_reported():
    assert ("retired_key", "[git] pull_timeout") in _kinds(
        "sysforge.toml", "[git]\npull_timeout = 45\n")
    assert ("retired_value", "[build] repo_mode") in _kinds(
        "packages.toml", '[build]\nrepo_mode = "profiled"\n')
    assert ("retired_value", "[profiles.mine] build_mode") in _kinds(
        "profiles.toml", '[profiles.mine]\nbuild_mode = "patched_pkgbuild"\n')
    assert ("retired_key", "[package] pkgbuild_patch") in _kinds(
        "packages.toml", '[[package]]\nname = "x"\npkgbuild_patch = true\n')


def test_unknown_keys_in_fixed_sections_but_not_in_open_ones():
    assert _kinds("sysforge.toml", '[ui]\ncolr = "never"\n') == [("unknown_key", "[ui] colr")]
    assert _kinds("sysforge.toml", "[nosuch]\nx = 1\n") == [("unknown_section", "[nosuch]")]
    assert _kinds("toolchain.toml", "enabld = true\n") == [("unknown_key", "enabld")]
    # User-named: a profile's flag variables, a rule, a package entry.
    assert _kinds("profiles.toml", '[profiles.mine]\nMY_VAR = "1"\n') == []
    assert _kinds("packages.toml", '[[package]]\nname = "x"\nanything = 1\n') == []


def test_a_commented_example_counts_as_known():
    assert _kinds("toolchain.toml", "reuse_unchanged = true\n") == []


def test_unparsable_file_is_one_parse_error():
    [(kind, _)] = _kinds("sysforge.toml", "[ui\n")
    assert kind == "parse_error"


# --- clean --------------------------------------------------------------------

def test_clean_renames_a_retired_key_and_keeps_comments():
    text = "[git]\n# my note\npull_timeout = 45  # trailing\n"
    new, fixed = cs.clean_text("sysforge.toml", text, unknown=False)
    assert new == "[git]\n# my note\nfetch_timeout = 45  # trailing\n"
    assert fixed[0][1] == "renamed to fetch_timeout"


def test_clean_drops_a_retired_key_whose_replacement_is_set():
    text = "[git]\nfetch_timeout = 30\npull_timeout = 45\n"
    new, fixed = cs.clean_text("sysforge.toml", text, unknown=False)
    assert new == "[git]\nfetch_timeout = 30\n"
    assert "already set" in fixed[0][1]


def test_clean_renames_within_each_array_table_entry():
    text = ('[[package]]\nname = "a"\npkgbuild_patch = true\n'
            '[[package]]\nname = "b"\nenable_build_from_source = false\npkgbuild_patch = true\n')
    new, _ = cs.clean_text("packages.toml", text, unknown=False)
    assert tomllib.loads(new)["package"] == [
        {"name": "a", "enable_build_from_source": True},
        {"name": "b", "enable_build_from_source": False}]


def test_clean_rewrites_a_retired_value():
    new, _ = cs.clean_text("profiles.toml",
                           '[profiles.mine]\nbuild_mode = "patched_pkgbuild"  # x\n',
                           unknown=False)
    assert new == '[profiles.mine]\nbuild_mode = "source_built"  # x\n'


def test_clean_leaves_unknown_keys_unless_asked():
    text = '[ui]\ncolr = [\n  "a",\n  "b",\n]\ncolor = "auto"\n[nosuch]\nx = 1\n'
    assert cs.clean_text("sysforge.toml", text, unknown=False) == (text, [])
    new, fixed = cs.clean_text("sysforge.toml", text, unknown=True)
    assert new == '[ui]\ncolor = "auto"\n'   # the whole multi-line array, and [nosuch]
    assert len(fixed) == 2


def test_clean_refuses_an_unparsable_file():
    with pytest.raises(cs.ConfigEditError):
        cs.clean_text("sysforge.toml", "[ui\n", unknown=True)


# --- reset --------------------------------------------------------------------

def test_reset_key_to_an_active_shipped_value():
    shipped = tomllib.loads(cs.shipped_text("sysforge.toml"))["git"]["fetch_timeout"]
    new, changes = cs.reset_keys_text("sysforge.toml", "[git]\nfetch_timeout = 999\n",
                                      ["git.fetch_timeout"])
    assert tomllib.loads(new)["git"]["fetch_timeout"] == shipped
    assert changes == ["git.fetch_timeout: set to the shipped value"]


def test_reset_key_the_default_leaves_unset_removes_it():
    new, changes = cs.reset_keys_text("sysforge.toml", "[build]\njobs = 3\n", ["build.jobs"])
    assert "jobs" not in tomllib.loads(new)["build"]
    assert "removed" in changes[0]


def test_reset_adds_a_missing_shipped_key_to_its_section():
    new, _ = cs.reset_keys_text("sysforge.toml", "[git]\n", ["git.fetch_timeout"])
    assert "fetch_timeout" in tomllib.loads(new)["git"]


def test_reset_refuses_array_table_keys():
    with pytest.raises(cs.ConfigEditError):
        cs.reset_keys_text("packages.toml", '[[package]]\nname = "x"\n', ["package.name"])


# --- the verbs ----------------------------------------------------------------

def _live(tmp_path):
    for name in cs.CONFIG_FILES:
        shutil.copy(SHIPPED / name, tmp_path / name)
    return tmp_path


def _args(*argv):
    return build_parser().parse_args(["config", *argv])


def test_check_exits_1_on_findings_and_0_when_clean(tmp_path):
    d = _live(tmp_path)
    assert config_cmd.cmd_config_check(_args("check", "--config-dir", str(d))) == 0
    (d / "sysforge.toml").write_text((d / "sysforge.toml").read_text()
                                     .replace("[git]", "[git]\npull_timeout = 1", 1))
    assert config_cmd.cmd_config_check(_args("check", "--config-dir", str(d))) == 1


def test_clean_without_a_terminal_needs_yes(tmp_path, monkeypatch):
    d = _live(tmp_path)
    path = d / "sysforge.toml"
    path.write_text(path.read_text().replace("[ui]", "[ui]\ncolr = 1", 1))
    before = path.read_text()
    monkeypatch.setattr(config_cmd.prompt, "is_interactive", lambda: False)
    argv = ["clean", "--unknown", "--config-dir", str(d)]
    assert config_cmd.cmd_config_clean(_args(*argv)) == 2
    assert config_cmd.cmd_config_clean(_args(*argv, "--dry-run")) == 0
    assert path.read_text() == before
    assert config_cmd.cmd_config_clean(_args(*argv, "--yes")) == 0
    assert "colr" not in path.read_text()


def test_reset_whole_file_keeps_a_backup(tmp_path):
    d = _live(tmp_path)
    path = d / "kernel.toml"
    path.write_text(path.read_text() + "\n# mine\n")
    assert config_cmd.cmd_config_reset(_args("reset", "kernel.toml", "--config-dir", str(d),
                                              "--yes")) == 0
    assert path.read_text() == (SHIPPED / "kernel.toml").read_text()
    [backup] = d.glob("kernel.toml.bak-*")
    assert backup.read_text().endswith("# mine\n")


def test_doctor_config_axis_reports_retired_settings(tmp_path, monkeypatch):
    from sysforge import doctor
    d = _live(tmp_path)
    (d / "packages.toml").write_text('[build]\nrepo_mode = "profiled"\n')
    monkeypatch.setattr("sysforge.primitives.paths.CONFIG_DIR", d)
    result = doctor._collect_config_findings()
    [finding] = result.findings
    assert finding.severity == "warn" and finding.remediation == "sysforge config clean"
