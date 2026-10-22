# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
The surface snapshot and its review rules (3.5.0-DEV18, tools/surface.py).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _surface():
    spec = importlib.util.spec_from_file_location("surface", REPO / "tools/surface.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("surface", mod)
    spec.loader.exec_module(mod)
    return mod


def _snap(flags=None, env=(), config_keys=()):
    return {
        "commands": {"state list": {"flags": flags or {"--json": None}, "positionals": {}}},
        "environment": list(env),
        "journal_fields": [],
        "config": {"sysforge.toml": {"top_level": [],
                                     "sections": {"git": list(config_keys)}}},
        "json": {},
        "run_logs": [],
    }


def test_committed_snapshot_is_current():
    s = _surface()
    assert (REPO / s.SNAPSHOT).read_text(encoding="utf-8") == s.render(s.collect()), \
        "run `make surface` and commit tests/data/surface.json"


def test_unchanged_surface_needs_nothing():
    s = _surface()
    assert s.judge(_snap(), _snap(), "", set()) == []


def test_an_added_flag_needs_a_release_note():
    s = _surface()
    head = _snap(flags={"--json": None, "--wide": None})
    [problem] = s.judge(_snap(), head, "", set())
    assert "added flag: state list --wide" in problem
    assert s.judge(_snap(), head, "- **`3.5.0-F99` — `state list --wide`.** ...", set()) == []


def test_an_added_value_and_env_var_need_naming():
    s = _surface()
    head = _snap(flags={"--json": None, "--sort": ["age", "name"]}, env=["SYSFORGE_X"])
    problems = s.judge(_snap(), head, "", set())
    assert {p.split(":")[0] for p in problems} == {"added flag", "added value", "added environment"}


@pytest.mark.parametrize("base, head, name", [
    (_snap(flags={"--json": None, "--old": None}), _snap(), "--old"),
    (_snap(env=["SYSFORGE_OLD"]), _snap(), "SYSFORGE_OLD"),
    (_snap(config_keys=["pull_timeout"]), _snap(), "pull_timeout"),
])
def test_a_name_gone_entirely_must_be_retired(base, head, name):
    s = _surface()
    notes = f"- **`3.5.0-F99` — `{name}` is gone.**"
    [problem] = s.judge(base, head, notes, set())
    assert "tools/retired_names.toml" in problem
    assert s.judge(base, head, notes, {name}) == []


def test_a_flag_still_on_another_command_is_not_retired():
    """Dropping --json from one command is a release-note matter only."""
    s = _surface()
    base = _snap()
    base["commands"]["state failed"] = {"flags": {"--json": None}, "positionals": {}}
    head = _snap()
    head["commands"]["state failed"] = {"flags": {}, "positionals": {}}
    notes = "`state failed` no longer takes `--json`"
    assert s.judge(base, head, notes, set()) == []

