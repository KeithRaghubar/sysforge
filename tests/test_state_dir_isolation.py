"""
test_state_dir_isolation.py — no test may write to the developer's live state dir.

`SYSFORGE_STATE_DIR` is exported by the developer's shell (`~/sf-state` on this
workstation), so a test that reaches `BuildState` without opting into the
`state_dir` fixture writes to the real `build_state.toml`. That is not
hypothetical: `test_run_profile_override_kernel_derives_kernel_build` patches
`_run_build`, but `makepkg_wrapper.run` still falls through to
`_record_build_state`, which stamped a `linux-unruled` entry (pkgbuild_dir under
`/tmp/pytest-of-*`, `owner_stage = "kernel"`) into a live state file — where it
read as a real drifted kernel package and was exempt from demotion.

The `_isolate_state_dir` autouse fixture in conftest closes it. These tests pin
the guarantee itself, so removing the fixture fails here rather than silently
resuming writes to someone's home.
"""
import os
from pathlib import Path

from sysforge.pipeline.state import resolve_state_dir


def test_state_dir_env_is_isolated_without_opting_in():
    """No fixture requested — the env var must still point somewhere throwaway."""
    resolved = Path(os.environ["SYSFORGE_STATE_DIR"])
    assert not resolved.is_relative_to(Path.home()), (
        f"SYSFORGE_STATE_DIR={resolved} is inside the developer's home; "
        "the _isolate_state_dir autouse fixture is missing or broken"
    )


def test_resolved_state_dir_is_isolated_without_opting_in():
    """The resolver — what production code actually calls — sees the same tree."""
    resolved, _ = resolve_state_dir(None)
    assert not Path(resolved).is_relative_to(Path.home())


def test_explicit_state_dir_fixture_still_wins(state_dir):
    """The opt-in fixture overrides the autouse default and returns its Path."""
    assert Path(os.environ["SYSFORGE_STATE_DIR"]) == state_dir
    resolved, _ = resolve_state_dir(None)
    assert Path(resolved) == state_dir


def test_recording_a_build_cannot_touch_the_live_state_file(tmp_path):
    """The exact shape that leaked: run() with _run_build patched still records."""
    from unittest.mock import patch

    from sysforge.primitives import makepkg_wrapper as mw
    from sysforge.primitives.build_state import BuildState
    from sysforge.primitives.makepkg_wrapper import BuildOptions

    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text("pkgname=linux-isolation-probe\npkgver=1\npkgrel=1\n")

    with patch.object(mw, "_run_build", side_effect=lambda *a, **k: None):
        mw.run(pkgbuild, options=BuildOptions(
            update=False, pkg_log=False, profile_override="kernel"))

    live = Path.home() / "sf-state"
    if (live / "build_state.toml").exists():
        assert BuildState(live).get("linux-isolation-probe") is None
