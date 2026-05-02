"""tests for sysforge.primitives.run.run_or_raise"""
from unittest.mock import MagicMock, patch

import pytest

from sysforge.primitives.run import run_or_raise


class TestRunOrRaiseSuccess:
    def test_returns_completed_process_on_success(self):
        with patch("sysforge.primitives.run.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            result = run_or_raise(["true"], tag="TEST")
        assert result.returncode == 0
        assert result.stdout == "ok"

    def test_default_captures_output(self):
        with patch("sysforge.primitives.run.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_or_raise(["true"], tag="TEST")
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True

    def test_capture_false_passes_no_capture(self):
        with patch("sysforge.primitives.run.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            run_or_raise(["true"], tag="TEST", capture=False)
        kwargs = mock_run.call_args.kwargs
        assert "capture_output" not in kwargs
        assert "text" not in kwargs

    def test_extra_kwargs_forwarded(self):
        with patch("sysforge.primitives.run.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_or_raise(["true"], tag="TEST", cwd="/tmp", env={"A": "1"})
        kwargs = mock_run.call_args.kwargs
        assert kwargs["cwd"] == "/tmp"
        assert kwargs["env"] == {"A": "1"}


class TestRunOrRaiseFailure:
    def test_failure_includes_tag_and_default_operation(self):
        with patch("sysforge.primitives.run.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=2, stdout="", stderr="permission denied",
            )
            with pytest.raises(RuntimeError) as exc:
                run_or_raise(["/usr/bin/sgdisk", "--clear"], tag="PARTITION")
        msg = str(exc.value)
        assert "[PARTITION]" in msg
        assert "sgdisk failed" in msg
        assert "exit 2" in msg
        assert "permission denied" in msg

    def test_failure_uses_explicit_operation(self):
        with patch("sysforge.primitives.run.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
            with pytest.raises(RuntimeError, match="custom-op failed"):
                run_or_raise(["sh", "-c", "false"], tag="X", operation="custom-op")

    def test_failure_falls_back_to_hint_when_no_stderr(self):
        with patch("sysforge.primitives.run.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            with pytest.raises(RuntimeError, match="check the keyring"):
                run_or_raise(
                    ["pacstrap", "/mnt"], tag="BASE_INSTALL", hint="check the keyring",
                )

    def test_failure_prefers_stderr_over_hint(self):
        with patch("sysforge.primitives.run.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="real error here",
            )
            with pytest.raises(RuntimeError) as exc:
                run_or_raise(
                    ["x"], tag="T", hint="generic hint",
                )
        msg = str(exc.value)
        assert "real error here" in msg
        assert "generic hint" not in msg

    def test_failure_without_capture_uses_hint(self):
        with patch("sysforge.primitives.run.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            with pytest.raises(RuntimeError, match="streaming hint"):
                run_or_raise(
                    ["pacstrap", "/mnt"], tag="BASE_INSTALL",
                    capture=False, hint="streaming hint",
                )

    def test_failure_generic_fallback_when_nothing_provided(self):
        with patch("sysforge.primitives.run.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            with pytest.raises(RuntimeError, match="no output captured"):
                run_or_raise(["x"], tag="T", capture=False)
