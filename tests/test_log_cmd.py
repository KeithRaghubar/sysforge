"""
test_log_cmd.py — coverage for `sysforge log [PKG]`.

Verifies path resolution for both the unified and per-package modes,
the missing-file error path, and that --no-pager produces plain stdout
that capsys can capture.
"""
from types import SimpleNamespace
from unittest.mock import patch

from sysforge.log_cmd import cmd_log


def _args(pkg=None, state_dir=None, no_pager=True):
    return SimpleNamespace(pkg=pkg, state_dir=str(state_dir) if state_dir else None,
                           no_pager=no_pager)


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
