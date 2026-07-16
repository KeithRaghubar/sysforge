"""tests for sysforge.primitives.privilege — the escalation seam (2.3.0-F10)."""
from unittest.mock import patch

from sysforge.primitives import privilege


def test_privileged_argv_prefixes_sudo_when_not_root():
    with patch("sysforge.primitives.privilege.os.geteuid", return_value=1000):
        assert privilege.privileged_argv(["pacman", "-Syu"]) == ["sudo", "pacman", "-Syu"]


def test_privileged_argv_bare_when_root():
    with patch("sysforge.primitives.privilege.os.geteuid", return_value=0):
        assert privilege.privileged_argv(["pacman", "-Syu"]) == ["pacman", "-Syu"]


def test_privileged_argv_noninteractive_inserts_dash_n_when_not_root():
    with patch("sysforge.primitives.privilege.os.geteuid", return_value=1000):
        assert privilege.privileged_argv(["rm", "-f", "/x"], noninteractive=True) == [
            "sudo", "-n", "rm", "-f", "/x",
        ]


def test_privileged_argv_noninteractive_moot_when_root():
    with patch("sysforge.primitives.privilege.os.geteuid", return_value=0):
        assert privilege.privileged_argv(["rm", "-f", "/x"], noninteractive=True) == [
            "rm", "-f", "/x",
        ]


def test_privileged_argv_does_not_mutate_input():
    argv = ["pacman", "-Syu"]
    with patch("sysforge.primitives.privilege.os.geteuid", return_value=1000):
        privilege.privileged_argv(argv)
    assert argv == ["pacman", "-Syu"]


def test_run_privileged_escalates_then_delegates_to_run_or_raise():
    seen = {}

    def fake_run_or_raise(cmd, *, tag, **kw):
        seen["cmd"] = cmd
        seen["tag"] = tag
        return "OK"

    with patch("sysforge.primitives.privilege.os.geteuid", return_value=1000), \
         patch("sysforge.primitives.privilege.run_or_raise", fake_run_or_raise):
        result = privilege.run_privileged(["pacman", "-U", "x.pkg"], tag="TEST")

    assert result == "OK"
    assert seen["cmd"] == ["sudo", "pacman", "-U", "x.pkg"]
    assert seen["tag"] == "TEST"
