"""
test_cli.py — unit tests for sysforge.cli helper functions.

Tests the pure, stateless preprocessing functions that have non-trivial
logic. Does not test main() or the _cmd_* handlers (those require the
full build pipeline and subprocess invocation).

Covers:
    _hoist_verbosity_flags   — hoists -v/-vv/-vvv/--verbose before subcommand,
                               leaves non-verbose flags in place, handles mixed
                               order, deduplicates multiple -v flags correctly
    _patch_makepkg_argv      — rewrites -m <value-starting-with-dash> to
                               --makepkg=<value>, passes through other tokens
    expand_makepkg_flags     — expands combined short flags, passes long flags,
                               handles empty/None input
"""
import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.cli import (
    _extract_implicit_makepkg_flags,
    _hoist_verbosity_flags,
    _patch_makepkg_argv,
)
from sysforge.primitives.makepkg_wrapper import expand_makepkg_flags


# ---------------------------------------------------------------------------
# _hoist_verbosity_flags
# ---------------------------------------------------------------------------

def test_hoist_v_before_subcommand():
    result = _hoist_verbosity_flags(["build", "PKGBUILD", "-v"])
    assert result == ["-v", "build", "PKGBUILD"]


def test_hoist_vv_before_subcommand():
    result = _hoist_verbosity_flags(["pipeline", "-vv"])
    assert result == ["-vv", "pipeline"]


def test_hoist_vvv_before_subcommand():
    result = _hoist_verbosity_flags(["build", "PKGBUILD", "-vvv", "--interactive"])
    assert result == ["-vvv", "build", "PKGBUILD", "--interactive"]


def test_hoist_verbose_long_form():
    result = _hoist_verbosity_flags(["build", "PKGBUILD", "--verbose"])
    assert result == ["--verbose", "build", "PKGBUILD"]


def test_hoist_multiple_v_flags():
    result = _hoist_verbosity_flags(["build", "PKGBUILD", "-v", "-v"])
    assert result == ["-v", "-v", "build", "PKGBUILD"]


def test_hoist_already_at_front():
    result = _hoist_verbosity_flags(["-vv", "build", "PKGBUILD"])
    assert result == ["-vv", "build", "PKGBUILD"]


def test_hoist_no_verbose_flags():
    result = _hoist_verbosity_flags(["build", "PKGBUILD", "--interactive"])
    assert result == ["build", "PKGBUILD", "--interactive"]


def test_hoist_empty_argv():
    assert _hoist_verbosity_flags([]) == []


def test_hoist_only_verbose():
    result = _hoist_verbosity_flags(["-v"])
    assert result == ["-v"]


def test_hoist_mixed_verbose_and_other_flags():
    result = _hoist_verbosity_flags(["build", "PKGBUILD", "--cc", "clang", "-vv", "--persist-log"])
    # -vv hoisted, rest preserved in original relative order
    assert result[0] == "-vv"
    assert "build" in result
    assert "--cc" in result
    assert "clang" in result
    assert "--persist-log" in result


# ---------------------------------------------------------------------------
# _patch_makepkg_argv
# ---------------------------------------------------------------------------

def test_patch_makepkg_long_form_dash_value():
    result = _patch_makepkg_argv(["build", "PKGBUILD", "--makepkg", "-sfci"])
    assert "--makepkg=-sfci" in result
    assert "--makepkg" not in result or result.index("--makepkg") == result.index("--makepkg=-sfci")


def test_patch_makepkg_short_form_dash_value():
    result = _patch_makepkg_argv(["build", "PKGBUILD", "-m", "-sfci"])
    assert "--makepkg=-sfci" in result


def test_patch_makepkg_value_not_starting_with_dash():
    # Values that don't start with '-' should be left alone
    result = _patch_makepkg_argv(["build", "PKGBUILD", "-m", "sfci"])
    assert result == ["build", "PKGBUILD", "-m", "sfci"]


def test_patch_makepkg_no_makepkg_flag():
    argv = ["build", "PKGBUILD", "--interactive"]
    result = _patch_makepkg_argv(argv)
    assert result == argv


def test_patch_makepkg_empty():
    assert _patch_makepkg_argv([]) == []


def test_patch_makepkg_m_at_end_no_value():
    # -m at end with no following value — should pass through unchanged
    result = _patch_makepkg_argv(["build", "-m"])
    assert result == ["build", "-m"]


def test_patch_makepkg_double_dash_value():
    result = _patch_makepkg_argv(["-m", "--noconfirm"])
    assert "--makepkg=--noconfirm" in result


def test_patch_makepkg_preserves_other_flags():
    result = _patch_makepkg_argv(["build", "PKGBUILD", "--cc", "clang", "-m", "-sf"])
    assert "--cc" in result
    assert "clang" in result
    assert "--makepkg=-sf" in result


# ---------------------------------------------------------------------------
# expand_makepkg_flags
# ---------------------------------------------------------------------------

def test_expand_combined_short_flags():
    assert expand_makepkg_flags("-sfci") == ["-s", "-f", "-c", "-i"]


def test_expand_single_short_flag():
    assert expand_makepkg_flags("-s") == ["-s"]


def test_expand_long_flag_unchanged():
    assert expand_makepkg_flags("--noconfirm") == ["--noconfirm"]


def test_expand_multiple_tokens():
    result = expand_makepkg_flags("-sf --noconfirm -i")
    assert "-s" in result
    assert "-f" in result
    assert "--noconfirm" in result
    assert "-i" in result


def test_expand_none_returns_empty():
    assert expand_makepkg_flags(None) == []


def test_expand_empty_string_returns_empty():
    assert expand_makepkg_flags("") == []


def test_expand_already_separated():
    result = expand_makepkg_flags("-s -f -c")
    assert result == ["-s", "-f", "-c"]


def test_expand_long_flag_with_value():
    # Long flags with = should pass through intact
    result = expand_makepkg_flags("--key=value")
    assert result == ["--key=value"]


def test_expand_mixed_short_and_long():
    result = expand_makepkg_flags("-sf --noconfirm")
    assert result == ["-s", "-f", "--noconfirm"]


# ---------------------------------------------------------------------------
# _extract_implicit_makepkg_flags
# ---------------------------------------------------------------------------

def test_implicit_basic_passthrough():
    result = _extract_implicit_makepkg_flags(["build", "ventoy", "-sfCci"])
    assert result == ["build", "ventoy", "-m", "-sfCci"]


def test_implicit_multiple_flag_groups():
    result = _extract_implicit_makepkg_flags(["build", "ventoy", "-sf", "-Cci"])
    assert result == ["build", "ventoy", "-m", "-sfCci"]


def test_implicit_no_subcommand():
    argv = ["--verbose", "resolve", "pkg"]
    assert _extract_implicit_makepkg_flags(argv) is argv


def test_implicit_no_flags():
    argv = ["build", "ventoy", "--interactive"]
    assert _extract_implicit_makepkg_flags(argv) is argv


def test_implicit_excluded_flags_not_extracted():
    """Flags in _PASSTHROUGH_EXCLUDE (-h, -V, -p, -m, -D) stay in argv."""
    result = _extract_implicit_makepkg_flags(["build", "ventoy", "-h"])
    assert "-m" not in result
    assert "-h" in result


def test_implicit_mixed_excluded_and_valid():
    """A token with any excluded char is left alone (not partially extracted)."""
    result = _extract_implicit_makepkg_flags(["build", "ventoy", "-sh"])
    # -sh contains h (excluded), so the whole token stays.
    assert "-m" not in result
    assert "-sh" in result


def test_implicit_update_subcommand():
    result = _extract_implicit_makepkg_flags(["update", "-sf"])
    assert result == ["update", "-m", "-sf"]


def test_implicit_merge_with_explicit_m():
    result = _extract_implicit_makepkg_flags(["build", "pkg", "-m", "--noconfirm", "-sf"])
    assert result == ["build", "pkg", "-m", "--noconfirm -sf", ]


def test_implicit_merge_with_explicit_makepkg_eq():
    result = _extract_implicit_makepkg_flags(["build", "pkg", "--makepkg=--noconfirm", "-sf"])
    assert result == ["build", "pkg", "--makepkg=--noconfirm -sf"]


def test_implicit_preserves_positional_args():
    result = _extract_implicit_makepkg_flags(["build", "ventoy", "cosmic-osd-git", "-sfCci"])
    assert result == ["build", "ventoy", "cosmic-osd-git", "-m", "-sfCci"]


def test_implicit_long_flags_not_extracted():
    """--noconfirm and other long flags should not be extracted."""
    argv = ["build", "pkg", "--interactive"]
    assert _extract_implicit_makepkg_flags(argv) is argv


def test_implicit_preserves_tokens_before_subcommand():
    result = _extract_implicit_makepkg_flags(["-vv", "build", "pkg", "-sf"])
    assert result == ["-vv", "build", "pkg", "-m", "-sf"]


# ---------------------------------------------------------------------------
# CLI-entry sentinel check skip on --dry-run
# ---------------------------------------------------------------------------

def _gate_args(*, command, dry_run):
    """Minimal args namespace for the sentinel-gate condition."""
    from types import SimpleNamespace
    return SimpleNamespace(command=command, dry_run=dry_run, state_dir=None)


def test_dry_run_skips_entry_sentinel_check():
    """
    Read-only invocations of install-bearing verbs (`update --dry-run`,
    `build --dry-run`, etc.) must not be blocked by a stale stage sentinel.
    UpdateVerb's inner sentinel scope already opts out under --dry-run; the
    outer CLI gate should match so the two stay in sync.
    """
    from sysforge.cli import _gate_sentinel_check
    for cmd in ("build", "update", "run", "setup"):
        assert _gate_sentinel_check(_gate_args(command=cmd, dry_run=True)) is False


def test_non_dry_run_still_hits_entry_sentinel_check():
    """Mutating invocations (no --dry-run) must still gate on the sentinel."""
    from sysforge.cli import _gate_sentinel_check
    for cmd in ("build", "update", "run", "setup"):
        assert _gate_sentinel_check(_gate_args(command=cmd, dry_run=False)) is True


def test_read_only_verbs_skip_entry_sentinel_check():
    """Verbs outside `_INSTALL_BEARING_COMMANDS` always skip the gate."""
    from sysforge.cli import _gate_sentinel_check
    for cmd in ("env", "doctor", "resolve", "fetch", "completions", "log",
                "packages", "state"):
        assert _gate_sentinel_check(_gate_args(command=cmd, dry_run=False)) is False


# ---------------------------------------------------------------------------
# _strip_venv_from_path
# ---------------------------------------------------------------------------

def test_strip_venv_from_path_removes_venv_bin(monkeypatch):
    """When running inside a venv, the venv's bin dir is removed from PATH
    and VIRTUAL_ENV/PYTHONPATH are popped, while other PATH entries keep
    their order."""
    from sysforge.cli import _strip_venv_from_path

    venv_root = "/tmp/fake-venv"
    venv_bin = f"{venv_root}/bin"
    monkeypatch.setattr(sys, "prefix", venv_root)
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(sys, "executable", f"{venv_bin}/python")
    monkeypatch.setenv("PATH", f"/usr/bin:{venv_bin}:/usr/local/bin")
    monkeypatch.setenv("VIRTUAL_ENV", venv_root)
    monkeypatch.setenv("PYTHONPATH", f"{venv_root}/lib/python3/site-packages")

    _strip_venv_from_path()

    assert os.environ["PATH"] == "/usr/bin:/usr/local/bin"
    assert "VIRTUAL_ENV" not in os.environ
    assert "PYTHONPATH" not in os.environ


def test_strip_venv_from_path_noop_outside_venv(monkeypatch):
    """A packaged install (sys.prefix == sys.base_prefix) is not a venv;
    PATH and friends must be left untouched so we don't strip /usr/bin."""
    from sysforge.cli import _strip_venv_from_path

    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(sys, "executable", "/usr/bin/python")
    monkeypatch.setenv("PATH", "/usr/bin:/usr/local/bin")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)

    _strip_venv_from_path()

    assert os.environ["PATH"] == "/usr/bin:/usr/local/bin"


def test_strip_venv_from_path_noop_when_venv_bin_absent(monkeypatch):
    """If sysforge is invoked via an entry-point whose PATH no longer
    contains the venv bin (e.g. from a non-zsh shell that didn't activate),
    leave PATH alone but still pop VIRTUAL_ENV/PYTHONPATH if present."""
    from sysforge.cli import _strip_venv_from_path

    venv_root = "/tmp/fake-venv"
    monkeypatch.setattr(sys, "prefix", venv_root)
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(sys, "executable", f"{venv_root}/bin/python")
    monkeypatch.setenv("PATH", "/usr/bin:/usr/local/bin")
    monkeypatch.setenv("VIRTUAL_ENV", venv_root)

    _strip_venv_from_path()

    # PATH unchanged because nothing to strip; VIRTUAL_ENV kept since the
    # function bails before the pop when no PATH entry matched.
    assert os.environ["PATH"] == "/usr/bin:/usr/local/bin"
    assert os.environ.get("VIRTUAL_ENV") == venv_root
