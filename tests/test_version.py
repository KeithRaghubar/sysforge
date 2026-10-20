"""
test_version.py — unit tests for sysforge.primitives.version

All subprocess calls are mocked; no real vercmp binary is required.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.resource_guard import lift_for_child
from sysforge.primitives.version import format_version, vercmp


# ---------------------------------------------------------------------------
# vercmp
# ---------------------------------------------------------------------------

def _mock_run(stdout_val, returncode=0):
    m = MagicMock()
    m.stdout = stdout_val
    m.returncode = returncode
    return m


def test_vercmp_newer():
    with patch("subprocess.run", return_value=_mock_run("1\n")) as mock:
        result = vercmp("3.4.1-1", "3.3.0-1")
    assert result == 1
    mock.assert_called_once_with(["vercmp", "3.4.1-1", "3.3.0-1"],
                                 capture_output=True, text=True, check=False,
                                 preexec_fn=lift_for_child)


def test_vercmp_equal():
    with patch("subprocess.run", return_value=_mock_run("0\n")):
        result = vercmp("3.4.1-1", "3.4.1-1")
    assert result == 0


def test_vercmp_older():
    with patch("subprocess.run", return_value=_mock_run("-1\n")):
        result = vercmp("3.3.0-1", "3.4.1-1")
    assert result == -1


def test_vercmp_large_positive_clamped():
    # vercmp may return values other than exactly 1
    with patch("subprocess.run", return_value=_mock_run("42\n")):
        result = vercmp("2.0-1", "1.0-1")
    assert result == 1


def test_vercmp_large_negative_clamped():
    with patch("subprocess.run", return_value=_mock_run("-7\n")):
        result = vercmp("1.0-1", "2.0-1")
    assert result == -1


def test_vercmp_with_epoch():
    with patch("subprocess.run", return_value=_mock_run("1\n")) as mock:
        result = vercmp("1:2.0-1", "0:3.0-1")
    assert result == 1
    # Verify the epoch-qualified strings were passed verbatim
    args = mock.call_args[0][0]
    assert args[1] == "1:2.0-1"
    assert args[2] == "0:3.0-1"


def test_vercmp_not_found_raises():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError, match="vercmp not found"):
            vercmp("1.0-1", "1.0-1")


def test_vercmp_bad_output_raises():
    with patch("subprocess.run", return_value=_mock_run("not-a-number\n")):
        with pytest.raises(RuntimeError, match="unexpected output"):
            vercmp("1.0-1", "1.0-1")


# ---------------------------------------------------------------------------
# format_version
# ---------------------------------------------------------------------------

def test_format_version_no_epoch():
    g = {"pkgver": "3.4.1", "pkgrel": "1", "epoch": "0"}
    assert format_version(g) == "3.4.1-1"


def test_format_version_with_epoch():
    g = {"pkgver": "3.4.1", "pkgrel": "1", "epoch": "2"}
    assert format_version(g) == "2:3.4.1-1"


def test_format_version_missing_epoch():
    g = {"pkgver": "3.4.1", "pkgrel": "2"}
    assert format_version(g) == "3.4.1-2"


def test_format_version_empty_epoch_treated_as_zero():
    g = {"pkgver": "1.0", "pkgrel": "1", "epoch": ""}
    assert format_version(g) == "1.0-1"


def test_format_version_default_pkgrel():
    g = {"pkgver": "1.0"}
    assert format_version(g) == "1.0-1"


# ---------------------------------------------------------------------------
# 2.6.1-F29 (adjacent fix) — the log line must carry its verdict
# ---------------------------------------------------------------------------

def test_vercmp_logs_operands_with_the_result():
    """Logging the operands *before* comparing left a line that asked a question
    and never answered it — useless when reading the run log after the fact."""
    lines, info = [], []
    with patch("subprocess.run", return_value=_mock_run("1\n")), \
         patch("sysforge.log.debug", side_effect=lambda tag, msg: lines.append((tag, msg))), \
         patch("sysforge.log.info", side_effect=lambda tag, msg: info.append((tag, msg))):
        assert vercmp("3.4.1-1", "3.3.0-1") == 1

    lines = [(tag, msg) for tag, msg in lines if tag == "[VERSION]"]

    assert lines == [("[VERSION]", "vercmp '3.4.1-1' '3.3.0-1' -> 1")]
    # 3.5.0-B11: bare operands say nothing about *what* was compared, so they
    # are mechanics (debug); the caller narrates the decision at info.
    assert info == []


def test_vercmp_log_line_carries_no_colour():
    """Message bodies on debug() bypass the colour gate and land verbatim in the
    file log (docs/design/12-logging.md § Colour) — this one stays plain."""
    from sysforge import log

    lines = []
    saved = log._COLOR_MODE
    try:
        log.set_color_mode("always")
        with patch("subprocess.run", return_value=_mock_run("-1\n")), \
             patch("sysforge.log.debug",
                   side_effect=lambda tag, msg: lines.append(msg) if tag == "[VERSION]" else None):
            assert vercmp("1.0-1", "2.0-1") == -1
    finally:
        log.set_color_mode(saved)

    assert "\033[" not in lines[0]


def test_vercmp_does_not_log_when_the_binary_is_missing():
    """A RuntimeError path has nothing to report a verdict for."""
    lines = []
    with patch("subprocess.run", side_effect=FileNotFoundError), \
         patch("sysforge.log.info", side_effect=lambda tag, msg: lines.append(msg)), \
         pytest.raises(RuntimeError, match="vercmp not found"):
        vercmp("1.0-1", "2.0-1")

    assert lines == []


# ---------------------------------------------------------------------------
# vercmp_local: pure port of libalpm's alpm_pkg_vercmp (3.4.0-F8)
# ---------------------------------------------------------------------------

_VERCMP_PAIRS = [
    ("1.0-1", "1.0-1"), ("1.0-1", "1.0-2"), ("1.0-2", "1.0-1"),
    ("1.0", "1.0.0"), ("1.0.0", "1.0"), ("1.0a", "1.0"), ("1.0", "1.0a"),
    ("1.0rc1", "1.0"), ("1.0alpha", "1.0beta"), ("1.0.a", "1.0.1"),
    ("1:1.0-1", "2.0-1"), ("2.0-1", "1:1.0-1"), ("0:1.0-1", "1.0-1"),
    ("1.0_1", "1.0.1"), ("1.0+1", "1.0.1"), ("1..0", "1.0"), ("001", "1"),
    ("1.2.3.r45.gabcdef0", "1.2.3.r50.g1234567"), ("r1234.abc", "r1240.def"),
    ("20240101", "20240215"), ("1.0-1.1", "1.0-1"), ("1.0", "1.0-1"),
    ("1.0-1", "1.0"), ("a", "1"), ("1", "a"), ("", "1"), ("1.0~rc1", "1.0"),
    ("2.10", "2.9"), ("1.0.0", "1.0.0.0"), ("1.a.1", "1.b.0"),
    ("1.", "1.0"), ("1.0", "1."), ("1", "1a"), ("1a", "1"), ("1", "1.1"),
    ("1.1", "1"), ("1.a", "1.0"), ("1_", "1"), ("1.0.", "1.0.0"),
    ("1..0", "1.0"), ("1.1", "1.a"), ("a:1.0", "1.0"), (":1.0", "1.0"),
]


@pytest.mark.parametrize("a,b,want", [
    ("1.0-1", "1.0-1", 0), ("1.0-1", "1.0-2", -1), ("2.10", "2.9", 1),
    ("1:1.0-1", "2.0-1", 1), ("1.0rc1", "1.0", -1), ("001", "1", 0),
    ("1.0", "1.0.0", -1), ("1.0a", "1.0", -1),
    # 3.5.0-DEV1: a trailing separator exhausts one side mid-segment, and the
    # tail rule (an alpha remainder loses, any other wins), checked against
    # vercmp(8) so they hold where the binary is absent (CI).
    ("1.", "1.0", -1), ("1.0", "1.", 1), ("1.0.", "1.0.0", -1),
    ("1", "1a", 1), ("1a", "1", -1), ("1", "1.1", -1), ("1.1", "1", 1),
    ("1.a", "1.0", -1), ("1_", "1", 1),
    # Separator runs of different length, and a number against letters: the
    # result is exactly -1/0/1, never a larger magnitude.
    ("1..0", "1.0", 1), ("1.0", "1..0", -1), ("1.1", "1.a", 1), ("1.a", "1.1", -1),
    # parseEVR: a non-numeric "epoch" is part of the pkgver; an empty one is dropped.
    ("a:1.0", "1.0", -1), (":1.0", "1.0", 0),
])
def test_vercmp_local_known_answers(a, b, want):
    from sysforge.primitives.version import vercmp_local
    assert vercmp_local(a, b) == want


@pytest.mark.skipif(not __import__("shutil").which("vercmp"),
                    reason="vercmp(8) not installed")
@pytest.mark.parametrize("a,b", _VERCMP_PAIRS)
def test_vercmp_local_agrees_with_vercmp_binary(a, b):
    import subprocess
    from sysforge.primitives.version import vercmp_local
    out = subprocess.run(["vercmp", a, b], capture_output=True, text=True,  # noqa: S603, S607
                         check=True).stdout.strip()
    want = (int(out) > 0) - (int(out) < 0)
    assert vercmp_local(a, b) == want, (a, b)


@pytest.mark.parametrize("c,alpha,digit", [
    ("a", True, False), ("z", True, False), ("A", True, False), ("Z", True, False),
    ("0", False, True), ("9", False, True),
    ("`", False, False), ("{", False, False), ("@", False, False), ("[", False, False),
    ("/", False, False), (":", False, False), (".", False, False),
])
def test_rpmvercmp_character_classes_are_ascii_ranges(c, alpha, digit):
    """3.5.0-DEV1: the range ends are inclusive, and their ASCII neighbours are
    separators."""
    from sysforge.primitives.version import _is_alpha, _is_digit
    assert _is_alpha(c) is alpha
    assert _is_digit(c) is digit


@pytest.mark.parametrize("ver,evr", [
    ("1:2.0-3", ("1", "2.0", "3")), (":2.0-3", ("0", "2.0", "3")),
    ("a:2.0-3", ("0", "a:2.0", "3")), ("2.0", ("0", "2.0", None)),
])
def test_parse_evr(ver, evr):
    from sysforge.primitives.version import _parse_evr
    assert _parse_evr(ver) == evr
