"""Tests for the one-home AlreadyBuilt policy seam (2.5.1-F2)."""

from unittest.mock import patch

import pytest

from sysforge.primitives.already_built import (
    AlreadyBuiltAction,
    resolve_already_built,
)


def test_reuse_posture_always_reuses_without_prompt():
    with patch("sysforge.primitives.prompt.prompt_choice") as mock_prompt:
        action = resolve_already_built("reuse", interactive=True)
    assert action is AlreadyBuiltAction.REUSE
    mock_prompt.assert_not_called()


def test_unknown_posture_raises_value_error():
    with pytest.raises(ValueError, match="posture"):
        resolve_already_built("wat", interactive=False)


@pytest.mark.parametrize(
    ("interactive", "non_interactive", "tty"),
    [
        (False, False, True),   # caller says non-interactive
        (True, True, True),     # --non-interactive override
        (True, False, False),   # no TTY
    ],
)
def test_review_gated_unattended_signals_each_force_reuse(
    interactive, non_interactive, tty
):
    with patch("sysforge.primitives.prompt.is_interactive", return_value=tty), \
         patch("sysforge.primitives.prompt.prompt_choice") as mock_prompt:
        action = resolve_already_built(
            "review-gated",
            interactive=interactive,
            non_interactive=non_interactive,
        )
    assert action is AlreadyBuiltAction.REUSE
    mock_prompt.assert_not_called()


@pytest.mark.parametrize(
    ("choice", "expected"),
    [("i", AlreadyBuiltAction.REUSE), ("r", AlreadyBuiltAction.REBUILD)],
)
def test_review_gated_interactive_prompt_maps_choice(choice, expected):
    with patch("sysforge.primitives.prompt.is_interactive", return_value=True), \
         patch("sysforge.primitives.prompt.prompt_choice",
               return_value=choice) as mock_prompt:
        action = resolve_already_built("review-gated", interactive=True)
    assert action is expected
    # Defaults preserved verbatim from the kernel B5 prompt.
    assert mock_prompt.call_args.kwargs["default"] == "a"
    assert mock_prompt.call_args.kwargs["eof_default"] == "a"


def test_review_gated_abort_raises_with_hint_and_tag():
    with patch("sysforge.primitives.prompt.is_interactive", return_value=True), \
         patch("sysforge.primitives.prompt.prompt_choice", return_value="a"), \
         pytest.raises(RuntimeError) as excinfo:
        resolve_already_built(
            "review-gated",
            interactive=True,
            tag="KERNEL",
            abort_hint="the kconfig review did not run.",
        )
    msg = str(excinfo.value)
    assert msg.startswith("[KERNEL] aborted: package already built")
    assert "the kconfig review did not run." in msg


def test_build_core_catch_site_routes_through_seam():
    """The build_core AlreadyBuilt handler must consult the policy seam —
    guards against a future edit re-inlining local interpretation."""
    import inspect

    import sysforge.build_core as build_core

    src = inspect.getsource(build_core.build_and_install)
    assert "resolve_already_built(" in src
    # And the module binds the real seam (monkeypatchable attribute).
    from sysforge.primitives.already_built import resolve_already_built
    assert build_core.resolve_already_built is resolve_already_built


def test_toolchain_build_pkg_reuses_on_already_built(tmp_path):
    """A stale artifact in PKGDEST must not crash a toolchain pass (was: no
    catch at all — AlreadyBuilt propagated and killed a 5-pass PGO run)."""
    from types import SimpleNamespace

    import sysforge.pipeline.stages.toolchain as tc
    from sysforge.primitives.makepkg_invoke import AlreadyBuilt

    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text("pkgname=llvm\n")
    options = SimpleNamespace(dry_run=False, makepkg_flags=[], no_update=True)

    with patch.object(
        tc, "makepkg_run", side_effect=AlreadyBuilt(pkgbuild)
    ), patch.object(
        tc, "make_build_options", return_value=object()
    ), patch(
        "sysforge.pipeline.stages.toolchain.resolve_already_built",
        wraps=__import__(
            "sysforge.primitives.already_built", fromlist=["x"]
        ).resolve_already_built,
    ) as routed:
        tc._build_pkg("llvm", pkgbuild, options)  # must not raise

    assert routed.call_count == 1
