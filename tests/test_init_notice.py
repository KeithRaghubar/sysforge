"""
test_init_notice.py — tests for the first-install init notice (F1).

The notice file (.sysforge-init-notice) is dropped by the PKGBUILD
post_install scriptlet. On every CLI invocation maybe_emit_init_notice():
  - does nothing if the file is absent,
  - advises running the incomplete bootstrap stages while present and
    reconfigure+hardware are not both done,
  - silently deletes the file once both stages are done.
"""
from sysforge.pipeline.state import PipelineState
from sysforge.primitives.init_notice import (
    FILENAME,
    maybe_emit_init_notice,
    notice_path,
)


def _make_notice(tmp_path):
    p = tmp_path / FILENAME
    p.write_text("")
    return p


def test_absent_notice_is_noop(tmp_path):
    assert maybe_emit_init_notice(tmp_path) is None


def test_present_with_incomplete_stages_advises(tmp_path):
    _make_notice(tmp_path)
    assert maybe_emit_init_notice(tmp_path) == "advised"
    # Advisory must not delete the notice — it persists until stages complete.
    assert notice_path(tmp_path).exists()


def test_present_with_one_stage_done_still_advises(tmp_path):
    _make_notice(tmp_path)
    ps = PipelineState(tmp_path)
    ps.mark_done("reconfigure")
    ps.save()
    assert maybe_emit_init_notice(tmp_path) == "advised"
    assert notice_path(tmp_path).exists()


def test_present_with_both_stages_done_clears_silently(tmp_path):
    _make_notice(tmp_path)
    ps = PipelineState(tmp_path)
    ps.mark_done("reconfigure")
    ps.mark_done("hardware")
    ps.save()
    assert maybe_emit_init_notice(tmp_path) == "cleared"
    assert not notice_path(tmp_path).exists()


def test_never_raises_on_bad_state_dir(tmp_path):
    # A non-existent / unreadable dir must not break the calling command.
    assert maybe_emit_init_notice(tmp_path / "does-not-exist") is None
