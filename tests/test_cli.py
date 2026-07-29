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
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent / ".."))

from sysforge.cli import (
    _extract_implicit_makepkg_flags,
    _hoist_global_flags,
    _hoist_verbosity_flags,
    _patch_makepkg_argv,
    _resolve_throttle_override,
    _resolve_verbosity,
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
# --quiet hoisting (global flag, distinct from doctor's local --quiet/-q)
# ---------------------------------------------------------------------------

def test_hoist_quiet_before_subcommand():
    result = _hoist_verbosity_flags(["update", "--quiet"])
    assert result == ["--quiet", "update"]


def test_hoist_quiet_not_hoisted_for_doctor():
    # doctor owns a *local* --quiet/-q; hoisting would clobber it.
    result = _hoist_verbosity_flags(["doctor", "--quiet"])
    assert result == ["doctor", "--quiet"]


def test_hoist_short_q_never_hoisted():
    # -q is doctor's short form only; global --quiet has no short alias.
    result = _hoist_verbosity_flags(["doctor", "-q"])
    assert result == ["doctor", "-q"]


# ---------------------------------------------------------------------------
# throttle-override global flags: --no-throttle / --turbo (2.1.0-F5)
# ---------------------------------------------------------------------------


def test_hoist_no_throttle_before_subcommand():
    assert _hoist_global_flags(["build", "PKGBUILD", "--no-throttle"]) == [
        "--no-throttle", "build", "PKGBUILD",
    ]


def test_hoist_turbo_before_subcommand():
    assert _hoist_global_flags(["update", "--turbo"]) == ["--turbo", "update"]


class _ThrottleArgs:
    def __init__(self, no_throttle=False, turbo=False):
        self.no_throttle = no_throttle
        self.turbo = turbo


def test_resolve_throttle_override_none():
    assert _resolve_throttle_override(_ThrottleArgs()) is None


def test_resolve_throttle_override_bypass():
    assert _resolve_throttle_override(_ThrottleArgs(no_throttle=True)) == "bypass"


def test_resolve_throttle_override_boost():
    assert _resolve_throttle_override(_ThrottleArgs(turbo=True)) == "boost"


def test_resolve_throttle_override_turbo_wins_over_no_throttle():
    # --turbo is the stronger request; it subsumes --no-throttle.
    assert _resolve_throttle_override(
        _ThrottleArgs(no_throttle=True, turbo=True)
    ) == "boost"


# ---------------------------------------------------------------------------
# _resolve_verbosity — precedence: --quiet > -v/-vv/-vvv > [log] verbosity > 0
# ---------------------------------------------------------------------------

class _Args:
    def __init__(self, verbose=0, quiet_global=False):
        self.verbose = verbose
        self.quiet_global = quiet_global


def _patch_log_cfg(monkeypatch, value):
    """Make _resolve_verbosity see {'log': {'verbosity': value}} (or {} if None)."""
    import sysforge.primitives.config as cfg_mod

    body = {} if value is None else {"log": {"verbosity": value}}
    monkeypatch.setattr(cfg_mod, "load_sysforge_toml", lambda: body)


def test_resolve_verbosity_default_zero(monkeypatch):
    _patch_log_cfg(monkeypatch, None)
    assert _resolve_verbosity(_Args()) == 0


def test_resolve_verbosity_quiet_wins_over_v(monkeypatch):
    _patch_log_cfg(monkeypatch, None)
    assert _resolve_verbosity(_Args(verbose=3, quiet_global=True)) == 0


def test_resolve_verbosity_quiet_wins_over_config(monkeypatch):
    _patch_log_cfg(monkeypatch, 3)
    assert _resolve_verbosity(_Args(quiet_global=True)) == 0


def test_resolve_verbosity_cli_v_wins_over_config(monkeypatch):
    _patch_log_cfg(monkeypatch, 1)
    assert _resolve_verbosity(_Args(verbose=2)) == 2


def test_resolve_verbosity_config_used_when_no_flag(monkeypatch):
    _patch_log_cfg(monkeypatch, 2)
    assert _resolve_verbosity(_Args()) == 2


def test_resolve_verbosity_config_clamped_high(monkeypatch):
    _patch_log_cfg(monkeypatch, 9)
    assert _resolve_verbosity(_Args()) == 3


def test_resolve_verbosity_config_clamped_low(monkeypatch):
    _patch_log_cfg(monkeypatch, -4)
    assert _resolve_verbosity(_Args()) == 0


def test_resolve_verbosity_config_non_int_ignored(monkeypatch):
    _patch_log_cfg(monkeypatch, "loud")
    assert _resolve_verbosity(_Args()) == 0


def test_resolve_verbosity_config_bad_load_never_aborts(monkeypatch):
    import sysforge.primitives.config as cfg_mod

    def _boom():
        raise RuntimeError("unreadable config")

    monkeypatch.setattr(cfg_mod, "load_sysforge_toml", _boom)
    assert _resolve_verbosity(_Args()) == 0


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


# ---------------------------------------------------------------------------
# _hoist_global_flags
# ---------------------------------------------------------------------------

def test_hoist_global_py_profile_after_subcommand():
    result = _hoist_global_flags(["update", "--py-profile"])
    assert result == ["--py-profile", "update"]


def test_hoist_global_py_profile_out_with_value_token():
    result = _hoist_global_flags(["update", "--py-profile-out", "out.prof", "--dry-run"])
    assert result == ["--py-profile-out", "out.prof", "update", "--dry-run"]


def test_hoist_global_py_profile_out_equals_form():
    result = _hoist_global_flags(["build", "PKGBUILD", "--py-profile-out=out.prof"])
    assert result == ["--py-profile-out=out.prof", "build", "PKGBUILD"]


def test_hoist_global_timings():
    result = _hoist_global_flags(["update", "--timings"])
    assert result == ["--timings", "update"]


def test_hoist_global_no_flags_passthrough():
    result = _hoist_global_flags(["build", "PKGBUILD", "--interactive"])
    assert result == ["build", "PKGBUILD", "--interactive"]


def test_hoist_global_composes_with_verbosity_hoist():
    result = _hoist_global_flags(_hoist_verbosity_flags(["update", "-vv", "--timings"]))
    assert result == ["--timings", "-vv", "update"]


def test_hoist_global_color_value_token():
    result = _hoist_global_flags(["build", "foo", "--color", "never"])
    assert result == ["--color", "never", "build", "foo"]


def test_hoist_global_color_equals_form():
    result = _hoist_global_flags(["update", "--color=always"])
    assert result == ["--color=always", "update"]


# ---------------------------------------------------------------------------
# --color flag + [ui] color config resolution
# ---------------------------------------------------------------------------

def test_parser_accepts_color_choices():
    from sysforge.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["--color", "always", "doctor"])
    assert args.color == "always"
    # Default is None so the config can supply the value when the flag is absent.
    args = parser.parse_args(["doctor"])
    assert args.color is None


def test_resolve_color_mode_flag_wins_over_config(monkeypatch):
    import sysforge.cli as cli
    monkeypatch.setattr(
        "sysforge.primitives.config.load_sysforge_toml",
        lambda: {"ui": {"color": "never"}},
    )
    assert cli._resolve_color_mode("always") == "always"


def test_resolve_color_mode_config_when_no_flag(monkeypatch):
    import sysforge.cli as cli
    monkeypatch.setattr(
        "sysforge.primitives.config.load_sysforge_toml",
        lambda: {"ui": {"color": "never"}},
    )
    assert cli._resolve_color_mode(None) == "never"


def test_resolve_color_mode_defaults_to_auto(monkeypatch):
    import sysforge.cli as cli
    monkeypatch.setattr(
        "sysforge.primitives.config.load_sysforge_toml",
        lambda: {},
    )
    assert cli._resolve_color_mode(None) == "auto"
    # A junk config value also degrades to auto rather than propagating.
    monkeypatch.setattr(
        "sysforge.primitives.config.load_sysforge_toml",
        lambda: {"ui": {"color": "rainbow"}},
    )
    assert cli._resolve_color_mode(None) == "auto"


# ---------------------------------------------------------------------------
# global profiling flags: parser defaults + _dispatch
# ---------------------------------------------------------------------------

def test_parser_profiling_flag_defaults():
    from sysforge.cli import _build_parser
    args = _build_parser().parse_args(["state", "list"])
    assert args.py_profile is False
    assert args.py_profile_out is None
    assert args.timings is False


def test_parser_profiling_flags_set():
    from sysforge.cli import _build_parser
    args = _build_parser().parse_args(
        ["--py-profile", "--py-profile-out", "out.prof", "--timings", "state", "list"]
    )
    assert args.py_profile is True
    assert args.py_profile_out == "out.prof"
    assert args.timings is True


class _FakeArgs:
    def __init__(self, py_profile=False, py_profile_out=None):
        self.py_profile = py_profile
        self.py_profile_out = py_profile_out


def test_dispatch_plain_skips_profiler(monkeypatch, capsys):
    import sysforge.cli as cli
    monkeypatch.setattr(cli, "run_verb", lambda verb, args: 0)
    assert cli._dispatch(object, _FakeArgs()) == 0
    assert "cumulative" not in capsys.readouterr().err


def test_dispatch_py_profile_prints_stats_to_stderr(monkeypatch, capsys):
    import sysforge.cli as cli
    monkeypatch.setattr(cli, "run_verb", lambda verb, args: 0)
    assert cli._dispatch(object, _FakeArgs(py_profile=True)) == 0
    err = capsys.readouterr().err
    assert "cumulative" in err
    assert "function calls" in err


def test_dispatch_py_profile_out_writes_dump(monkeypatch, tmp_path):
    import sysforge.cli as cli
    monkeypatch.setattr(cli, "run_verb", lambda verb, args: 0)
    out = tmp_path / "stats.prof"
    cli._dispatch(object, _FakeArgs(py_profile_out=str(out)))
    assert out.exists()
    import pstats
    pstats.Stats(str(out))  # loadable dump


def test_dispatch_emits_stats_on_sys_exit(monkeypatch, tmp_path):
    import pytest
    import sysforge.cli as cli

    def _exiting_run_verb(verb, args):
        raise SystemExit(3)

    monkeypatch.setattr(cli, "run_verb", _exiting_run_verb)
    out = tmp_path / "stats.prof"
    with pytest.raises(SystemExit):
        cli._dispatch(object, _FakeArgs(py_profile_out=str(out)))
    assert out.exists()


# ---------------------------------------------------------------------------
# KeyboardInterrupt handling (1.2.0-F39)
# ---------------------------------------------------------------------------
#
# A Ctrl-C anywhere under main() must exit 130 with one readable abort line
# (no traceback) and release the ui/progress scroll region — not unwind as a
# raw KeyboardInterrupt.


def test_main_handles_keyboard_interrupt(monkeypatch, capsys):
    import pytest
    import sysforge.cli as cli
    from sysforge.ui import progress

    def _interrupting_dispatch(verb_cls, args):
        raise KeyboardInterrupt

    shutdown_calls = []
    monkeypatch.setattr(cli, "_dispatch", _interrupting_dispatch)
    monkeypatch.setattr(progress, "shutdown", lambda: shutdown_calls.append(1))
    monkeypatch.setattr("sys.argv", ["sysforge", "env"])
    # main() applies argv verbosity/color to the global log state — restore
    # the conftest-pinned levels so this test doesn't leak into later modules.
    from sysforge import log
    saved_verbosity = log.get_verbosity()
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main()
    finally:
        log.set_verbosity(saved_verbosity)
        log.set_color_mode("auto")
    assert exc.value.code == 130
    assert shutdown_calls, "progress scroll region must be released"
    err = capsys.readouterr().err
    assert "aborted" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Tiered top-level COMMAND help (2.5.0-F1)
# ---------------------------------------------------------------------------

def _top_level_verbs():
    """Every user-facing top-level command name (excludes the internal
    `completions` plumbing verb)."""
    import argparse

    from sysforge.cli import _build_parser
    parser = _build_parser()
    sub = next(a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction))
    return {n for n in sub.choices if n != "completions"}


def test_tiered_command_order_covers_every_verb_exactly_once():
    """The tier map is the source of truth for both `--help` and the man page;
    it must partition the user-facing verbs — no verb missing, none duplicated,
    nothing stale."""
    from sysforge.cli import tiered_command_order

    order = tiered_command_order()
    assert len(order) == len(set(order)), "duplicate verb in tier map"
    assert set(order) == _top_level_verbs()


def test_help_groups_commands_under_usage_tiers():
    """`sysforge --help` prints the COMMAND list under Everyday/Inspect/Maintain
    headers, each verb under its assigned tier, instead of one flat block."""
    from sysforge.cli import _build_parser

    text = _build_parser().format_help()
    for label in ("Everyday:", "Inspect:", "Maintain:"):
        assert label in text, f"missing tier header {label!r}"

    # Tier headers appear in declared order.
    assert (text.index("Everyday:") < text.index("Inspect:")
            < text.index("Maintain:"))

    # A representative verb sits under its own tier and not an earlier one:
    # `build` (Everyday) precedes the Inspect header; `doctor` (Inspect) sits
    # between the Inspect and Maintain headers.
    assert text.index("build") < text.index("Inspect:")
    assert text.index("Inspect:") < text.index("doctor") < text.index("Maintain:")


def test_help_hides_internal_completions_verb():
    """The tiered formatter must not surface the internal `completions` verb —
    it is registered without help text and stays out of the listing."""
    from sysforge.cli import _build_parser

    text = _build_parser().format_help()
    # `completions` should not appear as a listed COMMAND entry.
    assert "\n    completions" not in text
    assert "completions:" not in text


# ---------------------------------------------------------------------------
# Parent-verb subcommand default (2.6.1-F6)
# ---------------------------------------------------------------------------
#
# `artifact` and `state` gain a default subverb (their single obvious
# read-only "show me" view), matching the `doctor`/`packages` pattern.
# `config`/`run` deliberately keep requiring a subcommand — their subverbs
# mutate or diverge with no natural landing point.

def test_bare_artifact_defaults_to_artifact_list():
    from sysforge.cli import ArtifactListVerb, _build_parser

    ns = _build_parser().parse_args(["artifact"])
    assert ns.verb_cls is ArtifactListVerb
    assert ns.artifact_cmd == "list"


def test_bare_state_defaults_to_state_list():
    from sysforge.cli import StateListVerb, _build_parser

    ns = _build_parser().parse_args(["state"])
    assert ns.verb_cls is StateListVerb
    assert ns.state_cmd == "list"


def test_mutating_namespaces_still_require_a_subcommand():
    """The two-tier rule: no default where subverbs mutate or diverge."""
    import pytest
    from sysforge.cli import _build_parser

    with pytest.raises(SystemExit):
        _build_parser().parse_args(["config"])
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["run"])


def test_bare_artifact_verb_actually_runs(monkeypatch, capsys):
    """Parsing a default is not enough — the verb must also execute cleanly
    off the parent namespace, i.e. it must not reach for an attribute only
    its own subparser defines (the failure mode the `doctor` system/pkg
    split hit with `packages=[]`)."""
    from sysforge.primitives import artifacts
    from sysforge.cli import _build_parser

    monkeypatch.setattr(artifacts, "unified_rows", lambda registry: [])
    monkeypatch.setattr(artifacts, "script_root_on_path", lambda: True)

    ns = _build_parser().parse_args(["artifact"])
    result = ns.verb_cls().execute(ns, ns.verb_cls().pre_check(ns))
    assert result.exit_code == 0
    assert "No managed artifacts." in capsys.readouterr().err


def test_bare_state_verb_actually_runs(monkeypatch, tmp_path, capsys):
    """Same runtime guarantee for `sysforge state` -> StateListVerb."""
    from sysforge.primitives.build_state import BuildState
    from sysforge.cli import _build_parser

    monkeypatch.setattr(BuildState, "all_packages", lambda self: {})
    monkeypatch.setattr(
        "sysforge.pipeline.state.resolve_state_dir",
        lambda override: (tmp_path, None),
    )
    monkeypatch.setattr("sysforge.state_cmd._print_untracked_foreign", lambda s: None)

    ns = _build_parser().parse_args(["state"])
    result = ns.verb_cls().execute(ns, ns.verb_cls().pre_check(ns))
    assert result.exit_code == 0
    assert "No build state recorded" in capsys.readouterr().out
