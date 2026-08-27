"""
test_version.py — unit tests for sysforge.primitives.version

All subprocess calls are mocked; no real vercmp binary is required.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
                                 capture_output=True, text=True)


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
    lines = []
    with patch("subprocess.run", return_value=_mock_run("1\n")), \
         patch("sysforge.log.info", side_effect=lambda tag, msg: lines.append((tag, msg))):
        assert vercmp("3.4.1-1", "3.3.0-1") == 1

    assert lines == [("[VERSION]", "vercmp '3.4.1-1' '3.3.0-1' -> 1")]


def test_vercmp_log_line_carries_no_colour():
    """Message bodies on info() bypass the colour gate and land verbatim in the
    file log (docs/design/12-logging.md § Colour) — this one stays plain."""
    from sysforge import log

    lines = []
    saved = log._COLOR_MODE
    try:
        log.set_color_mode("always")
        with patch("subprocess.run", return_value=_mock_run("-1\n")), \
             patch("sysforge.log.info",
                   side_effect=lambda tag, msg: lines.append(msg)):
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
