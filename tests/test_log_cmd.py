"""
test_log_cmd.py — coverage for `sysforge log [PKG]`.

Verifies path resolution for both the unified and per-package modes,
the missing-file error path, and that --no-pager produces plain stdout
that capsys can capture.
"""
from unittest.mock import patch

import pytest

from sysforge.log_cmd import cmd_log
from sysforge.verbs.registry import build_parser


def _args(pkg=None, state_dir=None, *extra):
    """Parsed by the real parser, so the namespace always has every field."""
    argv = ["log", "--no-pager", *extra]
    if state_dir:
        argv += ["--state-dir", str(state_dir)]
    if pkg:
        argv.append(pkg)
    return build_parser().parse_args(argv)


def test_log_unified_path_resolves(tmp_path, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "sysforge.log").write_text("UNIFIED LINE 1\nUNIFIED LINE 2\n")

    rc = cmd_log(_args(state_dir=state_dir))

    assert rc == 0
    out = capsys.readouterr().out
    assert "UNIFIED LINE 1" in out
    assert "UNIFIED LINE 2" in out


def test_log_per_pkg_path_resolves(tmp_path, capsys):
    pkg_dir = tmp_path / "foo"
    pkg_dir.mkdir()
    (pkg_dir / "sysforge_foo.log").write_text("PER-PKG LINE\n")

    fake_config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}
    with patch("sysforge.primitives.config.load_config", return_value=fake_config):
        rc = cmd_log(_args(pkg="foo"))

    assert rc == 0
    assert "PER-PKG LINE" in capsys.readouterr().out


def test_log_missing_pkg_errors(tmp_path, capsys):
    """Missing per-pkg log → non-zero exit; no AUR clone attempted."""
    fake_config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}
    with patch("sysforge.primitives.config.load_config", return_value=fake_config):
        rc = cmd_log(_args(pkg="nonexistent"))

    assert rc == 1
    err = capsys.readouterr().err
    assert "No sysforge log for nonexistent" in err
    expected = tmp_path / "nonexistent" / "sysforge_nonexistent.log"
    assert str(expected) in err


def test_log_missing_unified_errors(tmp_path, capsys):
    """Missing unified log → non-zero exit with the searched path in the message."""
    state_dir = tmp_path / "empty-state"

    rc = cmd_log(_args(state_dir=state_dir))

    assert rc == 1
    err = capsys.readouterr().err
    assert "No sysforge unified log" in err
    assert str(state_dir / "sysforge.log") in err


def test_log_per_pkg_without_pkgbuild_src_dir_errors(tmp_path, capsys):
    """If [paths] pkgbuild_src_dir is unset, per-pkg mode must refuse."""
    with patch("sysforge.primitives.config.load_config", return_value={}):
        rc = cmd_log(_args(pkg="foo"))

    assert rc == 1
    assert "pkgbuild_src_dir" in capsys.readouterr().err


# 3.5.0-F13: the standalone verbs' consolidated logs.

@pytest.mark.parametrize("verb", ["build", "doctor", "update"])
def test_log_verb_resolves_under_the_state_dir(tmp_path, capsys, verb):
    (tmp_path / f"sysforge-{verb}.log").write_text(f"{verb.upper()} LINE\n")
    assert cmd_log(_args(None, tmp_path, "--verb", verb)) == 0
    assert f"{verb.upper()} LINE" in capsys.readouterr().out


@pytest.mark.parametrize("verb", ["build", "doctor", "update"])
def test_log_verb_resolves_under_log_dir(tmp_path, capsys, verb):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / f"sysforge-{verb}.log").write_text("ELSEWHERE\n")
    assert cmd_log(_args(None, tmp_path / "state", "--verb", verb,
                         "--log-dir", str(logs))) == 0
    assert "ELSEWHERE" in capsys.readouterr().out


def test_log_pkg_resolves_under_log_dir(tmp_path, capsys):
    (tmp_path / "sysforge_foo.log").write_text("PER-PKG ELSEWHERE\n")
    assert cmd_log(_args("foo", None, "--log-dir", str(tmp_path))) == 0
    assert "PER-PKG ELSEWHERE" in capsys.readouterr().out


def test_log_verb_with_pkg_is_refused():
    with pytest.raises(SystemExit) as exc:
        _args("foo", None, "--verb", "build")
    assert exc.value.code == 2


def test_log_missing_verb_log_does_not_fall_back(tmp_path, capsys):
    (tmp_path / "sysforge.log").write_text("PIPELINE\n")
    assert cmd_log(_args(None, tmp_path, "--verb", "update")) == 1
    captured = capsys.readouterr()
    assert "PIPELINE" not in captured.out
    assert str(tmp_path / "sysforge-update.log") in captured.err


def test_every_run_log_writer_is_pageable():
    """Each verb's declared name is what the runner opens and what --verb
    accepts, so a verb that starts logging is pageable with no second edit."""
    from sysforge import log
    from sysforge.verbs.base import VerbGroup
    from sysforge.verbs.registry import COMMANDS, verb_log_names
    names = verb_log_names()
    args = _args(None, None)  # read only by doctor pkg (apply/dry_run), absent elsewhere
    verbs = [v for e in COMMANDS for v in (e.members if isinstance(e, VerbGroup) else (e,))]
    for v in verbs:
        if v.wants_run_log:
            assert v.run_log_name() in names
            assert v().unified_log_basename(args) == log.verb_log_name(v.run_log_name())
    # The standalone stage runner names its log run-<stage>, as the verbs are.
    from sysforge.run_cmd import RunToolchainVerb
    assert RunToolchainVerb.run_log_name() == "run-toolchain"
    assert {"build", "doctor", "update", "run-kernel", "state-repair"} <= set(names)


def test_completions_offer_every_verb_log():
    import re
    from pathlib import Path

    from sysforge.verbs.registry import verb_log_names
    root = Path(__file__).resolve().parent.parent / "completions"
    want = verb_log_names()
    bash = re.search(r'--verb\)\n.*?compgen -W "([^"]+)"',
                     (root / "sysforge.bash").read_text(), re.S)
    zsh = re.search(r":verb:\(([^)]+)\)", (root / "_sysforge").read_text())
    assert bash and sorted(bash.group(1).split()) == want
    assert zsh and sorted(zsh.group(1).split()) == want
