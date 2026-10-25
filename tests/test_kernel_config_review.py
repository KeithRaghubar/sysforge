# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
Kernel config review (3.5.0-F22): two questions, each answered in one place.

* Run the PKGBUILD's config UI? ``kernel/config.py::resolve_config_review`` —
  ``review_config`` (kernel.toml) and not ``--no-review-config`` and sysforge
  may ask.
* May sysforge ask its own questions? ``prompt.is_interactive()`` only, so
  ``--no-review-config`` no longer turns the stage's safety questions into
  refusals; ``--unattended`` (or no terminal) does.
"""
from types import SimpleNamespace

import pytest

from sysforge.pipeline.stages.kernel import config as kcfg
from sysforge.pipeline.stages.kernel import source
from sysforge.primitives import prompt


@pytest.fixture
def at_a_terminal(monkeypatch):
    monkeypatch.setattr(prompt.sys.stdin, "isatty", lambda: True, raising=False)


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(prompt.sys.stdin, "isatty", lambda: False, raising=False)


def _cfg(**data):
    return kcfg.KernelConfig.from_toml({"enabled": True, **data})


# ---------------------------------------------------------------------------
# kernel.toml review_config (was `interactive`)
# ---------------------------------------------------------------------------

def test_review_config_defaults_on():
    assert _cfg().review_config is True


def test_review_config_is_read():
    assert _cfg(review_config=False).review_config is False


def test_legacy_interactive_key_still_applies_with_a_warning(monkeypatch):
    """A kernel.toml from before 3.5.0-F22 that turned the review off must not
    get nconfig back silently on upgrade."""
    warned = []
    monkeypatch.setattr(kcfg, "_log", SimpleNamespace(
        warn=warned.append, info=lambda *_: None, debug=lambda *_: None))
    assert _cfg(interactive=False).review_config is False
    assert any("review_config" in w and "config clean" in w for w in warned)


def test_review_config_wins_over_the_legacy_key():
    assert _cfg(interactive=False, review_config=True).review_config is True


# ---------------------------------------------------------------------------
# resolve_config_review
# ---------------------------------------------------------------------------

def _opts(**kw):
    return SimpleNamespace(**{"no_review_config": False, **kw})


def test_review_runs_by_default_at_a_terminal(at_a_terminal):
    assert kcfg.resolve_config_review(_cfg(), _opts()) is True


def test_review_config_false_turns_it_off(at_a_terminal):
    assert kcfg.resolve_config_review(_cfg(review_config=False), _opts()) is False


def test_no_review_config_flag_turns_it_off(at_a_terminal):
    assert kcfg.resolve_config_review(_cfg(), _opts(no_review_config=True)) is False


def test_unattended_turns_it_off(at_a_terminal):
    prompt.set_unattended(True)
    assert kcfg.resolve_config_review(_cfg(), _opts()) is False


def test_no_terminal_turns_it_off(no_terminal):
    assert kcfg.resolve_config_review(_cfg(), _opts()) is False


# ---------------------------------------------------------------------------
# The stage's own questions follow prompt.is_interactive() only
# ---------------------------------------------------------------------------

def test_no_review_config_still_asks_the_shadow_question(at_a_terminal, monkeypatch):
    """Skipping the config UI is not consent to overwrite a repo package."""
    asked = []
    monkeypatch.setattr("sysforge.primitives.aur.is_repo_package", lambda name: True)
    monkeypatch.setattr("sysforge.primitives.prompt.prompt_choice",
                        lambda *a, **k: asked.append(a) or "n")
    with pytest.raises(RuntimeError):
        source.check_pkgname_repo_collision("linux", _opts(no_review_config=True, dry_run=False))
    assert asked, "the shadow question was not asked"


def test_unattended_refuses_the_shadow_question(at_a_terminal, monkeypatch):
    monkeypatch.setattr("sysforge.primitives.aur.is_repo_package", lambda name: True)
    monkeypatch.setattr("sysforge.primitives.prompt.prompt_choice",
                        lambda *a, **k: pytest.fail("asked while unattended"))
    prompt.set_unattended(True)
    with pytest.raises(RuntimeError, match="unattended run"):
        source.check_pkgname_repo_collision("linux", _opts(dry_run=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_run_kernel_takes_no_review_config():
    from tests.cli_args import cli_args
    args = cli_args("run", "kernel", "--no-review-config")
    assert args.no_review_config is True


def test_run_kernel_no_longer_takes_no_interactive():
    from tests.cli_args import cli_args
    with pytest.raises(SystemExit):
        cli_args("run", "kernel", "--no-interactive")


def test_config_check_says_the_old_key_is_still_read():
    """The loader still honours `interactive`, so the check must not say it is ignored."""
    from sysforge.primitives import config_schema
    issues = config_schema.inspect_text("kernel.toml", "interactive = false\nenabled = true\n")
    retired = [i for i in issues if i.kind == "retired_key"]
    assert retired and "still read for now; use review_config" in retired[0].message
