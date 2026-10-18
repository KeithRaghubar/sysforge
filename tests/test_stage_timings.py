"""
test_stage_timings.py — ``<state_dir>/stage_timings.toml`` (3.5.0-F6).

The step-duration history the progress bar's stage ETA reads: a five-entry
ring per step, read back as its median; advisory, so nothing here may raise.
"""
from sysforge.primitives import stage_timings


def test_record_then_expected_is_the_median(tmp_path):
    for secs in (100, 400, 120):
        stage_timings.record(tmp_path, "toolchain", "optimize", secs)
    stage_timings.record(tmp_path, "toolchain", "audit", 95.7)
    assert stage_timings.expected(tmp_path, "toolchain") == {"optimize": 120, "audit": 95}


def test_ring_keeps_the_last_five(tmp_path):
    for secs in range(1, 9):
        stage_timings.record(tmp_path, "toolchain", "train", secs)
    text = (tmp_path / stage_timings.FILENAME).read_text()
    assert 'train = "4,5,6,7,8"' in text


def test_stages_are_kept_apart(tmp_path):
    stage_timings.record(tmp_path, "toolchain", "build", 10)
    stage_timings.record(tmp_path, "kernel", "build", 99)
    assert stage_timings.expected(tmp_path, "toolchain") == {"build": 10}
    assert stage_timings.expected(tmp_path, "kernel") == {"build": 99}


def test_missing_or_corrupt_file_is_no_history(tmp_path):
    assert stage_timings.expected(tmp_path, "toolchain") == {}
    (tmp_path / stage_timings.FILENAME).write_text("[toolchain\nnot toml")
    assert stage_timings.expected(tmp_path, "toolchain") == {}
    # ... and a record over it starts afresh rather than raising.
    stage_timings.record(tmp_path, "toolchain", "audit", 5)
    assert stage_timings.expected(tmp_path, "toolchain") == {"audit": 5}


def test_record_never_raises_on_an_unwritable_dir(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        stage_timings.record(ro, "toolchain", "audit", 5)
    finally:
        ro.chmod(0o755)
    assert not (ro / stage_timings.FILENAME).exists()


def test_a_step_name_that_is_not_a_bare_key_is_not_written(tmp_path):
    stage_timings.record(tmp_path, "toolchain", 'bad"key', 5)
    assert stage_timings.expected(tmp_path, "toolchain") == {}
