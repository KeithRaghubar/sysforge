"""
test_init_notice.py — tests for the first-run advisories (F1, 3.1.0-F5).

The notice file (.sysforge-init-notice) is dropped by the PKGBUILD
post_install scriptlet. On every CLI invocation maybe_emit_init_notice():
  - does nothing if the file is absent,
  - advises running the incomplete bootstrap stages while present and
    reconfigure+hardware are not both done,
  - silently deletes the file once both stages are done.

maybe_emit_stage_config_notice() (3.1.0-F5) is the second advisory: the first
run toolchain / run kernel on an install points the user at the TOML that
decides what gets built, before it gets built.
"""
from sysforge.pipeline.state import PipelineState
from sysforge.primitives.init_notice import (
    FILENAME,
    maybe_emit_init_notice,
    maybe_emit_stage_config_notice,
    notice_path,
    stage_config_path,
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


# ---------------------------------------------------------------------------
# 3.1.0-F5 — first `run toolchain` / `run kernel` config advisory
# ---------------------------------------------------------------------------

def test_stage_config_notice_advises_on_a_never_run_stage(tmp_path, capsys):
    """PipelineState returns "pending" for a stage it has never seen, so no
    new state is needed to know this is a first run."""
    ps = PipelineState(tmp_path)
    assert maybe_emit_stage_config_notice("kernel", ps) == "advised"
    out = "".join(capsys.readouterr())
    assert str(stage_config_path("kernel")) in out
    assert "kconfig" in out


def test_stage_config_notice_silent_once_the_stage_is_done(tmp_path, capsys):
    ps = PipelineState(tmp_path)
    ps.mark_done("toolchain")
    ps.save()
    assert maybe_emit_stage_config_notice("toolchain", ps) is None
    assert "".join(capsys.readouterr()) == ""


def test_stage_config_notice_readvises_after_a_failed_first_attempt(tmp_path):
    """"First run" means never *completed*, not never *attempted*: a stage that
    failed is the one a user is most likely to retry without having looked at
    the config, and the config is exactly as unread as it was before."""
    ps = PipelineState(tmp_path)
    ps.mark_failed("kernel", "boom")
    ps.save()
    assert maybe_emit_stage_config_notice("kernel", ps) == "advised"


def test_stage_config_notice_readvises_while_running(tmp_path):
    ps = PipelineState(tmp_path)
    ps.mark_running("toolchain")
    ps.save()
    assert maybe_emit_stage_config_notice("toolchain", ps) == "advised"


def test_stage_config_notice_only_covers_toolchain_and_kernel(tmp_path, capsys):
    """The other stages are driven by the flags the user typed, so they get no
    advisory — a notice on every stage is a notice nobody reads."""
    ps = PipelineState(tmp_path)
    for stage in ("packages", "hardware", "configure", "reconfigure"):
        assert maybe_emit_stage_config_notice(stage, ps) is None
    assert "".join(capsys.readouterr()) == ""
    assert stage_config_path("packages") is None


def test_stage_config_notice_names_toolchain_settings(tmp_path, capsys):
    ps = PipelineState(tmp_path)
    assert maybe_emit_stage_config_notice("toolchain", ps) == "advised"
    out = "".join(capsys.readouterr())
    assert "toolchain.toml" in out
    assert "compiler" in out
    assert "pgo" in out.lower()


def test_stage_config_notice_never_raises_on_a_broken_state(tmp_path):
    """Strictly advisory: it must never break the stage it precedes."""
    class Broken:
        def stage_status(self, name):
            raise RuntimeError("state unreadable")

    assert maybe_emit_stage_config_notice("kernel", Broken()) is None
